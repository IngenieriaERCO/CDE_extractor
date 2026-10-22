import ast
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from cde_exporter.cad.detail_model import (
    Bounds3D,
    DetailSource,
    EntityCount,
    EntityDisposition,
    EntityStatus,
    ExternalDependency,
    ExtractedDetail,
    ExtractionWarning,
    ScaleInfo,
    SelectedReference,
    Severity,
    UnitInfo,
    detail_to_dict,
)


def make_detail(*, scale: ScaleInfo | None = None, bounds: Bounds3D | None = None):
    return ExtractedDetail(
        source=DetailSource(path="entrada.dxf", block_name="Detalle"),
        reference=SelectedReference(
            occurrence=2,
            handle="2A",
            layer="A-ARQ",
            insertion_xyz=(10.0, 20.0, 0.0),
            rotation_deg=90.0,
            scale_xyz=(1.0, 2.0, 1.0),
        ),
        units=UnitInfo(
            declared_code=4,
            name="mm",
            status="declaradas en el DXF y coherentes",
            millimeters_per_unit=1.0,
            warnings=("unidad comprobada",),
        ),
        scale=scale,
        bounds=bounds,
        inventory=(EntityCount("LINE", "A-ARQ", EntityStatus.CONSERVED, 3),),
        dispositions=(
            EntityDisposition(0, "30", "LINE", "A-ARQ", EntityStatus.CONSERVED, None),
            EntityDisposition(1, "30", "HATCH", "A-ARQ", EntityStatus.SIMPLIFIED, "modo auto"),
        ),
        dependencies=(ExternalDependency("XREF", "base.dxf", True, False, "no encontrado"),),
        warnings=(ExtractionWarning("units.verified", Severity.WARNING, "Unidades declaradas"),),
    )


def test_constructs_complete_and_minimal_details() -> None:
    complete = make_detail(
        scale=ScaleInfo(50.0, 0.02, (1.0, 2.0, 1.0)),
        bounds=Bounds3D((0.0, 0.0, 0.0), (100.0, 50.0, 0.0)),
    )
    minimal = make_detail()
    assert complete.scale is not None and complete.bounds is not None
    assert minimal.scale is None and minimal.bounds is None
    assert minimal.reference.occurrence == 2


def test_model_dataclasses_are_frozen() -> None:
    source = DetailSource("entrada.dxf", "Detalle")
    with pytest.raises(FrozenInstanceError):
        source.path = "otro.dxf"


def test_serialization_has_stable_keys_and_json_values() -> None:
    result = detail_to_dict(
        make_detail(
            scale=ScaleInfo(50.0, 0.02, (1.0, 2.0, 1.0)),
            bounds=Bounds3D((0.0, 0.0, 0.0), (100.0, 50.0, 0.0)),
        )
    )
    assert list(result) == [
        "model_version", "source", "reference", "units", "scale", "bounds",
        "inventory", "dispositions", "dependencies", "warnings", "extracted_at",
        "exporter_version", "block_definition_handle", "selection_method",
        "user_units_override", "detail_size", "output_factor_mm_per_unit",
        "content_size_mm", "page_size_mm", "margin_mm", "physical_scale_guaranteed",
        "physical_scale_note", "transformations", "nested_blocks", "fonts", "hatches",
        "result",
    ]
    assert result["model_version"] == "2"
    assert result["inventory"][0]["status"] == "conserved"
    assert result["warnings"][0]["severity"] == "warning"
    assert result["reference"]["insertion_xyz"] == [10.0, 20.0, 0.0]
    assert result["source"]["path"] == "entrada.dxf"
    json.dumps(result)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: SelectedReference(1, None, "0", (float("nan"), 0.0, 0.0), 0.0, (1.0, 1.0, 1.0)),
        lambda: SelectedReference(1, None, "0", (0.0, 0.0, 0.0), 0.0, (1.0, float("inf"), 1.0)),
        lambda: ScaleInfo(50.0, float("inf"), None),
        lambda: ScaleInfo(float("nan"), 0.02, None),
        lambda: Bounds3D((0.0, 0.0, 0.0), (1.0, float("inf"), 1.0)),
    ],
)
def test_non_finite_coordinates_and_scale_values_are_rejected(factory) -> None:
    with pytest.raises(ValueError, match="finito|NaN|infinit"):
        factory()


def test_bounds_reject_minimum_greater_than_maximum() -> None:
    with pytest.raises(ValueError, match="min_xyz"):
        Bounds3D((1.0, 0.0, 0.0), (0.0, 1.0, 1.0))


def test_invalid_entity_status_and_warning_severity_are_rejected() -> None:
    with pytest.raises(ValueError, match="status.*conserved"):
        EntityCount("LINE", "0", "unknown", 1)
    with pytest.raises(ValueError, match="severity.*warning"):
        ExtractionWarning("bad", "fatal", "Texto")
    with pytest.raises(ValueError):
        EntityStatus("unknown")
    with pytest.raises(ValueError):
        Severity("fatal")


def test_model_module_imports_only_standard_library_modules() -> None:
    source = Path(__file__).parents[1] / "src/cde_exporter/cad/detail_model.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imported <= {"dataclasses", "enum", "math", "__future__"}
    assert not imported & {"ezdxf", "pymupdf", "svg", "pdf"}
