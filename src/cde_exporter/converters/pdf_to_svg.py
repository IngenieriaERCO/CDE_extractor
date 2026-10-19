"""Conversión de páginas PDF a SVG mediante las primitivas vectoriales de PyMuPDF.

Las fuentes pueden convertirse a trazados o sustituirse según la disponibilidad local;
transparencias y detalles complejos pueden variar. Las imágenes incrustadas se conservan
como imágenes dentro del SVG, no se rasteriza deliberadamente la página completa. La
fidelidad depende del PDF de origen y no garantiza la preservación semántica de cada elemento.
"""

from pathlib import Path

import pymupdf

from cde_exporter.validators.pdf_validator import PDFValidationError, open_valid_pdf


class PDFConversionError(RuntimeError):
    """No se pudo convertir o escribir la página solicitada."""


def convert_page_to_svg(
    pdf_path: str | Path,
    page_index: int,
    output_path: str | Path,
) -> Path:
    """Exporta una página a SVG; ``page_index`` comienza en cero."""
    source = Path(pdf_path)
    destination = Path(output_path)

    try:
        if source.resolve() == destination.resolve():
            raise PDFConversionError("La salida no puede sobrescribir el PDF original")
    except OSError as error:
        raise PDFConversionError(f"No se pudieron resolver las rutas: {error}") from error

    try:
        with open_valid_pdf(source) as document:
            if page_index < 0 or page_index >= document.page_count:
                raise PDFConversionError(
                    f"Índice de página fuera de rango: {page_index}; "
                    f"el PDF tiene {document.page_count} páginas (índice desde cero)"
                )
            svg = document.load_page(page_index).get_svg_image(text_as_path=False)
    except PDFValidationError as error:
        raise PDFConversionError(str(error)) from error
    except (pymupdf.FileDataError, OSError, RuntimeError, ValueError) as error:
        raise PDFConversionError(f"Falló la conversión del PDF '{source}': {error}") from error

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(svg, encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise PDFConversionError(f"No se pudo escribir el SVG '{destination}': {error}") from error

    return destination
