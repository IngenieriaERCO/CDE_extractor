"""Lectura de un DXF para el trabajador y ``--list --json`` (docs/protocolo_trabajador.md).

- Lee el DXF una vez; si es grande (``large_dxf.is_large``), desde la copia por partes sin
  bloques sin usar, y calcula el SHA-256 en esa misma pasada (sin volver a leer el archivo).
- Calcula la huella por muestras (para la caché de vistas previas) y guarda tamaño y fecha
  (para detectar que el DXF cambió: ``DXF_CHANGED``).
- Los temporales van en carpetas con el prefijo ``.cde-tmp-`` y un archivo con el PID que las
  creó; ``clean_temporaries`` borra las que ya no son de ningún proceso vivo.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cde_exporter.cad.large_dxf import is_large, prune_unused_blocks, pruning_message
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


def load_dxf(path: Path, work_dir: Path, progress=None) -> LoadedDxf:
    """Lee el DXF (con la copia por partes si es grande) y calcula hash y huella.

    ``progress(etapa, fracción, mensaje)`` recibe el avance de ``read`` y ``hash``. Lanza
    ``FileNotFoundError`` si no existe y deja pasar los errores de lectura de ``read_dxf``.
    """
    report = progress or (lambda *_: None)
    path = Path(path)
    stat = path.stat()
    started = time.perf_counter()
    source_copy = None
    notices: list = []
    pruned = None
    if is_large(path):
        report("read", 0.0, f"Copia por partes sin bloques sin usar ({stat.st_size / 1e6:.0f} MB)")
        folder = make_temporary(work_dir)
        try:
            digest = hashlib.sha256()
            copy = folder / "copia.dxf"
            pruned = prune_unused_blocks(path, copy, digest)
            sha256 = digest.hexdigest()
            report("read", 0.5, "Leyendo la copia con ezdxf")
            document = read_dxf(copy if pruned.emptied else path)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        if not pruned.emptied:
            pruned = None
        else:
            source_copy = {
                "source_bytes": pruned.source_bytes,
                "copy_bytes": pruned.copy_bytes,
                "seconds": round(pruned.seconds, 1),
                "emptied_blocks": [
                    {"name": item.name, "entities": item.entities, "bytes": item.bytes}
                    for item in pruned.emptied
                ],
            }
            notices.append(
                {
                    "code": "SOURCE_UNUSED_BLOCKS_SKIPPED",
                    "severity": "info",
                    "message": pruning_message(pruned),
                    "advice": None,
                }
            )
    else:
        report("read", 0.0, "Leyendo el DXF con ezdxf")
        document = read_dxf(path)
        report("hash", 0.0, "SHA-256 del DXF")
        sha256 = _file_sha256(path)
    read_seconds = time.perf_counter() - started
    report("hash", 0.5, "Huella del DXF")
    return LoadedDxf(
        path=path.resolve(),
        document=document,
        bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        modified=datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        sha256=sha256,
        fingerprint=fingerprint(path),
        dxf_version=str(document.dxfversion),
        read_seconds=read_seconds,
        source_copy=source_copy,
        notices=notices,
        pruned=pruned,
    )
