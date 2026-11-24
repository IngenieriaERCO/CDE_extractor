"""Recorte geométrico contra una ventana XY (cad extract-modelspace con --window).

Se recortan de verdad (la geometría fuera de la ventana desaparece del DXF de trabajo):
- LINE;
- POLYLINE 3D (las curvas de nivel) y POLYLINE 2D y LWPOLYLINE de tramos rectos, con su cota
  (Z), su ancho (constante o por vértice, interpolado en el punto de corte) y sus propiedades
  (capa, color, tipo y grosor de línea…). Una polilínea que entra y sale varias veces da un
  tramo por cada paso dentro de la ventana; una cerrada que se corta queda en tramos abiertos.

- ARC, CIRCLE y ELLIPSE en planta (extrusión 0, 0, ±1): quedan los arcos (ARC o ELLIPSE
  parcial) que caen dentro; un círculo cortado se convierte en arcos (ARC). Se corta en el
  parámetro exacto, sin aproximar la curva.
- HATCH en planta (extrusión 0, 0, ±1): cada contorno que cruza el borde se aproxima por una
  poligonal fina (``hatch_tolerance``) y se recorta contra la ventana (Sutherland–Hodgman); los
  contornos enteros dentro se conservan tal cual y los enteros fuera se quitan. El patrón no
  cambia (se define en coordenadas absolutas), así que las rayas siguen en su sitio.

No se recortan (entran enteros y se cuentan; en el PDF los corta la máscara de recorte, ver
``exporters``): splines, texto, cotas, INSERT, mallas, polilíneas con arcos (bulge),
polilíneas ajustadas (curve/spline fit), y curvas, HATCH y polilíneas 2D cuyo plano no es el
XY. Lo que está entero dentro de la ventana no se toca.

Algoritmo: Liang–Barsky en XY por segmento; la Z y el ancho se interpolan con el mismo
parámetro. En las curvas, los cortes con los cuatro lados salen de resolver
``A·cos t + B·sin t = k`` y se conservan los tramos cuyo punto medio está dentro.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from ezdxf.entities.boundary_paths import PolylinePath
from ezdxf.math import Vec3
from ezdxf.path import from_hatch_boundary_path
from ezdxf.tools.text import plain_mtext

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


_CURVE_TYPES = frozenset({"ARC", "CIRCLE", "ELLIPSE"})
_TWO_PI = 2 * math.pi


def _plan_extrusion(entity) -> Vec3 | None:
    """Extrusión de una entidad en planta (0, 0, ±1); None si su plano no es el XY."""
    extrusion = Vec3(entity.dxf.get("extrusion", (0, 0, 1)))
    if abs(extrusion.x) > 1e-12 or abs(extrusion.y) > 1e-12 or abs(extrusion.z) < 1e-12:
        return None
    return extrusion


def curve_parameters(entity):
    """Curva en planta como ``P(t) = C + A·cos t + B·sin t`` (XY en el sistema universal) y su
    intervalo de parámetro ``[t0, t1]`` con ``t1 > t0``; None si no se puede recortar.

    En ARC y CIRCLE, ``t`` es el ángulo en su sistema de la entidad (OCS) en radianes; en
    ELLIPSE, su parámetro. Así el corte se escribe de vuelta en los mismos atributos.
    """
    kind = entity.dxftype()
    if kind not in _CURVE_TYPES or _plan_extrusion(entity) is None:
        return None
    if kind == "ELLIPSE":
        major = Vec3(entity.dxf.major_axis)
        ratio = float(entity.dxf.get("ratio", 1.0))
        extrusion = Vec3(entity.dxf.get("extrusion", (0, 0, 1)))
        if major.magnitude <= 0 or ratio <= 0:
            return None
        minor = extrusion.cross(major).normalize(major.magnitude * ratio)
        center = Vec3(entity.dxf.center)
        t0 = float(entity.dxf.get("start_param", 0.0))
        t1 = float(entity.dxf.get("end_param", _TWO_PI))
    else:
        radius = float(entity.dxf.radius)
        if radius <= 0:
            return None
        ocs = entity.ocs()
        center = ocs.to_wcs(Vec3(entity.dxf.center))
        major, minor = ocs.to_wcs(Vec3(radius, 0, 0)), ocs.to_wcs(Vec3(0, radius, 0))
        if kind == "CIRCLE":
            t0, t1 = 0.0, _TWO_PI
        else:
            t0 = math.radians(float(entity.dxf.start_angle))
            t1 = math.radians(float(entity.dxf.end_angle))
    while t1 <= t0:
        t1 += _TWO_PI
    if t1 - t0 > _TWO_PI + 1e-12:
        t1 = t0 + _TWO_PI
    return (center.x, center.y), (major.x, major.y), (minor.x, minor.y), t0, t1


def clip_curve(center, a_axis, b_axis, t0: float, t1: float, box: Box):
    """Intervalos de parámetro de la curva dentro de ``box`` y si hubo algún corte.

    Sin corte, la curva está entera dentro (la lista tiene el intervalo completo); con corte y
    lista vacía, está entera fuera.
    """
    full = t1 - t0 >= _TWO_PI - 1e-12
    cuts = []
    for axis, low, high in ((0, box[0], box[2]), (1, box[1], box[3])):
        a, b = a_axis[axis], b_axis[axis]
        amplitude = math.hypot(a, b)
        if amplitude <= 0:
            continue
        phase = math.atan2(b, a)
        for edge in (low, high):
            ratio = (edge - center[axis]) / amplitude
            if abs(ratio) > 1:
                continue
            spread = math.acos(max(-1.0, min(1.0, ratio)))
            for t in (phase + spread, phase - spread):
                t = t0 + (t - t0) % _TWO_PI
                if t0 < t < t1:
                    cuts.append(t)
    breaks = [t0, *sorted(cuts), t1]
    size = max(box[2] - box[0], box[3] - box[1], 1e-12)
    tolerance = size * 1e-9

    def point_inside(t: float) -> bool:
        x = center[0] + a_axis[0] * math.cos(t) + b_axis[0] * math.sin(t)
        y = center[1] + a_axis[1] * math.cos(t) + b_axis[1] * math.sin(t)
        return (
            box[0] - tolerance <= x <= box[2] + tolerance
            and box[1] - tolerance <= y <= box[3] + tolerance
        )

    intervals: list[list[float]] = []
    cut = False
    for start, end in zip(breaks, breaks[1:], strict=False):
        if end - start <= 1e-12:
            continue
        if not point_inside((start + end) / 2):
            cut = True
            continue
        if intervals and abs(intervals[-1][1] - start) <= 1e-12:
            intervals[-1][1] = end
        else:
            intervals.append([start, end])
    if not cut:
        return [(t0, t1)], False
    if full and len(intervals) > 1:
        first, last = intervals[0], intervals[-1]
        if abs(first[0] - t0) <= 1e-12 and abs(last[1] - t1) <= 1e-12:
            # El tramo que pasa por el comienzo del círculo o elipse completa es uno solo.
            intervals.pop()
            first[0] = last[0] - _TWO_PI
    return [(start, end) for start, end in intervals], True


def _clip_curve_entity(layout, entity, box: Box) -> int | None:
    """Recorta un ARC, CIRCLE o ELLIPSE; devuelve los tramos que quedan (0: entera fuera) o
    None si la curva no se cortó (entera dentro)."""
    data = curve_parameters(entity)
    if data is None:
        raise ValueError("curva no recortable")
    intervals, cut = clip_curve(*data, box)
    if not cut:
        return None
    kind = entity.dxftype()
    for start, end in intervals:
        if kind == "ELLIPSE":
            piece = entity.copy()
            piece.dxf.start_param = start % _TWO_PI
            piece.dxf.end_param = end % _TWO_PI
            if abs(piece.dxf.end_param - piece.dxf.start_param) <= 1e-12:
                piece.dxf.end_param = piece.dxf.start_param + (end - start)
            layout.add_entity(piece)
        elif kind == "ARC":
            piece = entity.copy()
            piece.dxf.start_angle = math.degrees(start) % 360
            piece.dxf.end_angle = math.degrees(end) % 360
            layout.add_entity(piece)
        else:  # CIRCLE: cada tramo es un ARC con sus mismas propiedades
            attributes = entity.dxfattribs(drop={"handle", "owner"})
            layout.add_arc(
                center=attributes.pop("center"),
                radius=attributes.pop("radius"),
                start_angle=math.degrees(start) % 360,
                end_angle=math.degrees(end) % 360,
                dxfattribs=attributes,
            )
    layout.delete_entity(entity)
    return len(intervals)


def clip_polygon(points: list[tuple[float, float]], box: Box) -> list[tuple[float, float]]:
    """Polígono cerrado (sin repetir el primer vértice) recortado contra ``box``
    (Sutherland–Hodgman). Puede dejar aristas sobre el borde que no encierran área: en un
    relleno par-impar no se ven."""
    edges = (
        (lambda p: p[0] >= box[0], lambda p, q: _cross_x(p, q, box[0])),
        (lambda p: p[0] <= box[2], lambda p, q: _cross_x(p, q, box[2])),
        (lambda p: p[1] >= box[1], lambda p, q: _cross_y(p, q, box[1])),
        (lambda p: p[1] <= box[3], lambda p, q: _cross_y(p, q, box[3])),
    )
    output = list(points)
    for is_in, crossing in edges:
        if not output:
            break
        source, output = output, []
        previous = source[-1]
        for current in source:
            if is_in(current):
                if not is_in(previous):
                    output.append(crossing(previous, current))
                output.append(current)
            elif is_in(previous):
                output.append(crossing(previous, current))
            previous = current
    return output


def _cross_x(p, q, x: float) -> tuple[float, float]:
    t = (x - p[0]) / (q[0] - p[0])
    return x, p[1] + (q[1] - p[1]) * t


def _cross_y(p, q, y: float) -> tuple[float, float]:
    t = (y - p[1]) / (q[1] - p[1])
    return p[0] + (q[0] - p[0]) * t, y


def _polygon_area(points) -> float:
    return abs(
        sum(
            a[0] * b[1] - b[0] * a[1]
            for a, b in zip(points, points[1:] + points[:1], strict=False)
        )
        / 2
    )


def hatch_tolerance(box: Box) -> float:
    """Distancia máxima (unidades CAD) entre un contorno curvo de HATCH y su poligonal: una
    millonésima de la diagonal de la zona (0,02 mm en una zona de 20 m)."""
    return max(math.hypot(box[2] - box[0], box[3] - box[1]) * 1e-6, 1e-9)


def _clip_hatch_entity(entity, box: Box) -> int | None:
    """Recorta los contornos de un HATCH; devuelve cuántos contornos quedan (0: entero fuera)
    o None si no hubo que cortar nada."""
    extrusion = _plan_extrusion(entity)
    if extrusion is None:
        raise ValueError("HATCH no recortable")
    # En OCS con extrusión (0, 0, −1), x cambia de signo e y no: la zona sigue siendo un
    # rectángulo con los lados paralelos a los ejes.
    obox = box if extrusion.z > 0 else (-box[2], box[1], -box[0], box[3])
    distance = hatch_tolerance(box)
    kept, changed = [], False
    for boundary in entity.paths:
        points = [
            (vertex.x, vertex.y)
            for vertex in from_hatch_boundary_path(boundary).flattening(distance)
        ]
        if len(points) > 1 and math.dist(points[0], points[-1]) <= 1e-12:
            points.pop()
        if len(points) < 3:
            kept.append(boundary)
            continue
        xs, ys = [x for x, _y in points], [y for _x, y in points]
        bounds = (min(xs), min(ys), max(xs), max(ys))
        if inside(bounds, obox):
            kept.append(boundary)
            continue
        changed = True
        polygon = clip_polygon(points, obox)
        if len(polygon) < 3 or _polygon_area(polygon) <= 0:
            continue
        flags = int(boundary.path_type_flags) & ~2  # el tipo de contorno, no «polilínea»
        kept.append(PolylinePath.from_vertices(polygon, True, flags))
    if not changed:
        return None
    entity.paths.clear()
    for boundary in kept:
        entity.paths.append(boundary)
    if kept:
        entity.dxf.associative = 0  # los contornos ya no son los de las entidades de origen
    return len(kept)


def is_clippable(entity) -> bool:
    """¿Se puede recortar esta entidad (y no solo meterla entera)?"""
    kind = entity.dxftype()
    if kind == "LINE":
        return True
    try:
        if kind in _CURVE_TYPES:
            return curve_parameters(entity) is not None
        if kind == "HATCH":
            return _plan_extrusion(entity) is not None
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
    # Texto completo de las que cruzan el borde y entran enteras (la máscara del PDF lo corta):
    # (tipo, capa, texto), en el orden del dibujo.
    masked_texts: list = field(default_factory=list)


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


def _plain(text: str) -> str:
    return " ".join(str(text).split())


def entity_texts(entity) -> list[str]:
    """Textos legibles de una entidad (TEXT, MTEXT, atributos de un INSERT, texto de una cota o
    de una directriz múltiple), en una línea cada uno."""
    kind = entity.dxftype()
    texts: list[str] = []
    try:
        if kind == "TEXT":
            texts.append(entity.plain_text())
        elif kind == "MTEXT":
            texts.append(entity.plain_text(split=False))
        elif kind == "INSERT":
            texts.extend(str(attrib.dxf.get("text", "")) for attrib in entity.attribs)
        elif kind == "DIMENSION":
            override = str(entity.dxf.get("text", "") or "")
            if override == " ":
                pass  # texto de la cota suprimido en el CAD
            elif override:
                texts.append(plain_mtext(override.replace("<>", _measurement(entity))))
            else:
                texts.append(_measurement(entity))
        elif kind == "MULTILEADER" and entity.context.mtext is not None:
            texts.append(plain_mtext(entity.context.mtext.default_content))
    except Exception:  # noqa: BLE001 - datos inválidos: sin texto
        return []
    return [text for text in (_plain(value) for value in texts) if text]


def _measurement(entity) -> str:
    try:
        value = entity.get_measurement()
    except Exception:  # noqa: BLE001 - cota sin medida
        return ""
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return ""


def _keep_whole(report: ClipReport, entity, kind: str, bounds: Box) -> None:
    report.not_clipped[kind] += 1
    report.not_clipped_bounds = union_boxes(report.not_clipped_bounds, bounds)
    layer = str(entity.dxf.get("layer", "0") or "0")
    report.masked_texts.extend((kind, layer, text) for text in entity_texts(entity))


def clip_layout(layout, box: Box, bounds_of=entity_bounds, ranks=None) -> ClipReport:
    """Recorta contra ``box`` las entidades de ``layout`` (una copia de trabajo, nunca el DXF de
    origen). Las que no se pueden recortar y cruzan el borde se dejan enteras y se cuentan.

    ``ranks`` (``id`` de entidad → puesto en el orden de dibujo): las piezas nuevas que deja el
    recorte (se añaden al final del layout) heredan el puesto de su entidad original, para que
    la zona se dibuje en el orden del DXF (``draw_order``)."""
    report = ClipReport()
    for entity in list(layout):
        before = len(layout)
        rank = None if ranks is None else ranks.get(id(entity))
        _clip_entity(layout, entity, box, bounds_of, report)
        if rank is not None:
            added = len(layout) - before + (0 if entity.is_alive else 1)
            for index in range(len(layout) - added, len(layout)):
                ranks[id(layout[index])] = rank
    return report


def _clip_entity(layout, entity, box: Box, bounds_of, report: ClipReport) -> None:
    """Recorta una entidad de ``clip_layout`` (las piezas nuevas van al final del layout)."""
    bounds = bounds_of(entity)
    if bounds is None or inside(bounds, box):
        return
    kind = entity.dxftype()
    if kind == "LINE":
        start, end = entity.dxf.start, entity.dxf.end
        clipped = clip_segment(start.x, start.y, end.x, end.y, box)
        if clipped is None:
            layout.delete_entity(entity)
            report.outside += 1
            return
        a = (start.x, start.y, start.z, 0.0, 0.0)
        b = (end.x, end.y, end.z, 0.0, 0.0)
        entity.dxf.start, entity.dxf.end = _at(a, b, clipped[0]), _at(a, b, clipped[1])
        report.clipped += 1
        report.pieces += 1
        return
    if kind in _CURVE_TYPES or kind == "HATCH":
        try:
            if kind == "HATCH":
                pieces = _clip_hatch_entity(entity, box)
            else:
                pieces = _clip_curve_entity(layout, entity, box)
        except Exception:  # noqa: BLE001 - no recortable o datos inválidos: entra entera
            _keep_whole(report, entity, kind, bounds)
            return
        if pieces is None:
            return
        if pieces == 0:
            if kind == "HATCH":
                layout.delete_entity(entity)
            report.outside += 1
        else:
            report.clipped += 1
            report.pieces += pieces
        return
    try:
        data = polyline_vertices(entity)
    except Exception:  # noqa: BLE001 - datos inválidos: entra entera
        data = None
    if data is None:
        _keep_whole(report, entity, kind, bounds)
        return
    pieces, cut = clip_path(data[0], data[1], box)
    if not cut:
        return
    for piece in pieces:
        _add_piece(layout, entity, piece)
    layout.delete_entity(entity)
    if pieces:
        report.clipped += 1
        report.pieces += len(pieces)
    else:
        report.outside += 1
