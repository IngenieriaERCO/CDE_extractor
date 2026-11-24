"""Vista previa PNG de una referencia de bloque (orden ``preview`` del trabajador).

docs/protocolo_trabajador.md, sección 4:
- Dibuja **solo esa referencia** con el frontend del exportador en modo ligero (texto como
  rectángulos, rayados rellenos, sin tipos de línea; reutilización de bloques repetidos) y pinta
  la grabación con Pillow, sin pasar por el PDF. No dibuja los 3DSOLID.
- PNG con fondo transparente; el color 7 sale negro (fondo lógico blanco, como el PDF).
- ``cad_to_px`` = ``[a, b, c, d, e, f]`` con ``px = a·x + c·y + e`` y ``py = b·x + d·y + f``,
  ``b = c = 0`` y ``a = −d``. El píxel (0, 0) ocupa de 0 a 1 en X y en Y.
- Caché en ``<work-dir>/previews/<huella>/<handle>_<px>_<capas>.png`` con un ``.json`` al lado
  (los datos de la respuesta). Si pasa del límite, se borran las usadas hace más tiempo.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from ezdxf.addons.drawing import RenderContext, config
from ezdxf.addons.drawing.properties import LayoutProperties, layer_key
from ezdxf.addons.drawing.recorder import (
    FilledPathsRecord,
    PathRecord,
    PointsRecord,
    Recorder,
    SolidLinesRecord,
)
from PIL import Image, ImageChops, ImageDraw

from cde_exporter.cad.exporters import (
    HATCHING_TIMEOUT,
    InstanceRecord,
    _StrictFrontend,
    xobjects_enabled,
)
from cde_exporter.cad.hidden_layers import layer_state
from cde_exporter.cad.reader import dxf_attr, unknown_entity_counts

DEFAULT_LONG_SIDE_PX = 1500
MIN_LONG_SIDE_PX = 64
MAX_LONG_SIDE_PX = 8000
MARGIN_PX = 10
NOT_DRAWN = ("3DSOLID",)  # por decisión: la vista previa no dibuja los sólidos
ALWAYS_REPORTED = ("3DSOLID", "REGION")
_PAINT_CHECK_EVERY = 5000

# (etapa, fracción 0–1 de la etapa, mensaje); y cancelación: lanza si se pidió.
Progress = Callable[[str, float, str], None]
Check = Callable[[], None]


def _noop_progress(stage: str, fraction: float, text: str) -> None:
    return None


def _noop_check() -> None:
    return None


def _configuration() -> config.Configuration:
    return config.Configuration(
        background_policy=config.BackgroundPolicy.WHITE,
        hatch_policy=config.HatchPolicy.SHOW_SOLID,
        text_policy=config.TextPolicy.REPLACE_RECT,
        line_policy=config.LinePolicy.SOLID,
        hatching_timeout=HATCHING_TIMEOUT,
    )


class _PreviewFrontend(_StrictFrontend):
    """Frontend del exportador que no dibuja los 3DSOLID, avisa de cada entidad del primer
    nivel del bloque (para el avance) y deja cancelar entre entidades."""

    def __init__(self, *args, on_entity: Callable[[int], None], **kwargs) -> None:
        super().__init__(*args, omit_unsupported=True, **kwargs)
        self.on_entity = on_entity
        self.depth = 0

    def draw_entity(self, entity, properties) -> None:
        self.on_entity(self.depth)
        if entity.dxftype() in NOT_DRAWN:
            original = getattr(entity, "source_of_copy", None) or entity
            self.omitted[(entity.dxftype(), str(dxf_attr(original, "layer", "0") or "0"))] += 1
            return
        self.depth += 1
        try:
            super().draw_entity(entity, properties)
        finally:
            self.depth -= 1


def _show_hidden_layers(ctx: RenderContext, document, layers: frozenset[str] | None) -> None:
    """Hace visibles, solo en este dibujo, las capas apagadas o congeladas pedidas (sin
    mayúsculas). El documento no cambia."""
    if not layers:
        return
    for layer in document.layers:
        name = str(layer.dxf.name)
        if name.casefold() in layers and layer_state(layer):
            properties = ctx.layers.get(layer_key(name))
            if properties is not None:
                properties.is_visible = True


def draw_reference(
    document,
    insert,
    *,
    hidden_layers: frozenset[str] | None = None,
    progress: Progress = _noop_progress,
    check: Check = _noop_check,
    plot_style=None,
    print_transparency: bool = True,
):
    """Graba el dibujo de ``insert`` (y nada más del Model Space). Devuelve (player, omitidas
    por tipo)."""
    total = max(1, len(document.blocks[insert.dxf.name]) + len(insert.attribs))
    done = 0

    def on_entity(depth: int) -> None:
        nonlocal done
        check()
        if depth == 1:  # entidades del primer nivel del bloque (también las reutilizadas)
            done += 1
            progress("draw", done / total, f"Dibujando «{insert.dxf.name}»: {done} de {total}")

    backend = Recorder()
    ctx = RenderContext(document)
    _show_hidden_layers(ctx, document, hidden_layers)
    properties = LayoutProperties.from_layout(document.modelspace())
    properties.set_colors("#ffffff")
    frontend = _PreviewFrontend(
        ctx,
        backend,
        config=_configuration(),
        on_entity=on_entity,
        plot_style=plot_style,
        print_transparency=print_transparency,
    )
    frontend.xobjects = frontend.instancing and xobjects_enabled()
    with contextlib.redirect_stdout(io.StringIO()):
        frontend.draw_layout(
            document.modelspace(),
            layout_properties=properties,
            filter_func=lambda entity: entity is insert,
        )
    omitted: Counter = Counter({kind: 0 for kind in ALWAYS_REPORTED})
    for (kind, _layer), count in frontend.omitted.items():
        omitted[kind] += count
    # Tipos de complementos que ezdxf no lee, por definición de bloque (como el aviso de cada
    # paquete): ezdxf ya los descarta al abrir los INSERT anidados.
    for kind, count in unknown_entity_counts(document, insert.dxf.name).items():
        omitted[kind] += count
    progress("draw", 1.0, f"Dibujado «{insert.dxf.name}»")
    return backend.player(), dict(sorted(omitted.items()))


# ----------------------------------------------------------------- pintado


def _rgba(color: str) -> tuple[int, int, int, int]:
    alpha = int(color[7:9], 16) if len(color) >= 9 else 255
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16), alpha


@dataclass
class _Geometry:
    """Figuras seguidas de la grabación: todos sus puntos en un arreglo N×3 (con columna de 1)
    y, por figura, (tipo, color, tramos [(inicio, fin), …])."""

    points: list = field(default_factory=list)
    items: list = field(default_factory=list)
    array: np.ndarray | None = None


@dataclass
class PreviewImage:
    image: Image.Image
    bounds: tuple[tuple[float, float], tuple[float, float]] | None
    cad_to_px: list[float] | None


def _tolerance(player, long_side_px: int, extent_hint: float | None) -> float:
    """Medio píxel en unidades CAD, para aplanar curvas. Con la caja de ``load`` como pista; sin
    ella, con la caja que calcula ezdxf (más lenta con muchos bloques reutilizados)."""
    extent = extent_hint
    if not extent or extent <= 0:
        box = player.bbox()
        extent = max(box.size.x, box.size.y) if box.has_data else 0.0
    if extent <= 0:
        return 1e-6
    return 0.5 * extent / max(1, long_side_px - 2 * MARGIN_PX)


def paint(
    player,
    long_side_px: int,
    check: Check = _noop_check,
    progress: Progress = _noop_progress,
    extent_hint: float | None = None,
    frame: tuple[float, float, float, float] | None = None,
) -> PreviewImage:
    """Pinta la grabación en un PNG RGBA de fondo transparente, ajustado al dibujo.

    ``extent_hint``: lado mayor aproximado del dibujo en unidades CAD (la caja de ``load``),
    solo para elegir con qué detalle se aplanan las curvas.
    ``frame``: (xmin, ymin, xmax, ymax) en unidades CAD; si se da, la imagen se ajusta a esa
    caja en vez de a lo dibujado (lo que queda fuera no se ve).
    """
    tolerance = _tolerance(player, long_side_px, extent_hint)
    colors = {key: _rgba(props.color) for key, props in player.properties.items()}

    def add(geometry: _Geometry, kind: str, color, polylines) -> None:
        spans = []
        for vertices in polylines:
            start = len(geometry.points)
            geometry.points.extend((v.x, v.y, 1.0) for v in vertices)
            spans.append((start, len(geometry.points)))
        if spans:
            geometry.items.append((kind, color, spans))

    def collect(records) -> list:
        """Partes en el orden de la grabación: figuras seguidas (_Geometry) o marcas de
        bloques reutilizados (InstanceRecord)."""
        parts: list = []
        geometry = _Geometry()
        for record in records:
            if isinstance(record, InstanceRecord):
                if geometry.points:
                    geometry.array = np.array(geometry.points)
                    parts.append(geometry)
                    geometry = _Geometry()
                parts.append(record)
                continue
            color = colors.get(record.property_hash, (0, 0, 0, 255))
            if isinstance(record, PathRecord):
                vertices = list(record.path.flattening(tolerance))
                if vertices:
                    add(geometry, "line", color, [vertices])
            elif isinstance(record, FilledPathsRecord):
                rings = [list(path.flattening(tolerance)) for path in record.paths]
                rings = [ring for ring in rings if len(ring) > 2]
                if rings:
                    add(geometry, "fill", color, rings)
            elif isinstance(record, PointsRecord):
                vertices = list(record.points.vertices())
                if len(vertices) > 2:
                    add(geometry, "fill", color, [vertices])
                elif vertices:
                    add(geometry, "line", color, [vertices])
            elif isinstance(record, SolidLinesRecord):
                vertices = list(record.lines.vertices())
                pairs = [list(pair) for pair in zip(vertices[0::2], vertices[1::2], strict=False)]
                if pairs:
                    add(geometry, "lines", color, pairs)
        if geometry.points:
            geometry.array = np.array(geometry.points)
            parts.append(geometry)
        return parts

    # 1. Cada conjunto de figuras con su colocación (los bloques reutilizados, una vez por marca)
    #    y sus puntos en coordenadas CAD (el marco único).
    placed: list[tuple[_Geometry, np.ndarray]] = []
    # Clave: la identidad de la grabación; el valor la guarda para que su ``id`` no se reutilice.
    instances: dict[int, tuple[object, list]] = {}

    def place(parts: list, matrix: np.ndarray | None) -> None:
        for part in parts:
            if isinstance(part, _Geometry):
                points = part.array if matrix is None else part.array @ matrix
                placed.append((part, points[:, :2]))
                continue
            key = id(part.instance)
            if key not in instances:
                instances[key] = (part.instance, collect(part.instance.records))
            inner = instances[key][1]
            local = np.array(part.matrix.get_2d_transformation()).reshape(3, 3)
            place(inner, local if matrix is None else local @ matrix)
            if len(placed) % 500 == 0:
                check()

    place(collect(player.records), None)
    progress("paint", 0.3, "Preparando la vista previa")
    if not placed and frame is None:
        return PreviewImage(Image.new("RGBA", (1, 1), (0, 0, 0, 0)), None, None)

    # 2. Caja de lo dibujado (o la pedida) y matriz CAD → píxeles.
    if frame is not None:
        xmin, ymin, xmax, ymax = (float(value) for value in frame)
    else:
        xmin = min(float(points[:, 0].min()) for _g, points in placed)
        ymin = min(float(points[:, 1].min()) for _g, points in placed)
        xmax = max(float(points[:, 0].max()) for _g, points in placed)
        ymax = max(float(points[:, 1].max()) for _g, points in placed)
    margin = min(MARGIN_PX, long_side_px // 20)
    width, height = xmax - xmin, ymax - ymin
    extent = max(width, height)
    scale = (long_side_px - 2 * margin) / extent if extent > 0 else 1.0
    size = (
        min(long_side_px, math.ceil(width * scale) + 2 * margin),
        min(long_side_px, math.ceil(height * scale) + 2 * margin),
    )
    # Centrado en la imagen: px = s·(x − xmin) + ox ; py = alto − (s·(y − ymin) + oy).
    e = (size[0] - width * scale) / 2 - xmin * scale
    f = size[1] - (size[1] - height * scale) / 2 + ymin * scale
    cad_to_px = [scale, 0.0, 0.0, -scale, e, f]
    # Pillow redondea cada coordenada al píxel más cercano: se resta medio píxel para que un
    # punto caiga en el píxel que lo contiene (el píxel (0, 0) va de 0 a 1).
    factor = np.array([scale, -scale])
    shift = np.array([e - 0.5, f - 0.5])

    # 3. Pintado, en el orden de la grabación.
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def fill_even_odd(rings: list[list[float]], color) -> None:
        """Relleno con huecos (regla par-impar): cada anillo invierte la máscara."""
        xs = [value for ring in rings for value in ring[0::2]]
        ys = [value for ring in rings for value in ring[1::2]]
        left, top = max(0, math.floor(min(xs))), max(0, math.floor(min(ys)))
        right, bottom = min(size[0], math.ceil(max(xs)) + 2), min(size[1], math.ceil(max(ys)) + 2)
        if right <= left or bottom <= top:
            return
        mask = Image.new("1", (right - left, bottom - top), 0)
        for ring in rings:
            layer = Image.new("1", mask.size, 0)
            shifted = [value - (left if i % 2 == 0 else top) for i, value in enumerate(ring)]
            ImageDraw.Draw(layer).polygon(shifted, fill=1)
            mask = ImageChops.logical_xor(mask, layer)
        image.paste(color, (left, top, right, bottom), mask)

    total = len(placed)
    for number, (geometry, points) in enumerate(placed, start=1):
        pixels = (points * factor + shift).ravel().tolist()
        for index, (kind, color, spans) in enumerate(geometry.items):
            if index % _PAINT_CHECK_EVERY == 0:
                check()
            flats = [pixels[2 * start : 2 * end] for start, end in spans]
            if kind == "fill":
                if len(flats) == 1:
                    draw.polygon(flats[0], fill=color)
                else:
                    fill_even_odd(flats, color)
                continue
            for flat in flats:
                if len(flat) == 2:
                    draw.point(flat, fill=color)
                else:
                    draw.line(flat, fill=color, width=1)
        if number % 500 == 0:
            check()
            progress("paint", 0.3 + 0.7 * number / total, "Pintando la vista previa")
    progress("paint", 1.0, "Vista previa pintada")
    return PreviewImage(
        image, ((xmin, ymin), (xmax, ymax)), [round(value, 9) for value in cad_to_px]
    )


# ----------------------------------------------------------------- caché


def layers_tag(hidden_layers: frozenset[str] | None, all_layers: bool) -> str:
    """``visible``, ``hidden-all`` o ``hidden-<6 cifras>`` (resumen de las capas pedidas)."""
    if not hidden_layers:
        return "visible"
    if all_layers:
        return "hidden-all"
    digest = hashlib.sha1("\n".join(sorted(hidden_layers)).encode("utf-8")).hexdigest()
    return f"hidden-{digest[:6]}"


def drawing_tag(layers: str, plot_style=None, print_transparency: bool = True) -> str:
    """Etiqueta de caché: la de las capas más la CTB y la transparencia si se piden
    (sin ellas, la de siempre: las vistas previas ya guardadas siguen valiendo). Corta a
    propósito: la ruta completa del PNG no puede pasar de 260 caracteres en Windows."""
    tag = layers
    if plot_style is not None:
        tag += f"_p{plot_style.sha256[:6]}"
    if not print_transparency:
        tag += "_t"
    return tag


class PreviewCache:
    """PNG + JSON por vista previa; se borran las usadas hace más tiempo al pasar del límite."""

    def __init__(self, folder: Path, limit_mb: float) -> None:
        self.folder = folder
        self.limit_bytes = max(0.0, float(limit_mb)) * 2**20

    def paths(self, fingerprint: str, handle: str, px: int, tag: str) -> tuple[Path, Path]:
        stem = self.folder / fingerprint / f"{handle}_{px}_{tag}"
        return stem.with_suffix(".png"), stem.with_suffix(".json")

    def get(self, png: Path, meta: Path) -> dict | None:
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not png.is_file():
            return None
        now = time.time()
        for path in (png, meta):
            with contextlib.suppress(OSError):
                os.utime(path, (now, now))  # «usada ahora»: la última en borrarse
        return data

    def put(self, png: Path, meta: Path, image: Image.Image, data: dict) -> None:
        png.parent.mkdir(parents=True, exist_ok=True)
        temporary = png.with_name(f".cde-tmp-{os.getpid()}-{png.name}")
        image.save(temporary, format="PNG")
        os.replace(temporary, png)
        temporary = meta.with_name(f".cde-tmp-{os.getpid()}-{meta.name}")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, meta)  # el JSON, el último: con él la entrada está completa

    def trim(self, keep: tuple[Path, ...] = ()) -> int:
        """Borra las entradas usadas hace más tiempo hasta quedar dentro del límite (nunca las
        de ``keep``). Devuelve cuántos archivos borró."""
        if not self.folder.is_dir():
            return 0
        files = []
        for path in self.folder.rglob("*"):
            if path.is_file():
                with contextlib.suppress(OSError):
                    stat = path.stat()
                    files.append((stat.st_mtime, stat.st_size, path))
        total = sum(size for _mtime, size, _path in files)
        kept = {path.resolve() for path in keep}
        removed = 0
        for _mtime, size, path in sorted(files, key=lambda item: item[0]):
            if total <= self.limit_bytes:
                break
            if path.resolve() in kept:
                continue
            with contextlib.suppress(OSError):
                path.unlink()
                total -= size
                removed += 1
        for folder in self.folder.iterdir():
            if folder.is_dir() and not any(folder.iterdir()):
                with contextlib.suppress(OSError):
                    folder.rmdir()
        return removed
