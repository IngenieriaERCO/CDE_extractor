import json
from dataclasses import FrozenInstanceError

import pytest

from cde_exporter.cad.detail_model import (
    Bounds3D,
    DetailSource,
    EntityCount,
    EntityDisposition,
    EntityStatus,
    ExtractedDetail,
    ExtractionResult,
    ExtractionWarning,
    FontUse,
    HatchDetail,
    HatchSummary,
    NestedBlockInfo,
    ResultStatus,
    SelectedReference,
    SelectionMethod,
    Severity,
    TransformationStep,
    UnitInfo,
    ValidationCheck,
    detail_to_dict,
    finalize_detail,
)


def make_detail(**extra) -> ExtractedDetail:
    return ExtractedDetail(
        source=DetailSource("entrada.dxf", "Detalle"),
        reference=SelectedReference(1, "2A", "A-ARQ", (0.0, 0.0, 0.0), 0.0, (1.0, 1.0, 1.0)),
        units=UnitInfo(4, "mm", "declaradas", 1.0, ()),
        scale=None,
        bounds=Bounds3D((0.0, 0.0, 0.0), (10.0, 5.0, 0.0)),
        inventory=(EntityCount("LINE", "A-ARQ", EntityStatus.CONSERVED, 1),),
        dispositions=(EntityDisposition(0, "30", "LINE", "A-ARQ", EntityStatus.CONSERVED, None),),
        dependencies=(),
        warnings=(ExtractionWarning("ok", Severity.WARNING, "Correcto"),),
        **extra,
    )


def test_minimal_detail_serializes_all_version_2_keys_with_empty_defaults() -> None:
    detail = make_detail()
    assert detail.extracted_at is None
    assert detail.result is None
    serialized = detail_to_dict(detail)
    assert list(serialized) == [
        "model_version", "source", "reference", "units", "scale", "bounds", "inventory",
        "dispositions", "dependencies", "warnings", "extracted_at", "exporter_version",
        "block_definition_handle", "selection_method", "user_units_override", "detail_size",
        "output_factor_mm_per_unit", "content_size_mm", "page_size_mm", "margin_mm",
        "physical_scale_guaranteed", "physical_scale_note", "transformations", "nested_blocks",
        "fonts", "hatches", "result", "hidden_layers", "included_hidden_layers",
        "modelspace_hidden_overlap", "modelspace_selection",
    ]
    assert serialized["model_version"] == "2"
    assert serialized["extracted_at"] is None
    assert serialized["selection_method"] is None
    assert serialized["transformations"] == []
    assert serialized["nested_blocks"] == []
    assert serialized["fonts"] == []
    assert serialized["hatches"] is None
    assert serialized["result"] is None


def test_detail_accepts_all_new_fields() -> None:
    detail = make_detail(
        extracted_at="2026-10-22T12:00:00+00:00",
        exporter_version="1.2.3",
        block_definition_handle="AB",
        selection_method=SelectionMethod.HANDLE,
        user_units_override="mm",
        detail_size=(10.0, 5.0),
        output_factor_mm_per_unit=1.0,
        content_size_mm=(10.0, 5.0),
        page_size_mm=(20.0, 15.0),
        margin_mm=2.5,
        physical_scale_guaranteed=True,
        physical_scale_note="Escala comprobada",
        transformations=(),
        nested_blocks=(),
        fonts=(),
        hatches=None,
        result=None,
    )
    with pytest.raises(FrozenInstanceError):
        detail.extracted_at = "otro"
    assert detail.detail_size == (10.0, 5.0)


@pytest.mark.parametrize(
    "fields",
    [
        {"detail_size": (-1.0, 1.0)},
        {"detail_size": (float("nan"), 1.0)},
        {"content_size_mm": (1.0, float("inf"))},
        {"page_size_mm": (-1.0, 2.0)},
        {"margin_mm": -0.1},
        {"margin_mm": float("nan")},
        {"output_factor_mm_per_unit": 0.0},
        {"output_factor_mm_per_unit": float("inf")},
        {"selection_method": "handle"},
    ],
)
def test_invalid_new_detail_values_are_rejected(fields) -> None:
    with pytest.raises(ValueError):
        make_detail(**fields)


def test_finalize_detail_returns_copy_and_rejects_second_finalization() -> None:
    detail = make_detail()
    result = ExtractionResult("detalle.pdf", ResultStatus.COMPLETE)
    finalized = finalize_detail(detail, result)
    assert detail.result is None
    assert finalized is not detail
    assert finalized.result is result
    assert detail_to_dict(finalized)["result"] == {
        "pdf_name": "detalle.pdf",
        "status": "complete",
        "validations": [],
        "warnings_after_export": [],
    }
    with pytest.raises(ValueError, match="finalizado"):
        finalize_detail(finalized, result)


def test_all_new_fields_serialize_as_json_with_nested_dataclasses_and_enums() -> None:
    detail = make_detail(
        extracted_at="2026-10-22T12:00:00+00:00",
        exporter_version="1.2.3",
        block_definition_handle="AB",
        selection_method=SelectionMethod.HANDLE,
        user_units_override="mm",
        detail_size=(10.0, 5.0),
        output_factor_mm_per_unit=1.0,
        content_size_mm=(10.0, 5.0),
        page_size_mm=(20.0, 15.0),
        margin_mm=2.5,
        physical_scale_guaranteed=True,
        physical_scale_note="Escala comprobada",
        transformations=(TransformationStep("scale", "Escala aplicada", (2.0,)),),
        nested_blocks=(NestedBlockInfo("Puerta", 2, 3),),
        fonts=(FontUse("Arial", "Liberation Sans", True, 4),),
        hatches=HatchSummary(
            1, 1, (HatchDetail("A-ARQ", "ANSI31", 1.0, "simplified", "densidad"),)
        ),
        result=ExtractionResult(
            "detalle.pdf", ResultStatus.COMPLETE_WITH_WARNINGS,
            (ValidationCheck("pdf.exists", True, "creado"),), ("revisar",),
        ),
    )
    serialized = detail_to_dict(detail)
    json.dumps(serialized)
    assert serialized["selection_method"] == "handle"
    assert serialized["transformations"] == [
        {"kind": "scale", "description": "Escala aplicada", "values": [2.0]}
    ]
    assert serialized["hatches"]["details"][0]["pattern"] == "ANSI31"
    assert serialized["result"]["status"] == "complete_with_warnings"
    assert serialized["result"]["validations"] == [
        {"name": "pdf.exists", "passed": True, "detail": "creado"}
    ]


def test_nan_in_new_field_is_rejected() -> None:
    with pytest.raises(ValueError):
        make_detail(detail_size=(float("nan"), 1.0))
