from __future__ import annotations

import ezdxf

from cde_exporter.cad.detail_extras import (
    font_uses,
    hatch_summary,
    nested_blocks,
    transformation_steps,
)
from cde_exporter.cad.detail_model import SelectedReference
from cde_exporter.cad.fidelity import FidelityReport


def _report(*, fonts=None, hatches=None, simplified=0, simplifications=()):
    return FidelityReport(
        entity_counts={},
        unsupported_counts={},
        fonts=fonts or {},
        hatches=hatches or {},
        hatch_mode="auto",
        simplified_hatches=simplified,
        lineweight_policy="DEFAULT",
        hatch_simplifications=simplifications,
    )


def test_transformation_steps_indicate_uniform_and_non_uniform_scale():
    reference = SelectedReference(1, "A1", "0", (3, 4, 5), 45, (1, 1, 1))
    steps = transformation_steps(reference)
    assert [step.kind for step in steps] == ["insertion", "rotation", "scale"]
    assert steps[0].values == (3, 4, 5)
    assert steps[1].values == (45,)
    assert steps[2].values == (1, 1, 1)
    assert "uniforme" in steps[2].description

    steps = transformation_steps(SelectedReference(1, None, "0", (0, 0, 0), 0, (2, 1, 1)))
    assert "no uniforme" in steps[2].description


def test_font_uses_preserve_report_counts_and_sort_requested_name():
    report = _report(
        fonts={
            ("Standard", "Zulu", "z.ttf", True): 1,
            ("Alt", "Arial", "Liberation.ttf", False): 2,
            ("Standard", "Arial", "Arial.ttf", True): 3,
        }
    )
    uses = font_uses(report)
    assert [(item.requested, item.resolved, item.found, item.uses) for item in uses] == [
        ("Arial", "Arial.ttf", True, 3),
        ("Arial", "Liberation.ttf", False, 2),
        ("Zulu", "z.ttf", True, 1),
    ]


def test_hatch_summary_reports_simplified_and_preserved_hatches():
    report = _report(
        hatches={("patrón", "ANSI35", "0.5"): 2},
        simplified=1,
        simplifications=(("A-ARQ", "ANSI35", "0.5", 1200),),
    )
    summary = hatch_summary(report)
    assert summary is not None
    assert summary.total == 2
    assert summary.simplified == 1
    assert summary.details[0].layer == "A-ARQ"
    assert summary.details[0].pattern == "ANSI35"
    assert summary.details[0].scale == 0.5
    assert summary.details[0].action == "simplified"
    assert "1200" in summary.details[0].reason
    assert hatch_summary(_report()) is None


def test_nested_blocks_depth_and_reference_count():
    document = ezdxf.new()
    document.blocks.new("TOP")
    document.blocks.new("ONE")
    document.blocks.new("TWO")
    document.blocks.new("THREE")
    document.blocks["TOP"].add_blockref("ONE", (0, 0))
    document.blocks["ONE"].add_blockref("TWO", (0, 0))
    document.blocks["TWO"].add_blockref("THREE", (0, 0))
    document.modelspace().add_blockref("TOP", (0, 0))

    info = nested_blocks(document)
    assert [(item.name, item.max_depth, item.reference_count) for item in info] == [
        ("ONE", 1, 1),
        ("THREE", 3, 1),
        ("TWO", 2, 1),
    ]


def test_nested_blocks_cycle_is_safe_and_empty_detail_is_empty():
    document = ezdxf.new()
    document.blocks.new("A").add_blockref("B", (0, 0))
    document.blocks.new("B").add_blockref("A", (0, 0))
    document.modelspace().add_blockref("A", (0, 0))
    actual = [(item.name, item.max_depth, item.reference_count) for item in nested_blocks(document)]
    assert actual == [
        ("A", 2, 1),
        ("B", 1, 1),
    ]

    empty = ezdxf.new()
    empty.blocks.new("EMPTY")
    empty.modelspace().add_blockref("EMPTY", (0, 0))
    assert nested_blocks(empty) == ()
