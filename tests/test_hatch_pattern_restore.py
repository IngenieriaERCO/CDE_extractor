"""Patrón de los HATCH dentro de un INSERT: corrección del doble escalado y giro de ezdxf 1.4.4."""

import random
from pathlib import Path

import ezdxf
import pytest
from ezdxf.math import Vec2
from ezdxf.render import hatching

from cde_exporter.cad.exporters import export_vector_outputs, restore_hatch_pattern

_SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10)]


def _document(scale: float = 0.5, angle: float = 30.0):
    document = ezdxf.new()
    block = document.blocks.new("B")
    hatch = block.add_hatch()
    hatch.set_pattern_fill("ANSI31", scale=scale, angle=angle)
    hatch.paths.add_polyline_path(_SQUARE, is_closed=True)
    return document, hatch


def _virtual_hatch(insert):
    return next(e for e in insert.virtual_entities() if e.dxftype() == "HATCH")


def _lines(hatch):
    return [
        (round(line.angle % 360, 6), tuple(round(c, 6) for c in line.offset))
        for line in hatch.pattern.lines
    ]


def _exact(hatch) -> int:
    loops = hatching.hatch_boundary_paths(hatch, filter_text_boxes=True)
    return sum(
        1
        for baseline in hatching.pattern_baselines(hatch)
        for line in hatching.hatch_paths(baseline, loops)
        for _ in baseline.pattern_renderer(line.distance).render(line.start, line.end)
    )


def test_ezdxf_still_scales_and_rotates_the_pattern_twice() -> None:
    # Si esta prueba falla, ezdxf corrigió DXFPolygon.transform: revisar restore_hatch_pattern.
    document, hatch = _document()
    virtual = _virtual_hatch(document.modelspace().add_blockref("B", (100, 0)))

    assert virtual.pattern.lines[0].angle == pytest.approx(hatch.pattern.lines[0].angle + 30)
    assert virtual.pattern.lines[0].offset.magnitude == pytest.approx(
        hatch.pattern.lines[0].offset.magnitude * 0.5
    )


def test_translation_keeps_the_pattern_and_moves_its_origin() -> None:
    document, hatch = _document()
    virtual = _virtual_hatch(document.modelspace().add_blockref("B", (100, 7)))

    restore_hatch_pattern(virtual)

    assert _lines(virtual) == _lines(hatch)
    assert virtual.pattern.lines[0].base_point.isclose(Vec2(100, 7))
    assert _exact(virtual) == _exact(hatch)


def test_rotated_and_scaled_insert_rotates_and_scales_the_pattern_once() -> None:
    document, hatch = _document(scale=0.5, angle=30)
    insert = document.modelspace().add_blockref(
        "B", (50, 20), dxfattribs={"xscale": 2, "yscale": 2, "rotation": 90}
    )
    virtual = _virtual_hatch(insert)

    restore_hatch_pattern(virtual)

    line = virtual.pattern.lines[0]
    expected_offset = hatch.pattern.lines[0].offset.rotate_deg(90) * 2
    assert line.angle % 360 == pytest.approx((hatch.pattern.lines[0].angle + 90) % 360)
    assert line.offset.isclose(expected_offset, abs_tol=1e-6)
    assert line.base_point.isclose(Vec2(50, 20), abs_tol=1e-6)
    # Geometría y patrón escalan juntos: los tramos no cambian.
    assert _exact(virtual) == pytest.approx(_exact(hatch), rel=0.02)


def test_nested_inserts_are_corrected_from_the_definition() -> None:
    document, hatch = _document(scale=0.25, angle=0)
    outer = document.blocks.new("EXTERIOR")
    outer.add_blockref("B", (10, 0), dxfattribs={"rotation": 45})
    insert = document.modelspace().add_blockref("EXTERIOR", (1000, 2000))
    inner = next(e for e in insert.virtual_entities() if e.dxftype() == "INSERT")
    virtual = _virtual_hatch(inner)

    restore_hatch_pattern(virtual)

    line = virtual.pattern.lines[0]
    assert line.angle % 360 == pytest.approx((hatch.pattern.lines[0].angle + 45) % 360)
    assert line.offset.magnitude == pytest.approx(hatch.pattern.lines[0].offset.magnitude)
    assert line.base_point.isclose(Vec2(1010, 2000), abs_tol=1e-6)


def test_modelspace_hatch_is_not_touched() -> None:
    document = ezdxf.new()
    hatch = document.modelspace().add_hatch()
    hatch.set_pattern_fill("ANSI31", scale=0.5, angle=30)
    hatch.paths.add_polyline_path(_SQUARE, is_closed=True)
    before = _lines(hatch)

    restore_hatch_pattern(hatch)

    assert _lines(hatch) == before


def test_drawing_a_pattern_hatch_is_reproducible(tmp_path: Path) -> None:
    document, _hatch = _document()
    document.modelspace().add_blockref("B", (4924093.7, 2632176.2))
    source = tmp_path / "rayado.dxf"
    document.saveas(source)

    random.seed(1)
    export_vector_outputs(source, tmp_path / "a.svg", tmp_path / "a.pdf")
    random.seed(2)
    export_vector_outputs(source, tmp_path / "b.svg", tmp_path / "b.pdf")

    assert (tmp_path / "a.svg").read_bytes() == (tmp_path / "b.svg").read_bytes()
