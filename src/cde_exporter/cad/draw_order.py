"""Orden de dibujo del DXF (tabla ``ACAD_SORTENTS``, la que escribe DRAWORDER de AutoCAD).

AutoCAD dibuja cada layout y cada bloque en el orden de su tabla de orden de dibujo: cada
entidad tiene un «handle de orden» (el suyo si no está en la tabla) y se dibujan de menor a
mayor. Sin tabla, en el orden en que están guardadas.

ezdxf respeta la tabla del layout que se dibuja (``Frontend.draw_layout``) pero no la de los
bloques (``exporters._StrictFrontend`` lo hace), y ``xref.Loader`` no copia ninguna tabla a la
copia de trabajo. Aquí:
- ``copy_block_draw_order``: pasa a la copia las tablas de los bloques del DXF de origen;
- ``modelspace_ranks``: el puesto de cada entidad del Model Space de origen en su orden de
  dibujo, para que la zona (``modelspace_commands.build_working_document``) lo conserve;
- ``set_draw_order``: escribe la tabla de un layout para que se dibuje en un orden dado.
"""

from __future__ import annotations

from collections.abc import Iterable


def has_draw_order(layout) -> bool:
    """Si el layout (o bloque) tiene tabla de orden de dibujo con alguna entrada."""
    try:
        return any(True for _ in layout.get_redraw_order())
    except Exception:  # noqa: BLE001 - una tabla dañada cuenta como ninguna
        return False


def drawing_sequence(layout) -> list:
    """Entidades del layout en el orden en que AutoCAD las dibuja."""
    if has_draw_order(layout):
        return list(layout.entities_in_redraw_order())
    return list(layout)


def modelspace_ranks(document) -> dict[int, int] | None:
    """``id`` de cada entidad del Model Space → su puesto en el orden de dibujo; None si el
    Model Space no tiene tabla (se dibuja en el orden guardado)."""
    modelspace = document.modelspace()
    if not has_draw_order(modelspace):
        return None
    return {id(entity): rank for rank, entity in enumerate(drawing_sequence(modelspace))}


def set_draw_order(layout, ordered: Iterable) -> None:
    """Escribe la tabla de orden de ``layout`` para que sus entidades se dibujen en el orden
    de ``ordered`` (todas las del layout). Se reparten los handles de las propias entidades,
    de menor a mayor, como handles de orden."""
    entities = list(ordered)
    handles = sorted((entity.dxf.handle for entity in entities), key=lambda value: int(value, 16))
    layout.set_redraw_order(
        {entity.dxf.handle: sort for entity, sort in zip(entities, handles, strict=True)}
    )


def _signature(layout) -> tuple[str, ...]:
    return tuple(entity.dxftype() for entity in layout)


def copy_block_draw_order(source, target) -> int:
    """Copia a ``target`` (copia de trabajo hecha con ``xref.Loader``) el orden de dibujo de
    los bloques de ``source`` que tienen tabla. El bloque de la copia se reconoce por su nombre
    o, si el Loader lo renombró (bloques anónimos ``*U``), por ser el único con la misma
    secuencia de tipos de entidad; sus entidades se emparejan por posición (el Loader las copia
    en orden). Devuelve cuántos bloques recibieron su tabla."""
    ordered = {}
    for block in source.blocks:
        if block.block_record.is_any_layout or not has_draw_order(block):
            continue
        ordered[block.name] = block
    if not ordered:
        return 0
    by_signature: dict[tuple[str, ...], list] = {}
    for block in ordered.values():
        by_signature.setdefault(_signature(block), []).append(block)
    copied = 0
    for block in target.blocks:
        if block.block_record.is_any_layout or has_draw_order(block):
            continue
        original = ordered.get(block.name)
        signature = _signature(block)
        if original is None:
            candidates = by_signature.get(signature, [])
            if len(candidates) != 1:
                continue
            original = candidates[0]
        source_entities = list(original)
        target_entities = list(block)
        if not target_entities or _signature(original) != signature:
            continue
        rank = {id(entity): position for position, entity in enumerate(drawing_sequence(original))}
        pairs = sorted(
            zip(source_entities, target_entities, strict=True), key=lambda pair: rank[id(pair[0])]
        )
        set_draw_order(block, [entity for _source, entity in pairs])
        copied += 1
    return copied
