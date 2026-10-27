"""Tipos compartidos por el flujo CAD."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from ezdxf.entities import DXFEntity

from cde_exporter.cad.entity_policy import INVALID_DATA

Bounds2D = tuple[float, float, float, float]


class CadProcessingError(RuntimeError):
    """Error controlado al inspeccionar, extraer o exportar un dibujo CAD."""


def invalid_data_reason(error: BaseException) -> str:
    return f"{INVALID_DATA}: {type(error).__name__}: {error}"


@dataclass(frozen=True, slots=True)
class CadWindow:
    """Ventana axis-alineada en XY, expresada en coordenadas del dibujo."""

    x_min: float
    y_min: float
    x_max: float
    y_max: float

    def __post_init__(self) -> None:
        values = (self.x_min, self.y_min, self.x_max, self.y_max)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Las coordenadas de la ventana deben ser números finitos")
        if self.x_min >= self.x_max or self.y_min >= self.y_max:
            raise ValueError("La ventana debe tener ancho y alto mayores que cero")

    def intersects(self, bounds: Bounds2D) -> bool:
        """Indica si una caja geométrica toca o cruza la ventana, incluidos sus bordes."""
        x_min, y_min, x_max, y_max = bounds
        return (
            x_min <= self.x_max
            and x_max >= self.x_min
            and y_min <= self.y_max
            and y_max >= self.y_min
        )


@dataclass(frozen=True, slots=True)
class DxfInspection:
    path: Path
    entity_count: int
    entity_types: dict[str, int]
    layers: dict[str, int]
    block_names: tuple[str, ...]
    units_code: int
    units_name: str
    extents: Bounds2D | None
    entities_without_bounds: dict[str, int]


@dataclass(frozen=True, slots=True)
class UnboundedSelectionDiagnostic:
    modelspace_order: int
    entity_type: str
    layer: str
    handle: str | None
    block_name: str | None
    status: str


@dataclass(frozen=True, slots=True)
class WindowSelection:
    entities: tuple[DXFEntity, ...]
    bounds: Bounds2D
    source_entity_count: int
    entity_types: dict[str, int]
    unbounded_entities: tuple[UnboundedSelectionDiagnostic, ...] = ()
