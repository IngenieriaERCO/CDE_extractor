"""Exportación vectorial de un DXF extraído a SVG y PDF."""

from __future__ import annotations

import contextlib
import io
import math
import os
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
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

from cde_exporter.cad.entity_policy import GEOMETRY_FROM_DATA_TYPES, unsupported_hint
from cde_exporter.cad.models import INVALID_DATA, CadProcessingError, invalid_data_reason
from cde_exporter.cad.reader import ensure_layer_table_entries, read_dxf


class _StrictFrontend(Frontend):
    """Registra las entidades que ezdxf omite en el renderizado.

    Las omitidas por "invisible" (capa apagada o congelada, o entidad invisible)
    solo se cuentan. Con ``omit_unsupported`` las omitidas por "unsupported" o por
    datos inválidos se cuentan por (tipo, capa). Cualquier otra omisión se guarda en
    ``skipped``.
    """

    def __init__(self, *args, omit_unsupported: bool = False, **kwargs) -> None:
        self.skipped: list[str] = []
        self.invisible = 0
        self.omit_unsupported = omit_unsupported
        self.omitted: Counter[tuple[str, str]] = Counter()
        self.messages: list[str] = []
        self.auto_hatch_threshold: int | None = None
        super().__init__(*args, **kwargs)
        backend = args[1] if len(args) > 1 else kwargs.get("backend")
        # Los backends SVG y PDF de ezdxf graban los trazos en ``records``.
        self._records = getattr(backend, "records", None)

    def draw_entity(self, entity, properties) -> None:
        # Una entidad que lanza un error al dibujarse se trata como no soportada por datos
        # inválidos; se deshacen los trazos que hubiera grabado para no dejarla a medias.
        start = len(self._records) if self._records is not None else None
        reported = len(self.skipped) + sum(self.omitted.values()) + self.invisible
        try:
            super().draw_entity(entity, properties)
        except (MemoryError, TimeoutError):
            raise  # agotamiento de recursos al dibujar rayados: se trata en export_vector_outputs
        except Exception as error:  # noqa: BLE001 - ezdxf puede lanzar cualquier error
            if start is not None:
                del self._records[start:]
            self.skip_entity(entity, invalid_data_reason(error))
        else:
            # ezdxf no avisa si un MULTILEADER no genera ninguna geometría: se trata como no
            # soportado (el inventario ya lo clasifica así), no se deja pasar en silencio.
            if (
                start is not None
                and entity.dxftype() in GEOMETRY_FROM_DATA_TYPES
                and len(self._records) == start
                and len(self.skipped) + sum(self.omitted.values()) + self.invisible == reported
            ):
                self.skip_entity(entity, "unsupported")

    def skip_entity(self, entity, msg: str) -> None:
        if msg == "invisible":
            self.invisible += 1
            return
        original = getattr(entity, "source_of_copy", None) or entity
        layer = str(original.dxf.get("layer", "0") or "0")
        invalid = msg.startswith(INVALID_DATA)
        if (msg == "unsupported" or invalid) and self.omit_unsupported:
            self.omitted[(entity.dxftype(), layer)] += 1
            return
        if invalid:
            handle = original.dxf.get("handle") or "(sin handle)"
            self.skipped.append(f"{entity.dxftype()} (handle {handle}, capa {layer}): {msg}")
            return
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


def _invisible_warning(count: int) -> str:
    if count == 1:
        return (
            "Se ignoró 1 entidad no visible (capa apagada o congelada, o entidad marcada "
            "como invisible); no aparece en el PDF porque tampoco se ve en el CAD."
        )
    return (
        f"Se ignoraron {count} entidades no visibles (capa apagada o congelada, o entidades "
        "marcadas como invisibles); no aparecen en el PDF porque tampoco se ven en el CAD."
    )


def _frontend_messages(frontend: _StrictFrontend) -> tuple[str, ...]:
    if frontend.invisible:
        return (*frontend.messages, _invisible_warning(frontend.invisible))
    return tuple(frontend.messages)


def _raise_if_skipped(
    frontend: _StrictFrontend, stage: str, suggest_solids_footprint: bool = False
) -> None:
    if not frontend.skipped:
        return
    details = ", ".join(frontend.skipped[:8])
    message = f"La exportación {stage} detuvo la operación porque ezdxf omite entidades: {details}"
    if not frontend.omit_unsupported and any(
        item.endswith(": unsupported") or f": {INVALID_DATA}: " in item
        for item in frontend.skipped
    ):
        unsupported_types = {
            item.split(":", 1)[0] for item in frontend.skipped if item.endswith(": unsupported")
        }
        message += unsupported_hint(unsupported_types, suggest_solids_footprint)
    raise CadProcessingError(message)


def _omitted_rows(frontend: _StrictFrontend) -> tuple[tuple[str, str, int], ...]:
    return tuple(
        (entity_type, layer, count)
        for (entity_type, layer), count in sorted(frontend.omitted.items())
    )


@dataclass(frozen=True, slots=True)
class ExportReport:
    svg_path: Path | None
    pdf_path: Path
    svg_element_count: int
    pdf_page_count: int
    pdf_vector_drawing_count: int
    warnings: tuple[str, ...]
    omitted: tuple[tuple[str, str, int], ...] = ()
    # Entidades que ezdxf descartó una a una por no ser visibles (capa apagada, etc.).
    invisible: int = 0


def _draw_to_svg(
    document: Drawing,
    page: layout.Page,
    settings: layout.Settings,
    hatch_mode: str = "normal",
    hatch_threshold: int = 50000,
    omit_unsupported: bool = False,
    suggest_solids_footprint: bool = False,
) -> tuple[str, tuple[str, ...], tuple[tuple[str, str, int], ...]]:
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
        omit_unsupported=omit_unsupported,
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
    _raise_if_skipped(frontend, "SVG", suggest_solids_footprint)
    return (
        backend.get_string(page, settings=settings),
        _frontend_messages(frontend),
        _omitted_rows(frontend),
    )


def _draw_to_pdf(
    document: Drawing,
    page: layout.Page,
    settings: layout.Settings,
    hatch_mode: str = "normal",
    hatch_threshold: int = 50000,
    omit_unsupported: bool = False,
    suggest_solids_footprint: bool = False,
    stats: dict[str, int] | None = None,
) -> tuple[bytes, tuple[str, ...], tuple[tuple[str, str, int], ...]]:
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
        omit_unsupported=omit_unsupported,
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
    _raise_if_skipped(frontend, "PDF", suggest_solids_footprint)
    if stats is not None:
        stats["invisible"] = frontend.invisible
    return (
        backend.get_pdf_bytes(page, settings=settings),
        _frontend_messages(frontend),
        _omitted_rows(frontend),
    )


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
    exact_page_size: bool = False,
    hatch_mode: str = "normal",
    hatch_threshold: int = 50000,
    omit_unsupported: bool = False,
    suggest_solids_footprint: bool = False,
    content_size: tuple[float, float] | None = None,
    draw_svg: bool = True,
) -> ExportReport:
    """Crea SVG/PDF vectoriales ajustados o con escala y página explícitas.

    ``content_size`` (ancho y alto del Model Space en unidades CAD) evita recalcular la
    caja cuando quien llama ya la tiene; si falta, se calcula aquí.

    Con ``draw_svg=False`` no se dibuja ni se escribe el SVG (``svg_path`` queda en
    None); los avisos, las entidades no visibles y las omitidas salen del dibujo del
    PDF, que recorre las mismas entidades.
    """
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
        if content_size is None:
            extents = bbox.extents(modelspace)
            if not extents.has_data:
                raise CadProcessingError(
                    "No se pudieron calcular extensiones para validar la escala"
                )
            content_size = (
                float(extents.extmax.x - extents.extmin.x),
                float(extents.extmax.y - extents.extmin.y),
            )
        content_width = content_size[0] * scale_factor
        content_height = content_size[1] * scale_factor
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
        svg_content, svg_warnings, svg_omitted, svg_element_count = None, (), None, 0
        if draw_svg:
            svg_content, svg_warnings, svg_omitted = _draw_to_svg(
                document,
                page,
                settings,
                hatch_mode,
                hatch_threshold,
                omit_unsupported,
                suggest_solids_footprint,
            )
            svg_element_count = _validate_svg(svg_content)
        stats: dict[str, int] = {}
        pdf_content, pdf_warnings, pdf_omitted = _draw_to_pdf(
            document,
            page,
            settings,
            hatch_mode,
            hatch_threshold,
            omit_unsupported,
            suggest_solids_footprint,
            stats=stats,
        )
        if draw_svg and svg_omitted != pdf_omitted:
            raise CadProcessingError(
                "El SVG y el PDF no coinciden en las entidades omitidas "
                f"(SVG {svg_omitted}, PDF {pdf_omitted}); no se generaron archivos."
            )
        if exact_page_size and page_size_mm is not None:
            with pymupdf.open(stream=pdf_content, filetype="pdf") as sized_pdf:
                pdf_page = sized_pdf[0]
                pdf_page.set_mediabox(
                    pymupdf.Rect(
                        0,
                        0,
                        page_size_mm[0] * 72 / 25.4,
                        page_size_mm[1] * 72 / 25.4,
                    )
                )
                pdf_content = sized_pdf.tobytes(garbage=4, deflate=True)
    except (MemoryError, TimeoutError) as error:
        text = str(error).lower()
        if isinstance(error, MemoryError) or "hatching timeout" in text:
            raise CadProcessingError(
                "Falló la etapa de dibujo vectorial por memoria o tiempo agotado al dibujar "
                "un rayado. Reintenta con --hatch-mode outline."
            ) from None
        raise
    pdf_page_count, pdf_vector_count = _validate_pdf(pdf_content)

    if svg_content is not None:
        _write_atomically(svg_destination, svg_content.encode("utf-8"), overwrite)
    try:
        _write_atomically(pdf_destination, pdf_content, overwrite)
    except CadProcessingError:
        if not overwrite and svg_content is not None:
            svg_destination.unlink(missing_ok=True)
        raise
    return ExportReport(
        svg_path=svg_destination if svg_content is not None else None,
        pdf_path=pdf_destination,
        svg_element_count=svg_element_count,
        pdf_page_count=pdf_page_count,
        pdf_vector_drawing_count=pdf_vector_count,
        warnings=tuple(dict.fromkeys((*svg_warnings, *pdf_warnings))),
        omitted=pdf_omitted,
        invisible=stats.get("invisible", 0),
    )
