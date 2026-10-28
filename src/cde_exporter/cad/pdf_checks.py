"""Validaciones del PDF producido para el resultado de una extracción."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from cde_exporter.cad.detail_model import (
    ADVISORY_CHECKS,
    DRAWING_SCALE_CHECK,
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
# Mayor diferencia admitida, en mm de papel, entre la longitud medida del dibujo y la que
# corresponde a su escala (en cada eje, sobre todo el dibujo).
DRAWING_SCALE_TOLERANCE_MM = 0.01


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


def _drawn_extent_pt(page: pymupdf.Page) -> tuple[float, float] | None:
    """Ancho y alto (pt) de lo dibujado: todos los puntos de los trazos, sin el fondo.

    Se cuentan también los puntos de control de las curvas, como hace ezdxf al medir lo
    que graba; el fondo es el rectángulo relleno del tamaño de la página.
    """
    xs: list[float] = []
    ys: list[float] = []
    page_rect = page.rect
    for drawing in page.get_drawings():
        is_page_sized = all(
            abs(side - page_side) < 0.5
            for side, page_side in zip(drawing["rect"], page_rect, strict=True)
        )
        if drawing.get("fill") is not None and is_page_sized:
            continue
        for item in drawing["items"]:
            for value in item[1:]:
                if isinstance(value, pymupdf.Point):
                    points = (value,)
                elif isinstance(value, pymupdf.Rect):
                    points = (value.tl, value.br)
                elif isinstance(value, pymupdf.Quad):
                    points = (value.ul, value.ur, value.ll, value.lr)
                else:
                    continue
                xs += [point.x for point in points]
                ys += [point.y for point in points]
    if not xs:
        return None
    return max(xs) - min(xs), max(ys) - min(ys)


def check_drawing_scale(
    path: str | Path,
    expected_mm_per_unit: float,
    drawn_size: tuple[float, float],
    denominator: float | None = None,
) -> ValidationCheck:
    """Mide en el PDF la escala real del dibujo y la compara con la esperada.

    ``drawn_size`` es el ancho y alto de lo dibujado en unidades CAD (la caja que mide
    ezdxf con los mismos puntos). En cada eje, la diferencia entre lo medido y lo esperado
    sobre todo el dibujo no puede superar ``DRAWING_SCALE_TOLERANCE_MM``.
    """
    with pymupdf.open(path) as document:
        extent = _drawn_extent_pt(document[0])
    if extent is None:
        return ValidationCheck(
            DRAWING_SCALE_CHECK, False, "No se pudo medir: el PDF no tiene trazos."
        )
    errors: list[float] = []
    measured: list[str] = []
    for axis, size_pt, cad_size in zip(("X", "Y"), extent, drawn_size, strict=True):
        if cad_size <= 0:
            continue
        measured_mm = size_pt * _MM_PER_POINT
        expected_mm = cad_size * expected_mm_per_unit
        errors.append(measured_mm - expected_mm)
        factor = measured_mm / cad_size
        if denominator is not None:
            measured.append(f"1:{denominator * expected_mm_per_unit / factor:.4f} ({axis})")
        else:
            measured.append(f"{factor:.6g} mm por unidad CAD ({axis})")
    if not errors:
        return ValidationCheck(
            DRAWING_SCALE_CHECK, False, "No se pudo medir: el dibujo no tiene ancho ni alto."
        )
    expected = (
        f"1:{denominator:g}"
        if denominator is not None
        else f"{expected_mm_per_unit:.6g} mm por unidad CAD"
    )
    worst = max(errors, key=abs)
    passed = abs(worst) <= DRAWING_SCALE_TOLERANCE_MM
    longest_mm = max(drawn_size) * expected_mm_per_unit
    detail = (
        f"Escala medida en el PDF: {' y '.join(measured)}; esperada {expected}. Mayor "
        f"diferencia de longitud: {worst:+.4f} mm sobre {longest_mm:.3f} mm de dibujo "
        f"(tolerancia: {DRAWING_SCALE_TOLERANCE_MM:.3f} mm)."
    )
    return ValidationCheck(DRAWING_SCALE_CHECK, passed, detail)


# Aviso que genera cada comprobación de aviso (``ADVISORY_CHECKS``) cuando no pasa.
_ADVISORY_WARNINGS = {
    SEARCHABLE_TEXT_CHECK: "TEXT_NOT_SEARCHABLE",
    DRAWING_SCALE_CHECK: "DRAWING_SCALE_MISMATCH",
}


def advisory_warnings(checks: tuple[ValidationCheck, ...]) -> tuple[ExtractionWarning, ...]:
    """Un aviso por cada comprobación que solo avisa y no pasó (no impide publicar)."""
    return tuple(
        ExtractionWarning(_ADVISORY_WARNINGS[check.name], Severity.WARNING, check.detail or "")
        for check in checks
        if check.name in _ADVISORY_WARNINGS and not check.passed
    )


def validate_output_pdf(
    path: str | Path,
    expected_page_mm: tuple[float, float] | None,
    searchable_lines: tuple[str, ...] = (),
    drawing_scale: tuple[float, tuple[float, float], float | None] | None = None,
) -> tuple[ValidationCheck, ...]:
    """Valida el PDF.

    Con ``searchable_lines`` (capa de texto activa) comprueba la búsqueda; con
    ``drawing_scale`` (mm por unidad CAD esperados, tamaño dibujado en unidades CAD y
    denominador nominal) mide la escala real del dibujo.
    """
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
    if drawing_scale is not None:
        checks.append(check_drawing_scale(path, *drawing_scale))
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
