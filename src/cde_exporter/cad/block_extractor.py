"""Selección y copia de referencias individuales de bloques."""

from __future__ import annotations

import difflib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

import ezdxf
from ezdxf import xref
from ezdxf.document import Drawing

from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.reader import entity_bounds, read_dxf


@dataclass(frozen=True, slots=True)
class BlockReference:
    order: int
    name: str
    layer: str
    handle: str | None
    insert: tuple[float, float, float]
    rotation: float
    xscale: float
    yscale: float
    zscale: float


@dataclass(frozen=True, slots=True)
class BlockExtraction:
    reference: BlockReference
    definition_entity_count: int
    units_code: int
    unbounded: bool


def references_for_block(document: Drawing, name: str) -> tuple[BlockReference, ...]:
    return tuple(
        BlockReference(
            order=order,
            name=entity.dxf.name,
            layer=str(entity.dxf.get("layer", "0")),
            handle=entity.dxf.get("handle"),
            insert=tuple(float(value) for value in entity.dxf.insert),
            rotation=float(entity.dxf.get("rotation", 0)),
            xscale=float(entity.dxf.get("xscale", 1)),
            yscale=float(entity.dxf.get("yscale", 1)),
            zscale=float(entity.dxf.get("zscale", 1)),
        )
        for order, entity in enumerate(
            (
                item
                for item in document.modelspace()
                if item.dxftype() == "INSERT" and item.dxf.name == name
            ),
            start=1,
        )
    )


def _similar_names(document: Drawing, name: str) -> str:
    matches = difflib.get_close_matches(
        name, [block.name for block in document.blocks], n=5, cutoff=0.35
    )
    return f" Nombres parecidos: {', '.join(matches)}." if matches else ""


def _reference_rows(references: tuple[BlockReference, ...]) -> str:
    return "\n".join(
        f"  orden {ref.order} | capa {ref.layer} | handle {ref.handle or '(sin handle)'} | "
        f"inserción {', '.join(f'{value:g}' for value in ref.insert)}"
        for ref in references
    )


def extract_block_reference(
    source: str | Path,
    block_name: str,
    occurrence: int,
    output: str | Path,
    *,
    overwrite: bool = False,
) -> BlockExtraction:
    source_path, destination = Path(source), Path(output)
    if source_path.resolve() == destination.resolve():
        raise CadProcessingError("La extracción no puede sobrescribir el DXF original")
    document = read_dxf(source_path)
    if block_name not in document.blocks:
        raise CadProcessingError(
            f"No existe el bloque '{block_name}'.{_similar_names(document, block_name)}"
        )
    references = references_for_block(document, block_name)
    if not references:
        raise CadProcessingError(f"El bloque '{block_name}' no tiene referencias en modelspace")
    if occurrence < 1 or occurrence > len(references):
        raise CadProcessingError(
            f"La ocurrencia {occurrence} no existe para '{block_name}' "
            f"(hay {len(references)} referencia(s))."
        )
    reference = references[occurrence - 1]
    entity = [
        item
        for item in document.modelspace()
        if item.dxftype() == "INSERT" and item.dxf.name == block_name
    ][occurrence - 1]
    target = ezdxf.new(document.dxfversion)
    loader = xref.Loader(document, target)
    loader.load_modelspace(filter_fn=lambda candidate: candidate is entity)
    loader.load_block_layout(document.blocks[block_name])
    loader.execute()
    target.header["$INSUNITS"] = int(document.header.get("$INSUNITS", 0) or 0)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise CadProcessingError(f"El archivo de salida ya existe: {destination}")
    temporary = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.tmp.dxf")
    try:
        target.saveas(temporary)
        if overwrite:
            os.replace(temporary, destination)
        else:
            temporary.rename(destination)
    except (ezdxf.DXFError, OSError, ValueError) as error:
        raise CadProcessingError(f"No se pudo escribir el DXF del bloque: {error}") from error
    finally:
        temporary.unlink(missing_ok=True)
    definition = document.blocks[block_name]
    return BlockExtraction(
        reference=reference,
        definition_entity_count=len(definition),
        units_code=int(document.header.get("$INSUNITS", 0) or 0),
        unbounded=_definition_has_unbounded(document, block_name, set()),
    )


def _definition_has_unbounded(document: Drawing, name: str, visited: set[str]) -> bool:
    if name in visited:
        return False
    visited.add(name)
    try:
        block = document.blocks[name]
    except ezdxf.DXFError:
        return True
    for entity in block:
        if entity.dxftype() == "INSERT":
            if _definition_has_unbounded(document, entity.dxf.name, visited):
                return True
        elif entity_bounds(entity) is None:
            return True
    return False
