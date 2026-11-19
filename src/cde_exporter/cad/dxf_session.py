"""Lectura de un DXF para el trabajador y ``--list --json`` (docs/protocolo_trabajador.md).

- Lee el DXF una vez; si es grande (``large_dxf.is_large``), desde una copia reducida hecha
  por partes, y calcula el SHA-256 en esa misma pasada (sin volver a leer el archivo). La
  copia quita el contenido de los bloques sin usar y, si aun así mediría 500 MB o más
  (``CDE_SELECTIVE_DXF_MB``), también lo suelto del Model Space (lectura selectiva).
- La copia reducida se guarda en ``<work-dir>/dxf-cache/<SHA-256 del DXF>/`` (las 3 usadas
  más recientemente): otro ``load`` del mismo DXF la reutiliza tras comprobar el SHA-256, sin
  repetir las pasadas.
- Antes de leer con ezdxf se estima la memoria (unos 7 bytes por byte de DXF más 300 MB); si
  no cabe, ``DxfTooLargeError`` (``DXF_TOO_LARGE``) en lugar de agotar el equipo.
- Calcula la huella por muestras (para la caché de vistas previas) y guarda tamaño y fecha
  (para detectar que el DXF cambió: ``DXF_CHANGED``).
- Los temporales van en carpetas con el prefijo ``.cde-tmp-`` y un archivo con el PID que las
  creó; ``clean_temporaries`` borra las que ya no son de ningún proceso vivo.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cde_exporter.cad.crs import document_crs
from cde_exporter.cad.large_dxf import (
    EmptiedBlock,
    LooseLayer,
    PrunedSource,
    is_large,
    loose_skipped_message,
    pruning_message,
    selective_threshold_bytes,
    unused_blocks,
    write_selective,
    write_without_blocks,
)
from cde_exporter.cad.reader import read_dxf

TEMP_PREFIX = ".cde-tmp-"
_PID_FILE = "pid"
FINGERPRINT_SAMPLES = 64
FINGERPRINT_CHUNK = 64 * 1024


@dataclass
class LoadedDxf:
    path: Path
    document: object
    bytes: int
    mtime_ns: int
    modified: str
    sha256: str
    fingerprint: str
    dxf_version: str
    read_seconds: float
    source_copy: dict | None
    notices: list
    # Informe de la copia sin bloques sin usar (``large_dxf.PrunedSource``) o None: cada
    # paquete lo declara en su TXT y su manifiesto, como en la línea de órdenes.
    pruned: object = None
    # Sistema de coordenadas del GEODATA, sin interpretar (``crs.document_crs``), o None.
    crs: dict | None = None

    def changed(self) -> bool:
        """¿Cambió el archivo en disco desde que se leyó? (tamaño o fecha)"""
        try:
            stat = self.path.stat()
        except OSError:
            return True
        return stat.st_size != self.bytes or stat.st_mtime_ns != self.mtime_ns

    def dxf_json(self) -> dict:
        return {
            "name": self.path.name,
            "path": str(self.path),
            "bytes": self.bytes,
            "modified": self.modified,
            "fingerprint": self.fingerprint,
            "sha256": self.sha256,
            "dxf_version": self.dxf_version,
            "read_seconds": round(self.read_seconds, 1),
            "source_copy": self.source_copy,
            "crs": self.crs,
        }


def fingerprint(path: Path) -> str:
    """SHA-256 del tamaño y de 64 trozos de 64 KiB repartidos por el archivo (el primero y el
    último incluidos). Solo para saber si una vista previa guardada es de este DXF."""
    size = path.stat().st_size
    digest = hashlib.sha256(str(size).encode("ascii"))
    with path.open("rb") as stream:
        for index in range(FINGERPRINT_SAMPLES):
            offset = max(0, (size - FINGERPRINT_CHUNK) * index // (FINGERPRINT_SAMPLES - 1))
            stream.seek(offset)
            digest.update(stream.read(FINGERPRINT_CHUNK))
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_temporary(parent: Path) -> Path:
    """Carpeta temporal ``.cde-tmp-…`` en ``parent``, con el PID que la creó dentro."""
    parent.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=parent))
    (folder / _PID_FILE).write_text(str(os.getpid()), encoding="ascii")
    return folder


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def clean_temporaries(parent: Path) -> int:
    """Borra las carpetas ``.cde-tmp-*`` de ``parent`` cuyo proceso ya no vive (o sin PID).
    Nunca toca nada sin ese prefijo. Devuelve cuántas borró."""
    if not parent.is_dir():
        return 0
    removed = 0
    for folder in parent.glob(f"{TEMP_PREFIX}*"):
        if not folder.is_dir():
            continue
        try:
            pid = int((folder / _PID_FILE).read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            pid = None
        if pid is not None and _pid_alive(pid):
            continue
        shutil.rmtree(folder, ignore_errors=True)
        removed += 1
    return removed


class DxfTooLargeError(Exception):
    """No hay memoria para leer el DXF (ni con la copia reducida)."""


# Memoria que necesita ezdxf para leer un DXF, medida (GUAMO, la copia de 106,7 MB del DXF de
# 830 MB, la copia selectiva de 30,6 MB del de 2,74 GB): unos 7 bytes por byte de DXF más el
# propio programa.
MEMORY_PER_DXF_BYTE = 7
MEMORY_BASE_BYTES = 300 * 1024 * 1024
CACHE_DIR = "dxf-cache"
CACHE_ENTRIES = 3  # copias reducidas que se guardan (las usadas hace más tiempo se borran)
_CACHE_COPY = "copia.dxf"
_CACHE_INFO = "copia.json"


def available_memory_bytes() -> int | None:
    """Memoria que el sistema aún puede dar a un proceso (RAM libre más archivo de paginación
    libre, en Windows). None si no se sabe."""
    if os.name != "nt":
        return None
    import ctypes

    class Status(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = Status()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.ullAvailPageFile)


def needed_memory_bytes(dxf_bytes: int) -> int:
    return MEMORY_BASE_BYTES + MEMORY_PER_DXF_BYTE * dxf_bytes


def check_memory(dxf_bytes: int, source_bytes: int, what: str) -> None:
    """``DxfTooLargeError`` si leer ``dxf_bytes`` de DXF no cabe en la memoria disponible."""
    available = available_memory_bytes()
    needed = needed_memory_bytes(dxf_bytes)
    if available is None or needed <= available:
        return
    raise DxfTooLargeError(
        f"No hay memoria para leer este DXF ({source_bytes / 1e6:.0f} MB): {what} mide "
        f"{dxf_bytes / 1e6:.0f} MB y leerla necesita unos {needed / 2**30:.1f} GB, pero el "
        f"equipo solo tiene {available / 2**30:.1f} GB libres (memoria más archivo de "
        "paginación)."
    )


def _file_sha256_progress(path: Path, report, stage: str, text: str) -> str:
    digest = hashlib.sha256()
    size = max(1, path.stat().st_size)
    done = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(16 << 20), b""):
            digest.update(chunk)
            done += len(chunk)
            report(stage, done / size, text)
    return digest.hexdigest()


def _report_json(report: PrunedSource) -> dict:
    return {
        "source_bytes": report.source_bytes,
        "copy_bytes": report.copy_bytes,
        "seconds": round(report.seconds, 1),
        "selective": report.selective,
        "emptied": [[item.name, item.entities, item.bytes] for item in report.emptied],
        "loose": [
            [item.layer, item.entities, item.vertices, item.types, item.bounds]
            for item in report.loose
        ],
    }


def _report_from_json(data: dict) -> PrunedSource:
    return PrunedSource(
        source_bytes=int(data["source_bytes"]),
        copy_bytes=int(data["copy_bytes"]),
        emptied=tuple(EmptiedBlock(*item) for item in data["emptied"]),
        seconds=float(data["seconds"]),
        selective=bool(data["selective"]),
        loose=tuple(
            LooseLayer(layer, entities, vertices, dict(types), tuple(box) if box else None)
            for layer, entities, vertices, types, box in data["loose"]
        ),
        cached=True,
    )


def _cached_copy(cache: Path, path: Path, size: int, print_: str, report):
    """Copia reducida guardada de este DXF: se busca por huella y tamaño y se confirma con el
    SHA-256 completo del archivo (una lectura seguida, sin interpretarlo). Devuelve (copia,
    informe, sha256) o None."""
    if not cache.is_dir():
        return None
    for folder in cache.iterdir():
        info_path = folder / _CACHE_INFO
        copy = folder / _CACHE_COPY
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if info.get("fingerprint") != print_ or info.get("bytes") != size or not copy.is_file():
            continue
        sha256 = _file_sha256_progress(path, report, "read", "Comprobando la copia guardada")
        if sha256 != info.get("sha256") or folder.name != sha256:
            return None  # mismo tamaño y huella, otro contenido: se rehace
        now = time.time()
        with contextlib.suppress(OSError):
            os.utime(info_path, (now, now))
        return copy, _report_from_json(info["report"]), sha256
    return None


def _store_copy(cache: Path, folder: Path, copy: Path, info: dict) -> Path:
    """Guarda la copia en la caché (carpeta = SHA-256 del DXF) y deja como mucho
    ``CACHE_ENTRIES`` copias (se borran las usadas hace más tiempo)."""
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / info["sha256"]
    (folder / _CACHE_INFO).write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    shutil.rmtree(target, ignore_errors=True)
    staged = folder / _CACHE_COPY
    if copy != staged:
        os.replace(copy, staged)
    (folder / _PID_FILE).unlink(missing_ok=True)
    os.replace(folder, target)
    entries = []
    for entry in cache.iterdir():
        with contextlib.suppress(OSError):
            entries.append(((entry / _CACHE_INFO).stat().st_mtime, entry))
    for _mtime, entry in sorted(entries, key=lambda item: item[0], reverse=True)[CACHE_ENTRIES:]:
        shutil.rmtree(entry, ignore_errors=True)
    return target / _CACHE_COPY


def _reduced_copy(path: Path, work_dir: Path, size: int, print_: str, report, stat):
    """Copia reducida de un DXF grande: de la caché o hecha ahora (dos pasadas por partes:
    bloques sin usar y SHA-256; luego la copia, selectiva si la normal mediría
    ``selective_threshold_bytes`` o más). Devuelve (copia o None, informe o None, sha256)."""
    cache = work_dir / CACHE_DIR
    found = _cached_copy(cache, path, size, print_, report)
    if found is not None:
        return found
    folder = make_temporary(work_dir)
    try:
        digest = hashlib.sha256()
        started = time.perf_counter()
        report("read", 0.0, f"Buscando bloques sin usar ({size / 1e6:.0f} MB)")
        unused = unused_blocks(
            path,
            digest,
            lambda done: report("read", 0.45 * done / size, "Buscando bloques sin usar"),
        )
        sha256 = digest.hexdigest()
        estimated = size - sum(item[2] for item in unused.values())
        selective = estimated >= selective_threshold_bytes()
        copy = folder / _CACHE_COPY
        text = "Copia sin lo suelto del Model Space" if selective else "Copia sin bloques sin usar"

        def step(done: int) -> None:
            report("read", 0.45 + 0.45 * done / size, text)

        if selective:
            written, loose = write_selective(path, copy, set(unused), step)
        else:
            if not unused:
                return None, None, sha256  # nada que quitar: se lee el original
            written, loose = write_without_blocks(path, copy, set(unused), step), ()
        emptied = tuple(
            sorted(
                (EmptiedBlock(*item) for item in unused.values()),
                key=lambda item: -item.bytes,
            )
        )
        result = PrunedSource(
            stat.st_size, written, emptied, time.perf_counter() - started, selective, loose
        )
        info = {
            "source_name": path.name,
            "bytes": size,
            "mtime_ns": stat.st_mtime_ns,
            "fingerprint": print_,
            "sha256": sha256,
            "report": _report_json(result),
        }
        stored = _store_copy(cache, folder, copy, info)
        return stored, result, sha256
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def load_dxf(path: Path, work_dir: Path, progress=None) -> LoadedDxf:
    """Lee el DXF (desde una copia reducida si es grande) y calcula hash y huella.

    ``progress(etapa, fracción, mensaje)`` recibe el avance de ``read`` y ``hash`` y puede
    lanzar una excepción para cancelar. Lanza ``FileNotFoundError`` si no existe,
    ``DxfTooLargeError`` si no cabe en memoria, y deja pasar los errores de ``read_dxf``.
    """
    report = progress or (lambda *_: None)
    path = Path(path)
    stat = path.stat()
    size = stat.st_size
    started = time.perf_counter()
    print_ = fingerprint(path)
    copy = pruned = None
    notices: list = []
    if is_large(path):
        copy, pruned, sha256 = _reduced_copy(path, work_dir, size, print_, report, stat)
        if pruned is not None:
            what = (
                "la copia sin lo suelto del Model Space"
                if pruned.selective
                else "la copia sin bloques sin usar"
            )
            check_memory(pruned.copy_bytes, size, what)
            report("read", 0.9, "Leyendo la copia con ezdxf")
            document = read_dxf(copy)
        else:
            check_memory(size, size, "el DXF")
            report("read", 0.9, "Leyendo el DXF con ezdxf")
            document = read_dxf(path)
    else:
        check_memory(size, size, "el DXF")
        report("read", 0.0, "Leyendo el DXF con ezdxf")
        document = read_dxf(path)
        report("hash", 0.0, "SHA-256 del DXF")
        sha256 = _file_sha256(path)
    read_seconds = time.perf_counter() - started
    source_copy = None
    if pruned is not None:
        source_copy = {
            "mode": "selective" if pruned.selective else "unused_blocks",
            "cached": pruned.cached,
            "source_bytes": pruned.source_bytes,
            "copy_bytes": pruned.copy_bytes,
            "seconds": round(pruned.seconds, 1),
            "emptied_blocks": [
                {"name": item.name, "entities": item.entities, "bytes": item.bytes}
                for item in pruned.emptied
            ],
            "loose_entities": pruned.loose_entities,
        }
        if pruned.emptied:
            notices.append(
                {
                    "code": "SOURCE_UNUSED_BLOCKS_SKIPPED",
                    "severity": "info",
                    "message": pruning_message(pruned),
                    "advice": None,
                }
            )
        if pruned.selective:
            notices.append(
                {
                    "code": "SOURCE_MODELSPACE_LOOSE_SKIPPED",
                    "severity": "info",
                    "message": loose_skipped_message(pruned),
                    "advice": None,
                }
            )
    report("hash", 0.5, "Huella del DXF")
    return LoadedDxf(
        path=path.resolve(),
        document=document,
        bytes=size,
        mtime_ns=stat.st_mtime_ns,
        modified=datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        sha256=sha256,
        fingerprint=print_,
        dxf_version=str(document.dxfversion),
        read_seconds=read_seconds,
        source_copy=source_copy,
        notices=notices,
        pruned=pruned,
        crs=document_crs(document),
    )
