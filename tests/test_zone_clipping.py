"""Zona del Model Space como detalle (subfase 1): recorte de arcos, círculos, elipses y HATCH,
máscara de recorte del PDF para lo que no se puede recortar y capas visibles dentro de los
bloques."""

import json
import math
from pathlib import Path

import ezdxf
import pymupdf
import pytest
from ezdxf import bbox
from ezdxf.math import Vec3

from cde_exporter.cad.clipping import (
    clip_curve,
    clip_layout,
    clip_polygon,
    curve_parameters,
    entity_texts,
    is_clippable,
)
from cde_exporter.cad.exporters import TextRun, apply_clip_mask, run_inside
from cde_exporter.cad.modelspace_commands import filter_block_layers
from cde_exporter.cli import main

BOX = (0.0, 0.0, 10.0, 10.0)


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def _layout():
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    return document, document.modelspace()


def _extents(entity) -> tuple[float, float, float, float]:
    box = bbox.extents([entity])
    return (box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y)


def _within(entity, box=BOX, tolerance=1e-6) -> bool:
    xmin, ymin, xmax, ymax = _extents(entity)
    return (
        xmin >= box[0] - tolerance
        and ymin >= box[1] - tolerance
        and xmax <= box[2] + tolerance
        and ymax <= box[3] + tolerance
    )


# --------------------------------------------------------------------------- curvas


def test_a_circle_crossing_one_side_becomes_the_arc_inside() -> None:
    _document, modelspace = _layout()
    modelspace.add_circle((10, 5), 2, dxfattribs={"color": 3, "lineweight": 50})

    report = clip_layout(modelspace, BOX)

    (arc,) = list(modelspace)
    assert report.clipped == 1 and report.pieces == 1 and not report.not_clipped
    assert arc.dxftype() == "ARC"
    assert (arc.dxf.start_angle, arc.dxf.end_angle) == pytest.approx((90, 270))
    assert (arc.dxf.color, arc.dxf.lineweight) == (3, 50)
    assert _within(arc)


def test_the_piece_that_passes_through_angle_zero_is_one_arc() -> None:
    _document, modelspace = _layout()
    modelspace.add_circle((0, 5), 2)

    clip_layout(modelspace, BOX)

    (arc,) = list(modelspace)
    assert (arc.dxf.start_angle, arc.dxf.end_angle) == pytest.approx((270, 90))
    assert _within(arc)


def test_a_circle_around_the_zone_corner_leaves_four_arcs() -> None:
    _document, modelspace = _layout()
    modelspace.add_circle((5, 5), 5.5)

    report = clip_layout(modelspace, BOX)

    arcs = list(modelspace)
    assert report.pieces == 4 and len(arcs) == 4
    assert all(arc.dxftype() == "ARC" and _within(arc) for arc in arcs)


def test_a_curve_whose_box_touches_the_zone_but_not_its_line_is_removed() -> None:
    _document, modelspace = _layout()
    modelspace.add_circle((5, 5), 20)  # la rodea: su caja la toca, su línea no

    report = clip_layout(modelspace, BOX)

    assert report.outside == 1 and len(modelspace) == 0


def test_a_mirrored_arc_is_cut_in_its_own_angles() -> None:
    """ARC con extrusión (0, 0, −1): en el sistema universal la x cambia de signo."""
    _document, modelspace = _layout()
    arc = modelspace.add_arc((-1, 5), 2, 0, 180, dxfattribs={"extrusion": (0, 0, -1)})
    assert _extents(arc)[0] == pytest.approx(-1)  # centro universal en (1, 5)

    clip_layout(modelspace, BOX)

    (piece,) = list(modelspace)
    assert (piece.dxf.start_angle, piece.dxf.end_angle) == pytest.approx((60, 180))
    assert Vec3(piece.dxf.extrusion) == Vec3(0, 0, -1)
    assert _within(piece)


def test_an_ellipse_is_cut_by_its_parameter() -> None:
    _document, modelspace = _layout()
    modelspace.add_ellipse((10, 5), (0, 3, 0), 0.5, start_param=1, end_param=5)

    clip_layout(modelspace, BOX)

    (piece,) = list(modelspace)
    assert piece.dxftype() == "ELLIPSE"
    assert (piece.dxf.start_param, piece.dxf.end_param) == pytest.approx((1, math.pi))
    assert _within(piece)


def test_clip_curve_reports_a_curve_fully_inside_as_not_cut() -> None:
    intervals, cut = clip_curve((5, 5), (1, 0), (0, 1), 0, 2 * math.pi, BOX)

    assert not cut and intervals == [(0, 2 * math.pi)]


def test_a_tilted_circle_is_not_clippable() -> None:
    _document, modelspace = _layout()
    circle = modelspace.add_circle((10, 5), 2, dxfattribs={"extrusion": (1, 0, 0)})

    assert curve_parameters(circle) is None and not is_clippable(circle)


# --------------------------------------------------------------------------- HATCH


def _area(polygon) -> float:
    return abs(
        sum(
            a[0] * b[1] - b[0] * a[1]
            for a, b in zip(polygon, polygon[1:] + polygon[:1], strict=False)
        )
        / 2
    )


def test_clip_polygon_keeps_the_part_inside() -> None:
    polygon = clip_polygon([(-5, -5), (5, -5), (5, 5), (-5, 5)], BOX)

    assert _area(polygon) == pytest.approx(25)
    assert all(0 <= x <= 5 and 0 <= y <= 5 for x, y in polygon)


def test_a_hatch_with_a_hole_is_cut_with_both_boundaries() -> None:
    _document, modelspace = _layout()
    hatch = modelspace.add_hatch(color=1, dxfattribs={"layer": "RELLENO"})
    hatch.paths.add_polyline_path([(-5, -5), (5, -5), (5, 5), (-5, 5)], flags=1)
    hatch.paths.add_polyline_path([(-1, -1), (1, -1), (1, 1), (-1, 1)], flags=16)
    hatch.paths.add_polyline_path([(2, 2), (3, 2), (3, 3), (2, 3)], flags=16)  # dentro

    report = clip_layout(modelspace, BOX)

    (clipped,) = list(modelspace)
    assert report.clipped == 1 and report.pieces == 3
    assert clipped.dxf.layer == "RELLENO" and clipped.dxf.color == 1
    assert _within(clipped)
    areas = sorted(_area([(v[0], v[1]) for v in path.vertices]) for path in clipped.paths)
    assert areas == pytest.approx([1, 1, 25])
    assert [path.path_type_flags & 17 for path in clipped.paths] == [1, 16, 16]


def test_a_hatch_with_a_curved_boundary_is_cut_close_to_the_curve() -> None:
    _document, modelspace = _layout()
    hatch = modelspace.add_hatch(color=5)
    edges = hatch.paths.add_edge_path()
    edges.add_arc((10, 10), 3, 0, 360)

    clip_layout(modelspace, BOX)

    (clipped,) = list(modelspace)
    (path,) = clipped.paths
    points = [(v[0], v[1]) for v in path.vertices]
    # ezdxf lleva el arco a curvas Bézier (como al dibujarlo), que se separan del círculo
    # exacto un 0,027 % del radio; el recorte sigue esa curva.
    assert _area(points) == pytest.approx(math.pi * 9 / 4, rel=1e-3)
    assert _within(clipped)


def test_a_pattern_hatch_keeps_its_pattern_and_loses_its_association() -> None:
    _document, modelspace = _layout()
    hatch = modelspace.add_hatch()
    hatch.set_pattern_fill("ANSI31", scale=0.5)
    hatch.dxf.associative = 1
    hatch.paths.add_polyline_path([(5, 5), (15, 5), (15, 8), (5, 8)])

    clip_layout(modelspace, BOX)

    (clipped,) = list(modelspace)
    assert clipped.dxf.pattern_name == "ANSI31"
    assert clipped.dxf.pattern_scale == pytest.approx(0.5)
    assert clipped.dxf.associative == 0
    assert _extents(clipped) == pytest.approx((5, 5, 10, 8))


def test_a_mirrored_hatch_is_cut_in_its_own_system() -> None:
    _document, modelspace = _layout()
    hatch = modelspace.add_hatch(dxfattribs={"extrusion": (0, 0, -1)})
    # OCS con extrusión (0, 0, −1): x = −8…−12 es x universal 8…12.
    hatch.paths.add_polyline_path([(-8, 2), (-12, 2), (-12, 4), (-8, 4)])

    clip_layout(modelspace, BOX)

    (clipped,) = list(modelspace)
    assert _extents(clipped) == pytest.approx((8, 2, 10, 4))


def test_a_hatch_fully_inside_is_not_touched_and_one_fully_outside_is_removed() -> None:
    _document, modelspace = _layout()
    inner = modelspace.add_hatch()
    inner.paths.add_polyline_path([(2, 2), (4, 2), (4, 4)])
    outer = modelspace.add_hatch()
    edges = outer.paths.add_edge_path()
    edges.add_arc((5, 5), 20, 0, 360)  # rodea la zona: no la recorta, la contiene
    far = modelspace.add_hatch()
    far.paths.add_polyline_path([(9, 9), (30, 9), (30, 30), (9, 30)])

    report = clip_layout(modelspace, BOX)

    kinds = [entity.dxftype() for entity in modelspace]
    assert kinds == ["HATCH", "HATCH", "HATCH"]
    assert report.clipped == 2  # la que rodea la zona queda como la zona; la otra, su esquina
    assert inner.paths.paths[0].vertices[0][:2] == (2, 2)


# --------------------------------------------------------------------------- textos


def test_entity_texts_reads_every_kind_of_text() -> None:
    document, modelspace = _layout()
    text = modelspace.add_text("COTA 12,5")
    mtext = modelspace.add_mtext("SECCIÓN\\PPEDESTAL %%c1/2")
    block = document.blocks.new("ROT")
    block.add_attdef("NUM", (0, 0))
    insert = modelspace.add_blockref("ROT", (0, 0))
    insert.add_auto_attribs({"NUM": "P-7"})
    dimension = modelspace.add_linear_dim(base=(0, 2), p1=(0, 0), p2=(4, 0), text="<> m")
    dimension.render()

    assert entity_texts(text) == ["COTA 12,5"]
    assert entity_texts(mtext) == ["SECCIÓN PEDESTAL Ø1/2"]
    assert entity_texts(insert) == ["P-7"]
    assert entity_texts(dimension.dimension) == ["4 m"]


def test_text_crossing_the_border_is_kept_whole_with_its_text() -> None:
    _document, modelspace = _layout()
    modelspace.add_text("DETALLE VECINO", dxfattribs={"insert": (8, 5), "height": 1})

    report = clip_layout(modelspace, BOX)

    assert dict(report.not_clipped) == {"TEXT": 1}
    assert report.masked_texts == [("TEXT", "0", "DETALLE VECINO")]


# --------------------------------------------------------------------------- máscara


def test_apply_clip_mask_hides_what_is_drawn_outside() -> None:
    document = pymupdf.open()
    page = document.new_page(width=200, height=100)
    page.draw_rect(pymupdf.Rect(10, 10, 190, 90), color=None, fill=(0, 0, 0))
    page.insert_text((20, 50), "texto")

    apply_clip_mask(page, pymupdf.Rect(0, 0, 100, 100))

    content = page.read_contents()
    assert content.startswith(b"q\n0 0 100 100 re W n\n") and content.rstrip().endswith(b"Q")
    pixmap = page.get_pixmap(dpi=72)
    assert pixmap.pixel(50, 50)[:3] == (0, 0, 0)  # dentro de la máscara: se ve
    assert pixmap.pixel(150, 50)[:3] == (255, 255, 255)  # fuera: no se ve
    assert "texto" in page.get_text()  # la capa de texto sigue (dentro de la máscara)


def test_run_inside_needs_the_whole_line_inside() -> None:
    run = TextRun("ABC", Vec3(8, 5), Vec3(9, 5), Vec3(8, 6), 1.0, 1.5)

    assert run_inside(run, BOX)
    assert not run_inside(TextRun("ABC", Vec3(9, 5), Vec3(10, 5), Vec3(9, 6), 1.0, 1.5), BOX)


# --------------------------------------------------------------------------- capas


def test_filter_block_layers_works_like_freezing_a_layer_in_a_viewport() -> None:
    document, _modelspace = _layout()
    for name in ("COTAS", "ACERO", "EJES"):
        document.layers.add(name)
    inner = document.blocks.new("TORNILLO")
    inner.add_circle((0, 0), 1)  # capa 0: toma la del INSERT
    detail = document.blocks.new("DETALLE")
    detail.add_line((0, 0), (1, 0), dxfattribs={"layer": "ACERO"})
    detail.add_line((0, 0), (0, 1), dxfattribs={"layer": "COTAS"})
    detail.add_line((0, 0), (1, 1))  # capa 0
    detail.add_blockref("TORNILLO", (0, 0), dxfattribs={"layer": "EJES"})
    detail.add_blockref("TORNILLO", (2, 0))  # capa 0

    removed = filter_block_layers(document, (), ("COTAS", "EJES"))

    assert dict(removed) == {"COTAS": 1, "EJES": 1}
    assert [(entity.dxftype(), entity.dxf.layer) for entity in detail] == [
        ("LINE", "ACERO"),
        ("LINE", "0"),
        ("INSERT", "0"),
    ]
    assert len(inner) == 1
    assert dict(filter_block_layers(document, ("ACER*",), ())) == {}  # ya solo hay ACERO y 0


# --------------------------------------------------------------------------- orden completa


def _source(path: Path) -> Path:
    """Dos detalles vecinos en metros: el de la izquierda (x 0…10) y su vecino (x 10…20).

    - HATCH, círculo y elipse del vecino que entran en la zona izquierda;
    - un título (MTEXT) del vecino que cruza el borde x = 10;
    - el bloque DET en la zona izquierda, con una línea en COTAS y otra en la capa 0.
    """
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    for name in ("DETALLE", "COTAS", "TITULOS"):
        document.layers.add(name)
    modelspace = document.modelspace()
    attributes = {"layer": "DETALLE"}
    modelspace.add_lwpolyline([(1, 1), (9, 1), (9, 9), (1, 9)], close=True, dxfattribs=attributes)
    hatch = modelspace.add_hatch(color=8, dxfattribs=attributes)
    hatch.paths.add_polyline_path([(8, 2), (14, 2), (14, 4), (8, 4)])
    modelspace.add_circle((10, 6), 1, dxfattribs=attributes)
    modelspace.add_ellipse((10, 8), (1.5, 0, 0), 0.5, dxfattribs=attributes)
    modelspace.add_mtext(
        "SECCIÓN VECINA ESC 1:20",
        dxfattribs={"layer": "TITULOS", "insert": (8, 0.6), "char_height": 0.4},
    )
    block = document.blocks.new("DET")
    block.add_line((0, 0), (2, 0), dxfattribs={"layer": "COTAS"})
    block.add_line((0, 0), (0, 2))
    modelspace.add_blockref("DET", (3, 3), dxfattribs=attributes)
    document.saveas(path)
    return path


def _export(source: Path, prefix: Path, *extra: str) -> dict:
    assert (
        main(
            [
                "cad",
                "extract-modelspace",
                str(source),
                "--window",
                "0",
                "0",
                "10",
                "10",
                "--scale",
                "1:100",
                "--output-prefix",
                str(prefix),
                *extra,
            ]
        )
        == 0
    )
    return json.loads(prefix.with_name(f"{prefix.name}.manifest.json").read_text("utf-8"))


def test_a_zone_comes_out_cut_masked_and_as_a_complete_package(tmp_path: Path) -> None:
    prefix = tmp_path / "zonas" / "Zona_01"

    manifest = _export(_source(tmp_path / "obra.dxf"), prefix, "--exclude-layers", "COTAS")

    # La página es la zona (10 × 10 m a 1:100 = 100 mm) más los márgenes.
    assert manifest["pdf"]["page_size_mm"] == pytest.approx([110, 110], abs=1e-4)
    clip = manifest["selection"]["clip"]
    assert clip["clipped_entities"] == 3  # HATCH, círculo y elipse
    assert clip["not_clipped"] == [{"type": "MTEXT", "count": 1}]
    checks = {item["name"]: item["passed"] for item in manifest["validation"]["checks"]}
    assert all(checks.values()), checks
    codes = {
        notice["code"]
        for notices in manifest["notices"]["by_category"].values()
        for notice in notices
    }
    assert {"MODELSPACE_MASKED", "MODELSPACE_LAYERS_IN_BLOCKS"} <= codes
    assert "MODELSPACE_NOT_CLIPPED" not in codes

    report = prefix.with_suffix(".txt").read_text("utf-8")
    assert "MTEXT, capa «TITULOS»: SECCIÓN VECINA ESC 1:20" in report
    assert "Quitado dentro de los bloques (capas que no se ven): «COTAS» 1" in report

    with pymupdf.open(prefix.with_suffix(".pdf")) as document:
        page = document[0]
        assert page.read_contents().startswith(b"q\n")  # máscara de recorte
        drawn = page.get_drawings()
        # Lo recortado no pasa de x = 10 m (página: margen de 5 mm + 100 mm).
        right = (5 + 100 + 0.5) * 72 / 25.4
        gray = [  # el HATCH (ACI 8); los glifos del título, en negro, los corta la máscara
            item for item in drawn if item.get("fill") is not None and 0.4 < item["fill"][0] < 0.6
        ]
        assert gray and max(item["rect"].x1 for item in gray) <= right + 0.01


def test_with_a_mask_the_manifest_drawing_is_the_visible_part(tmp_path: Path) -> None:
    """Manifiesto 1.5: con máscara, cad_bounds/page_bounds_mm son lo dibujado recortado a la
    zona (dentro de la página) y ``mask`` da la zona; así lo mide el Componedor
    (docs/manifiesto.md, «Cómo medir los trazos con máscara»)."""
    prefix = tmp_path / "Zona_01"

    manifest = _export(_source(tmp_path / "obra.dxf"), prefix)

    drawing = manifest["pdf"]["drawing"]
    page_w, page_h = manifest["pdf"]["page_size_mm"]
    bounds = drawing["page_bounds_mm"]
    # g) dentro de la página (el MTEXT sale de la zona por la derecha: se recorta su caja)
    assert 0 <= bounds["x0"] < bounds["x1"] <= page_w and 0 <= bounds["y0"] < bounds["y1"] <= page_h
    assert drawing["cad_bounds"]["max"][0] == pytest.approx(10.0)
    mask = drawing["mask"]
    assert mask["zone_cad"] == [0.0, 0.0, 10.0, 10.0]
    assert mask["bleed_mm"] == 0.5
    zone = mask["zone_page_mm"]
    assert [zone["x0"], zone["y0"], zone["x1"], zone["y1"]] == pytest.approx([5, 5, 105, 105])
    # h) los trazos del PDF (sin el fondo), cada uno recortado a zone_page_mm, frente a
    # page_bounds_mm: ±0,5 mm.
    mm = 25.4 / 72
    clip = pymupdf.Rect(zone["x0"] / mm, zone["y0"] / mm, zone["x1"] / mm, zone["y1"] / mm)
    with pymupdf.open(prefix.with_suffix(".pdf")) as document:
        page = document[0]
        rects = []
        for item in page.get_drawings():
            rect = item["rect"]
            # Como en el Componedor: los segmentos rectos dan un Rect «vacío»; se recorta a mano.
            x0, y0 = max(rect.x0, clip.x0), max(rect.y0, clip.y0)
            x1, y1 = min(rect.x1, clip.x1), min(rect.y1, clip.y1)
            if x0 <= x1 and y0 <= y1:
                rects.append((x0, y0, x1, y1))
    measured = (
        min(r[0] for r in rects) * mm,
        min(r[1] for r in rects) * mm,
        max(r[2] for r in rects) * mm,
        max(r[3] for r in rects) * mm,
    )
    declared = (bounds["x0"], bounds["y0"], bounds["x1"], bounds["y1"])
    assert max(abs(a - b) for a, b in zip(measured, declared, strict=True)) <= 0.5


def test_without_masked_entities_the_pdf_has_no_mask(tmp_path: Path) -> None:
    prefix = tmp_path / "Zona"

    manifest = _export(_source(tmp_path / "obra.dxf"), prefix, "--layers", "DETALLE")

    assert manifest["pdf"]["drawing"]["mask"] is None
    assert manifest["selection"]["clip"]["not_clipped"] == []
    with pymupdf.open(prefix.with_suffix(".pdf")) as document:
        assert not document[0].read_contents().startswith(b"q\n0")


def test_list_shows_the_layers_inside_the_blocks(tmp_path: Path, capsys) -> None:
    source = _source(tmp_path / "obra.dxf")

    code = main(
        [
            "cad",
            "extract-modelspace",
            str(source),
            "--window",
            "0",
            "0",
            "10",
            "10",
            "--layers",
            "DETALLE,TITULOS",
            "--list",
            "--output-prefix",
            str(tmp_path / "x"),
        ]
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "Capas dentro de los bloques elegidos" in output
    assert "capa «COTAS»: 1 (no se ve: se quita)" in output
    assert "capa «0»: 1 (toma la capa del INSERT)" in output
    assert "lo corta la máscara del PDF: MTEXT 1" in output
    assert not list(tmp_path.glob("x*"))
