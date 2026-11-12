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

Solo usa la biblioteca estándar (más ``detail_model`` para el aviso).
"""

from __future__ import annotations

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
class PrunedSource:
    """Resultado de la copia: tamaños, bloques vaciados y tiempo."""

    source_bytes: int
    copy_bytes: int
    emptied: tuple[EmptiedBlock, ...]
    seconds: float


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


def unused_blocks(path: Path) -> dict[str, tuple[str, int, int]]:
    """Bloques que nada usa (clave: nombre sin mayúsculas), con su nombre, las entidades y los
    bytes de su contenido. Primera pasada."""
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

    with path.open("rb") as stream:
        for raw_code, raw_value in _pairs(stream):
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


def write_without_blocks(source: Path, destination: Path, empty: set[str]) -> int:
    """Copia ``source`` a ``destination`` dejando vacíos los bloques de ``empty`` (sin
    mayúsculas). Segunda pasada; devuelve los bytes escritos."""
    section = None
    in_header = False
    target = False
    skipping = False
    written = 0
    with source.open("rb") as reader, destination.open("wb") as writer:
        for raw_code, raw_value in _pairs(reader):
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


def prune_unused_blocks(source: Path, destination: Path) -> PrunedSource:
    """Escribe en ``destination`` una copia de ``source`` sin el contenido de los bloques que
    no se usan (dos pasadas por partes). El DXF de origen no se modifica."""
    started = time.perf_counter()
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("La copia no puede sobrescribir el DXF de origen")
    unused = unused_blocks(source)
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
