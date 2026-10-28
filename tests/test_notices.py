"""Lista única de avisos (Fase 4, paso 4): código, severidad y categoría en un solo sitio."""

import re
from pathlib import Path

import ezdxf
import pytest

from cde_exporter.cad import pdf_checks
from cde_exporter.cad.notices import WARNING_CODES, NoticeCategory
from cde_exporter.cli import main

_SOURCE = Path(__file__).resolve().parents[1] / "src" / "cde_exporter" / "cad"
_ARIAL = Path("C:/Windows/Fonts/arial.ttf")


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def test_every_warning_code_in_the_code_has_a_category() -> None:
    """Un código nuevo sin categoría caería en "entities" sin que nadie lo decida."""
    codes = set()
    for path in _SOURCE.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        codes |= set(re.findall(r'ExtractionWarning\(\s*"([A-Z_]+)"', text))
        codes |= set(re.findall(r'^\s*"([A-Z_]{6,})",\s*$', text, flags=re.MULTILINE))
        codes |= set(re.findall(r'\("([A-Z_]+)", Severity\.', text))
    codes |= set(pdf_checks._ADVISORY_WARNINGS.values())
    codes &= {code for code in codes if "_" in code}
    missing = sorted(code for code in codes if code not in WARNING_CODES)

    assert "DEPENDENCY_UNRESOLVED_XREF" in codes  # la búsqueda encuentra los códigos
    assert missing == []


def test_categories_are_the_ones_of_the_manifest() -> None:
    assert {category.value for category in NoticeCategory} == {
        "fonts",
        "hatch",
        "dependencies",
        "units",
        "text",
        "renderer",
        "scale",
        "entities",
    }


def _extract(tmp_path: Path, document) -> str:
    document.modelspace().add_blockref("DET", (0, 0))
    source = tmp_path / "origen.dxf"
    document.saveas(source)
    out = tmp_path / "salida"
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
        ]
    )
    assert code == 0
    return (out / "detalle.txt").read_text(encoding="utf-8")


@pytest.mark.skipif(not _ARIAL.is_file(), reason="arial.ttf no está instalado")
def test_font_substitution_is_an_info_notice_and_keeps_the_status_complete(
    tmp_path: Path,
) -> None:
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    # Con $PDSIZE <= 0 (valor por defecto) ezdxf avisa "relative point size is not supported",
    # que sí cuenta como advertencia; aquí solo deben quedar avisos informativos.
    document.header["$PDSIZE"] = 1
    document.styles.add("ROMANS", font="romans.shx")
    block = document.blocks.new("DET")
    block.add_line((0, 0), (10, 0))
    block.add_text("Cota", height=0.5, dxfattribs={"insert": (0, 1), "style": "ROMANS"})

    report = _extract(tmp_path, document)

    notes = " ".join(report.split("A tener en cuenta", 1)[1].split())
    assert (
        "- Fuente sustituida: romans.shx se dibuja con arial.ttf en 1 texto (sustitución "
        "automática (fuente de respaldo))." in notes
    )
    # Solo hay avisos informativos (fuente sustituida y capa de texto): estado completo.
    assert "Estado final: COMPLETA\n" in report
