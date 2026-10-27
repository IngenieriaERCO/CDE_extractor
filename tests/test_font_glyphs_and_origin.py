"""Glifos ausentes en la fuente sustituta, origen de cada sustitución y registro de fuentes."""

import json
from pathlib import Path

import ezdxf
import pytest
from ezdxf.fonts import fonts

from cde_exporter.cad.fidelity import inspect_fidelity, missing_glyphs_message
from cde_exporter.cad.font_config import configure_fonts, register_font_file
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cli import main

_ARIAL = Path("C:/Windows/Fonts/arial.ttf")
pytestmark = pytest.mark.skipif(not _ARIAL.is_file(), reason="arial.ttf no está instalado")


def _arial_lacks() -> str:
    """Un carácter que Arial no tiene (se comprueba, no se supone)."""
    codepoints = fonts.font_manager.get_ttf_font("arial.ttf").getBestCmap()
    for char in "漢✓☃":
        if ord(char) not in codepoints:
            return char
    pytest.skip("Arial tiene todos los caracteres de prueba")


def _mtext(block, family: str, text: str) -> None:
    block.add_mtext(rf"{{\f{family}|b0|i0|c0|p34;{text}}}", dxfattribs={"char_height": 2})


def _source(path: Path, *, texts: list[tuple[str, str]]) -> Path:
    """DET con un MTEXT por cada (familia, texto)."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Millimeters
    block = document.blocks.new("DET")
    block.add_line((0, 0), (20, 10))
    for family, text in texts:
        _mtext(block, family, text)
    document.modelspace().add_blockref("DET", (0, 0))
    document.saveas(path)
    return path


def _extract(source: Path, out: Path, *extra: str):
    code = main(
        [
            "cad",
            "extract-block",
            str(source),
            "--block-name",
            "DET",
            "--scale",
            "1:50",
            "--output-prefix",
            str(out / "detalle"),
            *extra,
        ]
    )
    assert code == 0
    return (out / "detalle.txt").read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _fidelity(source: Path, mapping: dict | None = None, tmp_path: Path | None = None):
    json_path = None
    if mapping is not None:
        json_path = tmp_path / "sustituciones.json"
        json_path.write_text(json.dumps(mapping), encoding="utf-8")
    restore = configure_fonts(str(json_path) if json_path else None, None, source, str(_ARIAL))
    try:
        return inspect_fidelity(source)
    finally:
        restore()


def test_missing_glyphs_are_reported_per_substitute_font(tmp_path: Path) -> None:
    char = _arial_lacks()
    source = _source(
        tmp_path / "o.dxf",
        texts=[
            ("FuenteInventada987", f"Cota {char}{char}"),
            ("FuenteInventada987", f"Otra {char}"),
            ("FuenteInventada987", "Texto sin problema ≤ ± °"),
        ],
    )

    report = _fidelity(source)

    assert report.missing_glyphs == {("arial.ttf", char): 2}  # 2 textos, no 3 caracteres


def test_text_in_an_installed_font_is_not_checked(tmp_path: Path) -> None:
    source = _source(tmp_path / "o.dxf", texts=[("Arial", f"Cota {_arial_lacks()}")])

    assert _fidelity(source).missing_glyphs == {}


def test_all_characters_present_reports_nothing(tmp_path: Path) -> None:
    source = _source(tmp_path / "o.dxf", texts=[("FuenteInventada987", "Perfil 0+010 ≤ ±")])

    assert _fidelity(source).missing_glyphs == {}


def test_message_pluralizes_and_truncates() -> None:
    one = missing_glyphs_message({("arial.ttf", "漢"): 1})
    assert "arial.ttf: 1 carácter sin glifo («漢» U+6F22 (1 texto))" in one

    many = missing_glyphs_message({("arial.ttf", chr(0x4E00 + i)): 2 for i in range(10)})
    assert "arial.ttf: 10 caracteres sin glifo" in many
    assert "(2 textos)" in many
    assert "… y 2 más" in many
    assert "pueden salir vacíos o como un recuadro" in many


def test_report_and_terminal_warn_about_missing_glyphs(tmp_path: Path, capsys) -> None:
    char = _arial_lacks()
    source = _source(tmp_path / "o.dxf", texts=[("FuenteInventada987", f"Cota {char}")])

    report = _flat(_extract(source, tmp_path / "salida"))
    printed = capsys.readouterr().out

    assert "Aviso: La fuente sustituta no tiene todos los caracteres" in printed
    assert "GLYPHS_MISSING (advertencia): La fuente sustituta no tiene" in report
    notes = report.split("A tener en cuenta", 1)[1]
    assert f"arial.ttf: 1 carácter sin glifo («{char}» U+{ord(char):04X} (1 texto))" in notes
    assert "Indique con --font-substitutions una fuente que contenga esos caracteres." in notes
    assert "Estado final: COMPLETA CON ADVERTENCIAS" in report


def test_report_has_no_glyph_warning_when_nothing_is_missing(tmp_path: Path) -> None:
    source = _source(tmp_path / "o.dxf", texts=[("FuenteInventada987", "Solo ASCII")])

    report = _extract(source, tmp_path / "salida")

    assert "GLYPHS_MISSING" not in report and "sin glifo" not in report


def test_report_says_if_each_substitution_was_configured_or_automatic(tmp_path: Path) -> None:
    source = _source(
        tmp_path / "o.dxf",
        texts=[("FuenteConfigurada987", "uno"), ("FuenteAutomatica987", "dos")],
    )
    mapping = tmp_path / "sustituciones.json"
    mapping.write_text(json.dumps({"FuenteConfigurada987": _ARIAL.as_posix()}), encoding="utf-8")

    report = _flat(_extract(source, tmp_path / "salida", "--font-substitutions", str(mapping)))

    assert (
        "Fuente sustituida: FuenteConfigurada987 → arial.ttf; usos 1; no se encontró; "
        "sustitución configurada (JSON --font-substitutions)"
    ) in report
    assert (
        "Fuente sustituida: FuenteAutomatica987 → arial.ttf; usos 1; no se encontró; "
        "sustitución automática (fuente de respaldo)"
    ) in report


def test_font_origins_are_recorded_in_the_fidelity_report(tmp_path: Path) -> None:
    source = _source(
        tmp_path / "o.dxf",
        texts=[("FuenteConfigurada987", "uno"), ("FuenteAutomatica987", "dos")],
    )

    report = _fidelity(source, {"FuenteConfigurada987": _ARIAL.as_posix()}, tmp_path)

    assert report.font_origins == {
        "fuenteconfigurada987": "configurada",
        "fuenteautomatica987": "respaldo automático",
    }


def test_register_font_file_makes_the_font_known_by_file_name(tmp_path: Path) -> None:
    register_font_file(_ARIAL)

    assert fonts.font_manager.has_font("arial.ttf")


def test_register_font_file_explains_an_unreadable_font(tmp_path: Path) -> None:
    broken = tmp_path / "rota.ttf"
    broken.write_bytes(b"esto no es una fuente")

    with pytest.raises(CadProcessingError, match="No se pudo leer la fuente"):
        register_font_file(broken)


def test_ezdxf_still_has_the_internal_font_cache_we_rely_on() -> None:
    # Si esta prueba falla, ezdxf cambió su administrador de fuentes: revisar register_font_file.
    assert hasattr(fonts.font_manager, "_font_cache")
    assert callable(fonts.font_manager._font_cache.add_entry)


def test_register_font_file_fails_clearly_if_ezdxf_changes_internally(monkeypatch) -> None:
    monkeypatch.delattr(fonts.font_manager, "_font_cache")

    with pytest.raises(CadProcessingError) as error:
        register_font_file(_ARIAL)

    message = str(error.value)
    assert ezdxf.__version__ in message and "1.4.x" in message
    assert "administrador de fuentes" in message
