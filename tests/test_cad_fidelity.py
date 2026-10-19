from pathlib import Path

import ezdxf
import pytest

from cde_exporter.cad.fidelity import (
    format_fidelity_report,
    inspect_fidelity,
    write_fidelity_report,
)
from cde_exporter.cad.font_config import active_substitutions, configure_fonts


def create_fidelity_dxf(path: Path) -> Path:
    document = ezdxf.new()
    document.styles.new(
        "SinFuente",
        dxfattribs={"font": "fuente_que_no_existe_987.ttf"},
    )
    nested = document.blocks.new("INTERIOR")
    nested.add_text("texto bloque", dxfattribs={"height": 1, "style": "SinFuente"})
    nested.add_hatch().set_solid_fill()
    nested.add_hatch().set_pattern_fill("ANSI31", scale=2.5)
    nested.add_hatch().set_gradient(name="LINEAR")
    block = document.blocks.new("EXTERIOR")
    block.add_line((0, 0), (2, 2))
    block.add_blockref("INTERIOR", (1, 1))
    block.add_attdef("TAG", insert=(0, 0), dxfattribs={"height": 1, "style": "SinFuente"})
    document.modelspace().add_blockref("EXTERIOR", (10, 20)).add_auto_attribs({"TAG": "valor"})
    document.modelspace().add_mtext("texto múltiple", dxfattribs={"style": "SinFuente"})
    image_def = document.add_image_def("imagen_externa.png", (32, 24))
    document.modelspace().add_image(image_def, (0, 0), (4, 3))
    document.modelspace().add_linear_dim(base=(0, 2), p1=(0, 0), p2=(4, 0)).render()
    document.saveas(path)
    return path


def test_fidelity_counts_nested_types_fonts_hatches_and_policy(tmp_path: Path) -> None:
    source = create_fidelity_dxf(tmp_path / "fidelity.dxf")
    report = inspect_fidelity(source, hatch_mode="outline")
    text = format_fidelity_report(report)

    assert report.entity_total > 8
    assert any(
        kind == "HATCH" and count == 3 for (kind, _layer), count in report.entity_counts.items()
    )
    assert {kind for kind, _pattern, _scale in report.hatches} == {"sólido", "patrón", "degradado"}
    assert report.simplified_hatches == 3
    assert report.lineweight_policy == "ABSOLUTE"
    assert any(kind == "IMAGE" for kind, _layer, _reason in report.unsupported_counts)
    assert any(not found for (_style, _source, _resolved, found) in report.fonts)
    assert "ATTRIB" in text
    assert "DIMENSION" in text
    assert "fuente_que_no_existe_987.ttf" in text
    assert "grosor de línea: ABSOLUTE" in text


def test_fidelity_report_is_utf8_and_mode_specific(tmp_path: Path) -> None:
    source = create_fidelity_dxf(tmp_path / "fidelity.dxf")
    destination = tmp_path / "detalle_fidelidad.txt"
    report = write_fidelity_report(source, destination, "ignore")

    contents = destination.read_bytes().decode("utf-8")
    assert report.simplified_hatches == 3
    assert "degradado" in contents
    assert "Modo de rayado: ignore" in contents


def test_inline_mtext_font_family_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "inline-font.dxf"
    document = ezdxf.new()
    document.modelspace().add_mtext(r"{\\fRoboto|b0|i0|c0|p2;Texto}", dxfattribs={"char_height": 2})
    document.saveas(path)

    report = inspect_fidelity(path)
    assert any(source == "Roboto" for (_style, source, _resolved, _found) in report.fonts)
    assert "Roboto" in format_fidelity_report(report)


def test_unresolved_inline_families_get_automatic_fallback(tmp_path: Path) -> None:
    path = tmp_path / "inline-fonts.dxf"
    document = ezdxf.new()
    document.modelspace().add_mtext(
        r"{\fFuenteInventada|b0|i0|c0|p2;Texto inventado} {\Fromans.shx;Texto SHX}",
        dxfattribs={"char_height": 2},
    )
    document.saveas(path)
    system_font = Path("C:/Windows/Fonts/arial.ttf")
    if not system_font.is_file():
        pytest.skip("arial.ttf no está instalado en esta plataforma")

    restore = configure_fonts(None, None, path, str(system_font))
    try:
        substitutions = active_substitutions()
        assert {"fuenteinventada", "romans.shx"} <= substitutions.keys()
        report = inspect_fidelity(path)
        text = format_fidelity_report(report)
        assert "FuenteInventada" in text
        assert "romans.shx" in text
        assert "respaldo automático" in text
        assert "arial.ttf" in text
    finally:
        restore()


def test_auto_hatch_mode_marks_only_over_threshold(tmp_path: Path) -> None:
    path = tmp_path / "auto-hatch.dxf"
    document = ezdxf.new()
    hatch = document.modelspace().add_hatch(dxfattribs={"layer": "PATRON"})
    hatch.set_pattern_fill("ANSI35", scale=0.005)
    hatch.paths.add_polyline_path([(0, 0), (15, 0), (15, 0.2), (0, 0.2)], is_closed=True)
    document.saveas(path)

    report = inspect_fidelity(path, hatch_mode="auto", hatch_threshold=1)
    assert report.simplified_hatches == 1
    assert report.hatch_simplifications[0][:3] == ("PATRON", "ANSI35", "0.005")
    assert "tramos estimados" in format_fidelity_report(report)
