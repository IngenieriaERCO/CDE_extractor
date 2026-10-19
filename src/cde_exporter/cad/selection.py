"""Selección de entidades por intersección de cajas XY con una ventana."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import ezdxf
from ezdxf.document import Drawing

from cde_exporter.cad.models import (
    CadProcessingError,
    CadWindow,
    UnboundedSelectionDiagnostic,
    WindowSelection,
)
from cde_exporter.cad.reader import (
    _diagnostic_position,
    _layer_name,
    entities_without_bounds,
    entity_bounds,
    read_dxf,
)


def select_by_window(
    drawing: Drawing | str | Path,
    window: CadWindow,
    *,
    allow_unbounded: bool = False,
) -> WindowSelection:
    """Selecciona entidades completas cuya caja XY toca la ventana solicitada.

    La selección usa modelspace en la vista XY. No recorta entidades que crucen
    el borde. La operación se detiene si alguna entidad o bloque insertado no
    puede acotarse, porque no sería posible demostrar una selección completa.
    """
    document = read_dxf(drawing) if isinstance(drawing, (str, Path)) else drawing
    unsupported = entities_without_bounds(document)
    if unsupported and not allow_unbounded:
        details = ", ".join(f"{name} ({count})" for name, count in unsupported.items())
        raise CadProcessingError(
            "No se puede garantizar la selección por coordenadas; "
            f"ezdxf no pudo calcular cajas para: {details}"
        )

    selected = []
    selected_bounds = []
    modelspace = list(document.modelspace())
    unbounded_rows: list[UnboundedSelectionDiagnostic] = []
    for order, entity in enumerate(modelspace, start=1):
        bounds = entity_bounds(entity)
        if entity.dxftype() == "INSERT" and allow_unbounded:
            partial_touches = bounds is not None and window.intersects(bounds)
            layer = _layer_name(entity) or "<sin atributo layer>"
            if bounds is None:
                unbounded_rows.append(
                    UnboundedSelectionDiagnostic(
                        order,
                        "INSERT",
                        layer,
                        entity.dxf.get("handle"),
                        entity.dxf.get("name", "<bloque sin nombre>"),
                        "no verificado",
                    )
                )
            for nested, block_name in _iter_insert_contents_with_blocks(entity):
                if entity_bounds(nested) is not None:
                    continue
                unbounded_rows.append(
                    UnboundedSelectionDiagnostic(
                        order,
                        nested.dxftype(),
                        _layer_name(nested) or "<sin atributo layer>",
                        nested.dxf.get("handle"),
                        block_name,
                        "incluido en la selección" if partial_touches else "no verificado",
                    )
                )
        if bounds is None:
            entity_type = entity.dxftype()
            layer = _layer_name(entity) or "<sin atributo layer>"
            if entity_type != "INSERT":
                position, _ = _diagnostic_position(entity, entity_type)
                if (
                    allow_unbounded
                    and position is not None
                    and window.x_min <= position[0] <= window.x_max
                    and window.y_min <= position[1] <= window.y_max
                ):
                    raise CadProcessingError(
                        f"No se puede continuar con --allow-unbounded: la entidad "
                        f"{entity_type} (handle {entity.dxf.get('handle') or '(sin handle)'}, "
                        f"orden {order}) no tiene caja y su posición conocida cae dentro "
                        "de la ventana."
                    )
                if allow_unbounded:
                    unbounded_rows.append(
                        UnboundedSelectionDiagnostic(
                            order, entity_type, layer, entity.dxf.get("handle"), None,
                            "no verificado",
                        )
                    )
            continue
        if window.intersects(bounds):
            selected.append(entity)
            selected_bounds.append(bounds)
    if not selected:
        raise CadProcessingError("La ventana no intersecta ninguna entidad de modelspace")
    aggregate = (
        min(bounds[0] for bounds in selected_bounds),
        min(bounds[1] for bounds in selected_bounds),
        max(bounds[2] for bounds in selected_bounds),
        max(bounds[3] for bounds in selected_bounds),
    )
    type_counts = Counter(entity.dxftype() for entity in selected)
    return WindowSelection(
        entities=tuple(selected),
        bounds=aggregate,
        source_entity_count=len(modelspace),
        entity_types=dict(sorted(type_counts.items())),
        unbounded_entities=tuple(unbounded_rows),
    )


def _iter_insert_contents_with_blocks(insert):
    """Yield nested block entities with their definition name, avoiding cycles."""
    visited: set[str] = set()
    stack = [insert]
    while stack:
        current = stack.pop()
        try:
            block = current.block()
        except (KeyError, ezdxf.DXFError):
            continue
        if block is None or block.block.is_xref or block.name in visited:
            continue
        visited.add(block.name)
        members = list(block)
        for entity in members:
            yield entity, block.name
        stack.extend(entity for entity in members if entity.dxftype() == "INSERT")
