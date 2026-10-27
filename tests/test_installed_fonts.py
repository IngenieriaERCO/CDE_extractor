"""Las fuentes instaladas no se sustituyen por la de respaldo; las ausentes sí."""

import json
from pathlib import Path
from types import SimpleNamespace

import ezdxf
import pytest

from cde_exporter.cad.fidelity import inspect_fidelity
from cde_exporter.cad.font_config import (
    active_substitutions,
    configure_fonts,
    font_is_installed,
    font_stem,
    is_installed_font,
    is_shx_request,
)

_FONTS = Path("C:/Windows/Fonts")
_ARIAL = _FONTS / "arial.ttf"
pytestmark = pytest.mark.skipif(not _ARIAL.is_file(), reason="arial.ttf no está instalado")


def _dxf(path: Path, *families: str, style_font: str | None = None) -> Path:
    document = ezdxf.new()
    if style_font:
        document.styles.new("Estilo", dxfattribs={"font": style_font})
    for index, family in enumerate(families):
        document.modelspace().add_mtext(
            rf"{{\f{family}|b0|i0|c0|p34;Texto {index}}}", dxfattribs={"char_height": 2}
        )
    if style_font:
        document.modelspace().add_text("Hola", dxfattribs={"style": "Estilo", "height": 1})
    document.saveas(path)
    return path


def test_font_stem_and_shx_detection() -> None:
    assert font_stem("verdana.ttf") == "verdana"
    assert font_stem("Segoe UI") == "Segoe UI"
    assert font_stem("Arial Narrow") == "Arial Narrow"
    assert font_stem("romans.SHX") == "romans"
    assert is_shx_request("romans.shx") and is_shx_request("ROMANS.SHX")
    assert not is_shx_request("Verdana") and not is_shx_request("verdana.ttf")


def test_installed_match_requires_the_same_family_or_file_name() -> None:
    verdana = SimpleNamespace(family="Verdana", filename="verdana.ttf")
    assert is_installed_font("Verdana", verdana)
    assert is_installed_font("VERDANA", verdana)
    assert is_installed_font("verdana.ttf", verdana)
    assert not is_installed_font("Tahoma", verdana)  # aproximada: no es la pedida
    assert not is_installed_font("Verdana", None)


@pytest.mark.skipif(not (_FONTS / "verdana.ttf").is_file(), reason="verdana.ttf no instalada")
def test_installed_family_is_not_replaced_but_a_missing_one_is(tmp_path: Path) -> None:
    source = _dxf(tmp_path / "fuentes.dxf", "Verdana", "FuenteInventada987")

    restore = configure_fonts(None, None, source, str(_ARIAL))
    try:
        substitutions = active_substitutions()
        assert "verdana" not in substitutions
        assert substitutions["fuenteinventada987"][2] == "respaldo automático"
        fonts_used = {family: found for (_s, family, _r, found) in inspect_fidelity(source).fonts}
    finally:
        restore()

    assert fonts_used["Verdana"] is True
    assert fonts_used["FuenteInventada987"] is False


@pytest.mark.skipif(not (_FONTS / "verdana.ttf").is_file(), reason="verdana.ttf no instalada")
def test_installed_font_requested_by_file_name_in_a_style(tmp_path: Path) -> None:
    source = _dxf(tmp_path / "estilo.dxf", style_font="verdana.ttf")

    restore = configure_fonts(None, None, source, str(_ARIAL))
    try:
        assert "verdana.ttf" not in active_substitutions()
        report = inspect_fidelity(source)
    finally:
        restore()

    assert [found for (_s, family, _r, found) in report.fonts if family == "verdana.ttf"] == [True]


@pytest.mark.skipif(not (_FONTS / "segoeui.ttf").is_file(), reason="segoeui.ttf no instalada")
def test_file_name_that_differs_from_the_family_is_found() -> None:
    assert font_is_installed("segoeui.ttf")
    assert font_is_installed("Segoe UI")


def test_shx_and_unknown_fonts_are_not_installed() -> None:
    assert not font_is_installed("romans.shx")
    assert not font_is_installed("FuenteInventada987")
    assert not font_is_installed("fuente_inventada_987.ttf")


def test_configured_substitution_still_wins(tmp_path: Path) -> None:
    mapping = tmp_path / "sustituciones.json"
    mapping.write_text(json.dumps({"Verdana": _ARIAL.as_posix()}), encoding="utf-8")
    source = _dxf(tmp_path / "fuentes.dxf", "Verdana")

    restore = configure_fonts(str(mapping), None, source, str(_ARIAL))
    try:
        assert active_substitutions()["verdana"][2] == "configurada"
    finally:
        restore()
