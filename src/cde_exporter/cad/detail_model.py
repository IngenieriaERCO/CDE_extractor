"""Modelo de dominio independiente para un detalle CAD extraído."""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum, StrEnum


class EntityStatus(StrEnum):
    CONSERVED = "conserved"
    TRANSFORMED = "transformed"
    SIMPLIFIED = "simplified"
    OMITTED = "omitted"
    UNSUPPORTED = "unsupported"
    NOT_VISIBLE = "not_visible"


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
    total_occurrences: int | None = None

    def __post_init__(self) -> None:
        if self.occurrence < 1:
            raise ValueError("occurrence debe ser un entero desde 1")
        if self.total_occurrences is not None and (
            isinstance(self.total_occurrences, bool)
            or not isinstance(self.total_occurrences, int)
            or self.total_occurrences < self.occurrence
        ):
            raise ValueError("total_occurrences debe ser un entero mayor o igual que occurrence")
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
    extracted_at: str | None = None
    exporter_version: str | None = None
    block_definition_handle: str | None = None
    selection_method: SelectionMethod | None = None
    user_units_override: str | None = None
    detail_size: tuple[float, float] | None = None
    output_factor_mm_per_unit: float | None = None
    content_size_mm: tuple[float, float] | None = None
    page_size_mm: tuple[float, float] | None = None
    margin_mm: float | None = None
    physical_scale_guaranteed: bool | None = None
    physical_scale_note: str | None = None
    transformations: tuple[TransformationStep, ...] = ()
    nested_blocks: tuple[NestedBlockInfo, ...] = ()
    fonts: tuple[FontUse, ...] = ()
    hatches: HatchSummary | None = None
    result: ExtractionResult | None = None

    def __post_init__(self) -> None:
        _require_tuple("inventory", self.inventory)
        _require_tuple("dispositions", self.dispositions)
        _require_tuple("dependencies", self.dependencies)
        _require_tuple("warnings", self.warnings)
        _require_enum_if_present("selection_method", self.selection_method, SelectionMethod)
        for name in ("detail_size", "content_size_mm", "page_size_mm"):
            size = getattr(self, name)
            if size is not None:
                _require_finite_tuple(name, size, 2)
                if any(dimension < 0 for dimension in size):
                    raise ValueError(f"{name} no puede contener valores negativos")
        _require_finite("output_factor_mm_per_unit", self.output_factor_mm_per_unit)
        if self.output_factor_mm_per_unit is not None and self.output_factor_mm_per_unit <= 0:
            raise ValueError("output_factor_mm_per_unit debe ser positivo")
        _require_finite("margin_mm", self.margin_mm)
        if self.margin_mm is not None and self.margin_mm < 0:
            raise ValueError("margin_mm no puede ser negativo")
        for name in ("transformations", "nested_blocks", "fonts"):
            _require_tuple(name, getattr(self, name))


def _to_plain_data(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _to_plain_data(getattr(value, item.name))
            for item in fields(value)
        }
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
    result = {"model_version": "2"}
    result.update(_to_plain_data(detail))
    return result


def _require_enum_if_present(name: str, value: Enum | None, enum_type: type[Enum]) -> None:
    if value is not None:
        _require_enum(name, value, enum_type)


def finalize_detail(detail: ExtractedDetail, result: ExtractionResult) -> ExtractedDetail:
    """Devuelve una copia finalizada sin alterar el detalle original."""
    if not isinstance(detail, ExtractedDetail):
        raise TypeError("detail debe ser un ExtractedDetail")
    if not isinstance(result, ExtractionResult):
        raise TypeError("result debe ser un ExtractionResult")
    if detail.result is not None:
        raise ValueError("El detalle ya fue finalizado")
    return dataclasses.replace(detail, result=result)


OMITTED_REASON = "Omitida por --omit-unsupported: ezdxf no puede dibujar este tipo."
_OMITTED_GROUP_LIMIT = 10


def omitted_entities_message(omitted: tuple[tuple[str, str, int], ...]) -> str:
    """Resume las entidades omitidas por tipo y capa, con concordancia de número."""
    total = sum(count for _type, _layer, count in omitted)
    groups = [
        f"{entity_type} en capa {layer} ({count})"
        for entity_type, layer, count in omitted[:_OMITTED_GROUP_LIMIT]
    ]
    hidden = len(omitted) - _OMITTED_GROUP_LIMIT
    if hidden > 0:
        groups.append(f"… y {hidden} {'grupo' if hidden == 1 else 'grupos'} más")
    if total == 1:
        head, tail = "Se omitió 1 entidad no soportada", "no aparece en el PDF"
    else:
        head, tail = f"Se omitieron {total} entidades no soportadas", "no aparecen en el PDF"
    return f"{head} (--omit-unsupported): {'; '.join(groups)}; {tail}."


def mark_omitted_entities(
    detail: ExtractedDetail, omitted: tuple[tuple[str, str, int], ...]
) -> ExtractedDetail:
    """Marca como ``omitted`` las no soportadas que el renderizador omitió.

    ``omitted`` contiene (tipo, capa, cantidad) contados por el renderizador. Solo
    se convierten entidades que el modelo ya clasificó como no soportadas; si el
    renderizador informa más de las que hay, se añade un aviso de conciliación.
    """
    if not isinstance(detail, ExtractedDetail):
        raise TypeError("detail debe ser un ExtractedDetail")
    if detail.result is not None:
        raise ValueError("El detalle ya fue finalizado")
    if not omitted:
        return detail
    requested: dict[tuple[str, str], int] = {}
    for entity_type, layer, count in omitted:
        requested[(entity_type, layer)] = requested.get((entity_type, layer), 0) + count
    converted: dict[tuple[str, str], int] = {}
    dispositions = []
    for item in detail.dispositions:
        key = (item.entity_type, item.layer)
        if item.status is EntityStatus.UNSUPPORTED and converted.get(key, 0) < requested.get(
            key, 0
        ):
            converted[key] = converted.get(key, 0) + 1
            # Si la causa son datos inválidos (no el tipo), se conserva ese motivo.
            reason = item.reason or ""
            if reason.startswith("Datos inválidos"):
                reason = f"Omitida por --omit-unsupported: {reason[0].lower()}{reason[1:]}"
            else:
                reason = OMITTED_REASON
            item = dataclasses.replace(item, status=EntityStatus.OMITTED, reason=reason)
        dispositions.append(item)
    inventory = _move_unsupported(detail.inventory, converted, EntityStatus.OMITTED)
    warnings = [
        ExtractionWarning(
            "UNSUPPORTED_OMITTED", Severity.WARNING, omitted_entities_message(omitted)
        )
    ]
    for (entity_type, layer), count in sorted(requested.items()):
        missing = count - converted.get((entity_type, layer), 0)
        if missing > 0:
            warnings.append(
                ExtractionWarning(
                    "FIDELITY_RECONCILIATION",
                    Severity.WARNING,
                    f"No coincide la conciliación de omitidas ({entity_type} en capa {layer}: "
                    f"renderizador {count}, modelo {count - missing} no soportadas).",
                )
            )
    return dataclasses.replace(
        detail,
        inventory=tuple(inventory),
        dispositions=tuple(dispositions),
        warnings=(*detail.warnings, *warnings),
    )


def _move_unsupported(
    inventory: tuple[EntityCount, ...], moved: dict[tuple[str, str], int], status: EntityStatus
) -> list[EntityCount]:
    """Pasa ``moved[(tipo, capa)]`` entidades de las filas no soportadas a ``status``."""
    return _move_rows(inventory, moved, EntityStatus.UNSUPPORTED, status)


def _move_rows(
    inventory: tuple[EntityCount, ...],
    moved: dict[tuple[str, str], int],
    source: EntityStatus,
    status: EntityStatus,
) -> list[EntityCount]:
    """Pasa ``moved[(tipo, capa)]`` entidades de las filas ``source`` a ``status``."""
    totals: dict[tuple[str, str, EntityStatus], int] = {}
    for row in inventory:
        key = (row.entity_type, row.layer)
        count = moved.get(key, 0) if row.status is source else 0
        if row.count > count:
            totals[(*key, row.status)] = totals.get((*key, row.status), 0) + row.count - count
        if count:
            totals[(*key, status)] = totals.get((*key, status), 0) + count
    result = [
        EntityCount(entity_type, layer, row_status, count)
        for (entity_type, layer, row_status), count in totals.items()
    ]
    result.sort(key=lambda item: (item.entity_type, item.layer, item.status.value))
    return result


SOLID_INSTEAD_OF_PATTERN = "relleno sólido en lugar del patrón"
_DEGRADED_GROUP_LIMIT = 10


def mark_degraded_hatches(
    detail: ExtractedDetail, degraded: tuple[tuple[int, str | None, str, str, float, str], ...]
) -> ExtractedDetail:
    """Marca como ``simplified`` los HATCH conservados que se rellenaron al dibujarlos.

    ``degraded`` contiene (índice en el inventario, handle, capa, patrón, escala, motivo) de
    cada HATCH cuyo patrón agotó el tiempo o la memoria. Se actualizan el inventario, las
    disposiciones, el resumen de HATCH y se añade el aviso ``HATCH_DEGRADED``.
    """
    if not isinstance(detail, ExtractedDetail):
        raise TypeError("detail debe ser un ExtractedDetail")
    if detail.result is not None:
        raise ValueError("El detalle ya fue finalizado")
    if not degraded:
        return detail
    moved: dict[tuple[str, str], int] = {}
    for _index, _handle, layer, _pattern, _scale, _reason in degraded:
        moved[("HATCH", layer)] = moved.get(("HATCH", layer), 0) + 1
    available = {
        (row.entity_type, row.layer): row.count
        for row in detail.inventory
        if row.status is EntityStatus.CONSERVED
    }
    warnings = []
    for key, count in sorted(moved.items()):
        if available.get(key, 0) < count:
            warnings.append(
                ExtractionWarning(
                    "FIDELITY_RECONCILIATION",
                    Severity.WARNING,
                    f"No coincide la conciliación de HATCH rellenados (capa {key[1]}: "
                    f"renderizador {count}, modelo {available.get(key, 0)} conservados).",
                )
            )
            moved[key] = available.get(key, 0)
    dispositions = list(detail.dispositions)
    taken: dict[tuple[str, str], int] = {}
    for index, handle, layer, _pattern, _scale, reason in degraded:
        key = ("HATCH", layer)
        if taken.get(key, 0) >= moved.get(key, 0):
            continue
        taken[key] = taken.get(key, 0) + 1
        dispositions.append(
            EntityDisposition(
                index,
                handle,
                "HATCH",
                layer,
                EntityStatus.SIMPLIFIED,
                f"HATCH rellenado al dibujarlo: {reason}.",
            )
        )
    dispositions.sort(key=lambda item: item.index)
    warnings.insert(
        0, ExtractionWarning("HATCH_DEGRADED", Severity.WARNING, degraded_hatches_message(degraded))
    )
    return dataclasses.replace(
        detail,
        inventory=tuple(
            _move_rows(detail.inventory, moved, EntityStatus.CONSERVED, EntityStatus.SIMPLIFIED)
        ),
        dispositions=tuple(dispositions),
        hatches=_degrade_hatch_summary(detail.hatches, degraded),
        warnings=(*detail.warnings, *warnings),
    )


def _degrade_hatch_summary(
    summary: HatchSummary | None, degraded: tuple[tuple[int, str | None, str, str, float, str], ...]
) -> HatchSummary | None:
    if summary is None:
        return None
    groups = list(summary.groups)
    details = list(summary.details)
    simplified = summary.simplified
    for _index, _handle, layer, pattern, scale, reason in degraded:
        kept = next(
            (
                position
                for position, group in enumerate(groups)
                if group.action == "conserved"
                and group.pattern == pattern
                and group.scale is not None
                and math.isclose(group.scale, scale)
            ),
            None,
        )
        if kept is None:
            continue
        group = groups[kept]
        if group.count > 1:
            groups[kept] = dataclasses.replace(group, count=group.count - 1)
        else:
            groups.pop(kept)
        same = next(
            (
                position
                for position, other in enumerate(groups)
                if other.action == "simplified"
                and other.pattern == pattern
                and other.kind == group.kind
                and other.reason == reason
                and other.scale is not None
                and math.isclose(other.scale, scale)
            ),
            None,
        )
        if same is None:
            groups.append(
                HatchGroup(
                    group.kind, pattern, scale, 1, "simplified", SOLID_INSTEAD_OF_PATTERN, reason
                )
            )
        else:
            groups[same] = dataclasses.replace(groups[same], count=groups[same].count + 1)
        details.append(
            HatchDetail(layer, pattern, scale, "simplified", f"{reason}.", SOLID_INSTEAD_OF_PATTERN)
        )
        simplified += 1
    return dataclasses.replace(
        summary,
        simplified=min(simplified, summary.total),
        groups=tuple(groups),
        details=tuple(details),
    )


def degraded_hatches_message(
    degraded: tuple[tuple[int, str | None, str, str, float, str], ...],
) -> str:
    """Aviso de los HATCH rellenados al dibujarlos, con concordancia de número."""
    total = len(degraded)
    items = [
        f"capa {layer}, patrón {pattern}, escala {scale:g} ({reason})"
        for _index, _handle, layer, pattern, scale, reason in degraded[:_DEGRADED_GROUP_LIMIT]
    ]
    hidden = total - _DEGRADED_GROUP_LIMIT
    if hidden > 0:
        items.append(f"… y {hidden} más")
    head = (
        "Se rellenó 1 HATCH cuyo patrón no se pudo dibujar"
        if total == 1
        else f"Se rellenaron {total} HATCH cuyo patrón no se pudo dibujar"
    )
    return f"{head}: {'; '.join(items)}."


FOOTPRINT_REASON = "3DSOLID dibujado como huella 2D"


def mark_footprint_solids(
    detail: ExtractedDetail,
    converted_handles: tuple[str, ...],
    failure_reasons: tuple[tuple[str, int], ...] = (),
    max_error_mm: float = 0.0,
) -> ExtractedDetail:
    """Marca como ``transformed`` los 3DSOLID sustituidos por su huella (--solids footprint).

    Se relacionan por handle: son entidades de la definición del bloque, y cada copia
    anidada de esa definición tiene su propia disposición con el mismo handle.
    ``failure_reasons`` agrupa (motivo, cantidad de definiciones) de los no convertidos.
    """
    if not isinstance(detail, ExtractedDetail):
        raise TypeError("detail debe ser un ExtractedDetail")
    if detail.result is not None:
        raise ValueError("El detalle ya fue finalizado")
    _require_finite("max_error_mm", max_error_mm)
    handles = set(converted_handles)
    moved: dict[tuple[str, str], int] = {}
    dispositions = []
    for item in detail.dispositions:
        if (
            item.entity_type == "3DSOLID"
            and item.status is EntityStatus.UNSUPPORTED
            and item.handle in handles
        ):
            key = (item.entity_type, item.layer)
            moved[key] = moved.get(key, 0) + 1
            item = dataclasses.replace(
                item, status=EntityStatus.TRANSFORMED, reason=FOOTPRINT_REASON
            )
        dispositions.append(item)
    warnings = []
    total = sum(moved.values())
    if total:
        drawn = "Se dibujó 1 3DSOLID" if total == 1 else f"Se dibujaron {total} 3DSOLID"
        distinct = "1 sólido distinto" if len(handles) == 1 else f"{len(handles)} sólidos distintos"
        warnings.append(
            ExtractionWarning(
                "SOLIDS_AS_FOOTPRINT",
                Severity.WARNING,
                f"{drawn} como huella 2D (proyección en planta; {distinct}); "
                f"error máximo estimado {max_error_mm:.3f} mm en el papel.",
            )
        )
    if failure_reasons:
        count = sum(amount for _reason, amount in failure_reasons)
        reasons = "; ".join(f"{reason} ({amount})" for reason, amount in failure_reasons)
        head = (
            "1 sólido distinto no se pudo"
            if count == 1
            else f"{count} sólidos distintos no se pudieron"
        )
        warnings.append(
            ExtractionWarning(
                "SOLIDS_NOT_CONVERTED",
                Severity.WARNING,
                f"{head} convertir en huella 2D: {reasons}.",
            )
        )
    if not total and not warnings:
        return detail
    return dataclasses.replace(
        detail,
        inventory=tuple(_move_unsupported(detail.inventory, moved, EntityStatus.TRANSFORMED)),
        dispositions=tuple(dispositions),
        warnings=(*detail.warnings, *warnings),
    )


class SelectionMethod(StrEnum):
    AUTOMATIC = "automatic"
    OCCURRENCE = "occurrence"
    HANDLE = "handle"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class TransformationStep:
    kind: str
    description: str
    values: tuple[float, ...] = ()


@dataclass(frozen=True, slots=True)
class NestedBlockInfo:
    name: str
    max_depth: int
    reference_count: int

    def __post_init__(self) -> None:
        if self.max_depth < 0:
            raise ValueError("max_depth debe ser mayor o igual que cero")
        if self.reference_count < 0:
            raise ValueError("reference_count debe ser mayor o igual que cero")


@dataclass(frozen=True, slots=True)
class FontUse:
    requested: str
    resolved: str
    found: bool
    uses: int
    origin: str | None = None

    def __post_init__(self) -> None:
        if self.uses < 0:
            raise ValueError("uses debe ser mayor o igual que cero")


@dataclass(frozen=True, slots=True)
class HatchDetail:
    layer: str
    pattern: str
    scale: float | None
    action: str
    reason: str | None
    operation: str | None = None

    def __post_init__(self) -> None:
        _require_finite("scale", self.scale)


@dataclass(frozen=True, slots=True)
class HatchGroup:
    """HATCH del mismo tipo, patrón y escala que recibieron el mismo tratamiento."""

    kind: str
    pattern: str
    scale: float | None
    count: int
    action: str
    operation: str
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_finite("scale", self.scale)
        if self.count < 1:
            raise ValueError("count debe ser mayor que cero")


@dataclass(frozen=True, slots=True)
class HatchSummary:
    total: int
    simplified: int
    details: tuple[HatchDetail, ...] = ()
    groups: tuple[HatchGroup, ...] = ()
    mode: str | None = None

    def __post_init__(self) -> None:
        if self.total < 0:
            raise ValueError("total debe ser mayor o igual que cero")
        if self.simplified < 0:
            raise ValueError("simplified debe ser mayor o igual que cero")
        if self.simplified > self.total:
            raise ValueError("simplified debe ser menor o igual que total")
        _require_tuple("details", self.details)
        _require_tuple("groups", self.groups)


class ResultStatus(StrEnum):
    COMPLETE = "complete"
    COMPLETE_WITH_WARNINGS = "complete_with_warnings"


# Nombres de las comprobaciones del PDF (``pdf_checks``).
PDF_READABLE_CHECK = "PDF legible"
SINGLE_PAGE_CHECK = "Una sola página"
PAGE_SIZE_CHECK = "Tamaño de página coincide con la escala"
# Capa de texto buscable; el informe TXT elige su nota según esté o no.
SEARCHABLE_TEXT_CHECK = "Texto buscable"
# Escala real del dibujo medida en el PDF; la escala física solo es "garantizada" si pasa.
DRAWING_SCALE_CHECK = "Escala del dibujo"
# Manifiesto escrito frente al PDF (hash, bytes, páginas, tamaño y nombre), antes de publicar.
MANIFEST_CHECK = "Manifiesto coherente con el PDF"
# Toda comprobación está en una de las dos listas. Si una que bloquea no pasa, no se publica
# nada (código 1); si una que solo avisa no pasa, se publica con advertencia.
BLOCKING_CHECKS = frozenset(
    {PDF_READABLE_CHECK, SINGLE_PAGE_CHECK, PAGE_SIZE_CHECK, MANIFEST_CHECK}
)
ADVISORY_CHECKS = frozenset({SEARCHABLE_TEXT_CHECK, DRAWING_SCALE_CHECK})


def check_blocks(name: str) -> bool:
    """True si la comprobación ``name`` impide publicar cuando no pasa.

    Una comprobación sin clasificar es un error del programa: no se decide por omisión.
    """
    if name in BLOCKING_CHECKS:
        return True
    if name in ADVISORY_CHECKS:
        return False
    raise ValueError(f"Comprobación sin clasificar (ni bloquea ni avisa): {name!r}")


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    name: str
    passed: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    pdf_name: str
    status: ResultStatus
    validations: tuple[ValidationCheck, ...] = ()
    warnings_after_export: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_enum("status", self.status, ResultStatus)
        _require_tuple("validations", self.validations)
        _require_tuple("warnings_after_export", self.warnings_after_export)
        if any(not isinstance(warning, str) for warning in self.warnings_after_export):
            raise ValueError("warnings_after_export solo puede contener texto")
