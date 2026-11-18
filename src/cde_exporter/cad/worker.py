"""Trabajador persistente (``cde-exporter worker``): lee el DXF una vez y atiende pedidos en
JSON por líneas (docs/protocolo_trabajador.md).

- Un hilo lee stdin: ``status``, ``cancel`` y ``close`` se atienden ahí mismo, siempre y enseguida
  (también mientras el hilo principal está dentro de una operación larga de ezdxf); cualquier
  otro pedido se encola, o recibe ``BUSY`` si ya hay uno en curso.
- El hilo principal ejecuta los pedidos de uno en uno.
- stdout solo lleva JSON en UTF-8 (``_Output``); lo que el exportador escribe en la terminal y el
  registro de las librerías van a stderr.

Etapa 1 de la implementación: arranque, ``load``, ``status``, ``cancel`` y ``close``.
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
from cde_exporter.cad.dxf_session import LoadedDxf, clean_temporaries, load_dxf
from cde_exporter.cad.inventory import build_inventory
from cde_exporter.cad.manifest import SCHEMA_VERSION
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.scale import OVERRIDE_UNITS
from cde_exporter.cad.worker_protocol import Request, WorkerError

PROGRESS_INTERVAL = 0.25  # segundos entre eventos de avance (como mucho unos 4 por segundo)
LOAD_STAGES = (("read", 0.6), ("hash", 0.05), ("blocks", 0.3), ("layers", 0.05))


class Cancelled(Exception):
    """El pedido en curso se canceló."""


class _Output:
    """stdout del proceso: solo JSON, en UTF-8, una línea por mensaje y sin mezclar hilos."""

    def __init__(self, stream) -> None:
        self.stream = stream
        self.lock = threading.Lock()

    def send(self, message: dict) -> None:
        data = protocol.encode(message)
        with self.lock:
            self.stream.write(data)
            self.stream.flush()


@dataclass
class _Session:
    id: str
    loaded: LoadedDxf
    units: str | None
    margin_mm: float
    inventory: dict


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
        self.queue: queue.Queue = queue.Queue()

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
        return {
            "busy": current is not None,
            "current": current.id if current else None,
            "stage": self.state.stage if current else None,
            "session": session.id if session else None,
            "dxf": session.loaded.path.name if session else None,
            "memory_mb": _memory_mb(),
        }

    # ------------------------------------------------------------------ hilo principal
    def run(self) -> None:
        while True:
            request = self.queue.get()
            if request is None:
                return
            if request.command == "close":
                self.output.send(protocol.result(request, {}))
                return
            try:
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
        source = Path(path)
        if not source.is_file():
            raise WorkerError(
                protocol.DXF_NOT_FOUND,
                f"No existe el DXF: {path}",
                "Revise la ruta (absoluta) del archivo.",
            )
        with self.state.lock:
            self.state.session = None  # un «load» nuevo reemplaza al anterior
        progress = _Progress(self.output, request, LOAD_STAGES, self.state)
        try:
            loaded = load_dxf(source, self.work_dir, progress)
        except MemoryError as failure:
            raise WorkerError(
                protocol.DXF_TOO_LARGE,
                "No hay memoria suficiente para leer este DXF.",
                "Cierre otros programas o reduzca el DXF (bloques y entidades sin usar).",
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
        )
        with self.state.lock:
            self.state.sessions += 1
            session = _Session(f"s-{self.state.sessions}", loaded, units, float(margin), inventory)
            self.state.session = session
        return {"session": session.id, **load_response(loaded, inventory)}


def load_response(loaded: LoadedDxf, inventory: dict) -> dict:
    """``data`` de ``load`` sin ``session`` (también es el JSON de ``--list --json``)."""
    return {
        "dxf": loaded.dxf_json(),
        "units": inventory["units"],
        "margin_mm": inventory["margin_mm"],
        "blocks": inventory["blocks"],
        "omitted": inventory["omitted"],
        "layers": inventory["layers"],
        "modelspace_loose": inventory["modelspace_loose"],
        "notices": loaded.notices,
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
