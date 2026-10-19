import pymupdf
import pytest

from cde_exporter.converters.pdf_to_svg import PDFConversionError, convert_page_to_svg


def create_pdf(path) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "ERCO vector export")
    document.save(path)
    document.close()
    return path.read_bytes()


def test_export_page_as_svg_and_create_parent_directory(tmp_path) -> None:
    source = tmp_path / "source.pdf"
    original_bytes = create_pdf(source)
    destination = tmp_path / "nested" / "page.svg"

    result = convert_page_to_svg(source, 0, destination)

    assert result == destination
    assert "<svg" in destination.read_text(encoding="utf-8")
    assert source.read_bytes() == original_bytes


def test_reject_page_index_out_of_range(tmp_path) -> None:
    source = tmp_path / "source.pdf"
    create_pdf(source)

    with pytest.raises(PDFConversionError, match="desde cero"):
        convert_page_to_svg(source, 1, tmp_path / "page.svg")


def test_reject_output_that_would_overwrite_source(tmp_path) -> None:
    source = tmp_path / "source.pdf"
    original_bytes = create_pdf(source)

    with pytest.raises(PDFConversionError, match="sobrescribir"):
        convert_page_to_svg(source, 0, source)

    assert source.read_bytes() == original_bytes
