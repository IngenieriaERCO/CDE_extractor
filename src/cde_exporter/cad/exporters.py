"""Exportación vectorial de un DXF extraído a SVG y PDF."""

from __future__ import annotations

import contextlib
import copy
import io
import math
import os
import random
import time
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from ezdxf import bbox, xclip
from ezdxf.addons.drawing import Frontend, RenderContext, config, layout
from ezdxf.addons.drawing import pymupdf as pdf_backend
from ezdxf.addons.drawing import svg as svg_backend
from ezdxf.addons.drawing.backend import BackendProperties
from ezdxf.addons.drawing.config import HatchPolicy
from ezdxf.addons.drawing.pipeline import prepare_string_for_rendering
from ezdxf.addons.drawing.properties import LayoutProperties
from ezdxf.addons.drawing.recorder import (
    DataRecord,
    FilledPathsRecord,
    ImageRecord,
    PathRecord,
    PointsRecord,
    SolidLinesRecord,
)
from ezdxf.document import Drawing
from ezdxf.entities import Insert
from ezdxf.math import BoundingBox2d, Matrix44, Vec2, Vec3
from ezdxf.path import nesting
from ezdxf.render import hatching
from ezdxf.tools.pattern import scale_pattern

from cde_exporter.cad import run_hooks
from cde_exporter.cad.entity_policy import (
    GEOMETRY_FROM_DATA_TYPES,
    HATCH_MEMORY_REASON,
    HATCH_TIMEOUT_REASON,
    unsupported_hint,
)
from cde_exporter.cad.models import INVALID_DATA, CadProcessingError, invalid_data_reason
from cde_exporter.cad.reader import ensure_layer_table_entries, read_dxf

# Tiempo máximo para dibujar el patrón de un HATCH (el valor por defecto de ezdxf).
HATCHING_TIMEOUT = 30.0
# Altura de las mayúsculas de Helvetica, en fracción del tamaño de letra.
HELVETICA_CAP_HEIGHT = 0.718
_POINTS_PER_MM = 72 / 25.4

# Reutilización de bloques (``_StrictFrontend``): con CDE_INSTANCING=0 se dibuja cada INSERT
# entero, como antes (para comparar).
INSTANCING_ENV = "CDE_INSTANCING"
# Lo que impide reutilizar el dibujo de un bloque: texto (capa de texto buscable), cotas y
# similares, lo que depende de la vista o del tamaño (puntos, imágenes, WIPEOUT) y lo que el
# exportador trata aparte (sólidos, proxies). Los HATCH solo si son sólidos: el patrón se
# alinea con el origen del dibujo, no con el del bloque.
_NOT_INSTANCEABLE = frozenset(
    {
        "TEXT", "MTEXT", "ATTRIB", "DIMENSION", "ARC_DIMENSION", "LARGE_RADIAL_DIMENSION",
        "LEADER", "MULTILEADER", "MLEADER", "ACAD_TABLE", "TOLERANCE", "IMAGE", "WIPEOUT",
        "POINT", "SHAPE", "VIEWPORT", "OLE2FRAME", "ACAD_PROXY_ENTITY", "3DSOLID", "REGION",
        "BODY", "SURFACE", "MESH", "PDFUNDERLAY", "DWFUNDERLAY", "DGNUNDERLAY",
    }
)


# En el PDF, cada bloque reutilizado se escribe una vez como Form XObject y cada INSERT lo coloca
# con su matriz. CDE_XOBJECT=0 lo quita (los trazos se copian, como con el SVG); CDE_INSTANCING=0
# quita toda la reutilización.
XOBJECT_ENV = "CDE_XOBJECT"


# Cancelar mientras se dibuja (definida en run_hooks; se reexporta aquí).
DrawingCancelled = run_hooks.DrawingCancelled


def instancing_enabled() -> bool:
    return os.environ.get(INSTANCING_ENV, "").strip() not in {"0", "no", "false"}


def xobjects_enabled() -> bool:
    value = os.environ.get(XOBJECT_ENV, "").strip()
    return instancing_enabled() and value not in {"0", "no", "false"}


def _instanceable_insert(insert) -> bool:
    """El INSERT en sí: sin atributos, sin matriz de filas y columnas, sin recorte XCLIP y en
    planta (extrusión 0, 0, 1)."""
    if insert.attribs or insert.mcount > 1:
        return False
    if insert.has_extension_dict:
        clip = xclip.XClip(insert)
        if clip.has_clipping_path and clip.is_clipping_enabled:
            return False
    extrusion = insert.dxf.get("extrusion", (0, 0, 1))
    return abs(extrusion[0]) < 1e-12 and abs(extrusion[1]) < 1e-12 and extrusion[2] > 0


def _instanceable_block(document, name: str, known: dict[str, bool], stack=()) -> bool:
    """¿Sale igual el dibujo de este bloque en cualquier sitio donde se inserte?"""
    if name in known:
        return known[name]
    if name in stack or document is None or name not in document.blocks:
        return False
    result = True
    for entity in document.blocks[name]:
        kind = entity.dxftype()
        if kind in _NOT_INSTANCEABLE:
            result = False
        elif kind == "HATCH" and not entity.dxf.get("solid_fill", 0):
            result = False
        elif kind == "INSERT":
            result = _instanceable_insert(entity) and _instanceable_block(
                document, entity.dxf.name, known, (*stack, name)
            )
        if not result:
            break
    known[name] = result
    return result


@dataclass(slots=True)
class _Instance:
    """Dibujo grabado de un bloque, en coordenadas del bloque, y lo invisible que contiene."""

    records: list
    invisible: int


class InstanceRecord(DataRecord):
    """Marca en la grabación: «aquí va el dibujo de ``instance`` colocado con ``matrix``».

    Solo la usa el PDF (``_ExactPageRenderBackend.draw_instance``), que escribe el dibujo una vez
    como Form XObject y lo coloca en cada marca. ``matrix`` lleva de coordenadas del bloque a las
    de la grabación; la colocación en la página se le suma con ``transform_inplace``.
    """

    def __init__(self, instance: _Instance, matrix: Matrix44) -> None:
        super().__init__()
        self.instance = instance
        self.matrix = matrix

    def bbox(self) -> BoundingBox2d:
        box = BoundingBox2d()
        for record in self.instance.records:
            corners = record.bbox()
            if corners.has_data:
                box.extend(self.matrix.fast_2d_transform(corners.rect_vertices()))
        return box

    def transform_inplace(self, m: Matrix44) -> None:
        self.matrix = Matrix44.chain(self.matrix, m)

    def expanded(self) -> list:
        """Los registros del bloque colocados (para lo que no sabe de marcas)."""
        placed = []
        for record in self.instance.records:
            item = copy.deepcopy(record)
            item.transform_inplace(self.matrix)
            item.handle = self.handle
            placed.append(item)
        return placed


def replay_record(backend, record, properties) -> None:
    """Envía un registro al backend, como ``Player.replay`` de ezdxf 1.4.4."""
    if isinstance(record, PointsRecord):
        count = len(record.points)
        if count == 0:
            return
        if count > 2:
            backend.draw_filled_polygon(record.points, properties)
            return
        vertices = record.points.vertices()
        if len(vertices) == 1:
            backend.draw_point(vertices[0], properties)
        else:
            backend.draw_line(vertices[0], vertices[1], properties)
    elif isinstance(record, SolidLinesRecord):
        vertices = list(record.lines.vertices())
        backend.draw_solid_lines(zip(vertices[0::2], vertices[1::2], strict=False), properties)
    elif isinstance(record, PathRecord):
        backend.draw_path(record.path, properties)
    elif isinstance(record, FilledPathsRecord):
        backend.draw_filled_paths(record.paths, properties)
    elif isinstance(record, ImageRecord):
        backend.draw_image(record.image_data, properties)


@dataclass(frozen=True, slots=True)
class TextRun:
    """Una línea (o trozo) de texto tal como ezdxf la dibuja, en coordenadas CAD.

    ``origin``, ``x_axis`` e ``y_axis`` son los puntos (0, 0), (1, 0) y (0, 1) del texto
    llevados al dibujo (incluyen giro, escala de bloques anidados, factor de anchura y
    oblicuo); ``advance`` es la anchura de la línea con la fuente usada en el dibujo.
    """

    text: str
    origin: Vec3
    x_axis: Vec3
    y_axis: Vec3
    cap_height: float
    advance: float


class _StrictFrontend(Frontend):
    """Registra las entidades que ezdxf omite en el renderizado.

    Las omitidas por "invisible" (capa apagada o congelada, o entidad invisible)
    solo se cuentan. Con ``omit_unsupported`` las omitidas por "unsupported" o por
    datos inválidos se cuentan por (tipo, capa). Cualquier otra omisión se guarda en
    ``skipped``.
    """

    def __init__(
        self, *args, omit_unsupported: bool = False, capture_text: bool = False, **kwargs
    ) -> None:
        self.skipped: list[str] = []
        # Con ``capture_text``, cada línea de texto dibujada (para la capa de texto del PDF).
        self.text_runs: list[TextRun] = []
        self.invisible = 0
        self.omit_unsupported = omit_unsupported
        self.omitted: Counter[tuple[str, str]] = Counter()
        self.messages: list[str] = []
        self.auto_hatch_threshold: int | None = None
        # HATCH cuyo patrón no se pudo dibujar (tiempo o memoria) y se rellenaron:
        # (tipo, capa, handle, patrón, escala, motivo).
        self.degraded: list[tuple[str, str, str, str, float, str]] = []
        # Handle → motivo de los ya degradados en otra pasada (el PDF tras el SVG).
        self.known_degraded: dict[str, str] = {}
        super().__init__(*args, **kwargs)
        backend = args[1] if len(args) > 1 else kwargs.get("backend")
        # Los backends SVG y PDF de ezdxf graban los trazos en ``records``.
        self._records = getattr(backend, "records", None)
        # Bloques con muchas referencias idénticas: el primer INSERT se dibuja entero y su
        # grabación se reutiliza en los demás, colocada con su matriz (``_draw_instance``).
        self.instancing = instancing_enabled() and self._records is not None
        # Solo el PDF sabe escribir las marcas como XObject (el SVG copia los trazos).
        self.xobjects = False
        self._instances: dict[tuple, _Instance | None] = {}
        self._instanceable: dict[str, bool] = {}
        self.instances_reused = 0
        # Profundidad de la entidad que se dibuja (0: el INSERT del Model Space), para el
        # avance y la cancelación (``run_hooks``).
        self.hook_depth = 0
        if capture_text:
            self.pipeline.draw_text = self._capture_text(self.pipeline.draw_text)

    def _capture_text(self, draw_text):
        """Envuelve ``pipeline.draw_text`` (TEXT, MTEXT, ATTRIB y texto de cotas pasan por él)."""

        def capture(text, transform, properties, cap_height, dxftype="TEXT") -> None:
            start = len(self._records) if self._records is not None else 0
            draw_text(text, transform, properties, cap_height, dxftype)
            # Solo cuenta lo que ezdxf llegó a dibujar (no un texto vacío o sin glifos).
            if self._records is None or len(self._records) == start:
                return
            line = prepare_string_for_rendering(text, dxftype)
            font_face = properties.font or self.pipeline.default_font_face
            try:
                advance = self.pipeline.text_engine.get_text_line_width(
                    line, font_face, cap_height
                )
            except (RuntimeError, ValueError):
                advance = 0.0
            self.text_runs.append(
                TextRun(
                    line,
                    transform.transform(Vec3(0, 0, 0)),
                    transform.transform(Vec3(1, 0, 0)),
                    transform.transform(Vec3(0, 1, 0)),
                    cap_height,
                    advance,
                )
            )

        return capture

    def draw_entity(self, entity, properties) -> None:
        run_hooks.entity(self.hook_depth)  # puede cancelar (DrawingCancelled)
        self.hook_depth += 1
        try:
            self._draw_entity_checked(entity, properties)
        finally:
            self.hook_depth -= 1

    def _draw_entity_checked(self, entity, properties) -> None:
        # Una entidad que lanza un error al dibujarse se trata como no soportada por datos
        # inválidos; se deshacen los trazos (y el texto) que hubiera grabado para no dejarla a
        # medias.
        start = len(self._records) if self._records is not None else None
        texts = len(self.text_runs)
        reported = len(self.skipped) + sum(self.omitted.values()) + self.invisible
        try:
            super().draw_entity(entity, properties)
        except (MemoryError, TimeoutError, DrawingCancelled):
            # Cancelación, o agotamiento de recursos al dibujar rayados (se trata en
            # export_vector_outputs).
            raise
        except Exception as error:  # noqa: BLE001 - ezdxf puede lanzar cualquier error
            if start is not None:
                del self._records[start:]
            del self.text_runs[texts:]
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

    def draw_composite_entity(self, entity, properties) -> None:
        key = self._instance_key(entity, properties) if self.instancing else None
        if key is None:
            super().draw_composite_entity(entity, properties)
            return
        matrix = entity.matrix44()
        cached = self._instances.get(key, False)
        if cached:
            self._replay_instance(cached, matrix, entity)
            return
        before = (
            len(self.skipped), sum(self.omitted.values()), len(self.text_runs),
            len(self.degraded), len(self.messages),
        )
        invisible, start = self.invisible, len(self._records)
        super().draw_composite_entity(entity, properties)
        after = (
            len(self.skipped), sum(self.omitted.values()), len(self.text_runs),
            len(self.degraded), len(self.messages),
        )
        if cached is None or after != before:
            # Algo distinto de trazos (una omisión, un aviso…): ese bloque no se reutiliza.
            self._instances[key] = None
            return
        inverse = matrix.copy()
        inverse.inverse()
        records = [copy.deepcopy(record) for record in self._records[start:]]
        for record in records:
            record.transform_inplace(inverse)
        self._instances[key] = _Instance(records, self.invisible - invisible)

    def _instance_key(self, entity, properties) -> tuple | None:
        """Clave de los INSERT que se dibujan igual; None si este no se puede reutilizar."""
        if not isinstance(entity, Insert) or self.pipeline.clipping_portal.is_active:
            return None
        if not _instanceable_insert(entity):
            return None
        name = entity.dxf.name
        if not _instanceable_block(entity.doc, name, self._instanceable):
            return None
        # Lo que hereda el contenido del INSERT (PorBloque y capa «0») y la escala, con su signo
        # (el tipo de línea depende del tamaño).
        return (
            name,
            properties.color,
            properties.pen,
            properties.linetype_name,
            tuple(properties.linetype_pattern),
            properties.linetype_scale,
            properties.lineweight,
            properties.layer,
            properties.is_visible,
            round(float(entity.dxf.get("xscale", 1)), 12),
            round(float(entity.dxf.get("yscale", 1)), 12),
            round(float(entity.dxf.get("zscale", 1)), 12),
        )

    def _replay_instance(self, cached: _Instance, matrix: Matrix44, entity) -> None:
        handle = str(entity.dxf.get("handle") or "")
        if self.xobjects and cached.records:
            marker = InstanceRecord(cached, matrix)
            marker.property_hash = cached.records[0].property_hash
            marker.handle = handle
            self._records.append(marker)
            self.invisible += cached.invisible
            self.instances_reused += 1
            return
        for record in cached.records:
            placed = copy.deepcopy(record)
            placed.transform_inplace(matrix)
            placed.handle = handle
            self._records.append(placed)
        self.invisible += cached.invisible
        self.instances_reused += 1

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
        restore_hatch_pattern(entity)
        estimate = _estimate_hatch_segments(entity) if self.auto_hatch_threshold is not None else 0
        # ezdxf desplaza el origen de las líneas del patrón una cantidad aleatoria muy pequeña;
        # con una semilla fija por HATCH el dibujo es siempre el mismo (y el SVG, comparable).
        state = random.getstate()
        random.seed(_hatch_seed(entity))
        try:
            handle = _original_handle(entity)
            if estimate > (self.auto_hatch_threshold or 0):
                self._draw_hatch_as_solid(entity, properties, loops)
            elif handle and handle in self.known_degraded:
                self._draw_hatch_as_solid(entity, properties, loops)
                self._note_degraded(entity, self.known_degraded[handle])
            else:
                self._draw_hatch_or_degrade(entity, properties, loops)
        finally:
            random.setstate(state)

    def _draw_hatch_as_solid(self, entity, properties, loops) -> None:
        original = self.config
        self.config = self.config.with_changes(hatch_policy=HatchPolicy.SHOW_SOLID)
        try:
            super().draw_hatch_entity(entity, properties, loops=loops)
        finally:
            self.config = original

    def _draw_hatch_or_degrade(self, entity, properties, loops) -> None:
        """Dibuja el patrón; si agota el tiempo o la memoria, rellena solo ese HATCH.

        Al agotar el tiempo, ezdxf no lanza ningún error: avisa por pantalla y deja el patrón
        dibujado a medias. Se detecta por el tiempo transcurrido, se borran los trazos que
        dejó y se rellena el HATCH (la misma operación que la simplificación automática).
        """
        start = len(self._records) if self._records is not None else None
        began = time.perf_counter()
        try:
            super().draw_hatch_entity(entity, properties, loops=loops)
        except MemoryError:
            reason = HATCH_MEMORY_REASON
        else:
            limit = self.config.hatching_timeout
            if time.perf_counter() - began <= limit:
                return
            reason = HATCH_TIMEOUT_REASON.format(seconds=limit)
        if start is not None:
            del self._records[start:]
        self._draw_hatch_as_solid(entity, properties, loops)
        self._note_degraded(entity, reason)

    def _note_degraded(self, entity, reason: str) -> None:
        original = getattr(entity, "source_of_copy", None) or entity
        self.degraded.append(
            (
                entity.dxftype(),
                str(original.dxf.get("layer", "0") or "0"),
                _original_handle(entity),
                str(original.dxf.get("pattern_name", "") or ""),
                float(original.dxf.get("pattern_scale", 1.0)),
                reason,
            )
        )


def _original_handle(entity) -> str:
    original = getattr(entity, "source_of_copy", None) or entity
    return str(original.dxf.get("handle") or "")


def _hatch_seed(entity) -> str:
    return f"cde-hatch-{_original_handle(entity)}"


def restore_hatch_pattern(entity) -> None:
    """Corrige el patrón de un HATCH que ezdxf copió desde un bloque (entidad virtual).

    ezdxf 1.4.4 (``DXFPolygon.transform``) calcula bien la escala y el ángulo nuevos del
    patrón, pero los vuelve a aplicar enteros sobre unas líneas que ya los incluían: un
    patrón de escala 0,005 dentro de un INSERT sale 200 veces más denso, y uno girado 30°
    gira otros 30°. Aquí se rehace desde el original: escala relativa (nueva ÷ original),
    giro relativo (nuevo − original) y el origen del patrón trasladado con el HATCH, como en
    el CAD. Sin original (HATCH del Model Space) no se toca.
    """
    original = getattr(entity, "source_of_copy", None)
    if original is None or original is entity or original.dxftype() != entity.dxftype():
        return
    pattern = getattr(original, "pattern", None)
    if not pattern or not pattern.lines or entity.dxf.get("solid_fill", 0):
        return
    old_scale = original.dxf.get("pattern_scale", 1.0) or 1.0
    factor = entity.dxf.get("pattern_scale", 1.0) / old_scale
    delta = entity.dxf.get("pattern_angle", 0.0) - original.dxf.get("pattern_angle", 0.0)
    shift = _pattern_shift(original, entity, factor, delta)
    entity.set_pattern_definition(
        [
            (angle, Vec2(base_point) + shift, offset, dashes)
            for angle, base_point, offset, dashes in scale_pattern(
                pattern.as_list(), factor=factor, angle=delta
            )
        ]
    )


def _pattern_shift(original, entity, factor: float, delta: float) -> Vec2:
    """Traslación del HATCH: la misma que sufre el primer vértice de su contorno."""
    if not Vec3(original.dxf.extrusion).isclose(Vec3(entity.dxf.extrusion)):
        return Vec2()
    try:
        before = hatching.hatch_boundary_paths(original, filter_text_boxes=False)
        after = hatching.hatch_boundary_paths(entity, filter_text_boxes=False)
    except Exception:  # noqa: BLE001 - contorno ilegible: el patrón queda sin trasladar
        return Vec2()
    if not before or not after:
        return Vec2()
    return Vec2(after[0].start) - Vec2(before[0].start).rotate_deg(delta) * factor


def _estimate_hatch_segments(entity) -> int:
    """Tramos que ezdxf dibujaría para el patrón del HATCH (0 si es sólido o no tiene patrón).

    Usa las mismas líneas base que ezdxf (separación real, dirección y guiones) y el contorno
    real: por cada línea del patrón, los cortes con el borde, (suma de |borde · normal|) ÷
    (2 × separación), más los guiones que caben en la longitud total de línea dentro de la
    región, (área ÷ separación) × guiones ÷ periodo. Si las líneas están más juntas de lo que
    ezdxf puede dibujar, devuelve 0: ezdxf las rellena (lo declara la tabla de política).
    """
    if entity.dxf.get("solid_fill", 0) or not entity.pattern:
        return 0
    try:
        return _contour_estimate(entity)
    except hatching.DenseHatchingLinesError:
        return 0
    except Exception:  # noqa: BLE001 - contorno ilegible: se usa la estimación por la caja
        return _bbox_estimate(entity)


def _contour_estimate(entity) -> int:
    paths = hatching.hatch_boundary_paths(entity, filter_text_boxes=True)
    corners = [vertex for path in paths for vertex in path.control_vertices()]
    if len(corners) < 2:
        return 0
    xs = [vertex.x for vertex in corners]
    ys = [vertex.y for vertex in corners]
    distance = max(math.hypot(max(xs) - min(xs), max(ys) - min(ys)) / 1000, 1e-9)
    loops = {id(path): [(v.x, v.y) for v in path.flattening(distance)] for path in paths}

    def area(polygon, depth: int) -> float:
        # Estructura de ezdxf: [exterior, *huecos]; cada hueco es otro polígono.
        exterior, *holes = polygon
        vertices = loops[id(exterior)]
        surface = abs(
            sum(
                x0 * y1 - x1 * y0
                for (x0, y0), (x1, y1) in zip(vertices, vertices[1:] + vertices[:1], strict=True)
            )
            / 2
        )
        surface = surface if depth % 2 == 0 else -surface
        return surface + sum(area(hole, depth + 1) for hole in holes)

    region = abs(sum(area(polygon, 0) for polygon in nesting.make_polygon_structure(paths)))
    edges = [
        (x1 - x0, y1 - y0)
        for vertices in loops.values()
        for (x0, y0), (x1, y1) in zip(vertices, vertices[1:] + vertices[:1], strict=True)
    ]
    estimate = 0.0
    for baseline in hatching.pattern_baselines(entity):
        spacing = abs(baseline.normal_distance)
        if spacing <= 1e-12:
            continue
        normal_x, normal_y = -baseline.direction.y, baseline.direction.x
        crossings = sum(abs(dx * normal_x + dy * normal_y) for dx, dy in edges)
        estimate += crossings / (2 * spacing)
        period = sum(abs(value) for value in baseline.line_pattern)
        if period > 0:
            pieces = sum(1 for value in baseline.line_pattern if value >= 0)
            estimate += region / spacing * pieces / period
    return math.ceil(estimate)


def _bbox_estimate(entity) -> int:
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
    # HATCH rellenados al dibujar porque su patrón agotó el tiempo o la memoria:
    # (tipo, capa, handle, patrón, escala, motivo).
    degraded: tuple[tuple[str, str, str, str, float, str], ...] = ()
    # Líneas de texto escritas en la capa de texto invisible del PDF (vacío si no hay capa).
    text_lines: tuple[str, ...] = ()
    # Ancho y alto (unidades CAD) de lo dibujado en el PDF, para medir su escala real.
    drawn_size: tuple[float, float] | None = None
    # Caja de lo dibujado en coordenadas CAD: ((x mín., y mín.), (x máx., y máx.)).
    drawn_bounds: tuple[tuple[float, float], tuple[float, float]] | None = None
    # Relación CAD -> página del PDF en puntos, origen arriba a la izquierda e Y hacia abajo:
    # (a, b, c, d, e, f) con X = a·x + c·y + e e Y = b·x + d·y + f.
    cad_to_page_pt: tuple[float, float, float, float, float, float] | None = None


def affine_2d(matrix: Matrix44) -> tuple[float, float, float, float, float, float]:
    """(a, b, c, d, e, f) de la parte plana de ``matrix``: X = a·x + c·y + e, Y = b·x + d·y + f.

    Se leen los coeficientes (ezdxf usa vectores fila: X = x·m00 + y·m10 + m30). Restar
    puntos transformados perdería cifras con coordenadas georreferenciadas: con una
    traslación de ~10⁸ mm, la escala saldría 20,0000000096 en vez de 20 y desplazaría
    0,05 mm un punto a 4,9 millones de unidades.
    """
    return (
        float(matrix[0, 0]),
        float(matrix[0, 1]),
        float(matrix[1, 0]),
        float(matrix[1, 1]),
        float(matrix[3, 0]),
        float(matrix[3, 1]),
    )


def _page_point(matrix: Matrix44, point: Vec3) -> pymupdf.Point:
    mapped = matrix.transform(point)
    return pymupdf.Point(mapped.x, mapped.y)


def write_text_layer(page: pymupdf.Page, runs, matrix: Matrix44) -> None:
    """Escribe cada línea como texto invisible (``render_mode=3``) encima de sus glifos.

    ``matrix`` lleva las coordenadas CAD a las de la página (puntos, origen arriba a la
    izquierda). Se usa Helvetica con ``TextWriter``: fuente Type0, con todo Unicode (PyMuPDF
    añade glifos de respaldo si faltan) y el guion se extrae como «-». Cada línea se estira a
    la anchura con la que ezdxf la dibujó, es decir, con la fuente usada en el dibujo.
    """
    font = pymupdf.Font("helv")
    for run in runs:
        origin = _page_point(matrix, run.origin)
        x_axis = _page_point(matrix, run.x_axis) - origin
        y_axis = _page_point(matrix, run.y_axis) - origin
        size = run.cap_height / HELVETICA_CAP_HEIGHT
        natural = font.text_length(run.text, fontsize=size) if size > 0 else 0.0
        if natural <= 0 or abs(x_axis) == 0 or abs(y_axis) == 0:
            continue
        stretch = run.advance / natural if run.advance > 0 else 1.0
        # El texto se escribe en horizontal en el origen y se lleva a los ejes del dibujo:
        # eje x -> stretch · x_axis; eje y (hacia abajo en la página) -> −y_axis. PyMuPDF
        # aplica ``morph`` con el eje Y invertido, así que la matriz (a, b, c, d) se pasa como
        # (a, −b, −c, d); sin eso, un texto a 45° saldría a −45°.
        morph = pymupdf.Matrix(
            stretch * x_axis.x, -stretch * x_axis.y, y_axis.x, -y_axis.y, 0, 0
        )
        writer = pymupdf.TextWriter(page.rect)
        writer.append(origin, run.text, font=font, fontsize=size)
        writer.write_text(page, render_mode=3, morph=(origin, morph))


class _ExactPageRenderBackend(pdf_backend.PyMuPdfRenderBackend):
    """Página PDF con el tamaño exacto en puntos (ezdxf la crea truncada a puntos enteros).

    La página no lleva fondo: el componedor coloca varios detalles en una plancha y un
    rectángulo blanco del tamaño de la página taparía lo que queda debajo.
    """

    def __init__(
        self, page: layout.Page, settings: layout.Settings, size_mm: tuple[float, float]
    ) -> None:
        super().__init__(page, settings)
        self.page_width_in_pt = size_mm[0] * _POINTS_PER_MM
        self.page_height_in_pt = size_mm[1] * _POINTS_PER_MM
        self.page.set_mediabox(pymupdf.Rect(0, 0, self.page_width_in_pt, self.page_height_in_pt))
        # La forma compartida guarda la altura de la página al crearse: se crea de nuevo.
        self.content_shape = self.page.new_shape()

    def draw_instance(self, marker: InstanceRecord, properties_of) -> None:
        """Coloca el dibujo de un bloque reutilizado como Form XObject, en este punto del orden.

        La primera vez se dibujan sus registros (colocados con la matriz de esta marca) en la
        forma de contenido de la página y ese trozo se aparta como contenido del XObject. Cada
        marca escribe ``q <matriz> cm /Fm Do Q``: la matriz lleva del primer dibujo a este (solo
        giro, traslación o simetría: la escala es la misma por la clave de la reutilización).
        """
        forms = self.__dict__.setdefault("forms", {})
        shape = self.content_shape
        key = id(marker.instance)
        form = forms.get(key)
        if form is None:
            assert shape.draw_cont == ""
            start = len(shape.totalcont)
            for record in marker.expanded():
                if isinstance(record, InstanceRecord):
                    # Bloque anidado ya reutilizado: su propio XObject, dentro de este.
                    self.draw_instance(record, properties_of)
                else:
                    replay_record(self, record, properties_of(record))
            content = shape.totalcont[start:]
            shape.totalcont = shape.totalcont[:start]
            xref = self.doc.get_new_xref()
            self.doc.update_object(
                xref,
                "<< /Type /XObject /Subtype /Form "
                "/BBox [-100000 -100000 100000 100000] /Resources << >> >>",
            )
            self.doc.update_stream(xref, content.encode())
            name = f"FmCde{len(forms)}"
            _add_page_xobject(self.doc, self.page.xref, name, xref)
            inverse = marker.matrix.copy()
            inverse.inverse()
            form = (name, xref, inverse)
            forms[key] = form
        name, _xref, inverse = form
        # De la página (arriba a la izquierda) del primer dibujo a la de esta marca, y al espacio
        # PDF de la forma (``pctm``: PDF -> página; ``ipctm``: página -> PDF).
        relative = Matrix44.chain(inverse, marker.matrix)
        page_matrix = pymupdf.Matrix(
            relative[0, 0],
            relative[0, 1],
            relative[1, 0],
            relative[1, 1],
            relative[3, 0],
            relative[3, 1],
        )
        transform = shape.pctm * page_matrix * shape.ipctm
        values = " ".join(_pdf_number(value) for value in tuple(transform))
        shape.totalcont += f"\nq\n{values} cm\n/{name} Do\nQ\n"

    def link_form_resources(self) -> None:
        """Los XObject usan los mismos recursos que la página (capas como contenido opcional,
        transparencias): se les asigna su diccionario de recursos al terminar."""
        forms = self.__dict__.get("forms", {})
        if not forms:
            return
        kind, value = self.doc.xref_get_key(self.page.xref, "Resources")
        if kind == "null":
            return
        for _name, xref, _inverse in forms.values():
            self.doc.xref_set_key(xref, "Resources", value)

    def set_background(self, color) -> None:
        # ezdxf 1.4.4 pinta el fondo como un rectángulo relleno del tamaño de la página; quiere
        # saltarlo si es blanco, pero compara "#ffffff" con (1.0, 1.0, 1.0) y lo pinta siempre.
        # Aquí no se pinta. El color de fondo lógico sigue siendo blanco (BackgroundPolicy.WHITE
        # en el frontend): el color 7 sale negro y los WIPEOUT, blancos, como hasta ahora.
        return None


def _pdf_number(value: float) -> str:
    """Número para un flujo de contenido PDF: decimal fijo (PDF no admite «1e-13»)."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _indirect(value: str) -> int | None:
    parts = value.split()
    return int(parts[0]) if len(parts) == 3 and parts[2] == "R" else None


def _add_page_xobject(doc, page_xref: int, name: str, xref: int) -> None:
    """Añade ``/name xref 0 R`` a /Resources/XObject de la página, siguiendo las referencias
    indirectas (PyMuPDF no escribe una ruta de claves que pase por un objeto indirecto)."""
    target, key = page_xref, "Resources"
    kind, value = doc.xref_get_key(target, key)
    if kind == "xref":
        target, key = _indirect(value), ""
    sub = f"{key}/XObject" if key else "XObject"
    kind, value = doc.xref_get_key(target, sub)
    if kind == "xref":
        doc.xref_set_key(_indirect(value), name, f"{xref} 0 R")
    else:
        doc.xref_set_key(target, f"{sub}/{name}", f"{xref} 0 R")


def _replay_with_instances(player, backend) -> None:
    """``Player.replay`` de ezdxf, pero las marcas de bloques reutilizados se escriben como
    Form XObject en su lugar del orden de dibujo."""
    props = player.properties

    def properties_of(record) -> BackendProperties:
        return BackendProperties(*props[record.property_hash][:4], record.handle)

    backend.configure(player.config)
    backend.set_background(player.background)
    for record in player.records:
        if isinstance(record, InstanceRecord):
            backend.draw_instance(record, properties_of)
        else:
            replay_record(backend, record, properties_of(record))
    backend.finalize()
    backend.link_form_resources()


class _PdfBackend(pdf_backend.PyMuPdfBackend):
    """Backend PDF de ezdxf a escala exacta y con la capa de texto invisible y buscable.

    ezdxf 1.4.4 (``get_coordinate_output_space``) toma como espacio de salida el lado
    mayor de la página truncado a puntos enteros y dibuja cada unidad de ese espacio como
    1 pt: el dibujo sale encogido en int(lado) ÷ lado (−1,22 % en una página de 20 mm).
    Aquí el espacio de salida es el lado exacto en puntos y la página tiene su tamaño
    exacto, así que 1 unidad = 1 pt.

    ``text_runs`` se rellena tras dibujar (las líneas que capturó el frontend). Tras
    ``get_pdf_bytes``, ``placement_matrix`` es la matriz CAD -> página (puntos, origen
    arriba a la izquierda), ``drawn_size`` el ancho y alto de lo dibujado en unidades CAD y
    ``drawn_bounds`` su caja en coordenadas CAD.
    """

    def __init__(self) -> None:
        super().__init__()
        self.text_runs: list[TextRun] = []
        self.placement_matrix: Matrix44 | None = None
        self.drawn_size: tuple[float, float] | None = None
        self.drawn_bounds: tuple[tuple[float, float], tuple[float, float]] | None = None

    def get_replay(self, page, *, settings=None, render_box=None):
        # Igual que ``PyMuPdfBackend.get_replay`` de ezdxf 1.4.4, salvo el espacio de salida
        # y la página exactos.
        settings = settings if settings is not None else layout.Settings()
        player = self.player()  # cambia las grabaciones originales, como en ezdxf
        # Lo dibujado se mide siempre en la geometría real; ``render_box`` (si se pasa) solo fija
        # qué rectángulo se coloca en la página (extract-modelspace con recorte: la ventana).
        drawn = player.bbox()
        if render_box is None:
            render_box = drawn
        if drawn.has_data:
            self.drawn_size = (float(drawn.size.x), float(drawn.size.y))
            self.drawn_bounds = (
                (float(drawn.extmin.x), float(drawn.extmin.y)),
                (float(drawn.extmax.x), float(drawn.extmax.y)),
            )
        output_layout = layout.Layout(render_box, flip_y=self._init_flip_y)
        requested = page
        # ezdxf redondea la página final a 0,1 mm (``Page.width_in_mm`` redondea al leerse):
        # 417,0 × 119,4 en vez de 416,980 × 119,403 mm.
        page = output_layout.get_final_page(page, settings)
        size_mm = (page.width_in_mm, page.height_in_mm)
        if requested.width > 0 and requested.height > 0 and not (
            requested.max_width or requested.max_height
        ):
            size_mm = (
                requested.width * requested.to_mm_factor,
                requested.height * requested.to_mm_factor,
            )
        settings = copy.copy(settings)
        # Lado mayor en puntos de la página que usa ezdxf: 1 unidad de salida = 1 pt.
        settings.output_coordinate_space = (
            max(page.width_in_mm, page.height_in_mm) * _POINTS_PER_MM
        )
        self.placement_matrix = output_layout.get_placement_matrix(
            page, settings=settings, top_origin=True
        )
        if layout.is_center_aligned(settings.page_alignment):
            # ezdxf centra el dibujo en la página redondeada; se lleva al centro de la exacta.
            self.placement_matrix = Matrix44.chain(
                self.placement_matrix,
                Matrix44.translate(
                    (size_mm[0] - page.width_in_mm) / 2 * _POINTS_PER_MM,
                    (size_mm[1] - page.height_in_mm) / 2 * _POINTS_PER_MM,
                    0,
                ),
            )
        player.transform(self.placement_matrix)
        if settings.crop_at_margins:
            top_left, bottom_right = page.get_margin_rect(top_origin=True)
            output_scale = settings.page_output_scale_factor(page)
            player.crop_rect(
                top_left * output_scale, bottom_right * output_scale, 0.1 * _POINTS_PER_MM
            )
        self._init_flip_y = False
        backend = _ExactPageRenderBackend(page, settings, size_mm)
        if any(isinstance(record, InstanceRecord) for record in player.records):
            _replay_with_instances(player, backend)
        else:
            player.replay(backend)
        return backend

    def get_pdf_bytes(self, page, *, settings=None, render_box=None) -> bytes:
        backend = self.get_replay(page, settings=settings, render_box=render_box)
        if self.text_runs:
            write_text_layer(backend.page, self.text_runs, self.placement_matrix)
            backend.doc.subset_fonts()  # solo los glifos usados (Helvetica y los de respaldo)
        return backend.get_pdf_bytes()


def _draw_to_svg(
    document: Drawing,
    page: layout.Page,
    settings: layout.Settings,
    hatch_mode: str = "normal",
    hatch_threshold: int = 50000,
    omit_unsupported: bool = False,
    suggest_solids_footprint: bool = False,
    stats: dict | None = None,
    render_box: BoundingBox2d | None = None,
) -> tuple[str, tuple[str, ...], tuple[tuple[str, str, int], ...]]:
    backend = svg_backend.SVGBackend()
    layout_properties = LayoutProperties.from_layout(document.modelspace())
    layout_properties.set_colors("#ffffff")
    frontend = _StrictFrontend(
        RenderContext(document),
        backend,
        config=_drawing_configuration(hatch_mode),
        omit_unsupported=omit_unsupported,
    )
    frontend.auto_hatch_threshold = hatch_threshold if hatch_mode == "auto" else None
    output = io.StringIO()
    # ezdxf avisa por pantalla al agotar el tiempo de un rayado; ese HATCH ya se degradó.
    with contextlib.redirect_stdout(output):
        frontend.draw_layout(document.modelspace(), layout_properties=layout_properties)
    _raise_if_skipped(frontend, "SVG", suggest_solids_footprint)
    if stats is not None:
        stats["degraded"] = tuple(frontend.degraded)
    return (
        backend.get_string(page, settings=settings, render_box=render_box),
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
    stats: dict | None = None,
    text_layer: bool = False,
    render_box: BoundingBox2d | None = None,
) -> tuple[bytes, tuple[str, ...], tuple[tuple[str, str, int], ...]]:
    backend = _PdfBackend()
    layout_properties = LayoutProperties.from_layout(document.modelspace())
    layout_properties.set_colors("#ffffff")
    frontend = _StrictFrontend(
        RenderContext(document),
        backend,
        config=_drawing_configuration(hatch_mode),
        omit_unsupported=omit_unsupported,
        capture_text=text_layer,
    )
    frontend.xobjects = frontend.instancing and xobjects_enabled()
    frontend.auto_hatch_threshold = hatch_threshold if hatch_mode == "auto" else None
    if stats is not None:
        # Lo que ya se degradó al dibujar el SVG no se vuelve a intentar (otros 30 s).
        frontend.known_degraded = {
            handle: reason for _t, _l, handle, _p, _s, reason in stats.get("degraded", ())
        }
    output = io.StringIO()
    # ezdxf avisa por pantalla al agotar el tiempo de un rayado; ese HATCH ya se degradó.
    with contextlib.redirect_stdout(output):
        frontend.draw_layout(document.modelspace(), layout_properties=layout_properties)
    _raise_if_skipped(frontend, "PDF", suggest_solids_footprint)
    backend.text_runs = frontend.text_runs
    run_hooks.stage("pdf")
    pdf_content = backend.get_pdf_bytes(page, settings=settings, render_box=render_box)
    if stats is not None:
        stats["invisible"] = frontend.invisible
        stats["degraded"] = tuple(frontend.degraded)
        stats["text_lines"] = tuple(run.text.strip() for run in frontend.text_runs)
        stats["drawn_size"] = backend.drawn_size
        stats["drawn_bounds"] = backend.drawn_bounds
        if backend.placement_matrix is not None:
            stats["cad_to_page_pt"] = affine_2d(backend.placement_matrix)
    return (
        pdf_content,
        _frontend_messages(frontend),
        _omitted_rows(frontend),
    )


def _drawing_configuration(hatch_mode: str) -> config.Configuration:
    return config.Configuration(
        background_policy=config.BackgroundPolicy.WHITE,
        hatch_policy=_hatch_policy(hatch_mode),
        hatching_timeout=HATCHING_TIMEOUT,
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
    text_layer: bool = True,
    render_box: tuple[float, float, float, float] | None = None,
) -> ExportReport:
    """Crea SVG/PDF vectoriales ajustados o con escala y página explícitas.

    ``content_size`` (ancho y alto del Model Space en unidades CAD) evita recalcular la
    caja cuando quien llama ya la tiene; si falta, se calcula aquí.

    Con ``draw_svg=False`` no se dibuja ni se escribe el SVG (``svg_path`` queda en
    None); los avisos, las entidades no visibles y las omitidas salen del dibujo del
    PDF, que recorre las mismas entidades.

    Con ``text_layer`` el PDF lleva, encima del texto dibujado como trazados, una capa de
    texto invisible que se puede buscar, seleccionar y copiar. El SVG no cambia.
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
    stats: dict = {}
    # Rectángulo CAD (xmin, ymin, xmax, ymax) que se coloca en la página, en vez de la caja de lo
    # dibujado: con recorte, la página mide exactamente la ventana.
    box = (
        None
        if render_box is None
        else BoundingBox2d([(render_box[0], render_box[1]), (render_box[2], render_box[3])])
    )
    try:
        svg_content, svg_warnings, svg_omitted, svg_element_count = None, (), None, 0
        svg_degraded: tuple = ()
        if draw_svg:
            svg_content, svg_warnings, svg_omitted = _draw_to_svg(
                document,
                page,
                settings,
                hatch_mode,
                hatch_threshold,
                omit_unsupported,
                suggest_solids_footprint,
                stats=stats,
                render_box=box,
            )
            svg_element_count = _validate_svg(svg_content)
            svg_degraded = stats.get("degraded", ())
        pdf_content, pdf_warnings, pdf_omitted = _draw_to_pdf(
            document,
            page,
            settings,
            hatch_mode,
            hatch_threshold,
            omit_unsupported,
            suggest_solids_footprint,
            stats=stats,
            text_layer=text_layer,
            render_box=box,
        )
        if draw_svg and svg_omitted != pdf_omitted:
            raise CadProcessingError(
                "El SVG y el PDF no coinciden en las entidades omitidas "
                f"(SVG {svg_omitted}, PDF {pdf_omitted}); no se generaron archivos."
            )
        if draw_svg and svg_degraded != stats.get("degraded", ()):
            raise CadProcessingError(
                "El SVG y el PDF no coinciden en los HATCH rellenados por tiempo o memoria; "
                "no se generaron archivos."
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
        degraded=stats.get("degraded", ()),
        text_lines=stats.get("text_lines", ()),
        drawn_size=stats.get("drawn_size"),
        drawn_bounds=stats.get("drawn_bounds"),
        cad_to_page_pt=stats.get("cad_to_page_pt"),
    )
