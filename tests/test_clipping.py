"""Recorte geométrico contra la ventana (extract-modelspace --window), con casos sintéticos."""

import json
import math
from pathlib import Path

import ezdxf
import pytest

from cde_exporter.cad.clipping import clip_layout, clip_path, clip_segment
from cde_exporter.cli import main

BOX = (0.0, 0.0, 10.0, 10.0)


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def _xy(piece) -> list[tuple[float, float]]:
    return [(round(x, 9), round(y, 9)) for x, y, *_rest in piece]


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        ((2, 2, 8, 8), (0.0, 1.0)),  # dentro
        ((-5, 5, 15, 5), (0.25, 0.75)),  # atraviesa
        ((5, -5, 5, 5), (0.5, 1.0)),  # vertical, entra
        ((11, 0, 20, 5), None),  # fuera
        ((-5, 12, 15, 12), None),  # paralela, fuera
        ((3, 3, 3, 3), (0.0, 1.0)),  # un punto dentro
    ],
)
def test_clip_segment(segment, expected) -> None:
    result = clip_segment(*segment, BOX)
    assert result == (pytest.approx(expected) if expected else None)


def test_a_curve_that_enters_and_leaves_three_times_gives_three_pieces() -> None:
    # Zigzag en y: dentro, fuera por arriba, dentro, fuera, dentro.
    points = [(x, y, 0.0, 0.0, 0.0) for x, y in [(1, 5), (3, 15), (5, 5), (7, 15), (9, 5)]]

    pieces, cut = clip_path(points, False, BOX)

    assert cut
    assert [_xy(piece) for piece in pieces] == [
        [(1, 5), (2, 10)],
        [(4, 10), (5, 5), (6, 10)],
        [(8, 10), (9, 5)],
    ]


def test_a_closed_ring_cut_at_its_start_is_joined() -> None:
    # Empieza dentro de la ventana, sale por la derecha y vuelve: un solo tramo abierto.
    ring = [(x, y, 0.0, 0.0, 0.0) for x, y in [(5, 2), (15, 2), (15, 8), (5, 8)]]

    pieces, cut = clip_path(ring, True, BOX)

    assert cut
    assert [_xy(piece) for piece in pieces] == [[(10, 8), (5, 8), (5, 2), (10, 2)]]


def test_a_closed_ring_fully_inside_is_not_cut() -> None:
    ring = [(x, y, 0.0, 0.0, 0.0) for x, y in [(2, 2), (8, 2), (8, 8), (2, 8)]]

    pieces, cut = clip_path(ring, True, BOX)

    assert not cut and len(pieces) == 1


def test_z_and_width_are_interpolated_at_the_cut() -> None:
    path = [(0.0, 5.0, 10.0, 0.0, 2.0), (20.0, 5.0, 30.0, 0.0, 0.0)]

    (piece,), cut = clip_path(path, False, BOX)

    assert cut
    start, end = piece
    assert end[:3] == pytest.approx((10.0, 5.0, 20.0))
    assert start[3:] == pytest.approx((0.0, 1.0))  # el ancho llega a 1 en el borde


def _document():
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    document.layers.add("CURVAS", color=3)
    return document


def test_clip_layout_cuts_lines_and_polylines_and_keeps_the_rest() -> None:
    document = _document()
    modelspace = document.modelspace()
    attributes = {"layer": "CURVAS", "color": 5, "lineweight": 35}
    modelspace.add_line((-5, 5), (15, 5), dxfattribs=attributes)
    modelspace.add_lwpolyline(
        [(-5, 1), (5, 1), (15, 1)], dxfattribs={**attributes, "const_width": 0.2}
    )
    modelspace.add_lwpolyline([(-5, 3, 0, 0, 0.5), (15, 3)], format="xyseb")  # con bulge
    modelspace.add_circle((10, 5), 2)  # cruza el borde
    modelspace.add_circle((5, 5), 1)  # dentro
    modelspace.add_line((20, 20), (30, 30))  # entera fuera: se quita

    report = clip_layout(modelspace, BOX)

    # El círculo que cruza el borde también se recorta (queda un ARC); la polilínea con bulge
    # entra entera (la corta la máscara del PDF).
    assert report.clipped == 3 and report.pieces == 3 and report.outside == 1
    assert dict(report.not_clipped) == {"LWPOLYLINE": 1}
    lines = [entity for entity in modelspace if entity.dxftype() == "LINE"]
    assert lines[0].dxf.start.x == pytest.approx(0) and lines[0].dxf.end.x == pytest.approx(10)
    cut = [
        entity
        for entity in modelspace
        if entity.dxftype() == "LWPOLYLINE" and entity.dxf.get("const_width", 0) == 0.2
    ]
    (polyline,) = cut
    assert [(round(x, 9), round(y, 9)) for x, y in polyline.get_points("xy")] == [
        (0, 1),
        (5, 1),
        (10, 1),
    ]
    assert (polyline.dxf.layer, polyline.dxf.color, polyline.dxf.lineweight) == ("CURVAS", 5, 35)
    assert report.not_clipped_bounds[0] == pytest.approx(-5)


def test_a_long_3d_contour_with_many_vertices_is_cut_into_every_piece() -> None:
    """Curva de nivel de 2 001 vértices que sale 6 veces de la ventana (seno de 3 periodos que
    pasa de y = 10 y de y = 0): quedan 7 tramos."""
    document = _document()
    modelspace = document.modelspace()
    points = [(x / 100, 5 + 8 * math.sin(2 * math.pi * 3 * x / 2000), 42.5) for x in range(0, 2001)]
    modelspace.add_polyline3d([(x - 5, y, z) for x, y, z in points], dxfattribs={"layer": "CURVAS"})

    report = clip_layout(modelspace, (-5.0, 0.0, 15.0, 10.0))

    pieces = list(modelspace)
    assert report.clipped == 1 and report.pieces == len(pieces)
    assert len(pieces) == 7
    for piece in pieces:
        assert piece.is_3d_polyline and piece.dxf.layer == "CURVAS"
        for vertex in piece.vertices:
            x, y, z = vertex.dxf.location
            assert -5 - 1e-9 <= x <= 15 + 1e-9 and -1e-9 <= y <= 10 + 1e-9
            assert z == pytest.approx(42.5)


def _source(path: Path) -> Path:
    """Curvas de nivel sueltas que atraviesan toda la zona del bloque DET y un ARC que cruza."""
    document = _document()
    modelspace = document.modelspace()
    for y in range(-20, 41, 4):
        modelspace.add_polyline3d(
            [(-30, y, 30 + y / 10), (0, y + 2, 30 + y / 10), (60, y, 30 + y / 10)],
            dxfattribs={"layer": "CURVAS"},
        )
    block = document.blocks.new("DET")
    block.add_lwpolyline([(0, 0), (20, 0), (20, 12), (0, 12)], close=True)
    block.add_circle((10, 6), 3)
    modelspace.add_blockref("DET", (5, 5))
    document.saveas(path)
    return path


def _manifest(prefix: Path) -> dict:
    return json.loads(prefix.with_name(f"{prefix.name}.manifest.json").read_text("utf-8"))


def test_clipped_modelspace_page_overlays_the_block_page_exactly(tmp_path: Path) -> None:
    source = _source(tmp_path / "obra.dxf")
    block, curves = tmp_path / "bloque" / "det", tmp_path / "curvas" / "curvas"

    assert (
        main(
            [
                "cad",
                "extract-block",
                str(source),
                "--block-name",
                "DET",
                "--scale",
                "1:200",
                "--output-prefix",
                str(block),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "cad",
                "extract-modelspace",
                str(source),
                "--layers",
                "CURVAS",
                "--window-from-block",
                "DET",
                "--scale",
                "1:200",
                "--output-prefix",
                str(curves),
            ]
        )
        == 0
    )

    block_manifest, curves_manifest = _manifest(block), _manifest(curves)
    assert curves_manifest["pdf"]["page_size_mm"] == pytest.approx(
        block_manifest["pdf"]["page_size_mm"], abs=1e-6
    )
    assert curves_manifest["pdf"]["drawing"]["cad_to_page"]["matrix"] == pytest.approx(
        block_manifest["pdf"]["drawing"]["cad_to_page"]["matrix"], abs=1e-6
    )
    clip = curves_manifest["selection"]["clip"]
    assert clip["clipped_entities"] > 0 and clip["not_clipped"] == []
    checks = {item["name"]: item["passed"] for item in curves_manifest["validation"]["checks"]}
    assert all(checks.values()), checks


def test_an_arc_crossing_the_window_is_cut_and_the_page_is_the_window(tmp_path: Path) -> None:
    source = _source(tmp_path / "obra.dxf")
    document = ezdxf.readfile(source)
    document.modelspace().add_arc((25, 11), 4, 0, 180, dxfattribs={"layer": "CURVAS"})
    document.saveas(source)
    prefix = tmp_path / "curvas" / "curvas"

    assert (
        main(
            [
                "cad",
                "extract-modelspace",
                str(source),
                "--layers",
                "CURVAS",
                "--window",
                "5",
                "5",
                "25",
                "17",
                "--scale",
                "1:200",
                "--output-prefix",
                str(prefix),
            ]
        )
        == 0
    )

    manifest = _manifest(prefix)
    assert manifest["selection"]["clip"]["not_clipped"] == []
    width, height = manifest["pdf"]["page_size_mm"]
    # La zona mide 20 × 12 m: 100 × 60 mm a 1:200, más los márgenes; el arco ya no la amplía.
    assert (width, height) == pytest.approx((100 + 10, 60 + 10), abs=1e-4)
    notices = [item for items in manifest["notices"]["by_category"].values() for item in items]
    assert not any(item["code"] == "MODELSPACE_NOT_CLIPPED" for item in notices)


def test_a_closed_2d_polyline_with_widths_becomes_open_pieces() -> None:
    document = _document()
    modelspace = document.modelspace()
    ring = modelspace.add_polyline2d(
        [(5, 2, 0.4, 0.4, 0), (15, 2, 0.4, 0.4, 0), (15, 8, 0.4, 0.4, 0), (5, 8, 0.4, 0.4, 0)],
        format="xyseb",
        close=True,
        dxfattribs={"layer": "CURVAS", "elevation": (0, 0, 7.5)},
    )
    assert ring.is_closed

    report = clip_layout(modelspace, BOX)

    (piece,) = list(modelspace)
    assert report.clipped == 1 and report.pieces == 1
    assert piece.dxftype() == "POLYLINE" and piece.is_2d_polyline and not piece.is_closed
    assert [(round(v.dxf.location.x, 9), round(v.dxf.location.y, 9)) for v in piece.vertices] == [
        (10, 8),
        (5, 8),
        (5, 2),
        (10, 2),
    ]
    assert piece.dxf.elevation.z == pytest.approx(7.5)
    assert all(v.dxf.start_width == pytest.approx(0.4) for v in piece.vertices[:-1])
