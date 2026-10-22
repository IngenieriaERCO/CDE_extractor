import json
from datetime import datetime
from pathlib import Path

import ezdxf
import pytest

from cde_exporter import __version__
from cde_exporter.cad.detail_model import SelectionMethod
from cde_exporter.cli import main


@pytest.fixture
def synthetic_drawing(tmp_path: Path):
    def create(*, unit_code: int = ezdxf.units.InsertUnits.Millimeters, count: int = 1):
        path = tmp_path / f"synthetic-{unit_code}-{count}.dxf"
        drawing = ezdxf.new()
        drawing.units = unit_code
        definition = drawing.blocks.new("Detalle")
        definition.add_line((0, 0), (100, 50))
        references = [
            drawing.modelspace().add_blockref("Detalle", (index * 120, 0))
            for index in range(count)
        ]
        handles = tuple(reference.dxf.handle for reference in references)
        definition_handle = definition.block_record.dxf.handle
        drawing.saveas(path)
        return path, handles, definition_handle

    return create


def _extract(path: Path, output: Path, *options: str) -> list[dict]:
    prefix = output.with_suffix("")
    manifest = output
    assert main([
        "cad", "extract-block", str(path), "--block-name", "Detalle",
        *options, "--output-prefix", str(prefix), "--format", "svg",
        "--detail-json", str(manifest),
    ]) == 0
    manifests = sorted(manifest.parent.glob(f"{manifest.stem}*.json"))
    return [json.loads(item.read_text(encoding="utf-8")) for item in manifests]


def test_profile_with_nominal_scale_fills_sections_a_b_and_c(synthetic_drawing, tmp_path):
    source, _reference_handles, definition_handle = synthetic_drawing()
    detail = _extract(
        source, tmp_path / "scaled.json", "--scale", "1:50", "--margin-mm", "2"
    )[0]

    assert datetime.fromisoformat(detail["extracted_at"]).utcoffset() is not None
    assert detail["exporter_version"] == __version__
    assert detail["block_definition_handle"] == definition_handle
    assert detail["selection_method"] == SelectionMethod.AUTOMATIC.value
    assert detail["user_units_override"] is None
    assert detail["detail_size"] == [100.0, 50.0]
    assert detail["output_factor_mm_per_unit"] == 0.02
    assert detail["content_size_mm"] == [2.0, 1.0]
    assert detail["page_size_mm"] == [6.0, 5.0]
    assert detail["margin_mm"] == 2.0
    assert detail["physical_scale_guaranteed"] is True
    assert detail["physical_scale_note"] is None


def test_without_nominal_scale_keeps_scale_fields_null_and_fills_a_and_b(
    synthetic_drawing, tmp_path
):
    source, _reference_handles, definition_handle = synthetic_drawing()
    detail = _extract(source, tmp_path / "unscaled.json")[0]

    assert detail["extracted_at"]
    assert detail["exporter_version"] == __version__
    assert detail["block_definition_handle"] == definition_handle
    assert detail["selection_method"] == SelectionMethod.AUTOMATIC.value
    assert detail["detail_size"] == [100.0, 50.0]
    assert detail["output_factor_mm_per_unit"] is None
    assert detail["content_size_mm"] is None
    assert detail["page_size_mm"] is None
    assert detail["margin_mm"] is None
    assert detail["physical_scale_guaranteed"] is None
    assert detail["physical_scale_note"] is None


def test_unknown_units_make_physical_scale_unverified_with_reason(synthetic_drawing, tmp_path):
    source, _reference_handles, _definition_handle = synthetic_drawing(unit_code=0)
    detail = _extract(source, tmp_path / "unknown-units.json", "--scale", "1:50")[0]

    assert detail["physical_scale_guaranteed"] is False
    assert detail["physical_scale_note"] == (
        "Unidades sin verificar: desconocidas ($INSUNITS = 0 o sin valor)"
    )


def test_units_override_is_recorded(synthetic_drawing, tmp_path):
    source, _reference_handles, _definition_handle = synthetic_drawing(unit_code=0)
    detail = _extract(
        source, tmp_path / "units-override.json", "--scale", "1:50", "--units", "cm"
    )[0]

    assert detail["user_units_override"] == "cm"
    assert detail["output_factor_mm_per_unit"] == 0.2
    assert detail["physical_scale_guaranteed"] is True


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("automatic", SelectionMethod.AUTOMATIC.value),
        ("occurrence", SelectionMethod.OCCURRENCE.value),
        ("handle", SelectionMethod.HANDLE.value),
        ("all", SelectionMethod.ALL.value),
    ],
)
def test_selection_method_matches_cli_option(synthetic_drawing, tmp_path, mode, expected):
    count = 1 if mode == "automatic" else 2
    source, reference_handles, _definition_handle = synthetic_drawing(count=count)
    if mode == "automatic":
        options = ()
    elif mode == "occurrence":
        options = ("--occurrence", "2")
    elif mode == "handle":
        options = ("--handle", reference_handles[1])
    else:
        options = ("--all",)
    details = _extract(source, tmp_path / f"selection-{mode}.json", *options)

    assert [detail["selection_method"] for detail in details] == (
        [expected, expected] if mode == "all" else [expected]
    )
    if mode == "all":
        assert [detail["reference"]["handle"] for detail in details] == list(reference_handles)
