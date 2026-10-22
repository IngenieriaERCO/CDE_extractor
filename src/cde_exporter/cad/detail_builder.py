"""Traduce resultados CAD al modelo independiente de un detalle extraído."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from ezdxf.document import Drawing

from cde_exporter.cad.block_extractor import BlockExtraction, BlockReference
from cde_exporter.cad.detail_model import (
    Bounds3D,
    DetailSource,
    EntityCount,
    EntityDisposition,
    EntityStatus,
    ExternalDependency,
    ExtractedDetail,
    ExtractionWarning,
    ScaleInfo,
    SelectedReference,
    Severity,
    UnitInfo,
)
from cde_exporter.cad.fidelity import (
    FidelityReport,
    _style_and_font,
    fidelity_dispatch,
    hatch_simplification_reason,
    iter_detail_entities,
    unsupported_entity_reason,
)
from cde_exporter.cad.scale import UnitVerification

_URL_REFERENCE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def _dependency_resolution(reference: str, source_path: str | Path) -> tuple[bool, str | None]:
    """Comprueba una ruta local sin abrirla y bloquea URLs y rutas UNC."""
    if _URL_REFERENCE.match(reference):
        return False, "La referencia es una URL; no se consulta la red."
    if reference.startswith(("\\\\", "//")):
        return False, "La referencia es una ruta UNC; no se accede a la red."
    try:
        path = Path(reference)
        if path.exists():
            return True, "Archivo encontrado en la ruta indicada."
        source_directory = Path(source_path).parent
        source_directory_text = str(source_directory)
        if source_directory_text.startswith(("\\\\", "//")):
            return False, "No se accede a la carpeta de red del DXF de origen."
        if not path.is_absolute() and (source_directory / path).exists():
            return True, "Archivo encontrado relativo al DXF de origen."
    except (OSError, ValueError) as error:
        return False, f"No se pudo comprobar la ruta local: {error}."
    return False, "No se encontr\u00f3 el archivo en la ruta indicada ni junto al DXF de origen."


def _external_dependencies(
    document: Drawing, source_path: str | Path
) -> tuple[ExternalDependency, ...]:
    """Recoge dependencias usadas por el detalle y sus bloques anidados.

    ``document`` debe ser el DXF ya extraído, cuyo Model Space contiene solo el
    INSERT seleccionado. Pasar el DXF de origen completo incorporaría las entidades
    sueltas de su Model Space.
    """
    found: dict[tuple[str, str], ExternalDependency] = {}

    def add_file(kind: str, reference: str | None) -> None:
        value = str(reference or "")
        if value:
            resolved, detail = _dependency_resolution(value, source_path)
        else:
            resolved, detail = False, "La definici\u00f3n externa no contiene una ruta de archivo."
        key = (kind, value)
        found.setdefault(key, ExternalDependency(kind, value, True, resolved, detail))

    for entity in iter_detail_entities(document):
        entity_type = entity.dxftype()
        if entity_type == "INSERT":
            name = str(entity.dxf.get("name", "") or "")
            block = document.blocks.get(name)
            if block is None:
                key = ("missing_block", name)
                found.setdefault(
                    key,
                    ExternalDependency(
                        "missing_block", name, True, False,
                        f"No existe la definición del bloque {name!r} en el DXF.",
                    ),
                )
            elif block.block.is_xref or block.block.is_xref_overlay:
                add_file("xref", block.block.dxf.get("xref_path"))
        elif entity_type == "IMAGE":
            image_def = entity.image_def
            add_file("image", image_def.dxf.get("filename") if image_def else None)
        elif entity_type in {"PDFUNDERLAY", "DWFUNDERLAY", "DGNUNDERLAY"}:
            underlay_def = entity.get_underlay_def()
            add_file("underlay", underlay_def.dxf.get("filename") if underlay_def else None)

    return tuple(found[key] for key in sorted(found))


def build_extracted_detail(
    *,
    source_path: str | Path,
    block_name: str,
    reference: BlockReference,
    extraction: BlockExtraction,
    unit_verification: UnitVerification,
    nominal_denominator: float | None,
    factor_mm_per_drawing_unit: float | None,
    effective_bounds_xyz: tuple[tuple[float, float, float], tuple[float, float, float]] | None,
    document: Drawing | None = None,
    fidelity_report: FidelityReport | None = None,
    hatch_mode: str | None = None,
    hatch_threshold: int | None = None,
    include_dependencies: bool = False,
) -> ExtractedDetail:
    """Construye el detalle y clasifica cada entidad según la fidelidad existente.

    El documento recibido debe ser el DXF ya extraído, cuyo Model Space contiene solo
    el INSERT seleccionado. Pasar el DXF de origen completo incorporaría las entidades
    sueltas de su Model Space. Las dependencias requeridas son solo las usadas por la
    geometría del bloque seleccionado y sus bloques anidados.

    ``unsupported`` usa las mismas reglas y motivos que ``inspect_fidelity``;
    ``simplified`` refleja las políticas de HATCH y sus umbrales; ``transformed``
    marca texto cuya fuente figura como sustituta en el informe de fidelidad.
    El resto queda ``conserved``. ``omitted`` se reserva para omisiones explícitas
    del renderizador; actualmente esas omisiones abortan la exportación y no llegan
    a producir un detalle serializado.
    """
    selected_reference = SelectedReference(
        occurrence=reference.order,
        handle=reference.handle,
        layer=reference.layer,
        insertion_xyz=reference.insert,
        rotation_deg=reference.rotation,
        scale_xyz=(reference.xscale, reference.yscale, reference.zscale),
    )
    units = UnitInfo(
        declared_code=extraction.units_code,
        name=unit_verification.unit_name,
        status=unit_verification.status,
        millimeters_per_unit=unit_verification.millimeters_per_unit,
        warnings=unit_verification.warnings,
    )
    scale = None
    if nominal_denominator is not None:
        if factor_mm_per_drawing_unit is None:
            raise ValueError("Se requiere el factor de salida cuando se indica escala nominal")
        scale = ScaleInfo(
            nominal_denominator=nominal_denominator,
            factor_mm_per_drawing_unit=factor_mm_per_drawing_unit,
            insert_factor_xyz=(reference.xscale, reference.yscale, reference.zscale),
        )
    elif factor_mm_per_drawing_unit is not None:
        raise ValueError("No se puede indicar un factor de salida sin escala nominal")

    bounds = (
        Bounds3D(min_xyz=effective_bounds_xyz[0], max_xyz=effective_bounds_xyz[1])
        if effective_bounds_xyz is not None
        else None
    )
    warnings = []
    if unit_verification.warnings:
        code = (
            "UNITS_SUSPECT"
            if unit_verification.status == "con indicios de inconsistencia"
            else "UNITS_UNVERIFIED"
        )
        warnings.extend(
            ExtractionWarning(code, Severity.WARNING, message)
            for message in unit_verification.warnings
        )
    elif not unit_verification.verified:
        warnings.append(
            ExtractionWarning(
                "UNITS_UNVERIFIED",
                Severity.WARNING,
                f"Unidades sin verificar: {unit_verification.status}",
            )
        )
    if extraction.unbounded:
        warnings.append(
            ExtractionWarning(
                "ENTITIES_WITHOUT_BOUNDS",
                Severity.WARNING,
                "La extracción contiene entidades sin caja geométrica calculable.",
            )
        )

    counts: Counter[tuple[str, str, EntityStatus]] = Counter()
    dispositions = []
    if (
        document is not None
        and fidelity_report is not None
        and hatch_mode is not None
        and hatch_threshold is not None
    ):
        dispatch = fidelity_dispatch(document)
        for index, entity in enumerate(iter_detail_entities(document)):
            entity_type = entity.dxftype()
            layer = str(entity.dxf.get("layer", "0") or "0")
            reason = unsupported_entity_reason(entity, dispatch)
            if reason is not None:
                status = EntityStatus.UNSUPPORTED
                reason = f"Tipo no soportado o condicionado: {reason}."
            elif entity_type == "HATCH" and (
                hatch_reason := hatch_simplification_reason(entity, hatch_mode, hatch_threshold)
            ):
                status = EntityStatus.SIMPLIFIED
                reason = hatch_reason
            elif entity_type in {"TEXT", "MTEXT", "DIMENSION", "ATTRIB"}:
                _style, source_font, resolved_font, found = _style_and_font(entity, document)
                if not found:
                    status = EntityStatus.TRANSFORMED
                    reason = (
                        f"Fuente sustituida: {source_font} se representa con {resolved_font}."
                    )
                else:
                    status = EntityStatus.CONSERVED
            else:
                status = EntityStatus.CONSERVED
            counts[(entity_type, layer, status)] += 1
            if status is not EntityStatus.CONSERVED:
                dispositions.append(
                    EntityDisposition(
                        index=index,
                        handle=entity.dxf.get("handle"),
                        entity_type=entity_type,
                        layer=layer,
                        status=status,
                        reason=reason,
                    )
                )
    inventory = tuple(
        EntityCount(entity_type, layer, status, count)
        for (entity_type, layer, status), count in sorted(
            counts.items(), key=lambda item: (item[0][0], item[0][1], item[0][2].value)
        )
    )

    if (
        document is not None
        and fidelity_report is not None
        and hatch_mode is not None
        and hatch_threshold is not None
    ):
        model_unsupported: Counter[tuple[str, str]] = Counter()
        for row in inventory:
            if row.status is EntityStatus.UNSUPPORTED:
                model_unsupported[(row.entity_type, row.layer)] += row.count
        report_unsupported: Counter[tuple[str, str]] = Counter()
        for (entity_type, layer, _reason), count in fidelity_report.unsupported_counts.items():
            report_unsupported[(entity_type, layer)] += count
        mismatches = []
        model_total = sum(row.count for row in inventory)
        if model_total != fidelity_report.entity_total:
            mismatches.append(
                f"total de entidades: inventario {model_total}, "
                f"informe {fidelity_report.entity_total}"
            )
        if model_unsupported != report_unsupported:
            mismatches.append(
                f"entidades no soportadas: inventario {dict(model_unsupported)}, "
                f"informe {dict(report_unsupported)}"
            )
        model_simplified_hatches = sum(
            row.count
            for row in inventory
            if row.entity_type == "HATCH" and row.status is EntityStatus.SIMPLIFIED
        )
        if model_simplified_hatches != fidelity_report.simplified_hatches:
            mismatches.append(
                f"HATCH simplificados: inventario {model_simplified_hatches}, "
                f"informe {fidelity_report.simplified_hatches}"
            )
        model_transformed = sum(
            row.count for row in inventory if row.status is EntityStatus.TRANSFORMED
        )
        report_substitutions = sum(
            count
            for (_style, _source, _resolved, found), count in fidelity_report.fonts.items()
            if not found
        )
        if model_transformed != report_substitutions:
            mismatches.append(
                f"fuentes sustituidas: inventario {model_transformed}, "
                f"informe {report_substitutions}"
            )
        warnings.extend(
            ExtractionWarning(
                "FIDELITY_RECONCILIATION",
                Severity.WARNING,
                f"No coincide la conciliación con FidelityReport ({mismatch}).",
            )
            for mismatch in mismatches
        )

    dependencies = (
        _external_dependencies(document, source_path)
        if include_dependencies and document is not None
        else ()
    )
    warning_codes = {
        "xref": ("DEPENDENCY_UNRESOLVED_XREF", Severity.CRITICAL),
        "image": ("DEPENDENCY_UNRESOLVED_IMAGE", Severity.WARNING),
        "underlay": ("DEPENDENCY_UNRESOLVED_UNDERLAY", Severity.WARNING),
        "missing_block": ("DEPENDENCY_UNRESOLVED_BLOCK", Severity.CRITICAL),
    }
    for dependency in dependencies:
        if not dependency.resolved:
            code, severity = warning_codes[dependency.kind]
            warnings.append(
                ExtractionWarning(
                    code,
                    severity,
                    f"Dependencia {dependency.kind} no resuelta ({dependency.reference}): "
                    f"{dependency.detail}",
                )
            )

    return ExtractedDetail(
        source=DetailSource(path=str(Path(source_path)), block_name=block_name),
        reference=selected_reference,
        units=units,
        scale=scale,
        bounds=bounds,
        inventory=inventory,
        dispositions=tuple(dispositions),
        dependencies=dependencies,
        warnings=tuple(warnings),
    )
