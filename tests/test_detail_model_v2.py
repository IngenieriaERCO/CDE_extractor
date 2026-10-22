import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from cde_exporter.cad.detail_model import (
    ExtractionResult,
    FontUse,
    HatchDetail,
    HatchSummary,
    NestedBlockInfo,
    ResultStatus,
    SelectionMethod,
    TransformationStep,
    ValidationCheck,
)


def test_new_model_classes_construct_valid_values() -> None:
    assert SelectionMethod.AUTOMATIC == "automatic"
    assert TransformationStep("scale", "Escala aplicada", (2.0,)).values == (2.0,)
    assert NestedBlockInfo("Puerta", 2, 3).reference_count == 3
    assert FontUse("Arial", "Liberation Sans", True, 4).uses == 4
    hatch = HatchDetail("A-ARQ", "ANSI31", 1.0, "preserved", None)
    assert HatchSummary(2, 1, (hatch,)).details == (hatch,)
    check = ValidationCheck("pdf.exists", True)
    result = ExtractionResult("detalle.pdf", ResultStatus.COMPLETE, (check,), ())
    assert result.validations == (check,)


def test_new_model_classes_are_frozen() -> None:
    step = TransformationStep("scale", "Escala")
    with pytest.raises(FrozenInstanceError):
        step.kind = "rotation"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: NestedBlockInfo("B", -1, 0),
        lambda: NestedBlockInfo("B", 0, -1),
        lambda: FontUse("A", "A", True, -1),
        lambda: HatchSummary(-1, 0),
        lambda: HatchSummary(1, -1),
        lambda: HatchSummary(1, 2),
        lambda: HatchDetail("0", "ANSI31", float("nan"), "kept", None),
        lambda: ExtractionResult("x.pdf", "invalid"),
    ],
)
def test_new_model_classes_reject_invalid_values(factory) -> None:
    with pytest.raises(ValueError):
        factory()


def test_detail_model_module_does_not_import_ezdxf() -> None:
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
    assert "ezdxf" not in imported
