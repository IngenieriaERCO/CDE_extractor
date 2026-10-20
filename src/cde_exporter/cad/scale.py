"""Funciones puras para interpretar y convertir escalas nominales CAD."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_NOMINAL_SCALE = re.compile(r"1:([0-9]+(?:\.[0-9]+)?|\.[0-9]+)\Z")
UNIT_MM_PER_UNIT = {
    1: ("in", 25.4),
    2: ("ft", 304.8),
    3: ("mi", 1_609_344.0),
    4: ("mm", 1.0),
    5: ("cm", 10.0),
    6: ("m", 1000.0),
    7: ("km", 1_000_000.0),
    8: ("microin", 0.0000254),
    9: ("mil", 0.0254),
    10: ("yd", 914.4),
    11: ("angstrom", 1e-7),
    12: ("nm", 1e-6),
    13: ("micron", 0.001),
    14: ("dm", 100.0),
    15: ("dam", 10_000.0),
    16: ("hm", 100_000.0),
    17: ("Gm", 1e12),
    18: ("AU", 149_597_870_700_000.0),
    19: ("ly", 9.460730472e18),
    20: ("pc", 3.085677581e19),
}
OVERRIDE_UNITS = {"mm": 4, "cm": 5, "m": 6, "in": 1, "ft": 2}


@dataclass(frozen=True, slots=True)
class UnitVerification:
    status: str
    unit_name: str
    millimeters_per_unit: float
    verified: bool
    warnings: tuple[str, ...] = ()


def verify_drawing_units(
    dxf_units_code: int,
    block_units_code: int,
    *,
    max_block_dimension: float | None,
    insert_scale: tuple[float, float, float],
    insert_position: tuple[float, float, float],
    override: str | None = None,
) -> UnitVerification:
    """Verifica $INSUNITS, unidades de bloque y heurísticas geométricas simples."""
    if override is not None:
        code = OVERRIDE_UNITS[override]
        name, factor = UNIT_MM_PER_UNIT[code]
        return UnitVerification(
            f"indicadas por el usuario (--units {override})", name, factor, True
        )

    dxf_unit = UNIT_MM_PER_UNIT.get(dxf_units_code)
    block_unit = UNIT_MM_PER_UNIT.get(block_units_code)
    selected = dxf_unit or block_unit
    warnings = []
    # Heuristics: meters with >=1000 block units; or INSERT scale 0.001 at >=1e6 units.
    if dxf_unit and block_unit and dxf_units_code != block_units_code:
        warnings.append(
            f"$INSUNITS declara {dxf_unit[0]} y el bloque declara {block_unit[0]}"
        )
    if dxf_units_code == 6 and max_block_dimension is not None and max_block_dimension >= 1000:
        warnings.append(
            "el bloque mide 1000 o más unidades aunque $INSUNITS declara metros; "
            "podrían ser coordenadas expresadas en milímetros"
        )
    if any(math.isclose(abs(value), 0.001, rel_tol=0.0, abs_tol=1e-9) for value in insert_scale):
        if max(abs(value) for value in insert_position) >= 1_000_000:
            warnings.append("INSERT escala 0.001 con punto de inserción de un millón o más")

    if warnings:
        unit_name, factor = selected or ("desconocida", 1.0)
        return UnitVerification(
            "con indicios de inconsistencia", unit_name, factor, False, tuple(warnings)
        )
    if dxf_unit:
        return UnitVerification("declaradas en el DXF y coherentes", *dxf_unit, True)
    if block_unit:
        return UnitVerification(
            "declaradas en la definición del bloque", *block_unit, True
        )
    return UnitVerification("desconocidas ($INSUNITS = 0 o sin valor)", "desconocida", 1.0, False)


def parse_nominal_scale(value: str) -> float:
    """Devuelve N en ``1:N``; N debe ser un decimal positivo y finito."""
    match = _NOMINAL_SCALE.fullmatch(value.strip())
    if match is None:
        raise ValueError("Escala no válida; usa 1:N con N decimal positivo, por ejemplo 1:50")
    denominator = float(match.group(1))
    if not math.isfinite(denominator) or denominator <= 0:
        raise ValueError("El denominador de la escala debe ser un número positivo")
    return denominator


def output_factor(
    millimeters_per_drawing_unit: float,
    denominator: float,
    insert_scale: float = 1.0,
) -> float:
    """Calcula mm de papel por unidad CAD: mm/unidad × escala INSERT uniforme ÷ N."""
    values = (millimeters_per_drawing_unit, denominator, insert_scale)
    if any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("Unidades, denominador y escala INSERT deben ser positivos y finitos")
    return millimeters_per_drawing_unit * insert_scale / denominator
