"""Capa de texto invisible y buscable en el PDF (Fase 3, paso 3)."""

import math
from pathlib import Path

import ezdxf
import pymupdf
import pytest
from ezdxf.addons.drawing import RenderContext
from ezdxf.addons.drawing import pymupdf as pdf_backend
from ezdxf.math import Matrix44, Vec3

from cde_exporter.cad import exporters
from cde_exporter.cad.detail_model import SEARCHABLE_TEXT_CHECK, ResultStatus, ValidationCheck
from cde_exporter.cad.exporters import HELVETICA_CAP_HEIGHT, TextRun, write_text_layer
from cde_exporter.cad.pdf_checks import (
    PdfValidationError,
    build_extraction_result,
    check_searchable_text,
    searchable_text_warnings,
)
from cde_exporter.cli import main

_ARIAL = Path("C:/Windows/Fonts/arial.ttf")
_needs_arial = pytest.mark.skipif(not _ARIAL.is_file(), reason="arial.ttf no está instalado")
_MM_PER_POINT = 25.4 / 72


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def _new_document():
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    block = document.blocks.new("DET")
    block.add_lwpolyline([(-5, -5), (45, -5), (45, 15), (-5, 15)], close=True)
    return document, block


def _extract(document, tmp_path: Path, *extra: str) -> tuple[str, Path]:
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
            *extra,
        ]
    )
    assert code == 0
    return (out / "detalle.txt").read_text(encoding="utf-8"), out / "detalle.pdf"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _baseline_angle(quad: pymupdf.Quad) -> float:
    """Ángulo de la línea base (grados, antihorario como en el CAD; la página va hacia abajo)."""
    base = quad.ur - quad.ul
    return math.degrees(math.atan2(-base.y, base.x)) % 360


def _project(point: pymupdf.Point, origin: pymupdf.Point, axis: pymupdf.Point) -> float:
    """Distancia en mm de papel de ``origin`` a ``point`` a lo largo de ``axis`` (unitario)."""
    offset = point - origin
    return (offset.x * axis.x + offset.y * axis.y) * _MM_PER_POINT


@_needs_arial
def test_hidden_entities_do_not_reach_the_text_layer(tmp_path: Path) -> None:
    document, block = _new_document()
    document.layers.add("APAGADA").off()
    document.layers.add("CONGELADA").freeze()
    block.add_text("Visible", height=0.5, dxfattribs={"insert": (0, 0)})
    block.add_text("En capa apagada", height=0.5, dxfattribs={"insert": (0, 3), "layer": "APAGADA"})
    block.add_text(
        "En capa congelada", height=0.5, dxfattribs={"insert": (0, 6), "layer": "CONGELADA"}
    )
    block.add_text("Invisible", height=0.5, dxfattribs={"insert": (0, 9), "invisible": 1})

    report, pdf = _extract(document, tmp_path)

    with pymupdf.open(pdf) as opened:
        assert opened[0].get_text().split("\n")[0] == "Visible"
        assert opened[0].get_text().strip() == "Visible"
    assert "[OK] Texto buscable: 1 de 1" in _flat(report)


def _draw_one_text(monkeypatch, *, fail: bool) -> exporters._StrictFrontend:
    document = ezdxf.new("R2018")
    document.modelspace().add_text("Dañado", height=1, dxfattribs={"insert": (0, 0)})
    if fail:
        original = exporters.Frontend.draw_text_entity

        def draw_then_fail(self, entity, properties):
            original(self, entity, properties)  # el texto llega a dibujarse...
            raise ValueError("datos inválidos de prueba")  # ...y después la entidad falla

        monkeypatch.setattr(exporters.Frontend, "draw_text_entity", draw_then_fail)
    frontend = exporters._StrictFrontend(
        RenderContext(document),
        pdf_backend.PyMuPdfBackend(),
        omit_unsupported=True,
        capture_text=True,
    )
    frontend.draw_layout(document.modelspace())
    return frontend


def test_omitted_entities_leave_no_text_in_the_layer(monkeypatch) -> None:
    assert [run.text for run in _draw_one_text(monkeypatch, fail=False).text_runs] == ["Dañado"]

    frontend = _draw_one_text(monkeypatch, fail=True)

    assert frontend.text_runs == []
    assert sum(frontend.omitted.values()) == 1


@_needs_arial
def test_an_empty_mtext_is_not_missing_text(tmp_path: Path) -> None:
    document, block = _new_document()
    block.add_mtext("", dxfattribs={"insert": (0, 5), "char_height": 0.5})
    block.add_text("Solo uno", height=0.5, dxfattribs={"insert": (0, 0)})

    report, _pdf = _extract(document, tmp_path)

    assert "[OK] Texto buscable: 1 de 1" in _flat(report)


@_needs_arial
def test_rotated_and_nested_text_is_found_over_its_glyphs(tmp_path: Path) -> None:
    document, block = _new_document()
    block.add_text("GIRO CERO", height=0.5, dxfattribs={"insert": (0, 0)})
    block.add_text("GIRO CUARENTA", height=0.5, dxfattribs={"insert": (8, 0), "rotation": 45})
    block.add_text("GIRO NOVENTA", height=0.5, dxfattribs={"insert": (20, 0), "rotation": 90})
    inner = document.blocks.new("SUB2")
    inner.add_text("ANIDADO DOS", height=0.5, dxfattribs={"insert": (0, 0)})
    outer = document.blocks.new("SUB")
    outer.add_text("ANIDADO UNO", height=0.5, dxfattribs={"insert": (0, 0)})
    outer.add_blockref("SUB2", (0, -2), dxfattribs={"rotation": 90})
    block.add_blockref("SUB", (32, 4), dxfattribs={"rotation": 30})
    # Ángulo final: 30° del INSERT exterior y 30° + 90° del anidado.
    expected = {
        "GIRO CERO": 0,
        "GIRO CUARENTA": 45,
        "GIRO NOVENTA": 90,
        "ANIDADO UNO": 30,
        "ANIDADO DOS": 120,
    }

    _report, pdf = _extract(document, tmp_path)

    with pymupdf.open(pdf) as opened:
        page = opened[0]
        glyphs = [item for item in page.get_drawings() if item.get("fill") is not None]
        for text, angle in expected.items():
            quads = page.search_for(text, quads=True)
            assert len(quads) == 1, text
            quad = quads[0]
            assert _baseline_angle(quad) == pytest.approx(angle, abs=0.5), text
            # La caja encontrada cae sobre los glifos dibujados de ese texto. Se mide en los
            # ejes del texto (a lo largo de la línea base y a través de ella), en mm de papel.
            along_axis = (quad.ur - quad.ul) / abs(quad.ur - quad.ul)
            across_axis = (quad.ll - quad.ul) / abs(quad.ll - quad.ul)
            area = quad.rect + (-1, -1, 1, 1)
            points = [
                point
                for item in glyphs
                if item["rect"] in area
                for path in item["items"]
                for point in path[1:]
                if isinstance(point, pymupdf.Point)
            ]
            assert points, text
            along = [_project(point, quad.ul, along_axis) for point in points]
            across = [_project(point, quad.ul, across_axis) for point in points]
            length = abs(quad.ur - quad.ul) * _MM_PER_POINT
            height = abs(quad.ll - quad.ul) * _MM_PER_POINT
            # Letras de 10 mm en el papel (0,5 m a 1:50): el margen lateral de cada letra
            # separa la tinta del borde de la caja; se admite el 20 % de la altura (2 mm).
            assert min(along) == pytest.approx(0, abs=2.0), text
            assert max(along) == pytest.approx(length, abs=2.0), text
            assert 0 <= min(across) and max(across) <= height, text


@_needs_arial
def test_unicode_and_hyphen_are_extracted_as_written(tmp_path: Path) -> None:
    document, block = _new_document()
    text = "-9.70 ≥ 3 α √2 Ø25 ñ"
    block.add_text(text, height=0.5, dxfattribs={"insert": (0, 0)})

    _report, pdf = _extract(document, tmp_path)

    with pymupdf.open(pdf) as opened:
        page = opened[0]
        assert page.get_text().strip() == text  # «-» es U+002D, no el guion blando U+00AD
        assert len(page.search_for("-9.70")) == 1
        assert len(page.search_for("≥ 3 α")) == 1


def test_rotation_follows_pymupdf_morph_convention() -> None:
    """PyMuPDF aplica ``morph`` con el eje Y invertido: sin compensarlo, 45° saldría a −45°."""
    page_height = 400.0
    # CAD (y hacia arriba) -> página (y hacia abajo).
    to_page = Matrix44.chain(Matrix44.scale(1, -1, 1), Matrix44.translate(0, page_height, 0))
    origin = Vec3(100, 100, 0)
    direction = Vec3.from_deg_angle(45)
    cap_height = 10.0
    advance = pymupdf.Font("helv").text_length("ABC", fontsize=cap_height / HELVETICA_CAP_HEIGHT)
    run = TextRun(
        "ABC",
        origin,
        origin + direction,
        origin + direction.orthogonal(),
        cap_height,
        advance,
    )
    with pymupdf.open() as document:
        page = document.new_page(width=400, height=page_height)
        write_text_layer(page, [run], to_page)
        quads = page.search_for("ABC", quads=True)

        assert len(quads) == 1
        assert _baseline_angle(quads[0]) == pytest.approx(45, abs=0.1)
        # El texto empieza en el origen y avanza a lo largo de la línea base.
        start = pymupdf.Point(100, page_height - 100)
        along = start + pymupdf.Point(direction.x, -direction.y) * (advance / 2)
        assert along in quads[0].rect
        assert abs((quads[0].ul + quads[0].ll) * 0.5 - start) < cap_height


@_needs_arial
def test_no_text_layer_option_keeps_text_as_paths_only(tmp_path: Path) -> None:
    document, block = _new_document()
    block.add_text("Hola", height=0.5, dxfattribs={"insert": (0, 0)})

    report, pdf = _extract(document, tmp_path, "--no-text-layer")

    with pymupdf.open(pdf) as opened:
        assert opened[0].get_text().strip() == ""
    flat = _flat(report)
    assert "Texto buscable" not in flat
    assert "no se puede seleccionar, copiar ni buscar" in flat


def test_searchable_text_check_counts_found_lines(tmp_path: Path) -> None:
    path = tmp_path / "texto.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text((50, 50), "Uno")
        document.save(path)

    complete = check_searchable_text(path, ("Uno",))
    missing = check_searchable_text(path, ("Uno", "Dos"))

    assert complete.passed
    assert complete.detail.startswith("1 de 1")
    assert not missing.passed
    assert missing.detail.startswith("Faltan 1 de 2 líneas de texto")
    assert "'Dos'" in missing.detail


def test_searchable_text_check_never_blocks_publication() -> None:
    readable = ValidationCheck("PDF legible", True, "Se abre.")
    missing = ValidationCheck(SEARCHABLE_TEXT_CHECK, False, "Faltan 1 de 2 líneas de texto.")
    broken = ValidationCheck("Una sola página", False, "Tiene 2 páginas.")

    result = build_extraction_result("detalle.pdf", (readable, missing), (), ())

    assert result.status is ResultStatus.COMPLETE_WITH_WARNINGS
    assert [warning.code for warning in searchable_text_warnings((readable, missing))] == [
        "TEXT_NOT_SEARCHABLE"
    ]
    with pytest.raises(PdfValidationError) as error:
        build_extraction_result("detalle.pdf", (readable, missing, broken), (), ())
    assert error.value.checks == (broken,)


def test_a_missing_line_is_a_warning_not_a_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    """Si una línea no se encuentra, el PDF se publica igual: aviso y estado con advertencias."""
    original = exporters.write_text_layer

    def drop_one_line(page, runs, matrix):
        original(page, [run for run in runs if run.text != "Sin capa"], matrix)

    monkeypatch.setattr(exporters, "write_text_layer", drop_one_line)
    document, block = _new_document()
    block.add_text("Con capa", height=0.5, dxfattribs={"insert": (0, 0)})
    block.add_text("Sin capa", height=0.5, dxfattribs={"insert": (0, 5)})

    report, pdf = _extract(document, tmp_path)

    assert pdf.is_file()
    flat = _flat(report)
    message = (
        "Faltan 1 de 2 líneas de texto al buscarlas en el PDF: 'Sin capa'. Se ven en el "
        "dibujo, pero no se pueden buscar ni copiar."
    )
    assert f"[AVISO] Texto buscable: {message}" in flat
    assert "Estado final: COMPLETA CON ADVERTENCIAS" in flat
    assert f"- {message}" in _flat(report.split("A tener en cuenta", 1)[1])
    assert f"Aviso: {message}" in _flat(capsys.readouterr().err)
