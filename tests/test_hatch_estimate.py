"""Estimación de tramos de un HATCH con su contorno real, comparada con lo que dibuja ezdxf."""

import ezdxf
import pytest
from ezdxf.render import hatching

from cde_exporter.cad.exporters import _bbox_estimate, _estimate_hatch_segments


def _exact(hatch) -> int:
    """Tramos que ezdxf genera de verdad para el patrón (lo que se dibujaría)."""
    loops = hatching.hatch_boundary_paths(hatch, filter_text_boxes=True)
    return sum(
        1
        for baseline in hatching.pattern_baselines(hatch, jiggle_origin=True)
        for line in hatching.hatch_paths(baseline, loops)
        for _segment in baseline.pattern_renderer(line.distance).render(line.start, line.end)
    )


def _hatch(pattern: str, scale: float, *boundaries):
    document = ezdxf.new()
    hatch = document.modelspace().add_hatch()
    hatch.set_pattern_fill(pattern, scale=scale)
    for boundary in boundaries:
        hatch.paths.add_polyline_path(boundary, is_closed=True)
    return hatch


def _rectangle(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


@pytest.mark.parametrize(
    ("pattern", "scale", "boundary"),
    [
        ("ANSI31", 1.0, _rectangle(0, 0, 50, 20)),
        ("ANSI31", 0.1, _rectangle(0, 0, 5, 5)),
        ("ANSI35", 0.005, _rectangle(0, 0, 15, 0.2)),  # tira larga: la caja exageraba mucho
        ("ANSI37", 0.05, _rectangle(0, 0, 3, 2)),
        ("ANSI31", 0.2, [(0, 0), (10, 0), (5, 8)]),  # triángulo
    ],
)
def test_estimate_is_close_to_what_ezdxf_draws(pattern, scale, boundary) -> None:
    hatch = _hatch(pattern, scale, boundary)

    exact = _exact(hatch)
    estimate = _estimate_hatch_segments(hatch)

    assert exact > 0
    assert abs(estimate - exact) <= max(5, 0.1 * exact), (estimate, exact)


@pytest.mark.parametrize("pattern", ["ANSI31", "ANSI37", "ANSI35"])
def test_hatch_with_a_hole_and_an_island_matches_ezdxf(pattern: str) -> None:
    # Con un hueco cada línea se corta en dos (más tramos) y hay menos área con guiones.
    hatch = _hatch(
        pattern,
        0.1,
        _rectangle(0, 0, 10, 10),
        _rectangle(2, 2, 8, 8),
        _rectangle(4, 4, 6, 6),  # isla dentro del hueco: vuelve a rayarse
    )

    exact = _exact(hatch)

    assert abs(_estimate_hatch_segments(hatch) - exact) <= max(5, 0.1 * exact)


def test_long_thin_strip_is_no_longer_overestimated() -> None:
    # La estimación anterior (perímetro de la caja) daba unas 100 veces más tramos.
    strip = _hatch("ANSI35", 0.005, _rectangle(0, 0, 15, 0.2))

    assert _bbox_estimate(strip) > 50 * _estimate_hatch_segments(strip)


def test_solid_and_dense_hatches_have_no_pattern_segments() -> None:
    document = ezdxf.new()
    solid = document.modelspace().add_hatch()
    solid.set_solid_fill()
    solid.paths.add_polyline_path(_rectangle(0, 0, 1, 1), is_closed=True)
    dense = _hatch("ANSI31", 1e-6, _rectangle(0, 0, 1, 1))  # líneas más juntas que 0,0001

    assert _estimate_hatch_segments(solid) == 0
    assert _estimate_hatch_segments(dense) == 0


def test_unreadable_contour_falls_back_to_the_box(monkeypatch) -> None:
    hatch = _hatch("ANSI31", 1.0, _rectangle(0, 0, 10, 10))

    def broken(*_args, **_kwargs):
        raise ValueError("contorno roto")

    monkeypatch.setattr(hatching, "hatch_boundary_paths", broken)

    assert _estimate_hatch_segments(hatch) == _bbox_estimate(hatch)
