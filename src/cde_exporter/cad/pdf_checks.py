"""Validaciones del PDF producido para el resultado de una extracción."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from cde_exporter.cad.detail_model import (
    ADVISORY_CHECKS,
    SEARCHABLE_TEXT_CHECK,
    ExtractionResult,
    ExtractionWarning,
    ResultStatus,
    Severity,
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


def check_searchable_text(path: str | Path, lines: tuple[str, ...]) -> ValidationCheck:
    """Cuenta cuántas líneas de la capa de texto se encuentran al buscarlas en el PDF.

    ``lines`` son las líneas que ezdxf dibujó: un texto vacío no se dibuja ni se cuenta.
    Es una comprobación de aviso (``ADVISORY_CHECKS``): si falta alguna línea, el PDF se
    publica igual y ``searchable_text_warnings`` la convierte en advertencia.
    """
    with pymupdf.open(path) as document:
        page = document[0]
        textpage = page.get_textpage()
        found_by_text = {
            line: bool(page.search_for(line, textpage=textpage)) for line in set(lines)
        }
    missing = [line for line in lines if not found_by_text[line]]
    if not missing:
        total = len(lines)
        detail = f"{total} de {total} líneas de texto se encuentran al buscarlas en el PDF."
        return ValidationCheck(SEARCHABLE_TEXT_CHECK, True, detail)
    names = ", ".join(repr(line) for line in missing[:10])
    names += f" y {len(missing) - 10} más" if len(missing) > 10 else ""
    detail = (
        f"Faltan {len(missing)} de {len(lines)} líneas de texto al buscarlas en el PDF: {names}. "
        "Se ven en el dibujo, pero no se pueden buscar ni copiar."
    )
    return ValidationCheck(SEARCHABLE_TEXT_CHECK, False, detail)


def searchable_text_warnings(
    checks: tuple[ValidationCheck, ...],
) -> tuple[ExtractionWarning, ...]:
    """Aviso ``TEXT_NOT_SEARCHABLE`` por cada comprobación de texto buscable que no pasó."""
    return tuple(
        ExtractionWarning("TEXT_NOT_SEARCHABLE", Severity.WARNING, check.detail or "")
        for check in checks
        if check.name == SEARCHABLE_TEXT_CHECK and not check.passed
    )


def validate_output_pdf(
    path: str | Path,
    expected_page_mm: tuple[float, float] | None,
    searchable_lines: tuple[str, ...] = (),
) -> tuple[ValidationCheck, ...]:
    """Valida el PDF; con ``searchable_lines`` (capa de texto activa) comprueba la búsqueda."""
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
    if searchable_lines:
        checks.append(check_searchable_text(path, searchable_lines))
    return tuple(checks)


def build_extraction_result(
    pdf_name: str,
    checks: tuple[ValidationCheck, ...],
    detail_warnings: tuple[ExtractionWarning, ...],
    export_warnings: tuple[str, ...],
) -> ExtractionResult:
    failed = tuple(check for check in checks if not check.passed)
    blocking = tuple(check for check in failed if check.name not in ADVISORY_CHECKS)
    if blocking:
        raise PdfValidationError(blocking)
    status = (
        ResultStatus.COMPLETE_WITH_WARNINGS
        if detail_warnings or export_warnings or failed
        else ResultStatus.COMPLETE
    )
    return ExtractionResult(
        pdf_name=pdf_name,
        status=status,
        validations=checks,
        warnings_after_export=export_warnings,
    )
