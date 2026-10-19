"""Extracción geométrica de una región DXF a un documento independiente."""

from __future__ import annotations

import os
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import ezdxf
from ezdxf import xref
from ezdxf.document import Drawing

from cde_exporter.cad.models import (
    CadProcessingError,
    CadWindow,
    UnboundedSelectionDiagnostic,
    WindowSelection,
)
from cde_exporter.cad.reader import ensure_layer_table_entries, entity_bounds, read_dxf
from cde_exporter.cad.selection import select_by_window


@dataclass(frozen=True, slots=True)
class ExtractionReport:
    source: Path
    output: Path
    window: CadWindow
    source_entity_count: int
    extracted_entity_count: int
    entity_types: dict[str, int]
    bounds: tuple[float, float, float, float]
    units_code: int
    unbounded_entities: tuple[UnboundedSelectionDiagnostic, ...] = ()


def _write_extraction(
    target: Drawing,
    destination: Path,
    expected_types: dict[str, int],
    expected_geometry: tuple[tuple[str, tuple[float, ...]], ...],
    overwrite: bool,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise CadProcessingError(
            f"El archivo de salida ya existe: {destination}. Usa --overwrite para reemplazarlo."
        )
    temporary = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.tmp.dxf")
    try:
        target.saveas(temporary)
        check = read_dxf(temporary)
        actual_types = Counter(entity.dxftype() for entity in check.modelspace())
        if dict(sorted(actual_types.items())) != expected_types:
            raise CadProcessingError(
                "La validación del DXF extraído detectó una diferencia en los tipos "
                "o cantidades de entidades."
            )
        actual_geometry = _geometry_signature(check.modelspace())
        if actual_geometry != expected_geometry:
            raise CadProcessingError(
                "La validación del DXF extraído detectó una diferencia en las "
                "extensiones de las entidades seleccionadas."
            )
        if overwrite:
            os.replace(temporary, destination)
        else:
            temporary.rename(destination)
    except (ezdxf.DXFError, OSError, ValueError) as error:
        raise CadProcessingError(f"No se pudo escribir el DXF extraído: {error}") from error
    finally:
        temporary.unlink(missing_ok=True)


def _geometry_signature(entities) -> tuple[tuple[str, tuple[float, ...]], ...]:
    signature = []
    for entity in entities:
        bounds = entity_bounds(entity)
        if bounds is None:
            raise CadProcessingError(
                f"No se pudo validar la geometría de la entidad {entity.dxftype()}"
            )
        signature.append((entity.dxftype(), tuple(round(value, 8) for value in bounds)))
    return tuple(sorted(signature))


def extract_dxf_region(
    source: str | Path,
    window: CadWindow,
    output: str | Path,
    *,
    overwrite: bool = False,
    allow_unbounded: bool = False,
) -> ExtractionReport:
    """Copia entidades que intersectan una ventana a un DXF independiente."""
    source_path = Path(source)
    destination = Path(output)
    if destination.suffix.lower() != ".dxf":
        raise CadProcessingError("La ruta de salida de la extracción debe terminar en .dxf")
    try:
        if source_path.resolve() == destination.resolve():
            raise CadProcessingError("La extracción no puede sobrescribir el DXF original")
    except OSError as error:
        raise CadProcessingError(f"No se pudieron resolver las rutas CAD: {error}") from error

    source_document = read_dxf(source_path)
    selection: WindowSelection = select_by_window(
        source_document, window, allow_unbounded=allow_unbounded
    )
    try:
        target = xref.write_block(selection.entities, origin=(0, 0, 0))
    except (ezdxf.DXFError, OSError, ValueError) as error:
        raise CadProcessingError(
            f"No se pudieron copiar las entidades seleccionadas: {error}"
        ) from error
    units_code = int(source_document.header.get("$INSUNITS", 0) or 0)
    target.header["$INSUNITS"] = units_code
    ensure_layer_table_entries(target)
    _write_extraction(
        target,
        destination,
        selection.entity_types,
        _geometry_signature(selection.entities),
        overwrite,
    )
    return ExtractionReport(
        source=source_path,
        output=destination,
        window=window,
        source_entity_count=selection.source_entity_count,
        extracted_entity_count=len(selection.entities),
        entity_types=selection.entity_types,
        bounds=selection.bounds,
        units_code=units_code,
        unbounded_entities=selection.unbounded_entities,
    )
