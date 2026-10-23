"""Validaciones del PDF producido para el resultado de una extracción."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from cde_exporter.cad.detail_model import (
    ExtractionResult,
    ExtractionWarning,
    ResultStatus,
    ValidationCheck,
)
from cde_exporter.validators.pdf_validator import validate_pdf

_READABLE = "PDF legible"
_SINGLE_PAGE = "Una sola página"
_PAGE_SIZE = "Tamaño de página coincide con la escala"
_MM_PER_POINT = 25.4 / 72


class PdfValidationError(Exception):
    """La extracción no se puede publicar porque fallaron validaciones del PDF."""

    def __init__(self, checks: tuple[ValidationCheck, ...]) -> None:
        self.checks = tuple(check for check in checks if not check.passed)
        names = ", ".join(check.name for check in self.checks)
        super().__init__(f"Fallaron las validaciones del PDF: {names}.")


def check_pdf_readable(path: str | Path) -> ValidationCheck:
    result = validate_pdf(path)
    if result.valid:
        return ValidationCheck(
            _READABLE,
            True,
            "El PDF existe, se abre y contiene al menos una página.",
        )
    return ValidationCheck(_READABLE, False, result.error)


def check_single_page(path: str | Path) -> ValidationCheck:
    result = validate_pdf(path)
    if not result.valid:
        return ValidationCheck(
            _SINGLE_PAGE,
            False,
            f"No se pudo comprobar el número de páginas: {result.error}",
        )
    if result.page_count == 1:
        return ValidationCheck(_SINGLE_PAGE, True, "El PDF contiene exactamente una página.")
    return ValidationCheck(
        _SINGLE_PAGE,
        False,
        f"El PDF contiene {result.page_count} páginas; se esperaba exactamente una.",
    )


def check_page_size(
    path: str | Path,
    expected_mm: tuple[float, float],
    tolerance_mm: float = 0.1,
) -> ValidationCheck:
    with pymupdf.open(path) as document:
        page = document[0]
        actual_width = float(page.rect.width) * _MM_PER_POINT
        actual_height = float(page.rect.height) * _MM_PER_POINT
    expected_width, expected_height = expected_mm
    passed = (
        abs(actual_width - expected_width) < tolerance_mm
        and abs(actual_height - expected_height) < tolerance_mm
    )
    detail = (
        f"Tamaño medido: {actual_width:.3f} × {actual_height:.3f} mm; "
        f"tamaño esperado: {expected_width:.3f} × {expected_height:.3f} mm "
        f"(tolerancia: {tolerance_mm:.3f} mm)."
    )
    return ValidationCheck(_PAGE_SIZE, passed, detail)


def validate_output_pdf(
    path: str | Path,
    expected_page_mm: tuple[float, float] | None,
) -> tuple[ValidationCheck, ...]:
    readable = check_pdf_readable(path)
    checks = [readable]
    if not readable.passed:
        checks.append(
            ValidationCheck(
                _SINGLE_PAGE,
                False,
                "Comprobación no ejecutada porque el PDF no es legible.",
            )
        )
        if expected_page_mm is not None:
            checks.append(
                ValidationCheck(
                    _PAGE_SIZE,
                    False,
                    "Comprobación no ejecutada porque el PDF no es legible.",
                )
            )
        return tuple(checks)

    checks.append(check_single_page(path))
    if expected_page_mm is not None:
        checks.append(check_page_size(path, expected_page_mm))
    return tuple(checks)


def build_extraction_result(
    pdf_name: str,
    checks: tuple[ValidationCheck, ...],
    detail_warnings: tuple[ExtractionWarning, ...],
    export_warnings: tuple[str, ...],
) -> ExtractionResult:
    failed = tuple(check for check in checks if not check.passed)
    if failed:
        raise PdfValidationError(failed)
    status = (
        ResultStatus.COMPLETE_WITH_WARNINGS
        if detail_warnings or export_warnings
        else ResultStatus.COMPLETE
    )
    return ExtractionResult(
        pdf_name=pdf_name,
        status=status,
        validations=checks,
        warnings_after_export=export_warnings,
    )
