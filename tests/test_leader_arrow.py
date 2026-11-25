"""Punta de flecha de los LEADER (leader_arrows.py): AutoCAD no la dibuja si el
primer tramo mide menos que dos veces la flecha; ezdxf la dibujaba siempre (en PRGD, cuñas de
75 m con el estilo «ESC 1-75», DIMASZ 75, en tramos de menos de 1,5 m)."""

from pathlib import Path

import ezdxf
import pymupdf
import pytest

from cde_exporter.cad.leader_arrows import leader_arrow_too_big
from cde_exporter.cli import main


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def _document():
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    style = document.dimstyles.new("ESC 1-75")
    style.dxf.dimasz = 75
    return document


def test_the_arrow_is_dropped_only_when_it_does_not_fit() -> None:
    document = _document()
    modelspace = document.modelspace()
    short = modelspace.add_leader([(0, 0), (1, 1), (2, 1)], dimstyle="ESC 1-75")
    long = modelspace.add_leader([(0, 0), (200, 0), (210, 0)], dimstyle="ESC 1-75")
    normal = modelspace.add_leader([(0, 0), (10, 10), (12, 10)])  # flecha estándar (2,5)
    no_arrow = modelspace.add_leader([(0, 0), (1, 1)], dimstyle="ESC 1-75")
    no_arrow.dxf.has_arrowhead = 0

    assert leader_arrow_too_big(short)
    assert not leader_arrow_too_big(long)  # 200 ≥ 2 × 75
    assert not leader_arrow_too_big(normal)
    assert not leader_arrow_too_big(no_arrow)
    assert not leader_arrow_too_big(modelspace.add_line((0, 0), (1, 1)))
    # El DXF no cambia: el arreglo solo dibuja una copia sin punta.
    assert short.dxf.get("has_arrowhead", 1) == 1


def test_a_short_leader_is_drawn_without_its_giant_arrow(tmp_path: Path) -> None:
    """Un detalle de 10 × 10 m con un LEADER de tramo corto: sin el arreglo, la flecha de 75 m
    salía como una cuña enorme (y con máscara la tapaba solo en parte)."""
    document = _document()
    block = document.blocks.new("DETALLE")
    block.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)
    block.add_leader([(5, 5), (6, 6), (7, 6)], dimstyle="ESC 1-75")
    document.modelspace().add_blockref("DETALLE", (0, 0))
    document.saveas(tmp_path / "obra.dxf")
    prefix = tmp_path / "Detalle"

    code = main(
        [
            "cad",
            "extract-block",
            str(tmp_path / "obra.dxf"),
            "--block-name",
            "DETALLE",
            "--scale",
            "1:100",
            "--output-prefix",
            str(prefix),
        ]
    )

    assert code == 0
    with pymupdf.open(prefix.with_suffix(".pdf")) as pdf:
        width = pdf[0].rect.width * 25.4 / 72
        drawings = pdf[0].get_drawings()
    # Página: el detalle (100 mm) más márgenes; sin la flecha de 75 m (750 mm a 1:100).
    assert width == pytest.approx(110, abs=0.1)
    assert not [item for item in drawings if item.get("fill") is not None]
    widest = max(item["rect"].width for item in drawings) * 25.4 / 72
    assert widest <= 100.5
