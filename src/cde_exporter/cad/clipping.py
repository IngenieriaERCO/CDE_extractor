"""Recorte geométrico contra una ventana XY (cad extract-modelspace con --window).

Se recortan de verdad (la geometría fuera de la ventana desaparece del DXF de trabajo):
- LINE;
- POLYLINE 3D (las curvas de nivel) y POLYLINE 2D y LWPOLYLINE de tramos rectos, con su cota
  (Z), su ancho (constante o por vértice, interpolado en el punto de corte) y sus propiedades
  (capa, color, tipo y grosor de línea…). Una polilínea que entra y sale varias veces da un
  tramo por cada paso dentro de la ventana; una cerrada que se corta queda en tramos abiertos.

No se recortan (entran enteros y se cuentan): arcos, círculos, elipses, splines, texto, HATCH,
INSERT, mallas, polilíneas con arcos (bulge), polilíneas ajustadas (curve/spline fit) y
polilíneas 2D cuyo plano no es el XY (extrusión distinta de 0, 0, 1). Lo que está entero dentro
de la ventana no se toca.

Algoritmo: Liang–Barsky en XY por segmento; la Z y el ancho se interpolan con el mismo
parámetro.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from cde_exporter.cad.reader import entity_bounds

Box = tuple[float, float, float, float]  # xmin, ymin, xmax, ymax
# Vértice de trabajo: x, y, z, ancho inicial y ancho final del tramo que sale de él.
Vertex = tuple[float, float, float, float, float]

_POLYLINE_CURVE_FIT = 2
_POLYLINE_SPLINE_FIT = 4
_VERTEX_SPLINE_POINTS = 8 | 16  # vértices añadidos por el ajuste o marco de control


def clip_segment(
    x0: float, y0: float, x1: float, y1: float, box: Box
) -> tuple[float, float] | None:
    """Parámetros (t0, t1) del trozo del segmento dentro de ``box``; None si no lo toca."""
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - box[0]), (dx, box[2] - x0), (-dy, y0 - box[1]), (dy, box[3] - y0)):
        if p == 0:
            if q < 0:
                return None
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return None
            t0 = max(t0, r)
        else:
            if r < t0:
                return None
            t1 = min(t1, r)
    return t0, t1


def _at(a: Vertex, b: Vertex, t: float) -> tuple[float, float, float]:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def _width(a: Vertex, t: float) -> float:
    return a[3] + (a[4] - a[3]) * t


def clip_path(vertices: list[Vertex], closed: bool, box: Box) -> tuple[list[list[Vertex]], bool]:
    """Tramos de una polilínea de segmentos rectos dentro de ``box``.

    Devuelve los tramos (cada uno, una lista de vértices con el ancho del tramo que sale de
    cada vértice) y si hubo algún corte. Sin corte, la polilínea está entera dentro.
    """
    count = len(vertices)
    segments = count if closed else count - 1
    pieces: list[list[Vertex]] = []
    current: list[Vertex] = []
    cut = False
    starts_at_origin = ends_at_origin = False
    for index in range(max(segments, 0)):
        a, b = vertices[index], vertices[(index + 1) % count]
        clipped = clip_segment(a[0], a[1], b[0], b[1], box)
        if clipped is None:
            cut = True
            if current:
                pieces.append(current)
                current = []
            continue
        t0, t1 = clipped
        if t0 > 0 or t1 < 1:
            cut = True
        start = (*_at(a, b, t0), _width(a, t0), _width(a, t1))
        end = (*_at(a, b, t1), 0.0, 0.0)
        if current and t0 == 0:
            current[-1] = start  # sigue el tramo: el vértice toma el ancho de este segmento
        else:
            if current:
                pieces.append(current)
            current = [start]
            if index == 0 and t0 == 0:
                starts_at_origin = True
        current.append(end)
        if t1 < 1:
            pieces.append(current)
            current = []
        elif closed and index == segments - 1:
            ends_at_origin = True
    if current:
        pieces.append(current)
    if closed and cut and starts_at_origin and ends_at_origin and len(pieces) > 1:
        # El corte cae justo en el comienzo del anillo: el último tramo sigue en el primero.
        last = pieces.pop()
        pieces[0] = last[:-1] + pieces[0]
    return [piece for piece in pieces if len(piece) >= 2 and _length(piece) > 0], cut


def _length(piece: list[Vertex]) -> float:
    return sum(
        ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
        for a, b in zip(piece, piece[1:], strict=False)
    )


def _flat(entity) -> bool:
    extrusion = entity.dxf.get("extrusion", (0, 0, 1))
    return abs(extrusion[0]) < 1e-12 and abs(extrusion[1]) < 1e-12 and extrusion[2] > 0


def polyline_vertices(entity) -> tuple[list[Vertex], bool] | None:
    """Vértices recortables de una polilínea y si es cerrada; None si no se puede recortar."""
    kind = entity.dxftype()
    if kind == "LWPOLYLINE":
        if not _flat(entity):
            return None
        z = float(entity.dxf.get("elevation", 0.0))
        points = list(entity.get_points("xyseb"))
        if any(abs(bulge) > 1e-12 for *_rest, bulge in points):
            return None
        return [(x, y, z, s, e) for x, y, s, e, _b in points], bool(entity.closed)
    if kind != "POLYLINE" or entity.is_polygon_mesh or entity.is_poly_face_mesh:
        return None
    flags = int(entity.dxf.get("flags", 0))
    if flags & (_POLYLINE_CURVE_FIT | _POLYLINE_SPLINE_FIT):
        return None
    vertices = list(entity.vertices)
    if any(int(vertex.dxf.get("flags", 0)) & _VERTEX_SPLINE_POINTS for vertex in vertices):
        return None
    if entity.is_3d_polyline:
        return [
            (float(v.dxf.location.x), float(v.dxf.location.y), float(v.dxf.location.z), 0.0, 0.0)
            for v in vertices
        ], bool(entity.is_closed)
    if not _flat(entity):
        return None
    if any(abs(float(vertex.dxf.get("bulge", 0.0))) > 1e-12 for vertex in vertices):
        return None
    z = float(entity.dxf.get("elevation", (0, 0, 0))[2])
    start = float(entity.dxf.get("default_start_width", 0.0))
    end = float(entity.dxf.get("default_end_width", 0.0))
    return [
        (
            float(v.dxf.location.x),
            float(v.dxf.location.y),
            z,
            float(v.dxf.get("start_width", start)),
            float(v.dxf.get("end_width", end)),
        )
        for v in vertices
    ], bool(entity.is_closed)


def is_clippable(entity) -> bool:
    """¿Se puede recortar esta entidad (y no solo meterla entera)?"""
    if entity.dxftype() == "LINE":
        return True
    try:
        return polyline_vertices(entity) is not None
    except Exception:  # noqa: BLE001 - datos inválidos: entra entera
        return False


def inside(bounds: Box, box: Box) -> bool:
    return (
        box[0] <= bounds[0] and bounds[2] <= box[2] and box[1] <= bounds[1] and bounds[3] <= box[3]
    )


@dataclass
class ClipReport:
    clipped: int = 0  # entidades que se recortaron
    pieces: int = 0  # tramos que quedaron de ellas
    outside: int = 0  # entidades cuya caja tocaba la ventana pero su geometría no
    not_clipped: Counter = field(default_factory=Counter)  # por tipo: cruzan el borde, enteras
    not_clipped_bounds: Box | None = None  # caja de esas entidades enteras


def _attributes(entity) -> dict:
    attributes = entity.dxfattribs(drop={"handle", "owner"})
    attributes["flags"] = int(attributes.get("flags", 0)) & ~1  # los tramos son abiertos
    return attributes


def _add_piece(layout, entity, piece: list[Vertex]) -> None:
    attributes = _attributes(entity)
    kind = entity.dxftype()
    if kind == "LWPOLYLINE":
        layout.add_lwpolyline(
            [(x, y, s, e, 0.0) for x, y, _z, s, e in piece], format="xyseb", dxfattribs=attributes
        )
    elif entity.is_3d_polyline:
        layout.add_polyline3d([(x, y, z) for x, y, z, _s, _e in piece], dxfattribs=attributes)
    else:
        layout.add_polyline2d(
            [(x, y, s, e, 0.0) for x, y, _z, s, e in piece], format="xyseb", dxfattribs=attributes
        )


def union_boxes(box: Box | None, other: Box) -> Box:
    if box is None:
        return other
    return (
        min(box[0], other[0]),
        min(box[1], other[1]),
        max(box[2], other[2]),
        max(box[3], other[3]),
    )


def clip_layout(layout, box: Box, bounds_of=entity_bounds) -> ClipReport:
    """Recorta contra ``box`` las entidades de ``layout`` (una copia de trabajo, nunca el DXF de
    origen). Las que no se pueden recortar y cruzan el borde se dejan enteras y se cuentan."""
    report = ClipReport()
    for entity in list(layout):
        bounds = bounds_of(entity)
        if bounds is None or inside(bounds, box):
            continue
        kind = entity.dxftype()
        if kind == "LINE":
            start, end = entity.dxf.start, entity.dxf.end
            clipped = clip_segment(start.x, start.y, end.x, end.y, box)
            if clipped is None:
                layout.delete_entity(entity)
                report.outside += 1
                continue
            a = (start.x, start.y, start.z, 0.0, 0.0)
            b = (end.x, end.y, end.z, 0.0, 0.0)
            entity.dxf.start, entity.dxf.end = _at(a, b, clipped[0]), _at(a, b, clipped[1])
            report.clipped += 1
            report.pieces += 1
            continue
        try:
            data = polyline_vertices(entity)
        except Exception:  # noqa: BLE001 - datos inválidos: entra entera
            data = None
        if data is None:
            report.not_clipped[kind] += 1
            report.not_clipped_bounds = union_boxes(report.not_clipped_bounds, bounds)
            continue
        pieces, cut = clip_path(data[0], data[1], box)
        if not cut:
            continue
        for piece in pieces:
            _add_piece(layout, entity, piece)
        layout.delete_entity(entity)
        if pieces:
            report.clipped += 1
            report.pieces += len(pieces)
        else:
            report.outside += 1
    return report
