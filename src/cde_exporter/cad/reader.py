"""Apertura e inspección de dibujos DXF."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import ezdxf
from ezdxf import bbox
from ezdxf.document import Drawing
from ezdxf.entities import DXFEntity
from ezdxf.math import BoundingBox
from ezdxf.units import unit_name

from cde_exporter.cad import run_hooks
from cde_exporter.cad.models import Bounds2D, CadProcessingError, CadWindow, DxfInspection

UNBOUNDED_DIAGNOSTIC_TYPES = frozenset({"3DSOLID", "MTEXT", "POLYLINE", "REGION"})


@dataclass(frozen=True, slots=True)
class UnboundedEntityDiagnostic:
    modelspace_order: int
    entity_type: str
    layer: str
    handle: str | None
    position: tuple[float, float, float] | None
    position_note: str


@dataclass(frozen=True, slots=True)
class InsertContentsDiagnostic:
    modelspace_order: int
    layer: str
    handle: str | None
    block_name: str
    insert_position: tuple[float, float, float]
    unbounded_by_type: dict[str, int]
    insert_inside_window: bool | None
    partial_bounds: Bounds2D | None
    partial_bounds_touch_window: bool | None


@dataclass(frozen=True, slots=True)
class UnboundedDiagnosticReport:
    entities: tuple[UnboundedEntityDiagnostic, ...]
    counts_by_type: dict[str, int]
    counts_by_layer: dict[str, int]
    inside_window: int | None
    outside_window: int | None
    without_position: int | None
    block_inserts: tuple[InsertContentsDiagnostic, ...]
    nested_counts_by_type: dict[str, int]
    modelspace_counts_by_type: dict[str, int]
    inspect_counts_by_type: dict[str, int]
    inspect_total: int


@dataclass(frozen=True, slots=True)
class InvalidEntity:
    """Entidad que ezdxf no pudo medir (datos inválidos), con su origen para el aviso."""

    entity_type: str
    handle: str | None
    layer: str
    block: str | None
    reason: str
    entity: object | None = None


def _original(entity):
    """Entidad de la definición de la que procede una copia virtual (o la misma)."""
    return getattr(entity, "source_of_copy", None) or entity


def _with_entity_hook(items):
    for entity in items:
        run_hooks.entity(-1)
        yield entity


def safe_extents(entities, *, block: str | None = None):
    """Caja de ``entities`` que salta las entidades cuya medición lanza un error.

    Primero intenta medirlo todo de una vez (el caso normal, igual de rápido). Si falla,
    mide entidad por entidad; los INSERT que fallan se abren con ``virtual_entities()``
    para saltar solo la entidad dañada y no el bloque entero. Devuelve la caja y las
    entidades saltadas. ezdxf puede lanzar cualquier excepción ante datos inválidos
    (p. ej. un override de DIMSTYLE que apunta a un XRECORD), por eso se captura todo.
    ``entities`` debe poder recorrerse dos veces (un layout o una lista).
    """
    def checked(items):
        # Entre entidad y entidad se puede cancelar (run_hooks); sin gancho, las mismas
        # entidades sin envolver (ezdxf recibe el layout tal cual).
        if not run_hooks.entity_hook_active():
            return items
        return _with_entity_hook(items)

    try:
        return bbox.extents(checked(entities)), ()
    except run_hooks.DrawingCancelled:
        raise
    except Exception:  # noqa: BLE001 - se mide de nuevo entidad por entidad
        pass
    box = BoundingBox()
    invalid: list[InvalidEntity] = []

    def measure(items, owner: str | None) -> None:
        for entity in checked(items):
            try:
                extents = bbox.extents([entity])
            except Exception as error:  # noqa: BLE001 - ver la docstring
                if entity.dxftype() == "INSERT":
                    try:
                        children = list(entity.virtual_entities())
                    except Exception:  # noqa: BLE001 - el INSERT entero es inválido
                        children = None
                    if children is not None:
                        measure(children, entity.dxf.name)
                        continue
                original = _original(entity)
                invalid.append(
                    InvalidEntity(
                        entity_type=entity.dxftype(),
                        handle=original.dxf.get("handle"),
                        layer=str(original.dxf.get("layer", "0") or "0"),
                        block=owner,
                        reason=f"{type(error).__name__}: {error}",
                        entity=original,
                    )
                )
                continue
            if extents.has_data:
                box.extend([extents.extmin, extents.extmax])

    measure(entities, block)
    return box, tuple(invalid)


def read_dxf(path: str | Path) -> Drawing:
    """Abre un DXF; no convierte ni intenta interpretar archivos DWG."""
    source = Path(path)
    if source.suffix.lower() != ".dxf":
        raise CadProcessingError(
            f"Formato CAD no admitido: '{source.suffix or '(sin extensión)'}'. "
            "Este flujo lee DXF; DWG requiere un adaptador CAD externo."
        )
    if not source.exists():
        raise CadProcessingError(f"No existe el dibujo: {source}")
    if not source.is_file():
        raise CadProcessingError(f"La ruta CAD no es un archivo: {source}")
    try:
        return ezdxf.readfile(source)
    except (ezdxf.DXFError, OSError, UnicodeError, ValueError) as error:
        raise CadProcessingError(f"No se pudo leer el DXF '{source}': {error}") from error


def entity_bounds(entity: DXFEntity) -> Bounds2D | None:
    """Devuelve la caja XY calculada por ezdxf, o None si no puede acotarla."""
    try:
        extent = bbox.extents([entity])
        if not extent.has_data:
            return None
        return (
            float(extent.extmin.x),
            float(extent.extmin.y),
            float(extent.extmax.x),
            float(extent.extmax.y),
        )
    except Exception:  # noqa: BLE001 - datos inválidos: ezdxf puede lanzar cualquier error
        return None


def _walk_referenced_entities(document: Drawing) -> list[DXFEntity]:
    """Visita entidades de modelspace y bloques insertados, evitando ciclos."""
    result: list[DXFEntity] = []
    visited_blocks: set[str] = set()
    stack = list(document.modelspace())
    while stack:
        entity = stack.pop()
        result.append(entity)
        if entity.dxftype() != "INSERT":
            continue
        try:
            block = entity.block()
        except (KeyError, ezdxf.DXFError):
            block = None
        if block is None or block.block.is_xref or block.name in visited_blocks:
            continue
        visited_blocks.add(block.name)
        stack.extend(block)
    return result


def entities_without_bounds(document: Drawing) -> dict[str, int]:
    """Cuenta entidades de modelspace/bloques insertados sin caja calculable."""
    counts: Counter[str] = Counter()
    for entity in _walk_referenced_entities(document):
        if entity_bounds(entity) is None:
            counts[entity.dxftype()] += 1
            if entity.dxftype() == "INSERT":
                try:
                    block = entity.block()
                except (KeyError, ezdxf.DXFError):
                    block = None
                if block is None or block.block.is_xref:
                    counts["XREF_OR_UNRESOLVED_BLOCK"] += 1
    return dict(sorted(counts.items()))


def _layer_name(entity: DXFEntity) -> str | None:
    """Return layer name, or None if the DXF entity type has no layer attribute."""
    if not entity.is_supported_dxf_attrib("layer"):
        return None
    return str(entity.dxf.get("layer", "0") or "0")


def ensure_layer_table_entries(document: Drawing) -> None:
    """Añade entradas ACI 7 predeterminadas para capas referenciadas sin tabla."""
    layouts = [document.modelspace(), *document.blocks]
    for entity_layout in layouts:
        for entity in entity_layout:
            name = _layer_name(entity)
            if name is not None and name not in document.layers:
                document.layers.new(name)


def inspect_dxf(path: str | Path) -> DxfInspection:
    """Resume unidades, capas, tipos de entidad y extensiones de modelspace."""
    source = Path(path)
    document = read_dxf(source)
    modelspace = list(document.modelspace())
    type_counts = Counter(entity.dxftype() for entity in modelspace)
    layer_counts: Counter[str] = Counter()
    for entity in modelspace:
        name = _layer_name(entity)
        if name is None:
            name = f"<sin atributo layer: {entity.dxftype()}>"
        layer_counts[name] += 1
    extent = bbox.extents(modelspace)
    extents = None
    if extent.has_data:
        extents = (
            float(extent.extmin.x),
            float(extent.extmin.y),
            float(extent.extmax.x),
            float(extent.extmax.y),
        )
    units_code = int(document.header.get("$INSUNITS", 0) or 0)
    try:
        units_label = unit_name(units_code)
    except (KeyError, TypeError, ValueError):
        units_label = f"código {units_code}"
    return DxfInspection(
        path=source,
        entity_count=len(modelspace),
        entity_types=dict(sorted(type_counts.items())),
        layers=dict(sorted(layer_counts.items())),
        units_code=units_code,
        units_name=units_label,
        extents=extents,
        entities_without_bounds=entities_without_bounds(document),
        block_names=tuple(
            sorted(block.name for block in document.blocks if not block.name.startswith("*"))
        ),
    )


def _diagnostic_position(
    entity: DXFEntity, entity_type: str
) -> tuple[tuple[float, float, float] | None, str]:
    if entity_type in {"3DSOLID", "REGION"}:
        return None, "sin posición fiable"
    if entity_type == "MTEXT":
        if not entity.dxf.hasattr("insert"):
            return None, "sin punto de inserción"
        position = entity.dxf.get("insert")
        return tuple(float(value) for value in position), "punto de inserción MTEXT"
    if entity_type == "POLYLINE":
        vertices = entity.vertices
        if not vertices:
            return None, "sin vértices"
        position = vertices[0].dxf.get("location")
        return tuple(float(value) for value in position), "primer vértice POLYLINE"
    return None, "sin posición disponible"


def _iter_insert_contents(
    insert: DXFEntity, visited_blocks: set[str]
):
    """Yield block members once per block definition, including nested INSERTs."""
    try:
        block = insert.block()
    except (KeyError, ezdxf.DXFError):
        block = None
    if block is None or block.block.is_xref or block.name in visited_blocks:
        return
    visited_blocks.add(block.name)
    for entity in block:
        yield entity
        if entity.dxftype() == "INSERT":
            yield from _iter_insert_contents(entity, visited_blocks)


def _position_touches_window(
    position: tuple[float, float, float], window: CadWindow
) -> bool:
    return (
        window.x_min <= position[0] <= window.x_max
        and window.y_min <= position[1] <= window.y_max
    )


def _bounds_touch_window(bounds: Bounds2D, window: CadWindow) -> bool:
    return window.intersects(bounds)


def diagnose_unbounded_entities(
    path: str | Path, window: CadWindow | None = None
) -> UnboundedDiagnosticReport:
    """List supported modelspace types for which ezdxf returns no XY bounds."""
    document = read_dxf(path)
    rows: list[UnboundedEntityDiagnostic] = []
    type_counts: Counter[str] = Counter()
    layer_counts: Counter[str] = Counter()
    inside = outside = without_position = 0
    block_rows: list[InsertContentsDiagnostic] = []
    nested_counts: Counter[str] = Counter()
    modelspace_counts: Counter[str] = Counter()
    visited_blocks: set[str] = set()

    modelspace = list(document.modelspace())
    for order, entity in enumerate(modelspace, start=1):
        entity_type = entity.dxftype()
        if entity_type not in UNBOUNDED_DIAGNOSTIC_TYPES or entity_bounds(entity) is not None:
            if entity_type == "INSERT":
                insert_counts: Counter[str] = Counter()
                for nested in _iter_insert_contents(entity, set()):
                    if entity_bounds(nested) is None:
                        insert_counts[nested.dxftype()] += 1
                if insert_counts:
                    block = entity.block()
                    position = tuple(float(value) for value in entity.dxf.get("insert"))
                    partial_bounds = entity_bounds(entity) if window is not None else None
                    block_rows.append(
                        InsertContentsDiagnostic(
                            modelspace_order=order,
                            layer=_layer_name(entity) or "<sin atributo layer>",
                            handle=entity.dxf.get("handle"),
                            block_name=block.name if block is not None else entity.dxf.get("name"),
                            insert_position=position,
                            unbounded_by_type=dict(sorted(insert_counts.items())),
                            insert_inside_window=(
                                _position_touches_window(position, window)
                                if window is not None
                                else None
                            ),
                            partial_bounds=partial_bounds,
                            partial_bounds_touch_window=(
                                _bounds_touch_window(partial_bounds, window)
                                if partial_bounds is not None and window is not None
                                else None
                            ),
                        )
                    )
            continue
        layer = _layer_name(entity) or "<sin atributo layer>"
        position, position_note = _diagnostic_position(entity, entity_type)
        rows.append(
            UnboundedEntityDiagnostic(
                modelspace_order=order,
                entity_type=entity_type,
                layer=layer,
                handle=entity.dxf.get("handle"),
                position=position,
                position_note=position_note,
            )
        )
        type_counts[entity_type] += 1
        layer_counts[layer] += 1
        if window is not None:
            if position is None:
                without_position += 1
            elif (
                window.x_min <= position[0] <= window.x_max
                and window.y_min <= position[1] <= window.y_max
            ):
                inside += 1
            else:
                outside += 1

    for entity in modelspace:
        if entity_bounds(entity) is None:
            modelspace_counts[entity.dxftype()] += 1
        if entity.dxftype() == "INSERT":
            for nested in _iter_insert_contents(entity, visited_blocks):
                if entity_bounds(nested) is None:
                    nested_counts[nested.dxftype()] += 1

    inspect_counts = entities_without_bounds(document)

    return UnboundedDiagnosticReport(
        entities=tuple(rows),
        counts_by_type=dict(sorted(type_counts.items())),
        counts_by_layer=dict(sorted(layer_counts.items())),
        inside_window=inside if window is not None else None,
        outside_window=outside if window is not None else None,
        without_position=without_position if window is not None else None,
        block_inserts=tuple(block_rows),
        nested_counts_by_type=dict(sorted(nested_counts.items())),
        modelspace_counts_by_type=dict(sorted(modelspace_counts.items())),
        inspect_counts_by_type=inspect_counts,
        inspect_total=sum(inspect_counts.values()),
    )
