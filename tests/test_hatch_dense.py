"""HATCH con líneas de patrón demasiado juntas: ezdxf los rellena y el TXT lo declara."""

from pathlib import Path

import ezdxf
import pymupdf
import pytest

from cde_exporter.cad.detail_model import EntityStatus
from cde_exporter.cad.entity_policy import (
    HATCH_DENSE_REASON,
    EntityFacts,
    PolicyContext,
    decide,
)
from cde_exporter.cli import main


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def test_dense_pattern_is_simplified_by_the_policy() -> None:
    facts = EntityFacts("HATCH", hatch_kind="patrón", hatch_dense=True)

    for mode in ("normal", "auto"):
        decision = decide(facts, PolicyContext(mode, 50000))
        assert decision.status is EntityStatus.SIMPLIFIED
        assert decision.reason == HATCH_DENSE_REASON
    # Un modo que ya simplifica todos manda sobre esta regla.
    outline = decide(facts, PolicyContext("outline", 50000))
    assert outline.reason == "HATCH mostrado solo como contorno por --hatch-mode outline"


def _source(path: Path) -> Path:
    document = ezdxf.new()
    document.units = ezdxf.units.InsertUnits.Millimeters
    block = document.blocks.new("DET")
    dense = block.add_hatch(dxfattribs={"layer": "DENSA"})
    dense.set_pattern_fill("ANSI31", scale=1e-6)
    dense.paths.add_polyline_path([(0, 0), (1, 0), (1, 1), (0, 1)], is_closed=True)
    normal = block.add_hatch(dxfattribs={"layer": "NORMAL"})
    normal.set_pattern_fill("ANSI31", scale=0.25)
    normal.paths.add_polyline_path([(2, 0), (6, 0), (6, 4), (2, 4)], is_closed=True)
    document.modelspace().add_blockref("DET", (0, 0))
    document.saveas(path)
    return path


@pytest.mark.parametrize("mode", ["auto", "normal"])
def test_report_declares_the_dense_hatch_and_the_pdf_fills_it(tmp_path: Path, mode: str) -> None:
    out = tmp_path / "salida"
    code = main(
        [
            "cad",
            "extract-block",
            str(_source(tmp_path / "o.dxf")),
            "--block-name",
            "DET",
            "--scale",
            "1:1",
            "--hatch-mode",
            mode,
            "--output-prefix",
            str(out / "detalle"),
        ]
    )
    assert code == 0

    report = (out / "detalle.txt").read_text(encoding="utf-8")
    flat = " ".join(report.split())
    assert "HATCH | DENSA | simplificadas | 1" in report
    assert "HATCH | NORMAL | conservadas | 1" in report
    assert "HATCH simplificados: 1" in report
    assert (
        "patrón | ANSI31 | 1e-06 | relleno sólido en lugar del patrón; motivo: líneas del patrón "
        "más juntas que 0,0001 unidades | 1"
    ) in flat
    assert "FIDELITY_RECONCILIATION" not in report
    with pymupdf.open(out / "detalle.pdf") as document:
        drawings = document[0].get_drawings()
    assert any(item["type"] == "f" for item in drawings)  # el HATCH denso, relleno
    assert any(item["type"] == "s" and len(item["items"]) >= 5 for item in drawings)  # el normal
