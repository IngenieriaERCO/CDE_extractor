"""Validación y apertura segura de documentos PDF."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pymupdf


class PDFValidationError(ValueError):
    """El archivo no es un PDF legible y con al menos una página."""


@dataclass(frozen=True, slots=True)
class PdfValidationResult:
    path: Path
    valid: bool
    page_count: int | None
    error: str | None = None


@contextmanager
def open_valid_pdf(path: str | Path) -> Iterator[pymupdf.Document]:
    """Abre un PDF legible y cierra el documento al salir del contexto."""
    source = Path(path)
    if not source.exists():
        raise PDFValidationError(f"No existe el archivo: {source}")
    if not source.is_file():
        raise PDFValidationError(f"La ruta no es un archivo: {source}")

    try:
        document = pymupdf.open(source)
    except (pymupdf.FileDataError, OSError, RuntimeError, ValueError) as error:
        raise PDFValidationError(f"No se pudo abrir el PDF '{source}': {error}") from error

    try:
        if document.needs_pass:
            raise PDFValidationError(f"El PDF '{source}' está protegido por contraseña")
        if document.page_count < 1:
            raise PDFValidationError(f"El PDF '{source}' no contiene páginas")
        yield document
    finally:
        document.close()


def validate_pdf(path: str | Path) -> PdfValidationResult:
    """Comprueba existencia, legibilidad e integridad básica de un PDF."""
    source = Path(path)
    try:
        with open_valid_pdf(source) as document:
            return PdfValidationResult(path=source, valid=True, page_count=document.page_count)
    except PDFValidationError as error:
        return PdfValidationResult(path=source, valid=False, page_count=None, error=str(error))
