import pymupdf

from cde_exporter.validators.pdf_validator import validate_pdf


def make_pdf(path, *, with_page: bool = True):
    if not with_page:
        pdf_data = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [] /Count 0 >>",
        ]
        for object_number, body in enumerate(objects, start=1):
            offsets.append(len(pdf_data))
            pdf_data.extend(f"{object_number} 0 obj\n".encode("ascii"))
            pdf_data.extend(body + b"\nendobj\n")
        xref_offset = len(pdf_data)
        pdf_data.extend(b"xref\n0 3\n0000000000 65535 f \n")
        for offset in offsets[1:]:
            pdf_data.extend(f"{offset:010} 00000 n \n".encode("ascii"))
        pdf_data.extend(
            f"trailer\n<< /Size 3 /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
        )
        path.write_bytes(pdf_data)
        return path

    document = pymupdf.open()
    document.new_page()
    document.save(path)
    document.close()
    return path


def test_validate_readable_pdf_with_pages(tmp_path) -> None:
    result = validate_pdf(make_pdf(tmp_path / "valid.pdf"))

    assert result.valid is True
    assert result.page_count == 1
    assert result.error is None


def test_reject_missing_file(tmp_path) -> None:
    result = validate_pdf(tmp_path / "missing.pdf")

    assert result.valid is False
    assert result.page_count is None
    assert "No existe" in result.error


def test_reject_corrupt_file(tmp_path) -> None:
    corrupt_pdf = tmp_path / "corrupt.pdf"
    corrupt_pdf.write_bytes(b"not a PDF")

    result = validate_pdf(corrupt_pdf)

    assert result.valid is False
    assert "No se pudo abrir" in result.error


def test_reject_empty_document(tmp_path) -> None:
    result = validate_pdf(make_pdf(tmp_path / "empty.pdf", with_page=False))

    assert result.valid is False
    assert "no contiene p\u00e1ginas" in result.error
