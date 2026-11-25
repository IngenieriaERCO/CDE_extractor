"""Textos de una zona del Model Space (orden ``texts`` del trabajador, protocolo 1.3;
docs/protocolo_trabajador.md, sección 4.3).

- Reúne los TEXT, MTEXT, ATTRIB y MULTILEADER (texto de las directrices) que se verían en la
  zona con el filtro de capas de la exportación, también dentro de bloques (anidados), con su
  posición en coordenadas del Model Space, su altura, su giro y su capa efectiva (la «0» dentro
  de un bloque toma la del INSERT, como al dibujar). Las cotas no entran.
- Un texto está en la zona si lo está su punto de inserción.
- Visibilidad como al dibujar: lo de capas apagadas o congeladas no sale (salvo las pedidas en
  ``include_hidden_layers``), ni los atributos invisibles, ni un INSERT invisible o en una capa
  que no se ve (desaparece entero, como en ``modelspace_commands.filter_block_layers``).
- Para ir rápido, por cada definición de bloque se apunta una vez qué tiene texto (directo o en
  sus anidados) y lo demás ni se mira (``TextBlocks``, una por sesión).
- Con los textos, ``scale_text.propose`` propone la escala de la zona.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from ezdxf.addons.drawing import RenderContext
from ezdxf.addons.drawing.properties import Properties
from ezdxf.math import Matrix44, Vec3
from ezdxf.tools.text import plain_mtext

from cde_exporter.cad.modelspace_commands import _visible
from cde_exporter.cad.preview import _show_hidden_layers
from cde_exporter.cad.reader import is_unknown_entity

TEXT_TYPES = frozenset({"TEXT", "MTEXT", "ATTRIB", "MULTILEADER"})

# Textos que se devuelven como mucho por defecto y como máximo que se pueden pedir.
DEFAULT_MAX_TEXTS = 2000
MAX_TEXTS = 20000


class TextBlocks:
    """Por cada definición de bloque, sus entidades que pueden dar texto: los textos y los
    INSERT cuyo bloque tiene texto (o atributos). Se calcula al pedirse, una vez por bloque."""

    def __init__(self, document) -> None:
        self.document = document
        self.children: dict[str, list] = {}

    def relevant(self, name: str, stack: tuple[str, ...] = ()) -> list:
        found = self.children.get(name)
        if found is not None:
            return found
        if name in stack or name not in self.document.blocks:
            return []
        found = []
        for entity in self.document.blocks[name]:
            kind = entity.dxftype()
            if kind in TEXT_TYPES:
                found.append(entity)
            elif kind == "INSERT" and (
                entity.attribs or self.relevant(entity.dxf.name, (*stack, name))
            ):
                found.append(entity)
        self.children[name] = found
        return found


def _direction(degrees: float) -> Vec3:
    return Vec3.from_deg_angle(degrees)


def _geometry(entity) -> tuple[str, Vec3, Vec3, float] | None:
    """(texto plano, punto, dirección del texto, altura) en las coordenadas de su bloque."""
    kind = entity.dxftype()
    if kind in ("TEXT", "ATTRIB"):
        if kind == "ATTRIB" and entity.has_embedded_mtext_entity:
            text = entity.virtual_mtext_entity().plain_text(split=False)
        else:
            text = entity.plain_text()
        ocs = entity.ocs()
        point = ocs.to_wcs(Vec3(entity.dxf.get("insert", (0, 0, 0))))
        direction = ocs.to_wcs(_direction(float(entity.dxf.get("rotation", 0.0) or 0.0)))
        return text, point, direction, float(entity.dxf.get("height", 0.0) or 0.0)
    if kind == "MTEXT":
        text = entity.plain_text(split=False)
        point = Vec3(entity.dxf.get("insert", (0, 0, 0)))
        if entity.dxf.hasattr("text_direction"):
            direction = Vec3(entity.dxf.text_direction)
        else:
            direction = entity.ocs().to_wcs(_direction(entity.get_rotation()))
        return text, point, direction, float(entity.dxf.get("char_height", 0.0) or 0.0)
    if kind == "MULTILEADER":
        context = entity.context
        mtext = context.mtext
        if mtext is None:
            return None
        direction = Vec3(mtext.text_direction)
        return (
            plain_mtext(mtext.default_content),
            Vec3(mtext.insert),
            direction,
            float(context.char_height or 0.0),
        )
    return None


def _item(entity, matrix: Matrix44, layer: str, top, blocks: tuple[str, ...]) -> dict | None:
    geometry = _geometry(entity)
    if geometry is None:
        return None
    text, point, direction, height = geometry
    text = text.strip()
    if not text:
        return None
    if direction.is_null:
        direction = Vec3(1, 0, 0)
    direction = direction.normalize()
    up = Vec3(-direction.y, direction.x, 0) * height
    where = matrix.transform(point)
    along = matrix.transform_direction(direction)
    rotation = 0.0 if along.is_null else math.degrees(math.atan2(along.y, along.x)) % 360.0
    return {
        "text": text,
        "kind": entity.dxftype(),
        "x": round(where.x, 6),
        "y": round(where.y, 6),
        "height": round(matrix.transform_direction(up).magnitude, 6),
        "rotation": round(rotation, 3),
        "layer": layer,
        "handle": entity.dxf.get("handle"),
        "modelspace_handle": top.dxf.get("handle"),
        "blocks": list(blocks),
    }


def _inside(item: dict, zone) -> bool:
    return zone[0] <= item["x"] <= zone[2] and zone[1] <= item["y"] <= zone[3]


def zone_texts(
    document,
    candidates,
    zone: tuple[float, float, float, float],
    *,
    layers: tuple[str, ...] = (),
    exclude_layers: tuple[str, ...] = (),
    reveal: frozenset[str] | None = None,
    blocks: TextBlocks | None = None,
    check: Callable[[], None] | None = None,
) -> list[dict]:
    """Textos que se verían en ``zone`` entre ``candidates`` (las entidades del Model Space
    que pueden tocarla, ``zone_preview.zone_candidates``), ordenados de arriba abajo y de
    izquierda a derecha."""
    if blocks is None:
        blocks = TextBlocks(document)
    ctx = RenderContext(document)
    ctx.set_current_layout(document.modelspace())
    _show_hidden_layers(ctx, document, reveal)
    found: list[dict] = []

    def visit(entity, matrix: Matrix44, top, path: tuple[str, ...]) -> None:
        if is_unknown_entity(entity):
            return
        kind = entity.dxftype()
        if kind not in TEXT_TYPES and kind != "INSERT":
            return
        layer = str(ctx.resolve_layer(entity))
        if not _visible(layer, layers, exclude_layers) or not ctx.resolve_visible(entity):
            return
        if kind != "INSERT":
            item = _item(entity, matrix, layer, top, path)
            if item is not None and _inside(item, zone):
                found.append(item)
            return
        state = Properties()
        state.layer = layer
        ctx.push_state(state)
        try:
            for attrib in entity.attribs:  # en las coordenadas del INSERT, no del bloque
                visit(attrib, matrix, top, path)
            name = entity.dxf.name
            if name in path:
                return
            inner = entity.matrix44() @ matrix
            for child in blocks.relevant(name):
                visit(child, inner, top, (*path, name))
        finally:
            ctx.pop_state()

    identity = Matrix44()
    for entity in candidates:
        if check is not None:
            check()
        kind = entity.dxftype()
        if kind in TEXT_TYPES or (
            kind == "INSERT" and (entity.attribs or blocks.relevant(entity.dxf.name))
        ):
            visit(entity, identity, entity, ())
    found.sort(key=lambda item: (-item["y"], item["x"]))
    return found
