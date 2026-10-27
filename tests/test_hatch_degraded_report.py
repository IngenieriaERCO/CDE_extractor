"""HATCH rellenados al dibujarlos (tiempo o memoria): reclasificación en el TXT y aviso."""

from pathlib import Path

import ezdxf
import pytest

from cde_exporter.cad import exporters
from cde_exporter.cad.detail_model import (
    DetailSource,
    EntityCount,
    EntityStatus,
    ExtractedDetail,
    HatchGroup,
    HatchSummary,
    SelectedReference,
    UnitInfo,
    degraded_hatches_message,
    mark_degraded_hatches,
)
from cde_exporter.cli import main

_REASON = "tiempo agotado al dibujar el patrón (0.05 s)"


@pytest.fixture(autouse=True)
def _short_timeout(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)
    monkeypatch.setattr(exporters, "HATCHING_TIMEOUT", 0.05)


def _source(path: Path, *, copies: int = 1) -> Path:
    document = ezdxf.new()
    document.units = ezdxf.units.InsertUnits.Millimeters
    inner = document.blocks.new("DENSO")
    hatch = inner.add_hatch(dxfattribs={"layer": "DENSO"})
    hatch.set_pattern_fill("ANSI35", scale=0.002)
    hatch.paths.add_polyline_path([(0, 0), (20, 0), (20, 20), (0, 20)], is_closed=True)
    block = document.blocks.new("DET")
    block.add_line((0, 0), (50, 25))
    for copy in range(copies):
        block.add_blockref("DENSO", (25 * copy, 0))
    document.modelspace().add_blockref("DET", (0, 0))
    document.saveas(path)
    return path


def _run(source: Path, out: Path) -> int:
    return main(
        [
            "cad",
            "extract-block",
            str(source),
            "--block-name",
            "DET",
            "--scale",
            "1:1",
            "--hatch-mode",
            "normal",
            "--output-prefix",
            str(out / "detalle"),
        ]
    )


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_timed_out_hatch_is_reported_as_simplified(tmp_path: Path, capsys) -> None:
    out = tmp_path / "salida"

    assert _run(_source(tmp_path / "o.dxf"), out) == 0

    report = (out / "detalle.txt").read_text(encoding="utf-8")
    flat = _flat(report)
    assert "HATCH | DENSO | simplificadas | 1" in report
    assert "HATCH | DENSO | conservadas" not in report
    assert "HATCH simplificados: 1" in report
    assert (
        f"patrón | ANSI35 | 0.002 | relleno sólido en lugar del patrón; motivo: {_REASON} | 1"
    ) in flat
    assert (
        "Simplificación HATCH: capa DENSO, patrón ANSI35, escala 0.002; operación: relleno sólido "
        f"en lugar del patrón; motivo: {_REASON}."
    ) in flat
    assert f"HATCH | DENSO | simplificadas | HATCH rellenado al dibujarlo: {_REASON}. | 1" in flat
    message = (
        "Se rellenó 1 HATCH cuyo patrón no se pudo dibujar: capa DENSO, patrón ANSI35, "
        f"escala 0.002 ({_REASON})."
    )
    assert f"HATCH_DEGRADED (advertencia): {message}" in flat
    notes = flat.split("A tener en cuenta", 1)[1]
    assert message in notes
    assert "Si necesita ver el patrón, revise su escala en el CAD" in notes
    assert "Estado final: COMPLETA CON ADVERTENCIAS" in report
    assert "FIDELITY_RECONCILIATION" not in report
    assert f"Aviso: {message}" in _flat(capsys.readouterr().err)


def test_two_copies_of_the_same_hatch_are_both_reported(tmp_path: Path) -> None:
    out = tmp_path / "salida"

    assert _run(_source(tmp_path / "o.dxf", copies=2), out) == 0

    report = (out / "detalle.txt").read_text(encoding="utf-8")
    assert "HATCH | DENSO | simplificadas | 2" in report
    assert "Se rellenaron 2 HATCH cuyo patrón no se pudo dibujar" in _flat(report)
    assert "FIDELITY_RECONCILIATION" not in report


def _detail(conserved: int) -> ExtractedDetail:
    return ExtractedDetail(
        source=DetailSource("entrada.dxf", "DET"),
        reference=SelectedReference(1, "A1", "0", (0, 0, 0), 0, (1, 1, 1)),
        units=UnitInfo(4, "mm", "declaradas en el DXF y coherentes", 1.0, ()),
        scale=None,
        bounds=None,
        inventory=(EntityCount("HATCH", "D", EntityStatus.CONSERVED, conserved),),
        dispositions=(),
        dependencies=(),
        warnings=(),
        hatches=HatchSummary(
            conserved,
            0,
            groups=(HatchGroup("patrón", "ANSI35", 0.002, conserved, "conserved", "conservado"),),
            mode="normal",
        ),
    )


def test_mark_moves_the_counts_and_updates_the_hatch_summary() -> None:
    marked = mark_degraded_hatches(_detail(3), ((7, "AB", "D", "ANSI35", 0.002, _REASON),))

    assert [(row.status, row.count) for row in marked.inventory] == [
        (EntityStatus.CONSERVED, 2),
        (EntityStatus.SIMPLIFIED, 1),
    ]
    assert [(item.index, item.handle, item.status) for item in marked.dispositions] == [
        (7, "AB", EntityStatus.SIMPLIFIED)
    ]
    assert marked.hatches.simplified == 1
    assert [(group.action, group.count, group.reason) for group in marked.hatches.groups] == [
        ("conserved", 2, None),
        ("simplified", 1, _REASON),
    ]
    assert [warning.code for warning in marked.warnings] == ["HATCH_DEGRADED"]


def test_mark_warns_when_the_renderer_reports_more_than_the_model_has() -> None:
    marked = mark_degraded_hatches(
        _detail(1),
        (
            (1, "A", "D", "ANSI35", 0.002, _REASON),
            (2, "B", "D", "ANSI35", 0.002, _REASON),
        ),
    )

    codes = [warning.code for warning in marked.warnings]
    assert codes == ["HATCH_DEGRADED", "FIDELITY_RECONCILIATION"]
    assert [(row.status, row.count) for row in marked.inventory] == [(EntityStatus.SIMPLIFIED, 1)]


def test_message_singular_plural_and_limit() -> None:
    one = ((0, None, "D", "ANSI35", 0.002, _REASON),)
    many = tuple((i, None, f"C{i}", "ANSI31", 1.0, _REASON) for i in range(12))

    assert degraded_hatches_message(one).startswith("Se rellenó 1 HATCH")
    assert degraded_hatches_message(many).startswith("Se rellenaron 12 HATCH")
    assert degraded_hatches_message(many).endswith("… y 2 más.")
    assert mark_degraded_hatches(_detail(1), ()) == _detail(1)
