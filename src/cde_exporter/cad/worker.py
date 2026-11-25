"""Trabajador persistente (``cde-exporter worker``): lee el DXF una vez y atiende pedidos en
JSON por líneas (docs/protocolo_trabajador.md).

- Un hilo lee stdin: ``status``, ``cancel`` y ``close`` se atienden ahí mismo, siempre y enseguida
  (también mientras el hilo principal está dentro de una operación larga de ezdxf); cualquier
  otro pedido se encola, o recibe ``BUSY`` si ya hay uno en curso.
- El hilo principal ejecuta los pedidos de uno en uno.
- stdout solo lleva JSON en UTF-8 (``_Output``); lo que el exportador escribe en la terminal y el
  registro de las librerías van a stderr.

Etapas hechas: 1 (arranque, ``load``, ``status``, ``cancel`` y ``close``), 2 (``preview`` con
caché) y 3 (``export``, en ``worker_export``). Protocolo 1.1: zonas del Model Space en
``preview`` y ``export`` (``worker_zone``). Protocolo 1.2: vista previa preparada en segundo
plano tras ``load`` (``preview_index``), con eventos de avance y de «lista»; la preparación va
en un hilo aparte que cede el turno a cada pedido (``Gate``: nunca dibujan los dos a la vez).
Protocolo 1.3: ``texts``, los textos de una zona y la escala que proponen (``worker_zone``).
"""

from __future__ import annotations

import ctypes
import logging
import math
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from cde_exporter import __version__
from cde_exporter.cad import worker_protocol as protocol
from cde_exporter.cad.dxf_session import (
    DxfTooLargeError,
    LoadedDxf,
    clean_temporaries,
    load_dxf,
)
from cde_exporter.cad.exporters import DrawingCancelled
from cde_exporter.cad.inventory import build_inventory
from cde_exporter.cad.manifest import SCHEMA_VERSION
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.notices import UNKNOWN_ENTITIES_ADVICE
from cde_exporter.cad.preview import (
    DEFAULT_LONG_SIDE_PX,
    MAX_LONG_SIDE_PX,
    MIN_LONG_SIDE_PX,
    PreviewCache,
    draw_reference,
    drawing_tag,
    layers_tag,
    paint,
)
from cde_exporter.cad.preview_index import Gate, Preparation
from cde_exporter.cad.reader import unknown_entities_message
from cde_exporter.cad.scale import OVERRIDE_UNITS
from cde_exporter.cad.worker_protocol import Request, WorkerError

TOO_LARGE_ADVICE = (
    "Cierre otros programas, o reduzca el DXF en AutoCAD (PURGE, borrar lo suelto que no "
    "haga falta) y vuelva a cargarlo."
)
PROGRESS_INTERVAL = 0.25  # segundos entre eventos de avance (como mucho unos 4 por segundo)
LOAD_STAGES = (("read", 0.6), ("hash", 0.05), ("blocks", 0.3), ("layers", 0.05))
PREVIEW_STAGES = (("draw", 0.8), ("paint", 0.2))


class Cancelled(DrawingCancelled):
    """El pedido en curso se canceló (también dentro del dibujo de ezdxf)."""


class _Output:
    """stdout del proceso: solo JSON, en UTF-8, una línea por mensaje y sin mezclar hilos."""

    def __init__(self, stream) -> None:
        self.stream = stream
        self.lock = threading.Lock()
        # stdout cerrado (el Componedor terminó): ya no hay a quién responder; se avisa con
        # ``on_close`` (el trabajador cancela lo que esté haciendo).
        self.closed = threading.Event()
        self.on_close = None

    def send(self, message: dict) -> None:
        data = protocol.encode(message)
        with self.lock:
            if self.closed.is_set():
                return
            try:
                self.stream.write(data)
                self.stream.flush()
            except (OSError, ValueError):
                self.closed.set()
                if self.on_close is not None:
                    self.on_close()


@dataclass
class _Session:
    id: str
    loaded: LoadedDxf
    units: str | None
    margin_mm: float
    inventory: dict
    # Cajas rápidas del Model Space para las zonas (worker_zone.session_boxes): las calcula la
    # preparación de la vista previa o, si se piden antes, la primera zona.
    modelspace_boxes: list | None = None
    boxes_builder: object = None  # cálculo de las cajas a medias (zone_preview.ModelspaceBoxes)
    # Vista previa preparada en segundo plano (preview_index.Preparation), o None.
    preparation: object = None
    # Qué bloques tienen texto, para «texts» (zone_texts.TextBlocks): se rellena al pedirse.
    text_blocks: object = None


class _Progress:
    """Eventos ``progress`` de un pedido: porcentaje total por etapas con peso, solo sube."""

    def __init__(self, output: _Output, request: Request, stages, state: _State) -> None:
        self.output, self.request, self.state = output, request, state
        self.weights = dict(stages)
        self.order = [name for name, _ in stages]
        self.percent = 0.0
        self.last = 0.0
        self.stage = None

    def __call__(self, stage: str, fraction: float, text: str) -> None:
        self.state.check()  # cancelar también durante las pasadas por partes de «load»
        self.state.stage = stage
        index = self.order.index(stage) if stage in self.order else 0
        done = sum(self.weights[name] for name in self.order[:index])
        total = sum(self.weights.values()) or 1
        percent = 100 * (done + self.weights.get(stage, 0) * min(max(fraction, 0.0), 1.0)) / total
        percent = max(percent, self.percent)
        now = time.perf_counter()
        if stage == self.stage and now - self.last < PROGRESS_INTERVAL and fraction < 1.0:
            return
        self.stage, self.last, self.percent = stage, now, percent
        self.output.send(
            protocol.progress(
                self.request.id,
                {
                    "job": None,
                    "job_index": None,
                    "job_total": None,
                    "stage": stage,
                    "stage_index": index + 1,
                    "stage_total": len(self.order),
                    "percent": round(percent, 1),
                    "message": text,
                },
            )
        )


class _State:
    """Lo que comparten los dos hilos: pedido en curso, cancelación y sesión."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current: Request | None = None
        self.cancel = threading.Event()
        self.stage: str | None = None
        self.session: _Session | None = None
        self.sessions = 0

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()


def _memory_mb() -> int | None:
    if os.name != "nt":
        return None

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    function = kernel32.K32GetProcessMemoryInfo
    function.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    if not function(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None
    return round(counters.PagefileUsage / 2**20)


class Worker:
    def __init__(self, output: _Output, work_dir: Path, preview_cache_mb: int) -> None:
        self.output = output
        self.work_dir = work_dir
        self.preview_cache_mb = preview_cache_mb
        self.state = _State()
        output.on_close = self.state.cancel.set
        self.queue: queue.Queue = queue.Queue()
        self.previews = PreviewCache(work_dir / "previews", preview_cache_mb)
        # Turno entre los pedidos y la preparación de la vista previa (preview_index.Gate).
        self.gate = Gate()

    # ------------------------------------------------------------------ hilo de stdin
    def read_requests(self, stream) -> None:
        for raw in stream:
            line = raw.strip()
            if not line:
                continue
            try:
                request = protocol.parse_request(line)
            except WorkerError as failure:
                self.output.send(protocol.error(failure.details.get("id"), failure))
                continue
            if request.command == "status":
                self.output.send(protocol.result(request, self.status()))
                continue
            if request.command == "cancel":
                self.output.send(
                    protocol.result(request, {"accepted": self.request_cancel(request)})
                )
                continue
            if request.command == "close":
                self.state.cancel.set()
                self.stop_preparation()
                self.queue.put(request)
                return
            with self.state.lock:
                if self.state.current is not None:
                    busy = WorkerError(
                        protocol.BUSY,
                        f"El trabajador está atendiendo el pedido «{self.state.current.id}».",
                        "Espere su respuesta o envíe «cancel».",
                        {"current": self.state.current.id},
                    )
                    self.output.send(protocol.error(request.id, busy, request.command))
                    continue
                self.state.current = request
                self.state.cancel.clear()
            self.queue.put(request)
        self.state.cancel.set()
        self.stop_preparation()
        self.queue.put(None)  # fin de stdin: como «close»

    def request_cancel(self, request: Request) -> bool:
        target = request.data.get("target")
        with self.state.lock:
            current = self.state.current
            if current is None or (target is not None and target != current.id):
                return False
            self.state.cancel.set()
            return True

    def status(self) -> dict:
        with self.state.lock:
            current = self.state.current
            session = self.state.session
        preparation = session.preparation if session else None
        return {
            "busy": current is not None,
            "current": current.id if current else None,
            "stage": self.state.stage if current else None,
            "session": session.id if session else None,
            "dxf": session.loaded.path.name if session else None,
            "memory_mb": _memory_mb(),
            "preview_preparation": None if preparation is None else preparation.describe(),
        }

    # ------------------------------------------------------------------ vista previa preparada
    def stop_preparation(self) -> None:
        with self.state.lock:
            session = self.state.session
        if session is not None and session.preparation is not None:
            session.preparation.stop.set()

    def start_preparation(self, session: _Session, plot_style, print_transparency: bool) -> None:
        """Lanza (o relanza con otra configuración) la preparación de la vista previa."""
        if session.preparation is not None:
            session.preparation.stop.set()
        send = self.output.send

        def progress(stage: str, percent: float, text: str) -> None:
            send(
                protocol.event(
                    "preview_progress",
                    {
                        "session": session.id,
                        "stage": stage,
                        "percent": round(percent, 1),
                        "message": text,
                    },
                )
            )

        def done(preparation) -> None:
            if preparation.state == "ready":
                send(
                    protocol.event(
                        "preview_ready",
                        {"session": session.id, **preparation.index.describe()},
                    )
                )
            elif preparation.state == "failed":
                send(
                    protocol.event(
                        "preview_failed",
                        {"session": session.id, "message": preparation.error},
                    )
                )

        preparation = Preparation(
            session.loaded.document,
            plot_style=plot_style,
            print_transparency=print_transparency,
            progress=progress,
            on_done=done,
            boxes_target=session,
        )
        session.preparation = preparation
        preparation.start(self.gate)

    # ------------------------------------------------------------------ hilo principal
    def run(self) -> None:
        while True:
            request = self.queue.get()
            if request is None or self.output.closed.is_set():
                return
            if request.command == "close":
                self.stop_preparation()
                self.output.send(protocol.result(request, {}))
                return
            try:
                # La preparación de la vista previa cede el turno (como mucho, lo que tarda una
                # entidad) y lo recupera al terminar el pedido.
                with self.gate.turn():
                    data = self.dispatch(request)
            except WorkerError as failure:
                self.output.send(protocol.error(request.id, failure, request.command))
            except Cancelled:
                cancelled = WorkerError(protocol.CANCELLED, "Pedido cancelado.")
                self.output.send(protocol.error(request.id, cancelled, request.command))
            except Exception as failure:  # noqa: BLE001 - nunca se cae el trabajador
                details = {"type": type(failure).__name__}
                if os.environ.get("CDE_DEBUG"):
                    details["traceback"] = traceback.format_exc()
                internal = WorkerError(
                    protocol.INTERNAL,
                    f"Error inesperado del exportador ({type(failure).__name__}): {failure}",
                    "Repita el pedido; si vuelve a fallar, guarde el registro de stderr.",
                    details,
                )
                self.output.send(protocol.error(request.id, internal, request.command))
            else:
                self.output.send(protocol.result(request, data))
            finally:
                with self.state.lock:
                    self.state.current = None
                    self.state.stage = None
                    self.state.cancel.clear()

    def dispatch(self, request: Request) -> dict:
        if request.command == "load":
            return self.load(request)
        if request.command == "preview":
            return self.preview(request)
        if request.command == "texts":
            from cde_exporter.cad.worker_zone import zone_texts_response

            session = self.session_for(request)
            return zone_texts_response(session, request.data, self.state.check)
        if request.command == "export":
            return self.export(request)
        raise WorkerError(
            protocol.UNKNOWN_COMMAND,
            f"La orden «{request.command}» todavía no está implementada en este trabajador.",
            "Llegará en una etapa siguiente de la implementación.",
        )

    # ------------------------------------------------------------------ load
    def load(self, request: Request) -> dict:
        data = request.data
        path = data.get("dxf")
        if not isinstance(path, str) or not path:
            raise WorkerError(protocol.BAD_REQUEST, "Falta «dxf» (ruta del DXF) en «load».")
        units = data.get("units")
        if units is not None and units not in OVERRIDE_UNITS:
            raise WorkerError(
                protocol.BAD_REQUEST,
                f"Unidades no válidas: {units!r}.",
                "Use null o una de: " + ", ".join(OVERRIDE_UNITS) + ".",
            )
        margin = data.get("margin_mm", 5.0)
        if (
            isinstance(margin, bool)
            or not isinstance(margin, (int, float))
            or not (math.isfinite(margin) and margin >= 0)
        ):
            raise WorkerError(protocol.BAD_REQUEST, "«margin_mm» debe ser un número ≥ 0.")
        include_layers = data.get("include_layers", True)
        if not isinstance(include_layers, bool):
            raise WorkerError(protocol.BAD_REQUEST, "«include_layers» debe ser true o false.")
        # Umbral de los «*U» ofrecidos: si viene, manda sobre CDE_ANON_MIN_ENTITIES.
        anonymous_min = data.get("anonymous_min_entities")
        if anonymous_min is not None and (
            isinstance(anonymous_min, bool)
            or not isinstance(anonymous_min, int)
            or anonymous_min < 0
        ):
            raise WorkerError(
                protocol.BAD_REQUEST, "«anonymous_min_entities» debe ser un entero ≥ 0 o null."
            )
        prepare = data.get("prepare_preview", True)
        if isinstance(prepare, bool):
            prepare_options = {} if prepare else None
        elif isinstance(prepare, dict):
            prepare_options = prepare
        else:
            raise WorkerError(
                protocol.BAD_REQUEST,
                "«prepare_preview» debe ser true, false o un objeto con «plot_style» y "
                "«print_transparency».",
            )
        if prepare_options is not None:
            from cde_exporter.cad.worker_export import plot_style_option

            prepare_style = plot_style_option(prepare_options.get("plot_style"))
            prepare_transparency = prepare_options.get("print_transparency", True)
            if not isinstance(prepare_transparency, bool):
                raise WorkerError(
                    protocol.BAD_REQUEST, "«print_transparency» debe ser true o false."
                )
        source = Path(path)
        if not source.is_file():
            raise WorkerError(
                protocol.DXF_NOT_FOUND,
                f"No existe el DXF: {path}",
                "Revise la ruta (absoluta) del archivo.",
            )
        self.stop_preparation()
        with self.state.lock:
            self.state.session = None  # un «load» nuevo reemplaza al anterior
        progress = _Progress(self.output, request, LOAD_STAGES, self.state)
        try:
            loaded = load_dxf(source, self.work_dir, progress)
        except DxfTooLargeError as failure:
            raise WorkerError(protocol.DXF_TOO_LARGE, str(failure), TOO_LARGE_ADVICE) from failure
        except MemoryError as failure:
            raise WorkerError(
                protocol.DXF_TOO_LARGE,
                "No hay memoria suficiente para leer este DXF.",
                TOO_LARGE_ADVICE,
            ) from failure
        except (CadProcessingError, OSError, ValueError) as failure:
            raise WorkerError(
                protocol.DXF_INVALID,
                f"No se pudo leer el DXF: {failure}",
                "Si AutoCAD lo estaba guardando, repita «load».",
            ) from failure
        self.state.check()
        inventory = build_inventory(
            loaded.document,
            units=units,
            margin_mm=float(margin),
            include_layers=include_layers,
            progress=progress,
            check=self.state.check,
            source_copy=loaded.pruned,
            anonymous_min_entities=anonymous_min,
        )
        with self.state.lock:
            self.state.sessions += 1
            session = _Session(f"s-{self.state.sessions}", loaded, units, float(margin), inventory)
            self.state.session = session
        if prepare_options is not None:
            # En segundo plano: empieza cuando este pedido termina (al soltar el turno).
            self.start_preparation(session, prepare_style, prepare_transparency)
        preparation = session.preparation
        return {
            "session": session.id,
            **load_response(loaded, inventory),
            "preview_preparation": None if preparation is None else preparation.describe(),
        }

    # ------------------------------------------------------------------ preview
    def session_for(self, request: Request) -> _Session:
        """La sesión pedida, si es la actual y el DXF no cambió en disco."""
        with self.state.lock:
            session = self.state.session
        wanted = request.data.get("session")
        if session is None or wanted != session.id:
            raise WorkerError(
                protocol.NO_SESSION,
                "No hay ningún DXF cargado."
                if session is None
                else f"La sesión «{wanted}» no es la actual («{session.id}»).",
                "Envíe «load» y use el «session» que devuelve.",
                {"session": wanted, "current": session.id if session else None},
            )
        if session.loaded.changed():
            raise WorkerError(
                protocol.DXF_CHANGED,
                f"El DXF «{session.loaded.path.name}» cambió en disco desde «load».",
                "Vuelva a cargarlo con «load».",
                {"dxf": str(session.loaded.path)},
            )
        return session

    def preview(self, request: Request) -> dict:
        data = request.data
        session = self.session_for(request)
        if "zone" in data or "view" in data:
            return self.preview_zone(request, session)
        block = find_reference(session.inventory, data)
        long_side = data.get("long_side_px", DEFAULT_LONG_SIDE_PX)
        if (
            isinstance(long_side, bool)
            or not isinstance(long_side, int)
            or not MIN_LONG_SIDE_PX <= long_side <= MAX_LONG_SIDE_PX
        ):
            raise WorkerError(
                protocol.BAD_REQUEST,
                f"«long_side_px» debe ser un entero de {MIN_LONG_SIDE_PX} a {MAX_LONG_SIDE_PX}.",
            )
        document = session.loaded.document
        hidden, all_layers = hidden_layer_selection(document, data.get("include_hidden_layers"))
        from cde_exporter.cad.worker_export import plot_style_option

        plot_style = plot_style_option(data.get("plot_style"))  # BAD_REQUEST si no vale
        print_transparency = data.get("print_transparency", True)
        if not isinstance(print_transparency, bool):
            raise WorkerError(
                protocol.BAD_REQUEST, "«print_transparency» debe ser true o false."
            )
        tag = drawing_tag(layers_tag(hidden, all_layers), plot_style, print_transparency)
        drawing = {
            "plot_style": None if plot_style is None else plot_style.describe(),
            "print_transparency": print_transparency,
        }
        png, meta = self.previews.paths(session.loaded.fingerprint, block["handle"], long_side, tag)
        started = time.perf_counter()
        cached = self.previews.get(png, meta)
        if cached is not None:
            seconds = round(time.perf_counter() - started, 2)
            return {
                **cached,
                **drawing,
                "png": str(png),
                "cached": True,
                "seconds": seconds,
            }
        progress = _Progress(self.output, request, PREVIEW_STAGES, self.state)
        insert = document.entitydb.get(block["handle"])
        player, omitted = draw_reference(
            document,
            insert,
            hidden_layers=hidden,
            progress=progress,
            check=self.state.check,
            plot_style=plot_style,
            print_transparency=print_transparency,
        )
        self.state.check()
        hint = block["size_cad"]
        picture = paint(
            player, long_side, self.state.check, progress, max(hint) if hint else None
        )
        self.state.check()
        if all_layers:
            requested = ["*"]
        else:
            requested = sorted(hidden) if hidden else None
        result = {
            "ref": block["ref"],
            "handle": block["handle"],
            "px": list(picture.image.size),
            "bounds": None
            if picture.bounds is None
            else {
                "min": [round(value, 6) for value in picture.bounds[0]],
                "max": [round(value, 6) for value in picture.bounds[1]],
            },
            "cad_to_px": picture.cad_to_px,
            "omitted": omitted,
            "include_hidden_layers": requested,
            **drawing,
        }
        self.previews.put(png, meta, picture.image, result)
        self.previews.trim(keep=(png, meta))
        seconds = round(time.perf_counter() - started, 2)
        return {**result, "png": str(png), "cached": False, "seconds": seconds}


    def preview_zone(self, request: Request, session: _Session) -> dict:
        """Vista previa de una zona, de un trozo o de todo el Model Space (sección 4.1)."""
        from cde_exporter.cad.worker_export import plot_style_option
        from cde_exporter.cad.worker_zone import (
            ZONE_PREVIEW_STAGES,
            draw_zone_preview,
            parse_scale,
            preview_key,
            zone_paper,
            zone_request,
        )

        data = request.data
        long_side = data.get("long_side_px", DEFAULT_LONG_SIDE_PX)
        if (
            isinstance(long_side, bool)
            or not isinstance(long_side, int)
            or not MIN_LONG_SIDE_PX <= long_side <= MAX_LONG_SIDE_PX
        ):
            raise WorkerError(
                protocol.BAD_REQUEST,
                f"«long_side_px» debe ser un entero de {MIN_LONG_SIDE_PX} a {MAX_LONG_SIDE_PX}.",
            )
        document = session.loaded.document
        hidden, all_layers = hidden_layer_selection(document, data.get("include_hidden_layers"))
        zone = zone_request(data, document, hidden)
        scale = None if data.get("scale") is None else parse_scale(data["scale"], "«scale»")
        plot_style = plot_style_option(data.get("plot_style"))
        print_transparency = data.get("print_transparency", True)
        if not isinstance(print_transparency, bool):
            raise WorkerError(
                protocol.BAD_REQUEST, "«print_transparency» debe ser true o false."
            )
        tag = drawing_tag(layers_tag(hidden, all_layers), plot_style, print_transparency)
        drawing = {
            "plot_style": None if plot_style is None else plot_style.describe(),
            "print_transparency": print_transparency,
            "include_hidden_layers": ["*"]
            if all_layers
            else (sorted(hidden) if hidden else None),
        }
        # Vista previa preparada: si está lista para esta CTB y transparencia y no se piden
        # capas ocultas del CAD. Si se pide otra configuración, se prepara la nueva (la última
        # pedida) y, mientras, se dibuja como antes.
        preparation = session.preparation
        index = None
        if hidden is None and preparation is not None:
            if preparation.matches(plot_style, print_transparency):
                if preparation.state == "ready":
                    index = preparation.index
            elif preparation.state in ("ready", "running", "failed", "stopped"):
                self.start_preparation(session, plot_style, print_transparency)
        if index is not None:
            tag += "_i"
        png, meta = self.previews.paths(
            session.loaded.fingerprint, preview_key(zone), long_side, tag
        )
        started = time.perf_counter()
        result = self.previews.get(png, meta)
        cached = result is not None
        if not cached:
            progress = _Progress(self.output, request, ZONE_PREVIEW_STAGES, self.state)
            image, result = draw_zone_preview(
                session,
                zone,
                long_side,
                hidden_layers=hidden,
                plot_style=plot_style,
                print_transparency=print_transparency,
                check=self.state.check,
                progress=progress,
                index=index,
            )
            self.state.check()
            result = {**result, **drawing}
            self.previews.put(png, meta, image, result)
            self.previews.trim(keep=(png, meta))
        frame = None if result["view"] is None else tuple(result["view"])
        seconds = round(time.perf_counter() - started, 2)
        return {
            **result,
            **drawing,
            "scale": scale,
            "paper": zone_paper(session, zone, scale, frame),
            "png": str(png),
            "cached": cached,
            "seconds": seconds,
        }

    # ------------------------------------------------------------------ export
    def export(self, request: Request) -> dict:
        from cde_exporter.cad.worker_export import run_export

        session = self.session_for(request)

        def session_check() -> str | None:
            if session.loaded.changed():
                return f"El DXF «{session.loaded.path.name}» cambió en disco desde «load»."
            return None

        return run_export(
            request,
            session,
            send=self.output.send,
            state=self.state,
            check=self.state.check,
            cancelled_type=Cancelled,
            session_check=session_check,
        )


def find_reference(inventory: dict, data: dict) -> dict:
    """El bloque de ``inventory["blocks"]`` que nombra el pedido: ``ref``, ``block`` con
    ``occurrence`` o ``handle``, o solo ``handle`` (el del INSERT, que no cambia aunque AutoCAD
    renumere un «*U» al guardar)."""
    ref, name = data.get("ref"), data.get("block")
    occurrence, handle = data.get("occurrence"), data.get("handle")
    if ref is None and name is None and handle is not None:
        return _find_by_handle(inventory, handle)
    if ref is not None:
        if not isinstance(ref, str) or "#" not in ref:
            raise WorkerError(protocol.BAD_REQUEST, "«ref» debe ser «bloque#ocurrencia».")
        name, _sep, number = ref.rpartition("#")
        if not number.isdigit():
            raise WorkerError(protocol.BAD_REQUEST, "«ref» debe ser «bloque#ocurrencia».")
        occurrence = int(number)
    elif not isinstance(name, str) or not name:
        raise WorkerError(
            protocol.BAD_REQUEST, "Falta «ref», «handle», o «block» con «occurrence» o «handle»."
        )
    elif handle is None and (isinstance(occurrence, bool) or not isinstance(occurrence, int)):
        raise WorkerError(
            protocol.BAD_REQUEST, "Con «block», indique «occurrence» (entero) o «handle»."
        )

    def matches(item: dict) -> bool:
        if item["name"] != name:
            return False
        if handle is not None:
            return str(item["handle"]).upper() == str(handle).upper()
        return item["occurrence"] == occurrence

    for item in inventory["blocks"]:
        if matches(item):
            return item
    for item in inventory["omitted"]:
        if matches(item):
            raise WorkerError(
                protocol.NOT_EXPORTABLE,
                f"La referencia «{name}» no se exporta: {item['reason']}.",
                details={"block": name, "handle": item["handle"], "reason": item["reason"]},
            )
    known = {item["name"] for item in inventory["blocks"]}
    known |= {item["name"] for item in inventory["omitted"]}
    if name not in known:
        raise WorkerError(
            protocol.BLOCK_NOT_FOUND,
            f"No existe el bloque «{name}» en el DXF cargado.",
            "Revise la lista de bloques que devolvió «load».",
            {"block": name},
        )
    raise WorkerError(
        protocol.REFERENCE_NOT_FOUND,
        f"El bloque «{name}» no tiene esa referencia.",
        "Revise «occurrence» o «handle» en la lista que devolvió «load».",
        {"block": name, "occurrence": occurrence, "handle": handle},
    )


def _find_by_handle(inventory: dict, handle) -> dict:
    """La referencia cuyo INSERT tiene ``handle``, sea cual sea el nombre de su bloque."""
    if not isinstance(handle, str) or not handle.strip():
        raise WorkerError(protocol.BAD_REQUEST, "«handle» debe ser un texto (handle del INSERT).")
    wanted = handle.strip().upper()
    found = [item for item in inventory["blocks"] if str(item["handle"]).upper() == wanted]
    if len(found) > 1:
        raise WorkerError(
            protocol.BAD_REQUEST,
            f"El handle «{handle}» está repetido en el DXF.",
            "Use «ref» de la lista que devolvió «load».",
            {"handle": handle, "refs": [item["ref"] for item in found]},
        )
    if found:
        return found[0]
    for item in inventory["omitted"]:
        if str(item["handle"]).upper() == wanted:
            raise WorkerError(
                protocol.NOT_EXPORTABLE,
                f"La referencia «{item['name']}» no se exporta: {item['reason']}.",
                details={"block": item["name"], "handle": item["handle"], "reason": item["reason"]},
            )
    raise WorkerError(
        protocol.REFERENCE_NOT_FOUND,
        f"No hay ninguna referencia del Model Space con el handle «{handle}».",
        "Revise la lista de bloques que devolvió «load».",
        {"handle": handle},
    )


def hidden_layer_selection(document, value) -> tuple[frozenset[str] | None, bool]:
    """``include_hidden_layers``: ``null``, ``["*"]`` (todas) o lista de capas. Devuelve
    (capas sin mayúsculas o None, si eran todas). Una capa que no existe es ``BAD_REQUEST``."""
    if value is None:
        return None, False
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WorkerError(
            protocol.BAD_REQUEST,
            '«include_hidden_layers» debe ser null, ["*"] o una lista de nombres de capa.',
        )
    existing = {str(layer.dxf.name).casefold() for layer in document.layers}
    if value == ["*"]:
        return frozenset(existing), True
    unknown = sorted(item for item in value if item.strip().casefold() not in existing)
    if unknown:
        raise WorkerError(
            protocol.BAD_REQUEST,
            "Capas que no existen en el DXF: " + ", ".join(f"«{n}»" for n in unknown) + ".",
            details={"layers": unknown},
        )
    names = frozenset(item.strip().casefold() for item in value if item.strip())
    return (names or None), False


def load_response(loaded: LoadedDxf, inventory: dict) -> dict:
    """``data`` de ``load`` sin ``session`` (también es el JSON de ``--list --json``)."""
    notices = list(loaded.notices)
    unknown = inventory["unknown_entities"]
    if unknown:
        counts = {item["type"]: item["modelspace"] + item["in_blocks"] for item in unknown}
        notices.append(
            {
                "code": "UNKNOWN_ENTITIES_OMITTED",
                "severity": "warning",
                "message": unknown_entities_message(counts)
                + " Cada paquete dice cuántas tenía su bloque.",
                "advice": UNKNOWN_ENTITIES_ADVICE,
            }
        )
    return {
        "dxf": loaded.dxf_json(),
        "units": inventory["units"],
        "margin_mm": inventory["margin_mm"],
        "anonymous_min_entities": inventory["anonymous_min_entities"],
        "blocks": inventory["blocks"],
        "omitted": inventory["omitted"],
        "layers": inventory["layers"],
        "modelspace_loose": inventory["modelspace_loose"],
        "unknown_entities": unknown,
        "notices": notices,
    }


def _versions() -> dict:
    from importlib.metadata import PackageNotFoundError, version

    found = {}
    for name in ("ezdxf", "PyMuPDF"):
        try:
            found[name] = version(name)
        except PackageNotFoundError:
            found[name] = None
    return found


def run_worker(work_dir: str | None = None, preview_cache_mb: int = 500) -> int:
    """Punto de entrada de ``cde-exporter worker``."""
    output = _Output(sys.stdout.buffer)
    # Nada más que JSON por stdout: lo que el exportador escribe con print va a stderr.
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, force=True)
    own_dir = work_dir is None
    folder = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="cde-worker-"))
    folder.mkdir(parents=True, exist_ok=True)
    clean_temporaries(folder)
    worker = Worker(output, folder, preview_cache_mb)
    versions = _versions()
    output.send(
        protocol.message(
            "ready",
            None,
            data={
                "protocol_version": protocol.PROTOCOL_VERSION,
                "exporter_version": __version__,
                "ezdxf": versions["ezdxf"],
                "pymupdf": versions["PyMuPDF"],
                "manifest_schema_version": SCHEMA_VERSION,
                "work_dir": str(folder.resolve()),
            },
        )
    )
    reader = threading.Thread(
        target=worker.read_requests, args=(sys.stdin.buffer,), name="stdin", daemon=True
    )
    reader.start()
    try:
        worker.run()
    finally:
        clean_temporaries(folder)
        if own_dir:
            shutil.rmtree(folder, ignore_errors=True)
    return 0
