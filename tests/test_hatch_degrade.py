"""Un HATCH cuyo patrón agota el tiempo o la memoria se rellena; el resto del dibujo sigue igual."""

from pathlib import Path

import ezdxf
import pymupdf
import pytest
from ezdxf.addons.drawing.frontend import Frontend

from cde_exporter.cad import exporters
from cde_exporter.cad.exporters import export_vector_outputs


def _source(path: Path, *, with_normal: bool = False) -> Path:
    """Bloque con un HATCH de patrón densísimo (millones de tramos) y, si se pide, uno normal."""
    document = ezdxf.new()
    block = document.blocks.new("B")
    dense = block.add_hatch(dxfattribs={"layer": "DENSO"})
    dense.set_pattern_fill("ANSI35", scale=0.002)
    dense.paths.add_polyline_path([(0, 0), (20, 0), (20, 20), (0, 20)], is_closed=True)
    if with_normal:
        normal = block.add_hatch(dxfattribs={"layer": "NORMAL"})
        normal.set_pattern_fill("ANSI31", scale=0.25)
        normal.paths.add_polyline_path([(30, 0), (40, 0), (40, 10), (30, 10)], is_closed=True)
    document.modelspace().add_blockref("B", (0, 0))
    document.saveas(path)
    return path


def _drawings(pdf: Path):
    with pymupdf.open(pdf) as document:
        return document[0].get_drawings()


def _export(tmp_path: Path, source: Path, name: str, **options):
    return export_vector_outputs(
        source, tmp_path / f"{name}.svg", tmp_path / f"{name}.pdf", hatch_mode="normal", **options
    )


@pytest.fixture
def short_timeout(monkeypatch):
    monkeypatch.setattr(exporters, "HATCHING_TIMEOUT", 0.05)


def test_timed_out_hatch_is_filled_instead_of_stopping(tmp_path: Path, short_timeout) -> None:
    report = _export(tmp_path, _source(tmp_path / "o.dxf"), "a")

    assert report.degraded == (
        (
            "HATCH",
            "DENSO",
            report.degraded[0][2],
            "ANSI35",
            0.002,
            "tiempo agotado al dibujar el patrón (0.05 s)",
        ),
    )
    drawings = _drawings(tmp_path / "a.pdf")
    # Solo queda el relleno: ni rastro de las líneas que ezdxf dejó a medias.
    assert any(item["type"] == "f" for item in drawings)
    assert all(len(item["items"]) < 100 for item in drawings)


def test_the_pdf_does_not_try_again_what_the_svg_already_degraded(
    tmp_path: Path, short_timeout, monkeypatch
) -> None:
    attempts = []
    original = exporters._StrictFrontend._draw_hatch_or_degrade

    def counting(self, *args, **kwargs):
        attempts.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(exporters._StrictFrontend, "_draw_hatch_or_degrade", counting)

    report = _export(tmp_path, _source(tmp_path / "o.dxf"), "b", draw_svg=True)

    assert len(attempts) == 1  # el SVG lo intentó; el PDF lo rellenó directamente
    assert len(report.degraded) == 1
    assert (tmp_path / "b.svg").exists() and (tmp_path / "b.pdf").exists()


def test_only_the_problematic_hatch_is_degraded(tmp_path: Path, short_timeout) -> None:
    report = _export(tmp_path, _source(tmp_path / "o.dxf", with_normal=True), "c")

    assert [row[1] for row in report.degraded] == ["DENSO"]
    # El HATCH normal sigue dibujado con su patrón (líneas).
    assert any(
        item["type"] == "s" and len(item["items"]) >= 5 for item in _drawings(tmp_path / "c.pdf")
    )


def test_memory_error_in_a_pattern_is_degraded_too(tmp_path: Path, monkeypatch) -> None:
    def out_of_memory(self, polygon, paths, properties):
        raise MemoryError

    monkeypatch.setattr(Frontend, "draw_hatch_pattern", out_of_memory)

    report = _export(tmp_path, _source(tmp_path / "o.dxf"), "d")

    assert [row[5] for row in report.degraded] == ["memoria insuficiente al dibujar el patrón"]
    assert any(item["type"] == "f" for item in _drawings(tmp_path / "d.pdf"))


def test_nothing_is_degraded_with_the_normal_time_limit(tmp_path: Path) -> None:
    document = ezdxf.new()
    block = document.blocks.new("B")
    hatch = block.add_hatch()
    hatch.set_pattern_fill("ANSI31", scale=1)
    hatch.paths.add_polyline_path([(0, 0), (10, 0), (10, 10), (0, 10)], is_closed=True)
    document.modelspace().add_blockref("B", (0, 0))
    source = tmp_path / "normal.dxf"
    document.saveas(source)

    report = _export(tmp_path, source, "e")

    assert report.degraded == ()
    assert exporters.HATCHING_TIMEOUT == 30.0
