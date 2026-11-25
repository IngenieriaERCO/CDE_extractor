"""Vista previa preparada del Model Space (protocolo 1.2, docs/protocolo_trabajador.md, 4.2).

Medido con PRGD: una vista lejana tardaba 41–45 s en dibujarse con ezdxf cada vez (25 822
entidades, 159 000 trazos), aunque casi todo mide menos de un píxel. Aquí el Model Space se
dibuja **una sola vez** por sesión, en segundo plano, y cada vista solo filtra y pinta:

1. ``Preparation``: en un hilo aparte, calcula las cajas del Model Space (las usan también las
   zonas) y graba todo el Model Space con el frontend de la vista previa (sin reutilizar
   bloques: cada trazo queda en coordenadas CAD), en el orden de dibujo del DXF. Cede el turno
   a los pedidos antes de cada entidad, a cualquier profundidad (``Gate``): ezdxf nunca dibuja
   en dos hilos a la vez y un pedido espera como mucho lo que tarda una entidad. Cada entidad
   del Model Space dibujada se indexa enseguida y su grabación se descarta (memoria).
2. Índice: cada trazo aplanado (curvas con una tolerancia relativa a su tamaño), con su caja,
   color, tipo y la «cadena de capas» de la que depende su visibilidad (la de la entidad del
   Model Space y la de cada entidad anidada que no está en la capa «0»; la «0» toma la del
   INSERT). Todo en arreglos de numpy (``PreviewIndex``).
3. ``paint_index``: los trazos cuya caja toca la vista y cuya cadena de capas se ve (filtro
   vectorizado), pintados con Pillow en el orden de dibujo. **Menos detalle de lejos:** un
   trazo que en la vista mide menos de un píxel se pinta como un punto (decisión de Luis Angel:
   se acepta perder detalle diminuto en vistas lejanas).

La preparación es para una configuración (tabla CTB y transparencia al imprimir) y sin capas
ocultas del CAD; un pedido con otra configuración o con ``include_hidden_layers`` se dibuja
como antes (``worker_zone.draw_zone_preview``).
"""

from __future__ import annotations

import contextlib
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
from ezdxf.addons.drawing import RenderContext
from ezdxf.addons.drawing.properties import LayoutProperties
from ezdxf.addons.drawing.recorder import (
    FilledPathsRecord,
    PathRecord,
    PointsRecord,
    Recorder,
    SolidLinesRecord,
)
from PIL import Image, ImageChops, ImageDraw

from cde_exporter.cad.draw_order import drawing_sequence
from cde_exporter.cad.preview import (
    MARGIN_PX,
    PreviewImage,
    _configuration,
    _PreviewFrontend,
    _rgba,
)
from cde_exporter.cad.reader import dxf_attr
from cde_exporter.cad.run_hooks import DrawingCancelled
from cde_exporter.cad.zone_preview import boxes_for

LINE, FILL, PAIRS = 0, 1, 2
_FLATTEN_RELATIVE = 0.002  # tolerancia al aplanar: 0,2 % del tamaño del trazo
LOD_PIXELS = 1.0  # menos de un píxel en la vista: se pinta como un punto

# (etapa, fracción 0–1, mensaje)
Progress = Callable[[str, float, str], None]


# ----------------------------------------------------------------- grabación con capas


class _TaggingFrontend(_PreviewFrontend):
    """Frontend de la vista previa que anota, por trazo grabado, la cadena de capas de la
    que depende (la de la entidad del Model Space y la de cada anidada que no está en «0»).

    ``checkpoint`` se llama antes de cada entidad, a cualquier profundidad (la preparación se
    pausa ahí si llega un pedido); ``flush`` al terminar cada entidad del Model Space (sus
    trazos ya tienen capas: se indexan y se descartan, para no guardar toda la grabación)."""

    def __init__(self, *args, records: list, checkpoint, flush, **kwargs) -> None:
        super().__init__(*args, on_entity=lambda depth: None, **kwargs)
        self.records = records
        self.tags: list[int | None] = []
        self.chain: list[str] = []
        self.chains: dict[tuple[str, ...], int] = {}
        self.checkpoint = checkpoint
        self.flush = flush

    def draw_entity(self, entity, properties) -> None:
        self.checkpoint()
        original = getattr(entity, "source_of_copy", None) or entity
        layer = str(dxf_attr(original, "layer", "0") or "0")
        top = self.depth == 0
        pushed = top or layer != "0"
        if pushed:
            self.chain.append(layer)
        start = len(self.records)
        try:
            super().draw_entity(entity, properties)
        finally:
            key = self.chains.setdefault(tuple(self.chain), len(self.chains))
            tags = self.tags
            tags.extend([None] * (len(self.records) - len(tags)))
            for index in range(start, len(tags)):
                if tags[index] is None:
                    tags[index] = key
            if pushed:
                self.chain.pop()
        if top:
            self.flush()


# ----------------------------------------------------------------- índice


@dataclass
class PreviewIndex:
    """Trazos del Model Space aplanados, en el orden de dibujo, con su caja y capas."""

    points: np.ndarray  # (P, 2) coordenadas CAD
    span_start: np.ndarray  # (S + 1,) primer punto de cada tramo
    item_span: np.ndarray  # (K + 1,) primer tramo de cada trazo
    kind: np.ndarray  # (K,) LINE, FILL o PAIRS
    color: np.ndarray  # (K,) índice en ``colors``
    chain: np.ndarray  # (K,) índice en ``chains``
    box: np.ndarray  # (K, 4) xmin, ymin, xmax, ymax
    colors: list[tuple[int, int, int, int]]
    chains: list[tuple[str, ...]]
    extent: tuple[float, float, float, float] | None
    plot_style: object = None
    print_transparency: bool = True
    omitted: dict = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def items(self) -> int:
        return len(self.kind)

    @property
    def nbytes(self) -> int:
        arrays = (self.points, self.span_start, self.item_span, self.kind, self.color, self.chain)
        return int(sum(array.nbytes for array in arrays) + self.box.nbytes)

    def describe(self) -> dict:
        return {
            "items": self.items,
            "points": int(len(self.points)),
            "memory_mb": round(self.nbytes / 2**20, 1),
            "seconds": round(self.seconds, 1),
            "plot_style": None if self.plot_style is None else self.plot_style.describe(),
            "print_transparency": self.print_transparency,
        }


class _Builder:
    """Acumula trazos (en listas) y al final los pasa a arreglos de numpy."""

    def __init__(self) -> None:
        self.points: list[np.ndarray] = []
        self.count = 0
        self.span_start = [0]
        self.item_span = [0]
        self.kind: list[int] = []
        self.color: list[int] = []
        self.chain: list[int] = []
        self.box: list[tuple[float, float, float, float]] = []
        self.colors: dict[tuple[int, int, int, int], int] = {}

    def add(self, kind: int, rgba, chain: int, polylines: list[np.ndarray]) -> None:
        polylines = [line for line in polylines if len(line)]
        if not polylines:
            return
        for line in polylines:
            self.points.append(line)
            self.count += len(line)
            self.span_start.append(self.count)
        self.item_span.append(len(self.span_start) - 1)
        joined = np.concatenate(polylines) if len(polylines) > 1 else polylines[0]
        mins, maxs = joined.min(axis=0), joined.max(axis=0)
        self.box.append((float(mins[0]), float(mins[1]), float(maxs[0]), float(maxs[1])))
        self.kind.append(kind)
        self.color.append(self.colors.setdefault(rgba, len(self.colors)))
        self.chain.append(chain)

    def finish(self, chains: dict, plot_style, print_transparency, omitted) -> PreviewIndex:
        box = np.array(self.box, dtype=np.float64).reshape(-1, 4)
        extent = None
        if len(box):
            extent = (
                float(box[:, 0].min()),
                float(box[:, 1].min()),
                float(box[:, 2].max()),
                float(box[:, 3].max()),
            )
        ordered_chains = [chain for chain, _ in sorted(chains.items(), key=lambda kv: kv[1])]
        return PreviewIndex(
            points=np.concatenate(self.points) if self.points else np.zeros((0, 2)),
            span_start=np.array(self.span_start, dtype=np.int64),
            item_span=np.array(self.item_span, dtype=np.int64),
            kind=np.array(self.kind, dtype=np.uint8),
            color=np.array(self.color, dtype=np.uint32),
            chain=np.array(self.chain, dtype=np.uint32),
            box=box,
            colors=[rgba for rgba, _ in sorted(self.colors.items(), key=lambda kv: kv[1])],
            chains=ordered_chains,
            extent=extent,
            plot_style=plot_style,
            print_transparency=print_transparency,
            omitted=omitted,
        )


def _xy(vertices) -> np.ndarray:
    return np.array([(v.x, v.y) for v in vertices], dtype=np.float64).reshape(-1, 2)


def _tolerance(record) -> float:
    box = record.bbox()
    if not box.has_data:
        return 1e-6
    return max(_FLATTEN_RELATIVE * max(box.size.x, box.size.y), 1e-9)


def _add_record(builder: _Builder, record, rgba, chain: int) -> None:
    if isinstance(record, PathRecord):
        builder.add(LINE, rgba, chain, [_xy(record.path.flattening(_tolerance(record)))])
    elif isinstance(record, FilledPathsRecord):
        tolerance = _tolerance(record)
        rings = [_xy(path.flattening(tolerance)) for path in record.paths]
        builder.add(FILL, rgba, chain, [ring for ring in rings if len(ring) > 2])
    elif isinstance(record, PointsRecord):
        vertices = _xy(record.points.vertices())
        builder.add(FILL if len(vertices) > 2 else LINE, rgba, chain, [vertices])
    elif isinstance(record, SolidLinesRecord):
        vertices = _xy(record.lines.vertices())
        pairs = [vertices[i : i + 2] for i in range(0, len(vertices) - 1, 2)]
        builder.add(PAIRS, rgba, chain, pairs)


class PreparationStopped(DrawingCancelled):
    """La preparación se detuvo (otro ``load``, ``close`` o fin de stdin). Es una cancelación:
    el frontend, ``safe_extents`` y las cajas nunca la toman por datos inválidos."""


class Gate:
    """Turno entre la preparación (hilo aparte) y los pedidos (hilo principal): nunca dibujan
    los dos a la vez con el mismo DXF (ezdxf no lo admite). La preparación tiene el turno y lo
    cede en ``checkpoint`` (antes de cada entidad, a cualquier profundidad) en cuanto un
    pedido lo pide con ``turn``; lo recupera cuando el pedido termina."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wanted = threading.Event()

    @contextlib.contextmanager
    def turn(self):
        """Para el hilo principal: espera (como mucho, lo que tarda una entidad) y atiende."""
        self._wanted.set()
        with self._lock:
            try:
                yield
            finally:
                self._wanted.clear()

    def hold(self) -> None:
        self._lock.acquire()

    def release(self) -> None:
        self._lock.release()

    def checkpoint(self, stop: threading.Event) -> None:
        if self._wanted.is_set():
            self._lock.release()
            while self._wanted.is_set() and not stop.is_set():
                time.sleep(0.005)
            self._lock.acquire()
        if stop.is_set():
            raise PreparationStopped()


class Preparation:
    """Preparación de la vista previa de un DXF cargado, para una configuración (CTB y
    transparencia). ``start`` la lanza en un hilo aparte que cede el turno a los pedidos
    (``Gate``); ``build`` la hace en el hilo que llama (pruebas y línea de órdenes).

    ``progress(etapa, fracción, mensaje)`` se llama como mucho cada ``PROGRESS_SECONDS``;
    etapas: ``boxes`` (cajas del Model Space, también para las zonas), ``draw`` e ``index``.
    Al terminar, ``state`` es ``ready`` (``index`` listo), ``failed`` (``error``) o
    ``stopped``, y se llama ``on_done(self)``."""

    PROGRESS_SECONDS = 0.5

    def __init__(
        self,
        document,
        *,
        plot_style=None,
        print_transparency: bool = True,
        progress: Progress | None = None,
        on_done: Callable[[Preparation], None] | None = None,
        boxes_target=None,
    ) -> None:
        self.document = document
        self.plot_style = plot_style
        self.print_transparency = print_transparency
        self.progress = progress
        self.on_done = on_done
        # Objeto con ``modelspace_boxes`` (la sesión del trabajador): se rellena si falta.
        self.boxes_target = boxes_target
        self.index: PreviewIndex | None = None
        self.state = "running"
        self.stage = "boxes"
        self.percent = 0.0
        self.error: str | None = None
        self.stop = threading.Event()
        self.gate: Gate | None = None
        self.thread: threading.Thread | None = None
        self._last = 0.0

    def matches(self, plot_style, print_transparency: bool) -> bool:
        """Si sirve para un pedido con esa CTB y esa transparencia."""
        mine = None if self.plot_style is None else self.plot_style.sha256
        theirs = None if plot_style is None else plot_style.sha256
        return mine == theirs and self.print_transparency == print_transparency

    def describe(self) -> dict:
        return {
            "state": self.state,
            "stage": self.stage if self.state == "running" else None,
            "percent": round(self.percent, 1),
            "plot_style": None if self.plot_style is None else self.plot_style.describe(),
            "print_transparency": self.print_transparency,
            "error": self.error,
        }

    # -- en un hilo aparte

    def start(self, gate: Gate) -> None:
        self.gate = gate
        self.thread = threading.Thread(target=self._thread, name="cde-preview", daemon=True)
        self.thread.start()

    def _thread(self) -> None:
        assert self.gate is not None
        self.gate.hold()
        try:
            self.build()
        except PreparationStopped:
            self.state = "stopped"
        except Exception as error:  # noqa: BLE001 - la preparación nunca tumba el trabajador
            self.state = "failed"
            self.error = f"{type(error).__name__}: {error}"
        finally:
            self.gate.release()
            if self.on_done is not None:
                self.on_done(self)

    def _checkpoint(self) -> None:
        if self.gate is not None:
            self.gate.checkpoint(self.stop)
        elif self.stop.is_set():
            raise PreparationStopped()

    def _report(self, stage: str, fraction: float, text: str, force: bool = False) -> None:
        weights = {"boxes": (0.0, 0.25), "draw": (0.25, 0.70), "index": (0.95, 0.05)}
        start, weight = weights[stage]
        self.stage = stage
        self.percent = max(self.percent, 100 * (start + weight * min(max(fraction, 0.0), 1.0)))
        now = time.perf_counter()
        if self.progress is not None and (force or now - self._last >= self.PROGRESS_SECONDS):
            self._last = now
            self.progress(stage, self.percent, text)

    # -- el trabajo

    def build(self) -> PreviewIndex:
        started = time.perf_counter()
        document = self.document
        target = self.boxes_target
        if target is not None and getattr(target, "modelspace_boxes", None) is None:
            self._report("boxes", 0.0, "Calculando las cajas del Model Space", force=True)
            boxes_for(target, document, self._checkpoint)
        self._report("boxes", 1.0, "Cajas del Model Space calculadas", force=True)
        backend = Recorder()
        records = backend.records
        builder = _Builder()

        def flush() -> None:
            for record, tag in zip(records, frontend.tags, strict=False):
                rgba = colors.get(record.property_hash)
                if rgba is None:
                    rgba = colors[record.property_hash] = _rgba(
                        backend.properties[record.property_hash].color
                    )
                _add_record(builder, record, rgba, tag if tag is not None else 0)
            records.clear()
            frontend.tags.clear()

        colors: dict[int, tuple[int, int, int, int]] = {}
        ctx = RenderContext(document)
        properties = LayoutProperties.from_layout(document.modelspace())
        properties.set_colors("#ffffff")
        frontend = _TaggingFrontend(
            ctx,
            backend,
            config=_configuration(),
            records=records,
            checkpoint=self._checkpoint,
            flush=flush,
            plot_style=self.plot_style,
            print_transparency=self.print_transparency,
        )
        # Sin reutilizar bloques: cada trazo, en coordenadas CAD (y con sus capas).
        frontend.instancing = False
        frontend.xobjects = False
        ctx.current_layout_properties = properties
        frontend.set_background(properties.background_color)
        frontend.parent_stack = []
        entities = drawing_sequence(document.modelspace())
        total = max(1, len(entities))
        for position, entity in enumerate(entities, start=1):
            frontend.draw_entities([entity])
            self._report(
                "draw", position / total, f"Preparando la vista previa: {position} de {total}"
            )
        frontend.pipeline.finalize()
        flush()
        self._report("index", 0.5, "Indexando la vista previa", force=True)
        omitted: dict = {}
        for (kind, _layer), count in frontend.omitted.items():
            omitted[kind] = omitted.get(kind, 0) + count
        if not frontend.chains:
            frontend.chains[()] = 0
        index = builder.finish(frontend.chains, self.plot_style, self.print_transparency, omitted)
        index.seconds = time.perf_counter() - started
        self.index = index
        self.state = "ready"
        self._report("index", 1.0, "Vista previa preparada", force=True)
        return index


# ----------------------------------------------------------------- pintado de una vista


def visible_chains(index: PreviewIndex, visible: Callable[[str], bool]) -> np.ndarray:
    """Máscara (por cadena) de las que se ven: todas sus capas se ven."""
    return np.array([all(visible(layer) for layer in chain) for chain in index.chains], bool)


def paint_index(
    index: PreviewIndex,
    frame: tuple[float, float, float, float],
    long_side_px: int,
    visible: Callable[[str], bool] | None = None,
    check: Callable[[], None] | None = None,
) -> tuple[PreviewImage, dict]:
    """Pinta la vista ``frame`` (coordenadas CAD) desde el índice: misma imagen y misma
    ``cad_to_px`` que ``preview.paint(..., frame=frame)``, con menos detalle de lejos."""
    xmin, ymin, xmax, ymax = (float(value) for value in frame)
    margin = min(MARGIN_PX, long_side_px // 20)
    width, height = xmax - xmin, ymax - ymin
    extent = max(width, height)
    scale = (long_side_px - 2 * margin) / extent if extent > 0 else 1.0
    size = (
        min(long_side_px, math.ceil(width * scale) + 2 * margin),
        min(long_side_px, math.ceil(height * scale) + 2 * margin),
    )
    e = (size[0] - width * scale) / 2 - xmin * scale
    f = size[1] - (size[1] - height * scale) / 2 + ymin * scale
    cad_to_px = [scale, 0.0, 0.0, -scale, e, f]
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    stats = {"items": 0, "dots": 0}
    if index.items == 0:
        return PreviewImage(image, ((xmin, ymin), (xmax, ymax)), _round(cad_to_px)), stats
    # Una caja de un píxel de holgura (las líneas tienen grosor de un píxel).
    pad = 1.0 / scale if scale > 0 else 0.0
    box = index.box
    mask = (
        (box[:, 2] >= xmin - pad)
        & (box[:, 0] <= xmax + pad)
        & (box[:, 3] >= ymin - pad)
        & (box[:, 1] <= ymax + pad)
    )
    if visible is not None:
        mask &= visible_chains(index, visible)[index.chain]
    chosen = np.nonzero(mask)[0]
    sizes = np.maximum(box[chosen, 2] - box[chosen, 0], box[chosen, 3] - box[chosen, 1]) * scale
    tiny = sizes < LOD_PIXELS
    draw = ImageDraw.Draw(image)
    factor = np.array([scale, -scale])
    shift = np.array([e - 0.5, f - 0.5])
    # 1. Lo diminuto, como puntos (por color), debajo de lo demás.
    dots = chosen[tiny]
    if len(dots):
        centers = np.column_stack(
            ((box[dots, 0] + box[dots, 2]) / 2, (box[dots, 1] + box[dots, 3]) / 2)
        )
        pixels = np.floor(centers * factor + shift + 0.5).astype(np.int64)
        colors = index.color[dots]
        for color in np.unique(colors):
            same = pixels[colors == color]
            draw.point([tuple(p) for p in np.unique(same, axis=0)], fill=index.colors[color])
    # 2. Lo demás, en el orden de dibujo.
    rest = chosen[~tiny]
    for number, item in enumerate(rest):
        if check is not None and number % 5000 == 0:
            check()
        first, last = index.item_span[item], index.item_span[item + 1]
        rgba = index.colors[index.color[item]]
        flats = []
        for span in range(first, last):
            points = index.points[index.span_start[span] : index.span_start[span + 1]]
            flats.append((points * factor + shift).ravel().tolist())
        if index.kind[item] == FILL:
            if len(flats) == 1:
                draw.polygon(flats[0], fill=rgba)
            else:
                _fill_even_odd(image, flats, rgba)
            continue
        for flat in flats:
            if len(flat) == 2:
                draw.point(flat, fill=rgba)
            else:
                draw.line(flat, fill=rgba, width=1)
    stats = {"items": int(len(chosen)), "dots": int(len(dots))}
    return PreviewImage(image, ((xmin, ymin), (xmax, ymax)), _round(cad_to_px)), stats


def _round(values: list[float]) -> list[float]:
    return [round(value, 9) for value in values]


def _fill_even_odd(image: Image.Image, rings: list[list[float]], color) -> None:
    """Relleno con huecos (regla par-impar), como ``preview.paint``."""
    xs = [value for ring in rings for value in ring[0::2]]
    ys = [value for ring in rings for value in ring[1::2]]
    left, top = max(0, math.floor(min(xs))), max(0, math.floor(min(ys)))
    right = min(image.size[0], math.ceil(max(xs)) + 2)
    bottom = min(image.size[1], math.ceil(max(ys)) + 2)
    if right <= left or bottom <= top:
        return
    mask = Image.new("1", (right - left, bottom - top), 0)
    for ring in rings:
        layer = Image.new("1", mask.size, 0)
        shifted = [value - (left if i % 2 == 0 else top) for i, value in enumerate(ring)]
        ImageDraw.Draw(layer).polygon(shifted, fill=1)
        mask = ImageChops.logical_xor(mask, layer)
    image.paste(color, (left, top, right, bottom), mask)
