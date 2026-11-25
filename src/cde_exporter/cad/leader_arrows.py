"""Punta de flecha de los LEADER como en AutoCAD.

AutoCAD no dibuja la punta de un LEADER cuyo primer tramo mide menos que dos veces el tamaño de
la flecha (DIMASZ × DIMSCALE de su estilo, con sus sobrescrituras). ezdxf 1.4.4 la dibuja
siempre. En PRGD, el estilo «ESC 1-75» tiene DIMASZ 75 (75 m en un dibujo en metros) y sus
LEADER tienen tramos de menos de 1,5 m: ezdxf dibujaba cuñas de 75 m que no están en el CAD (en
el PDF, el SVG y la vista previa) y las contaba en las cajas (páginas enormes).

``install`` envuelve una sola vez ``ezdxf.render.leader.virtual_entities``, que ezdxf llama en
cada uso de un LEADER (``Leader.__virtual_entities__``: dibujo, cajas, explosión): si la punta
no cabe, se generan las piezas de una copia sin punta. El DXF no cambia.
"""

from __future__ import annotations

from ezdxf.math import Vec3
from ezdxf.render import leader as _leader_render

_ORIGINAL = getattr(_leader_render.virtual_entities, "__wrapped__", _leader_render.virtual_entities)


def leader_arrow_too_big(leader) -> bool:
    """Si la punta de flecha de un LEADER no cabe en su primer tramo (no se dibuja). El tamaño
    se calcula como en ``ezdxf.render.leader``."""
    if leader.dxftype() != "LEADER" or leader.doc is None:
        return False
    if not leader.dxf.get("has_arrowhead", 1):
        return False
    vertices = Vec3.list(leader.vertices)
    if len(vertices) < 2:
        return False
    override = leader.override()
    if override.get("dimldrblk", "") is None:
        return False  # sin flecha
    dimscale = override.get("dimscale", 1.0) or 1.0
    metric = leader.doc.header.get("$MEASUREMENT", 0)
    size = override.get("dimasz", 2.5 if metric else 0.1875) * dimscale
    return vertices[0].distance(vertices[1]) < 2 * abs(size)


def virtual_entities(leader):
    """``ezdxf.render.leader.virtual_entities`` sin la punta que no cabe."""
    if leader_arrow_too_big(leader):
        copy = leader.copy()
        copy.dxf.has_arrowhead = 0
        leader = copy
    yield from _ORIGINAL(leader)


virtual_entities.__wrapped__ = _ORIGINAL


def install() -> None:
    """Pone la regla en ezdxf (una sola vez; se llama al importar ``cde_exporter.cad``)."""
    _leader_render.virtual_entities = virtual_entities
