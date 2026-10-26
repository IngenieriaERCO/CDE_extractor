"""Sustitución de 3DSOLID por su huella en planta (opción --solids footprint).

Trabaja sobre el DXF temporal de la extracción y escribe una copia de render; el DXF
de origen nunca se toca. Cada definición de bloque se procesa una sola vez: la huella
se calcula en el sistema local del bloque y los INSERT la colocan al dibujar.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import ezdxf
from ezdxf.acis import api as acis
from ezdxf.math import Matrix44, Vec3

from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.reader import read_dxf

FOOTPRINT_TOLERANCE_MM = 0.1
_COPIED_ATTRIBUTES = (
    "layer",
    "color",
    "true_color",
    "linetype",
    "lineweight",
    "ltscale",
    "transparency",
)
# Errores con los que ezdxf.acis señala un ACIS que no sabe leer o mallar.
_ACIS_ERRORS = (
    acis.AcisException,
    ezdxf.DXFError,
    ArithmeticError,
    AttributeError,
    IndexError,
    KeyError,
    TypeError,
    ValueError,
)


@dataclass(frozen=True, slots=True)
class FootprintResult:
    converted: tuple[str, ...]
    failures: tuple[tuple[str, str], ...]
    max_error_mm: float

    @property
    def failure_reasons(self) -> tuple[tuple[str, int], ...]:
        counts = Counter(reason for _handle, reason in self.failures)
        return tuple(sorted(counts.items()))


def _convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    unique = sorted(set(points))

    def cross(o, a, b) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    upper: list[tuple[float, float]] = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def _polygon_area(points) -> float:
    return (
        abs(
            sum(
                points[i][0] * points[i - 1][1] - points[i - 1][0] * points[i][1]
                for i in range(len(points))
            )
        )
        / 2
    )


def solid_footprint(solid) -> tuple[list[tuple[float, float]], float, float]:
    """Devuelve la huella XY (contorno convexo), la cota Z media y la media altura.

    Lanza ValueError con el motivo si el ACIS no se puede leer o mallar, o si la huella
    no es convexa: en un sólido cerrado, las áreas proyectadas con signo suman cero y su
    parte positiva cubre la huella; solo si coincide con la envolvente no hay entrantes.
    """
    try:
        bodies = acis.load_dxf(solid)
        meshes = [mesh for body in bodies for mesh in acis.mesh_from_body(body)]
    except _ACIS_ERRORS as error:
        raise ValueError(f"ACIS no convertible ({type(error).__name__})") from error
    vertices = [vertex for mesh in meshes for vertex in mesh.vertices]
    if not vertices:
        raise ValueError("ACIS sin geometría")
    hull = _convex_hull([(round(v.x, 9), round(v.y, 9)) for v in vertices])
    hull_area = _polygon_area(hull) if len(hull) >= 3 else 0.0
    if hull_area <= 1e-12:
        raise ValueError("huella de área nula")
    covered = 0.0
    for mesh in meshes:
        for face in mesh.faces:
            ring = [mesh.vertices[index] for index in face]
            signed = (
                sum(ring[i].x * ring[i - 1].y - ring[i - 1].x * ring[i].y for i in range(len(ring)))
                / 2
            )
            covered += max(signed, 0.0)
    if abs(covered - hull_area) > 1e-6 * hull_area:
        raise ValueError("huella no convexa")
    low, high = min(v.z for v in vertices), max(v.z for v in vertices)
    return hull, (low + high) / 2, (high - low) / 2


def _tilt_by_definition(document) -> dict[str, float]:
    """Mayor componente horizontal del eje Z local de cada definición tras los INSERT."""
    tilts: dict[str, float] = {}

    def walk(layout, matrix: Matrix44, stack: tuple[str, ...]) -> None:
        for entity in layout:
            if entity.dxftype() != "INSERT" or entity.dxf.name in stack:
                continue
            name = entity.dxf.name
            local = entity.matrix44() @ matrix
            z_axis = local.transform_direction(Vec3(0, 0, 1))
            tilts[name] = max(tilts.get(name, 0.0), math.hypot(z_axis.x, z_axis.y))
            block = document.blocks.get(name)
            if block is not None:
                walk(block, local, (*stack, name))

    walk(document.modelspace(), Matrix44(), ())
    return tilts


def footprint_document(
    extracted_path: str | Path,
    render_path: str | Path,
    *,
    millimeters_per_unit_on_paper: float,
    tolerance_mm: float = FOOTPRINT_TOLERANCE_MM,
) -> FootprintResult:
    """Escribe en ``render_path`` una copia con los 3DSOLID convertibles como huella.

    El error por inclinación es la media altura del sólido por la parte horizontal de
    su eje Z ya transformado; si supera ``tolerance_mm`` en el papel, el sólido no se
    convierte y queda como 3DSOLID con su motivo.
    """
    source, destination = Path(extracted_path), Path(render_path)
    if source.resolve() == destination.resolve():
        raise CadProcessingError("La copia de render no puede sobrescribir el DXF extraído")
    document = read_dxf(source)
    tilts = _tilt_by_definition(document)
    converted: list[str] = []
    failures: list[tuple[str, str]] = []
    max_error = 0.0
    for block in document.blocks:
        solids = [entity for entity in block if entity.dxftype() == "3DSOLID"]
        if not solids:
            continue
        tilt = tilts.get(block.name, 0.0)
        for solid in solids:
            handle = str(solid.dxf.handle)
            try:
                hull, z_mid, half_height = solid_footprint(solid)
            except ValueError as error:
                failures.append((handle, str(error)))
                continue
            error_mm = half_height * tilt * millimeters_per_unit_on_paper
            if error_mm > tolerance_mm:
                failures.append(
                    (
                        handle,
                        f"INSERT inclinado: error de huella {error_mm:.3f} mm > "
                        f"{tolerance_mm:g} mm",
                    )
                )
                continue
            attributes = {
                name: solid.dxf.get(name) for name in _COPIED_ATTRIBUTES if solid.dxf.hasattr(name)
            }
            attributes["elevation"] = z_mid
            block.add_lwpolyline(hull, close=True, dxfattribs=attributes)
            block.delete_entity(solid)
            converted.append(handle)
            max_error = max(max_error, error_mm)
    try:
        document.saveas(destination)
    except OSError as error:
        raise CadProcessingError(f"No se pudo escribir la copia de render: {error}") from error
    return FootprintResult(tuple(converted), tuple(failures), max_error)
