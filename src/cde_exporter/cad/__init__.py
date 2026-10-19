"""Lectura, selección, extracción y exportación local de dibujos DXF."""

from cde_exporter.cad.exporters import ExportReport, export_vector_outputs
from cde_exporter.cad.extractor import ExtractionReport, extract_dxf_region
from cde_exporter.cad.models import CadProcessingError, CadWindow, DxfInspection
from cde_exporter.cad.reader import diagnose_unbounded_entities, inspect_dxf, read_dxf
from cde_exporter.cad.selection import select_by_window

__all__ = [
    "CadProcessingError",
    "CadWindow",
    "DxfInspection",
    "ExportReport",
    "ExtractionReport",
    "export_vector_outputs",
    "diagnose_unbounded_entities",
    "extract_dxf_region",
    "inspect_dxf",
    "read_dxf",
    "select_by_window",
]
