"""InspecciÃ³n de bloques referenciados en modelspace."""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import ezdxf
from ezdxf.document import Drawing

from cde_exporter.cad.reader import entity_bounds, read_dxf


@dataclass(frozen=True, slots=True)
class BlockSummary:
    name: str
    references: int
    handles: tuple[str | None, ...]
    layers: tuple[str, ...]
    anonymous: bool
    unbounded: bool


def _normalized(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.casefold())
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


def _has_unbounded(document: Drawing, block_name: str, visited: set[str]) -> bool:
    if block_name in visited:
        return False
    visited.add(block_name)
    try:
        block = document.blocks[block_name]
    except ezdxf.DXFError:
        return True
    for entity in block:
        if entity.dxftype() == "INSERT":
            if _has_unbounded(document, entity.dxf.name, visited):
                return True
        elif entity_bounds(entity) is None:
            return True
    return False


def list_referenced_blocks(
    source: str | Path, *, include_anonymous: bool = False, filter_text: str = ""
) -> tuple[BlockSummary, ...]:
    document = read_dxf(source)
    references: dict[str, list[str]] = defaultdict(list)
    handles: dict[str, list[str | None]] = defaultdict(list)
    for insert in document.modelspace().query("INSERT"):
        name = insert.dxf.name
        references[name].append(str(insert.dxf.get("layer", "0")))
        handles[name].append(insert.dxf.get("handle"))
    match = _normalized(filter_text)
    rows = []
    for name, layers in references.items():
        anonymous = name.startswith("*")
        if anonymous and not include_anonymous:
            continue
        if match and match not in _normalized(name):
            continue
        rows.append(
            BlockSummary(
                name=name,
                references=len(layers),
                handles=tuple(handles[name]),
                layers=tuple(sorted(set(layers), key=str.casefold)),
                anonymous=anonymous,
                unbounded=_has_unbounded(document, name, set()),
            )
        )
    return tuple(sorted(rows, key=lambda row: row.name.casefold()))


def format_block_catalog(
    rows: tuple[BlockSummary, ...], *, include_handles: bool = False
) -> str:
    lines = ["Bloque | referencias | capas | anónimo | contenido sin caja"]
    if include_handles:
        lines[0] += " | handles por ocurrencia"
    for row in rows:
        line = (
            f"{row.name} | {row.references} | {', '.join(row.layers)} | "
            f"{'sí' if row.anonymous else 'no'} | {'sí' if row.unbounded else 'no'}"
        )
        if include_handles:
            shown = [
                f"{index}: {handle or '(sin handle)'}"
                for index, handle in enumerate(row.handles[:10], start=1)
            ]
            remainder = len(row.handles) - len(shown)
            if remainder:
                shown.append(f"… y {remainder} más")
            line += " | " + ", ".join(shown)
        lines.append(line)
    if not rows:
        lines.append("No se encontraron bloques referenciados con esos filtros.")
    return "\n".join(lines) + "\n"
