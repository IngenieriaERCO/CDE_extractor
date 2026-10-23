from pathlib import Path

import pymupdf
import pytest

from cde_exporter.cad.detail_model import (
    ExtractionWarning,
    ResultStatus,
    Severity,
    ValidationCheck,
)
from cde_exporter.cad.pdf_checks import (
    PdfValidationError,
    build_extraction_result,
    check_page_size,
    check_pdf_readable,
    check_single_page,
    validate_output_pdf,
)

PAGE_MM = (416.98, 119.40)


def _make_pdf(path: Path, sizes_mm: tuple[tuple[float, float], ...]) -> Path:
    document = pymupdf.open()
    for width_mm, height_mm in sizes_mm:
        document.new_page(width=width_mm * 72 / 25.4, height=height_mm * 72 / 25.4)
    document.save(path)
    document.close()
    return path


def test_readable_single_page_and_expected_size_pass(tmp_path: Path) -> None:
    pdf = _make_pdf(tmp_path / "una-pagina.pdf", (PAGE_MM,))

    assert check_pdf_readable(pdf).passed
    assert check_single_page(pdf).passed
    size_check = check_page_size(pdf, PAGE_MM)
    assert size_check.passed
    assert "416.980 × 119.400 mm" in size_check.detail


def test_single_page_fails_for_two_page_pdf(tmp_path: Path) -> None:
    pdf = _make_pdf(tmp_path / "dos-paginas.pdf", (PAGE_MM, PAGE_MM))

    check = check_single_page(pdf)

    assert not check.passed
    assert "contiene 2 páginas" in check.detail


@pytest.mark.parametrize("missing", [False, True])
def test_unreadable_pdf_fails_and_skips_other_checks(tmp_path: Path, missing: bool) -> None:
    pdf = tmp_path / "no-es-pdf.pdf"
    if not missing:
        pdf.write_bytes(b"esto no es un PDF")
    readable = check_pdf_readable(pdf)
    checks = validate_output_pdf(pdf, PAGE_MM)

    assert not readable.passed
    assert [check.name for check in checks] == [
        "PDF legible",
        "Una sola página",
        "Tamaño de página coincide con la escala",
    ]
    assert all(not check.passed for check in checks)
    assert "no es legible" in checks[1].detail
    assert "no es legible" in checks[2].detail


def test_page_size_tolerance_passes_and_outside_tolerance_fails(tmp_path: Path) -> None:
    inside = _make_pdf(tmp_path / "dentro.pdf", ((PAGE_MM[0] + 0.05, PAGE_MM[1]),))
    outside = _make_pdf(tmp_path / "fuera.pdf", ((PAGE_MM[0] + 0.2, PAGE_MM[1]),))

    assert check_page_size(inside, PAGE_MM).passed
    failed = check_page_size(outside, PAGE_MM)

    assert not failed.passed
    assert "417.180 × 119.400 mm" in failed.detail
    assert "416.980 × 119.400 mm" in failed.detail


def test_validate_output_without_expected_size_omits_size_check(tmp_path: Path) -> None:
    pdf = _make_pdf(tmp_path / "una-pagina.pdf", (PAGE_MM,))

    checks = validate_output_pdf(pdf, None)

    assert [check.name for check in checks] == ["PDF legible", "Una sola página"]
    assert all(check.passed for check in checks)


def test_validate_output_checks_page_count_and_size_in_order(tmp_path: Path) -> None:
    one_page = _make_pdf(tmp_path / "bien.pdf", (PAGE_MM,))
    two_pages = _make_pdf(tmp_path / "dos.pdf", (PAGE_MM, PAGE_MM))

    good_checks = validate_output_pdf(one_page, PAGE_MM)
    two_page_checks = validate_output_pdf(two_pages, PAGE_MM)

    assert [check.name for check in good_checks] == [
        "PDF legible",
        "Una sola página",
        "Tamaño de página coincide con la escala",
    ]
    assert all(check.passed for check in good_checks)
    assert two_page_checks[0].passed
    assert not two_page_checks[1].passed
    assert two_page_checks[2].passed


def test_build_extraction_result_status_and_failed_checks() -> None:
    checks = (ValidationCheck("PDF legible", True, "Correcto."),)
    complete = build_extraction_result("detalle.pdf", checks, (), ())
    warning = ExtractionWarning("UNITS_UNVERIFIED", Severity.WARNING, "Unidades sin verificar.")
    with_warnings = build_extraction_result("detalle.pdf", checks, (warning,), ("Aviso PDF.",))

    assert complete.status is ResultStatus.COMPLETE
    assert complete.validations == checks
    assert complete.warnings_after_export == ()
    assert with_warnings.status is ResultStatus.COMPLETE_WITH_WARNINGS
    assert with_warnings.warnings_after_export == ("Aviso PDF.",)

    failed = ValidationCheck("Una sola página", False, "Se encontraron dos páginas.")
    with pytest.raises(PdfValidationError) as error:
        build_extraction_result("detalle.pdf", (*checks, failed), (), ())
    assert error.value.checks == (failed,)
