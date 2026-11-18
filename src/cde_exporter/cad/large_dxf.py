"""DXF muy grandes: copia temporal sin el contenido de los bloques que no se usan.

ezdxf carga todo el DXF en memoria; un DXF de 830 MB no cabe en un equipo de 7,7 GB aunque
casi todo sea un bloque que nada usa. Aquí se lee el archivo por partes (pares código/valor,
línea a línea, sin cargarlo entero) y se escribe una copia en la que esos bloques quedan vacíos
(se conservan su cabecera BLOCK y su ENDBLK, y su BLOCK_RECORD). El DXF de origen no se toca.

Un bloque «se usa» si lo nombra o lo apunta (por el handle de su BLOCK_RECORD) la cabecera,
una tabla (p. ej. las flechas de un DIMSTYLE), una entidad del Model Space o del Paper Space,
un objeto del OBJECTS u otro bloque que se usa. Es conservador: cualquier valor que coincida con
el nombre de un bloque (también una etiqueta o un texto de XDATA) cuenta como uso. Los
espacios de trabajo (``*Model_Space``, ``*Paper_Space…``) y las referencias externas (XREF)
siempre se conservan.

Lectura selectiva (``write_selective``): si ni siquiera esa copia cabe en memoria (el DXF de
2,74 GB con 9 millones de VERTEX en polilíneas sueltas), el Model Space de la copia guarda solo
los INSERT (con sus ATTRIB y su SEQEND) y el Paper Space entero; lo suelto del Model Space se
resume por capa (tipos, entidades, vértices y una caja aproximada) sin cargarlo. Sirve para
listar y exportar bloques, que nunca dependen de lo suelto.

Solo usa la biblioteca estándar (más ``detail_model`` para el aviso).
"""

from __future__ import annotations

import math
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from cde_exporter.cad.detail_model import ExtractionWarning, Severity

LARGE_DXF_ENV = "CDE_LARGE_DXF_MB"
# Desde este tamaño se lee una copia sin bloques sin usar (se puede cambiar con la variable de
# entorno CDE_LARGE_DXF_MB; 0 = siempre).
DEFAULT_LARGE_DXF_MB = 200.0
SOURCE_PRUNED_CODE = "SOURCE_UNUSED_BLOCKS_SKIPPED"
SELECTIVE_DXF_ENV = "CDE_SELECTIVE_DXF_MB"
# Si la copia sin bloques sin usar mediría esto o más, se hace la copia selectiva (sin lo
# suelto del Model Space). CDE_SELECTIVE_DXF_MB lo cambia (0 = siempre).
DEFAULT_SELECTIVE_DXF_MB = 500.0
SOURCE_LOOSE_CODE = "SOURCE_MODELSPACE_LOOSE_SKIPPED"
PROGRESS_STEP_BYTES = 16 * 1024 * 1024

_BINARY_SENTINEL = b"AutoCAD Binary DXF"
# Códigos de grupo que guardan un handle (punteros y XDATA 1005) o un texto que puede ser el
# nombre de un bloque. El 330 no cuenta: dice quién es el dueño (el diccionario o la tabla de
# orden de un bloque apuntan así a su BLOCK_RECORD), no que el bloque se use.
_POINTER_CODES = frozenset({*range(331, 370), *range(390, 400), 480, 481, 1005})
_NAME_CODES = frozenset({1, 2, 3, 1000})
_ROOT_PREFIXES = ("*model_space", "*paper_space")
_SMALL_SECTIONS = ("HEADER", "TABLES")  # se leen antes que BLOCK_RECORD: se filtran al final
_XREF_FLAGS = 4 | 32  # referencia externa o superpuesta


@dataclass(frozen=True, slots=True)
class EmptiedBlock:
    name: str
    entities: int  # entidades de su definición (VERTEX y SEQEND no cuentan)
    bytes: int


@dataclass(frozen=True, slots=True)
class LooseLayer:
    """Lo suelto del Model Space de una capa que la copia selectiva no lleva."""

    layer: str
    entities: int
    vertices: int
    types: dict[str, int]
    # Caja aproximada (xmin, ymin, xmax, ymax) de los puntos que guarda cada entidad: los
    # vértices, los extremos, el centro ± el radio. En coordenadas del objeto (OCS) para lo
    # que tiene extrusión distinta de la de planta. None si no tenía puntos.
    bounds: tuple[float, float, float, float] | None


@dataclass(frozen=True, slots=True)
class PrunedSource:
    """Resultado de la copia: tamaños, bloques vaciados y tiempo. Con ``selective``, la copia
    tampoco lleva lo suelto del Model Space (``loose``, por capa). ``cached``: se tomó de la
    caché de copias (el tiempo es el de cuando se hizo)."""

    source_bytes: int
    copy_bytes: int
    emptied: tuple[EmptiedBlock, ...]
    seconds: float
    selective: bool = False
    loose: tuple[LooseLayer, ...] = ()
    cached: bool = False

    @property
    def loose_entities(self) -> int:
        return sum(item.entities for item in self.loose)


def selective_threshold_bytes() -> float:
    """Tamaño de la copia sin bloques sin usar desde el que se hace la selectiva
    (CDE_SELECTIVE_DXF_MB o 500 MB)."""
    value = os.environ.get(SELECTIVE_DXF_ENV, "").strip()
    try:
        megabytes = float(value) if value else DEFAULT_SELECTIVE_DXF_MB
    except ValueError:
        megabytes = DEFAULT_SELECTIVE_DXF_MB
    return max(megabytes, 0.0) * 1_000_000


def large_threshold_bytes() -> float:
    """Tamaño desde el que se usa la copia (CDE_LARGE_DXF_MB o 200 MB)."""
    value = os.environ.get(LARGE_DXF_ENV, "").strip()
    try:
        megabytes = float(value) if value else DEFAULT_LARGE_DXF_MB
    except ValueError:
        megabytes = DEFAULT_LARGE_DXF_MB
    return max(megabytes, 0.0) * 1_000_000


def is_large(path: Path) -> bool:
    """¿Es un DXF de texto lo bastante grande como para leerlo desde la copia?"""
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            head = stream.read(len(_BINARY_SENTINEL))
    except OSError:
        return False
    return head != _BINARY_SENTINEL and size >= large_threshold_bytes()


def _pairs(stream):
    while True:
        code = stream.readline()
        if not code:
            return
        yield code, stream.readline()


def _code(raw: bytes) -> int | None:
    try:
        return int(raw)
    except ValueError:
        return None


def _text(raw: bytes) -> str:
    return raw.strip().decode("utf-8", "replace")


def unused_blocks(path: Path, digest=None, progress=None) -> dict[str, tuple[str, int, int]]:
    """Bloques que nada usa (clave: nombre sin mayúsculas), con su nombre, las entidades y los
    bytes de su contenido. Primera pasada.

    Con ``digest`` (p. ej. ``hashlib.sha256()``), le suma todos los bytes leídos: así el hash
    del archivo sale de esta misma pasada, sin volver a leerlo. ``progress(bytes_leídos)`` se
    llama cada 16 MB (puede lanzar una excepción para cancelar).
    """
    section = None
    table = None
    entity = None
    block = None  # nombre (sin mayúsculas) del bloque en curso
    in_header = False
    record_handle = None
    handles: dict[str, str] = {}  # handle del BLOCK_RECORD -> nombre sin mayúsculas
    names: set[str] = set()
    original: dict[str, str] = {}  # nombre sin mayúsculas -> nombre tal cual
    flags: dict[str, int] = {}
    content: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # entidades, bytes
    refs: dict[str, set[str]] = defaultdict(set)  # contenedor -> nombres usados
    raw_small: dict[str, set[str]] = defaultdict(set)  # valores de HEADER y TABLES

    read = next_report = 0
    with path.open("rb") as stream:
        for raw_code, raw_value in _pairs(stream):
            if digest is not None:
                digest.update(raw_code)
                digest.update(raw_value)
            if progress is not None:
                read += len(raw_code) + len(raw_value)
                if read >= next_report:
                    progress(read)
                    next_report = read + PROGRESS_STEP_BYTES
            code = _code(raw_code)
            if code == 0:
                value = _text(raw_value)
                if value == "SECTION":
                    section = "?"
                elif value == "ENDSEC":
                    section = table = None
                elif section == "TABLES":
                    table = "?" if value == "TABLE" else (None if value == "ENDTAB" else table)
                elif section == "BLOCKS":
                    if value == "BLOCK":
                        block, in_header = None, True
                    else:
                        in_header = False
                        if block is not None and value not in ("ENDBLK", "VERTEX", "SEQEND"):
                            content[block][0] += 1
                entity = value
                record_handle = None
                if section == "BLOCKS" and block is not None and not in_header:
                    if entity != "ENDBLK":
                        content[block][1] += len(raw_code) + len(raw_value)
                continue
            if section == "BLOCKS" and block is not None and not in_header and entity != "ENDBLK":
                content[block][1] += len(raw_code) + len(raw_value)
            if section == "?" and code == 2:
                section = _text(raw_value)
                continue
            if code is None or not (
                code in _POINTER_CODES or code in _NAME_CODES or code in (5, 70)
            ):
                continue
            if section in _SMALL_SECTIONS:
                if section == "TABLES" and table == "?" and code == 2:
                    table = _text(raw_value)
                    continue
                if table == "BLOCK_RECORD" and entity == "BLOCK_RECORD":
                    if code == 5:
                        record_handle = _text(raw_value)
                    elif code == 2 and record_handle is not None:
                        name = _text(raw_value).casefold()
                        handles[record_handle] = name
                        names.add(name)
                    continue  # el BLOCK_RECORD no «usa» su propio bloque
                if code in _POINTER_CODES or code in _NAME_CODES:
                    raw_small[section].add(_text(raw_value))
                continue
            if section == "BLOCKS" and in_header:
                if code == 2 and block is None:
                    block = _text(raw_value).casefold()
                    original[block] = _text(raw_value)
                elif code == 70 and block is not None:
                    flags[block] = _code(raw_value) or 0
                continue
            if section not in ("BLOCKS", "ENTITIES", "OBJECTS"):
                continue
            if code not in _POINTER_CODES and code not in _NAME_CODES:
                continue  # handle propio (5) o indicadores (70): no apuntan a un bloque
            container = block if section == "BLOCKS" else section
            if container is None:
                continue
            value = _text(raw_value)
            if code in _POINTER_CODES:
                target = handles.get(value)
            else:
                target = value.casefold() if value.casefold() in names else None
            if target is not None and target != container:
                refs[container].add(target)

    for section, values in raw_small.items():
        for value in values:
            target = handles.get(value) or (value.casefold() if value.casefold() in names else None)
            if target is not None:
                refs[section].add(target)
    # Lo que se usa: los espacios de trabajo, las XREF, lo que nombran o apuntan la cabecera,
    # las tablas, las entidades y los objetos, y lo que usan esos bloques (recorrido completo).
    used = {name for name in names if name.startswith(_ROOT_PREFIXES)}
    used |= {name for name, value in flags.items() if value & _XREF_FLAGS}
    for root in ("HEADER", "TABLES", "ENTITIES", "OBJECTS"):
        used |= refs[root]
    pending = list(used)
    while pending:
        for child in refs.get(pending.pop(), ()):
            if child not in used:
                used.add(child)
                pending.append(child)
    return {
        name: (original.get(name, name), content[name][0], content[name][1])
        for name in flags
        if name not in used and content[name][1] > 0
    }


def write_without_blocks(source: Path, destination: Path, empty: set[str], progress=None) -> int:
    """Copia ``source`` a ``destination`` dejando vacíos los bloques de ``empty`` (sin
    mayúsculas). Segunda pasada; devuelve los bytes escritos. ``progress(bytes_leídos)`` se
    llama cada 16 MB."""
    section = None
    in_header = False
    target = False
    skipping = False
    written = read = next_report = 0
    with source.open("rb") as reader, destination.open("wb") as writer:
        for raw_code, raw_value in _pairs(reader):
            if progress is not None:
                read += len(raw_code) + len(raw_value)
                if read >= next_report:
                    progress(read)
                    next_report = read + PROGRESS_STEP_BYTES
            code = _code(raw_code)
            if code == 0:
                value = _text(raw_value)
                if value == "SECTION":
                    section = "?"
                elif value == "ENDSEC":
                    section = None
                if section == "BLOCKS" and value == "BLOCK":
                    in_header, target, skipping = True, False, False
                elif target and value == "ENDBLK":
                    target = skipping = False
                elif target:
                    in_header, skipping = False, True
                else:
                    in_header = False
            elif section == "?" and code == 2:
                section = _text(raw_value)
            elif in_header and code == 2 and _text(raw_value).casefold() in empty:
                target = True
            if skipping:
                continue
            writer.write(raw_code)
            writer.write(raw_value)
            written += len(raw_code) + len(raw_value)
    return written


_FOLLOWERS = ("ATTRIB", "SEQEND", "VERTEX")  # van detrás de su INSERT o su POLYLINE
# Códigos de punto X (10–18; su Y es el código + 10). Lo que no es un punto, por tipo.
_X_CODES = frozenset(range(10, 19))
_NOT_POINTS = {
    "TEXT": frozenset({11}),  # punto de alineación: (0, 0) si el texto no se alinea
    "MTEXT": frozenset({11}),  # dirección del eje X
    "SPLINE": frozenset({12, 13}),  # tangentes de inicio y fin
    "XLINE": frozenset({11}),  # dirección (la línea es infinita)
    "RAY": frozenset({11}),
    "HATCH": frozenset({11}),  # eje mayor de un borde elíptico (relativo a su centro)
    "DIMENSION": frozenset({12}),  # punto de inserción del bloque de la cota (suele ser 0, 0)
    "TOLERANCE": frozenset({11}),  # dirección
    "MULTILEADER": frozenset({11, 12, 13}),  # direcciones y normales
}
# IMAGE y WIPEOUT: 10 es la esquina; 11 y 12, los vectores de un píxel en U y en V; 13, el
# tamaño en píxeles; 14, el contorno de recorte en píxeles.
_IMAGES = frozenset({"IMAGE", "WIPEOUT"})


class _LooseEntity:
    """Una entidad suelta del Model Space mientras se lee: tipo, capa, puntos y vértices."""

    __slots__ = (
        "kind", "layer", "xs", "ys", "pending", "radius", "axis", "vertices", "pairs", "vectors",
    )

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.layer = "0"
        self.xs: list[float] = []
        self.ys: list[float] = []
        self.pending: dict[int, float] = {}
        self.radius = 0.0
        self.axis = [0.0, 0.0]
        self.vertices = 0
        self.pairs = 0  # pares 10/20 vistos (el primero de un HATCH es su elevación)
        self.vectors: dict[int, list[float]] = {}  # IMAGE: 11, 12 y 13

    def tag(self, code: int, raw: bytes) -> None:
        if code == 8:
            self.layer = _text(raw)
        elif self.kind == "HATCH" and (code == 98 or self.pairs < 0):
            # Tras el 98 vienen las semillas del rayado: puntos que AutoCAD no mueve con el
            # HATCH (pueden quedar lejos). No cuentan para la caja.
            self.pairs = -1
        elif code == 40 and self.kind in ("CIRCLE", "ARC"):
            self.radius = abs(_float(raw))
        elif self.kind == "ELLIPSE" and code in (11, 21):
            self.axis[0 if code == 11 else 1] = _float(raw)  # eje mayor, relativo al centro
        elif self.kind in _IMAGES and code in (11, 12, 13, 14, 21, 22, 23, 24):
            if code in (11, 12, 13):
                self.vectors.setdefault(code, [0.0, 0.0])[0] = _float(raw)
            elif code in (21, 22, 23):
                self.vectors.setdefault(code - 10, [0.0, 0.0])[1] = _float(raw)
        elif code in _X_CODES and code not in _NOT_POINTS.get(self.kind, ()):
            self.pending[code] = _float(raw)
        elif 20 <= code <= 28 and code - 10 in self.pending:
            x, y = self.pending.pop(code - 10), _float(raw)
            if code == 20:
                self.pairs += 1
                if self.kind == "LWPOLYLINE":
                    self.vertices += 1
                # Sin coordenadas reales: la cabecera de una POLYLINE (0, 0, elevación) y el
                # primer punto de un HATCH (su elevación).
                if self.kind == "POLYLINE" or (self.kind == "HATCH" and self.pairs == 1):
                    return
            if math.isfinite(x) and math.isfinite(y):
                self.xs.append(x)
                self.ys.append(y)

    def box(self) -> tuple[float, float, float, float] | None:
        if not self.xs:
            return None
        if self.kind in _IMAGES and {11, 12, 13} <= set(self.vectors):
            (ux, uy), (vx, vy), (width, height) = (self.vectors[code] for code in (11, 12, 13))
            x0, y0 = self.xs[0], self.ys[0]
            for a, b in ((width, 0.0), (0.0, height), (width, height)):
                self.xs.append(x0 + ux * a + vx * b)
                self.ys.append(y0 + uy * a + vy * b)
        grow = self.radius or math.hypot(*self.axis)
        return (min(self.xs) - grow, min(self.ys) - grow, max(self.xs) + grow, max(self.ys) + grow)


def _float(raw: bytes) -> float:
    try:
        return float(raw)
    except ValueError:
        return math.nan


def _merge(old, box):
    if old is None:
        return box
    return (min(old[0], box[0]), min(old[1], box[1]), max(old[2], box[2]), max(old[3], box[3]))


def write_selective(
    source: Path, destination: Path, empty: set[str], progress=None
) -> tuple[int, tuple[LooseLayer, ...]]:
    """Como ``write_without_blocks`` y, además, sin lo suelto del Model Space: de la sección
    ENTITIES solo pasan los INSERT (con sus ATTRIB y SEQEND) y lo del Paper Space (código 67
    = 1). Lo que no pasa se resume por capa. Devuelve los bytes escritos y el resumen.
    ``progress(bytes_leídos)`` se llama cada 16 MB."""
    section = None
    in_header = target = skipping = False
    written = read = next_report = 0
    buffer: list[bytes] = []
    kind = None
    paper = False
    parent_kept = False
    parent_layer = "0"
    current: _LooseEntity | None = None
    layers: dict[str, list] = {}  # capa -> [entidades, vértices, tipos, caja]

    def flush(writer) -> None:
        nonlocal written, parent_kept, parent_layer
        if not buffer:
            return
        if kind in _FOLLOWERS:
            keep = parent_kept
        else:
            keep = paper or kind == "INSERT"
            parent_kept = keep
            parent_layer = current.layer
        if keep:
            data = b"".join(buffer)
            writer.write(data)
            written += len(data)
        else:
            layer = parent_layer if kind in _FOLLOWERS else current.layer
            entry = layers.setdefault(layer, [0, 0, defaultdict(int), None])
            if kind not in _FOLLOWERS:
                entry[0] += 1
                entry[2][kind] += 1
            entry[1] += 1 if kind == "VERTEX" else current.vertices
            box = current.box()
            if box is not None:
                entry[3] = _merge(entry[3], box)
        buffer.clear()

    with source.open("rb") as reader, destination.open("wb") as writer:
        for raw_code, raw_value in _pairs(reader):
            if progress is not None:
                read += len(raw_code) + len(raw_value)
                if read >= next_report:
                    progress(read)
                    next_report = read + PROGRESS_STEP_BYTES
            code = _code(raw_code)
            if code == 0:
                value = _text(raw_value)
                if section == "ENTITIES":
                    flush(writer)
                    if value != "ENDSEC":
                        kind = value
                        if kind not in _FOLLOWERS:
                            paper = False
                        current = _LooseEntity(kind)
                        buffer.extend((raw_code, raw_value))
                        continue
                if value == "SECTION":
                    section = "?"
                elif value == "ENDSEC":
                    section = None
                if section == "BLOCKS" and value == "BLOCK":
                    in_header, target, skipping = True, False, False
                elif target and value == "ENDBLK":
                    target = skipping = False
                elif target:
                    in_header, skipping = False, True
                else:
                    in_header = False
            elif section == "ENTITIES" and buffer:
                buffer.extend((raw_code, raw_value))
                if code == 67 and kind not in _FOLLOWERS:
                    paper = raw_value.strip() == b"1"
                elif code is not None:
                    current.tag(code, raw_value)
                continue
            elif section == "?" and code == 2:
                section = _text(raw_value)
            elif in_header and code == 2 and _text(raw_value).casefold() in empty:
                target = True
            if skipping:
                continue
            writer.write(raw_code)
            writer.write(raw_value)
            written += len(raw_code) + len(raw_value)
        flush(writer)
    loose = tuple(
        LooseLayer(layer, entry[0], entry[1], dict(sorted(entry[2].items())), entry[3])
        for layer, entry in sorted(layers.items(), key=lambda item: item[0].casefold())
    )
    return written, loose


def prune_unused_blocks(source: Path, destination: Path, digest=None) -> PrunedSource:
    """Escribe en ``destination`` una copia de ``source`` sin el contenido de los bloques que
    no se usan (dos pasadas por partes). El DXF de origen no se modifica. Con ``digest``, la
    primera pasada le suma el contenido del archivo (ver ``unused_blocks``)."""
    started = time.perf_counter()
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("La copia no puede sobrescribir el DXF de origen")
    unused = unused_blocks(source, digest)
    written = write_without_blocks(source, destination, set(unused))
    emptied = tuple(
        sorted(
            (EmptiedBlock(name, entities, size) for name, entities, size in unused.values()),
            key=lambda item: -item.bytes,
        )
    )
    return PrunedSource(source.stat().st_size, written, emptied, time.perf_counter() - started)


def _megabytes(size: int) -> str:
    return f"{size / 1_000_000:.1f}".replace(".", ",") + " MB"


def pruning_message(report: PrunedSource) -> str:
    biggest = ", ".join(f"«{item.name}» ({_megabytes(item.bytes)})" for item in report.emptied[:3])
    count = len(report.emptied)
    return (
        f"El DXF de origen ({_megabytes(report.source_bytes)}) se leyó desde una copia temporal "
        f"de {_megabytes(report.copy_bytes)} sin el contenido de {count} "
        f"{'bloque que no se usa' if count == 1 else 'bloques que no se usan'} en ningún sitio "
        f"({'el mayor' if count == 1 else 'los mayores'}: {biggest}). El DXF de origen no "
        "cambia y el dibujo es el mismo."
    )


def pruning_warning(report: PrunedSource) -> ExtractionWarning:
    return ExtractionWarning(SOURCE_PRUNED_CODE, Severity.INFO, pruning_message(report))


def loose_skipped_message(report: PrunedSource) -> str:
    """Aviso ``SOURCE_MODELSPACE_LOOSE_SKIPPED`` de la copia selectiva."""
    count = report.loose_entities
    vertices = sum(item.vertices for item in report.loose)
    return (
        f"El DXF de origen ({_megabytes(report.source_bytes)}) es tan grande que se leyó sin "
        f"sus {count} entidades sueltas del Model Space (fuera de cualquier bloque; "
        f"{vertices} vértices): los bloques no dependen de ellas y su dibujo es el mismo. No "
        "se comprobó si alguna, en una capa apagada o congelada, cae sobre la zona del bloque."
    )


def loose_skipped_warning(report: PrunedSource) -> ExtractionWarning:
    return ExtractionWarning(SOURCE_LOOSE_CODE, Severity.INFO, loose_skipped_message(report))
