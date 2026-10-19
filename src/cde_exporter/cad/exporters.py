"""Exportación vectorial de un DXF extraído a SVG y PDF."""

from __future__ import annotations

import contextlib
import io
import math
import os
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from ezdxf import bbox
from ezdxf.addons.drawing import Frontend, RenderContext, config, layout
from ezdxf.addons.drawing import pymupdf as pdf_backend
from ezdxf.addons.drawing import svg as svg_backend
from ezdxf.addons.drawing.config import HatchPolicy
from ezdxf.addons.drawing.properties import LayoutProperties
from ezdxf.document import Drawing

from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.reader import ensure_layer_table_entries, read_dxf


class _StrictFrontend(Frontend):
    """Registra las entidades que ezdxf omite en el renderizado."""

    def __init__(self, *args, **kwargs) -> None:
        self.skipped: list[str] = []
        self.messages: list[str] = []
        self.auto_hatch_threshold: int | None = None
        super().__init__(*args, **kwargs)

    def skip_entity(self, entity, msg: str) -> None:
        self.skipped.append(f"{entity.dxftype()}: {msg}")

    def log_message(self, message: str) -> None:
        self.messages.append(message)

    def draw_hatch_entity(self, entity, properties, *, loops=None) -> None:
        estimate = _estimate_hatch_segments(entity) if self.auto_hatch_threshold is not None else 0
        if estimate > (self.auto_hatch_threshold or 0):
            original = self.config
            self.config = self.config.with_changes(hatch_policy=HatchPolicy.SHOW_SOLID)
            try:
                super().draw_hatch_entity(entity, properties, loops=loops)
            finally:
                self.config = original
        else:
            super().draw_hatch_entity(entity, properties, loops=loops)


def _estimate_hatch_segments(entity) -> int:
    if entity.dxf.get("solid_fill", 0) or not entity.pattern:
        return 0
    ext = bbox.extents([entity])
    if not ext.has_data:
        return 0
    width = abs(float(ext.extmax.x - ext.extmin.x))
    height = abs(float(ext.extmax.y - ext.extmin.y))
    perimeter = 2.0 * (width + height)
    estimate = 0
    for line in entity.pattern.lines:
        spacing = math.hypot(*line.offset)
        if spacing <= 1e-12:
            continue
        dashes = line.dash_length_items
        period = sum(abs(float(value)) for value in dashes)
        pieces = sum(float(value) > 0 for value in dashes)
        segments_per_line = max(1, math.ceil(perimeter * pieces / period)) if period > 0 else 1
        estimate += math.ceil(perimeter / spacing) * segments_per_line
    return estimate


@dataclass(frozen=True, slots=True)
class ExportReport:
    svg_path: Path
    pdf_path: Path
    svg_element_count: int
    pdf_page_count: int
    pdf_vector_drawing_count: int
    warnings: tuple[str, ...]


def _draw_to_svg(
    document: Drawing,
    page: layout.Page,
    settings: layout.Settings,
    hatch_mode: str = "normal",
    hatch_threshold: int = 50000,
) -> tuple[str, tuple[str, ...]]:
    backend = svg_backend.SVGBackend()
    layout_properties = LayoutProperties.from_layout(document.modelspace())
    layout_properties.set_colors("#ffffff")
    frontend = _StrictFrontend(
        RenderContext(document),
        backend,
        config=config.Configuration(
            background_policy=config.BackgroundPolicy.WHITE,
            hatch_policy=_hatch_policy(hatch_mode),
        ),
    )
    frontend.auto_hatch_threshold = hatch_threshold if hatch_mode == "auto" else None
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        frontend.draw_layout(document.modelspace(), layout_properties=layout_properties)
    if "hatching timeout" in output.getvalue().lower():
        raise CadProcessingError(
            "Falló la etapa de dibujo SVG: ezdxf agotó el tiempo al dibujar un rayado. "
            "Reintenta con --hatch-mode outline."
        )
    if frontend.skipped:
        details = ", ".join(frontend.skipped[:8])
        raise CadProcessingError(
            f"La exportación SVG detuvo la operación porque ezdxf omite entidades: {details}"
        )
    return backend.get_string(page, settings=settings), tuple(frontend.messages)


def _draw_to_pdf(
    document: Drawing,
    page: layout.Page,
    settings: layout.Settings,
    hatch_mode: str = "normal",
    hatch_threshold: int = 50000,
) -> tuple[bytes, tuple[str, ...]]:
    backend = pdf_backend.PyMuPdfBackend()
    layout_properties = LayoutProperties.from_layout(document.modelspace())
    layout_properties.set_colors("#ffffff")
    frontend = _StrictFrontend(
        RenderContext(document),
        backend,
        config=config.Configuration(
            background_policy=config.BackgroundPolicy.WHITE,
            hatch_policy=_hatch_policy(hatch_mode),
        ),
    )
    frontend.auto_hatch_threshold = hatch_threshold if hatch_mode == "auto" else None
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        frontend.draw_layout(document.modelspace(), layout_properties=layout_properties)
    if "hatching timeout" in output.getvalue().lower():
        raise CadProcessingError(
            "Falló la etapa de dibujo PDF: ezdxf agotó el tiempo al dibujar un rayado. "
            "Reintenta con --hatch-mode outline."
        )
    if frontend.skipped:
        details = ", ".join(frontend.skipped[:8])
        raise CadProcessingError(
            f"La exportación PDF detuvo la operación porque ezdxf omite entidades: {details}"
        )
    return backend.get_pdf_bytes(page, settings=settings), tuple(frontend.messages)


def _hatch_policy(mode: str) -> config.HatchPolicy:
    return {
        "normal": HatchPolicy.NORMAL,
        "outline": HatchPolicy.SHOW_OUTLINE,
        "solid": HatchPolicy.SHOW_SOLID,
        "ignore": HatchPolicy.IGNORE,
        "auto": HatchPolicy.NORMAL,
    }[mode]


def _validate_svg(content: str) -> int:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as error:
        raise CadProcessingError(f"El SVG generado no es XML válido: {error}") from error
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise CadProcessingError("El archivo SVG generado no tiene un elemento raíz svg")
    if not root.attrib.get("viewBox"):
        raise CadProcessingError("El SVG generado no define un viewBox")
    return sum(1 for _ in root.iter())


def _validate_pdf(content: bytes) -> tuple[int, int]:
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
    except (pymupdf.FileDataError, RuntimeError, ValueError) as error:
        raise CadProcessingError(f"El PDF generado no se puede volver a abrir: {error}") from error
    try:
        if document.page_count != 1:
            raise CadProcessingError(
                f"El PDF generado tiene {document.page_count} páginas; se esperaba una."
            )
        page = document[0]
        if page.rect.width <= 0 or page.rect.height <= 0:
            raise CadProcessingError("El PDF generado tiene dimensiones de página inválidas")
        return document.page_count, len(page.get_drawings())
    finally:
        document.close()


def _write_atomically(path: Path, content: bytes, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise CadProcessingError(
            f"El archivo de salida ya existe: {path}. Usa --overwrite para reemplazarlo."
        )
    temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp{path.suffix}")
    try:
        temporary.write_bytes(content)
        if overwrite:
            os.replace(temporary, path)
        else:
            temporary.rename(path)
    except OSError as error:
        raise CadProcessingError(f"No se pudo escribir '{path}': {error}") from error
    finally:
        temporary.unlink(missing_ok=True)


def export_vector_outputs(
    source: str | Path,
    svg_path: str | Path,
    pdf_path: str | Path,
    *,
    overwrite: bool = False,
    page_size_mm: tuple[float, float] | None = None,
    margin_mm: float = 5.0,
    scale_factor: float | None = None,
    hatch_mode: str = "normal",
    hatch_threshold: int = 50000,
) -> ExportReport:
    """Crea SVG/PDF vectoriales ajustados o con escala y página explícitas."""
    source_path = Path(source)
    svg_destination = Path(svg_path)
    pdf_destination = Path(pdf_path)
    if svg_destination.resolve() == pdf_destination.resolve():
        raise CadProcessingError("Las rutas SVG y PDF deben ser distintas")
    for destination in (svg_destination, pdf_destination):
        if destination.resolve() == source_path.resolve():
            raise CadProcessingError("La exportación no puede sobrescribir el DXF de origen")
        if destination.exists() and not overwrite:
            raise CadProcessingError(
                f"El archivo de salida ya existe: {destination}. Usa --overwrite para reemplazarlo."
            )

    document = read_dxf(source_path)
    ensure_layer_table_entries(document)
    modelspace = list(document.modelspace())
    if not modelspace:
        raise CadProcessingError("El DXF no contiene entidades para exportar")
    if not math.isfinite(margin_mm) or margin_mm < 0:
        raise CadProcessingError("El margen debe ser un número finito mayor o igual que cero")
    if page_size_mm is not None:
        if len(page_size_mm) != 2 or any(
            not math.isfinite(value) or value <= 0 for value in page_size_mm
        ):
            raise CadProcessingError("El ancho y alto de página deben ser positivos y finitos")
    if scale_factor is not None and (not math.isfinite(scale_factor) or scale_factor <= 0):
        raise CadProcessingError("El factor de escala debe ser positivo y finito")

    width, height = page_size_mm if page_size_mm is not None else (0.0, 0.0)
    page = layout.Page(
        width,
        height,
        layout.Units.mm,
        margins=layout.Margins.all(margin_mm),
    )
    fit_page = scale_factor is None
    settings = layout.Settings(scale=scale_factor or 1.0, fit_page=fit_page)
    if page_size_mm is not None and scale_factor is not None:
        extents = bbox.extents(modelspace)
        if not extents.has_data:
            raise CadProcessingError("No se pudieron calcular extensiones para validar la escala")
        content_width = float(extents.extmax.x - extents.extmin.x) * scale_factor
        content_height = float(extents.extmax.y - extents.extmin.y) * scale_factor
        if (
            content_width + 2 * margin_mm > width + 1e-8
            or content_height + 2 * margin_mm > height + 1e-8
        ):
            raise CadProcessingError(
                "La geometría no cabe en la página con ese factor de escala y margen."
            )
    if hatch_mode not in {"normal", "outline", "solid", "ignore", "auto"}:
        raise CadProcessingError(f"Modo de rayado no válido: {hatch_mode}")
    if hatch_threshold < 1:
        raise CadProcessingError("El umbral de rayados debe ser un entero positivo")
    try:
        svg_content, svg_warnings = _draw_to_svg(
            document, page, settings, hatch_mode, hatch_threshold
        )
        svg_element_count = _validate_svg(svg_content)
        pdf_content, pdf_warnings = _draw_to_pdf(
            document, page, settings, hatch_mode, hatch_threshold
        )
    except (MemoryError, TimeoutError) as error:
        text = str(error).lower()
        if isinstance(error, MemoryError) or "hatching timeout" in text:
            raise CadProcessingError(
                "Falló la etapa de dibujo vectorial por memoria o tiempo agotado al dibujar "
                "un rayado. Reintenta con --hatch-mode outline."
            ) from None
        raise
    pdf_page_count, pdf_vector_count = _validate_pdf(pdf_content)

    _write_atomically(svg_destination, svg_content.encode("utf-8"), overwrite)
    try:
        _write_atomically(pdf_destination, pdf_content, overwrite)
    except CadProcessingError:
        if not overwrite:
            svg_destination.unlink(missing_ok=True)
        raise
    return ExportReport(
        svg_path=svg_destination,
        pdf_path=pdf_destination,
        svg_element_count=svg_element_count,
        pdf_page_count=pdf_page_count,
        pdf_vector_drawing_count=pdf_vector_count,
        warnings=tuple(dict.fromkeys((*svg_warnings, *pdf_warnings))),
    )
