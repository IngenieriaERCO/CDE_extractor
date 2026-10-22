"""Modelo de dominio independiente para un detalle CAD extraído."""

from __future__ import annotations

import math
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum, StrEnum


class EntityStatus(StrEnum):
    CONSERVED = "conserved"
    TRANSFORMED = "transformed"
    SIMPLIFIED = "simplified"
    OMITTED = "omitted"
    UNSUPPORTED = "unsupported"


class Severity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


def _require_tuple(name: str, value: object, length: int | None = None) -> tuple:
    if not isinstance(value, tuple):
        raise ValueError(f"{name} debe ser una tupla inmutable")
    if length is not None and len(value) != length:
        raise ValueError(f"{name} debe contener exactamente {length} valores")
    return value


def _require_finite(name: str, value: float | None) -> None:
    if value is not None and not math.isfinite(value):
        raise ValueError(f"{name} debe ser un número finito")


def _require_finite_tuple(name: str, value: tuple, length: int) -> None:
    _require_tuple(name, value, length)
    for index, coordinate in enumerate(value):
        if not isinstance(coordinate, (int, float)) or isinstance(coordinate, bool):
            raise ValueError(f"{name}[{index}] debe ser un número finito")
        _require_finite(f"{name}[{index}]", float(coordinate))


def _require_enum(name: str, value: Enum, enum_type: type[Enum]) -> None:
    if not isinstance(value, enum_type):
        choices = ", ".join(member.value for member in enum_type)
        raise ValueError(f"{name} debe ser uno de: {choices}")


@dataclass(frozen=True, slots=True)
class DetailSource:
    path: str
    block_name: str


@dataclass(frozen=True, slots=True)
class SelectedReference:
    occurrence: int
    handle: str | None
    layer: str
    insertion_xyz: tuple[float, float, float]
    rotation_deg: float
    scale_xyz: tuple[float, float, float]

    def __post_init__(self) -> None:
        if self.occurrence < 1:
            raise ValueError("occurrence debe ser un entero desde 1")
        _require_finite_tuple("insertion_xyz", self.insertion_xyz, 3)
        _require_finite("rotation_deg", self.rotation_deg)
        _require_finite_tuple("scale_xyz", self.scale_xyz, 3)


@dataclass(frozen=True, slots=True)
class UnitInfo:
    declared_code: int | None
    name: str
    status: str
    millimeters_per_unit: float | None
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_finite("millimeters_per_unit", self.millimeters_per_unit)
        _require_tuple("warnings", self.warnings)
        if any(not isinstance(warning, str) for warning in self.warnings):
            raise ValueError("warnings solo puede contener texto")


@dataclass(frozen=True, slots=True)
class ScaleInfo:
    nominal_denominator: float | None
    factor_mm_per_drawing_unit: float | None
    insert_factor_xyz: tuple[float, float, float] | None

    def __post_init__(self) -> None:
        _require_finite("nominal_denominator", self.nominal_denominator)
        _require_finite("factor_mm_per_drawing_unit", self.factor_mm_per_drawing_unit)
        if self.insert_factor_xyz is not None:
            _require_finite_tuple("insert_factor_xyz", self.insert_factor_xyz, 3)


@dataclass(frozen=True, slots=True)
class Bounds3D:
    min_xyz: tuple[float, float, float]
    max_xyz: tuple[float, float, float]

    def __post_init__(self) -> None:
        _require_finite_tuple("min_xyz", self.min_xyz, 3)
        _require_finite_tuple("max_xyz", self.max_xyz, 3)
        if any(
            minimum > maximum for minimum, maximum in zip(self.min_xyz, self.max_xyz, strict=True)
        ):
            raise ValueError("Cada coordenada min_xyz debe ser menor o igual que max_xyz")


@dataclass(frozen=True, slots=True)
class EntityCount:
    entity_type: str
    layer: str
    status: EntityStatus
    count: int

    def __post_init__(self) -> None:
        _require_enum("status", self.status, EntityStatus)
        if self.count < 0:
            raise ValueError("count debe ser mayor o igual que cero")


@dataclass(frozen=True, slots=True)
class EntityDisposition:
    index: int
    handle: str | None
    entity_type: str
    layer: str
    status: EntityStatus
    reason: str | None

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("index debe ser mayor o igual que cero")
        _require_enum("status", self.status, EntityStatus)


@dataclass(frozen=True, slots=True)
class ExternalDependency:
    kind: str
    reference: str
    required: bool
    resolved: bool
    detail: str | None


@dataclass(frozen=True, slots=True)
class ExtractionWarning:
    code: str
    severity: Severity
    message: str

    def __post_init__(self) -> None:
        _require_enum("severity", self.severity, Severity)


@dataclass(frozen=True, slots=True)
class ExtractedDetail:
    source: DetailSource
    reference: SelectedReference
    units: UnitInfo
    scale: ScaleInfo | None
    bounds: Bounds3D | None
    inventory: tuple[EntityCount, ...]
    dispositions: tuple[EntityDisposition, ...]
    dependencies: tuple[ExternalDependency, ...]
    warnings: tuple[ExtractionWarning, ...]

    def __post_init__(self) -> None:
        _require_tuple("inventory", self.inventory)
        _require_tuple("dispositions", self.dispositions)
        _require_tuple("dependencies", self.dependencies)
        _require_tuple("warnings", self.warnings)


def _to_plain_data(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _to_plain_data(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, tuple):
        return [_to_plain_data(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("No se pueden serializar valores NaN o infinitos")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"Tipo no serializable en el modelo de detalle: {type(value).__name__}")


def detail_to_dict(detail: ExtractedDetail) -> dict:
    """Serializa el modelo a datos JSON estables, sin dependencias de formatos CAD."""
    if not isinstance(detail, ExtractedDetail):
        raise TypeError("detail debe ser un ExtractedDetail")
    result = {"model_version": "1"}
    result.update(_to_plain_data(detail))
    return result
