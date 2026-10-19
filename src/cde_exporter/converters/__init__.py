"""Conversores de documentos a formatos vectoriales."""

from cde_exporter.converters.pdf_to_svg import PDFConversionError, convert_page_to_svg

__all__ = ["PDFConversionError", "convert_page_to_svg"]
