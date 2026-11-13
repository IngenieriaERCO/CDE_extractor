"""Traduce resultados CAD al modelo independiente de un detalle extraído."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from ezdxf.document import Drawing

from cde_exporter.cad.block_extractor import BlockExtraction, BlockReference
from cde_exporter.cad.detail_extras import (
    FontUse,
    HatchSummary,
    NestedBlockInfo,
    TransformationStep,
)
from cde_exporter.cad.detail_model import (
    Bounds3D,
    DetailSource,
    EntityCount,
    EntityDisposition,
    EntityStatus,
    ExternalDependency,
    ExtractedDetail,
    ExtractionWarning,
    HiddenLayerContent,
    ModelSpaceSelection,
    ScaleInfo,
    SelectedReference,
    SelectionMethod,
    Severity,
    UnitInfo,
)
from cde_exporter.cad.entity_policy import PolicyContext, decide
from cde_exporter.cad.fidelity import (
    FidelityReport,
    entity_facts,
    fidelity_dispatch,
    iter_detail_entities,
    iter_detail_entities_with_visibility,
    missing_glyphs_message,
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


def _entities_text(count: int) -> str:
    return "1 entidad" if count == 1 else f"{count} entidades"


def hidden_layer_warnings(
    block_name: str,
    hidden: tuple[HiddenLayerContent, ...],
    included: tuple[HiddenLayerContent, ...],
    overlap: tuple[HiddenLayerContent, ...],
    *,
    modelspace: bool = False,
) -> list[ExtractionWarning]:
    """Un aviso por capa oculta: dentro del bloque (info), incluida a propósito (info) y
    suelta en el Model Space sobre la zona del bloque (warning: puede faltar en el PDF).

    Con ``modelspace`` (cad extract-modelspace) se habla de la selección, no del bloque de
    trabajo que la agrupa."""
    owner = "de la selección del Model Space" if modelspace else f"del bloque «{block_name}»"
    warnings = [
        ExtractionWarning(
            "HIDDEN_LAYERS_CONTENT",
            Severity.INFO,
            f"Capa {item.state} «{item.layer}»: {_entities_text(item.count)} {owner} no se "
            f"{'dibuja' if item.count == 1 else 'dibujan'}.",
        )
        for item in hidden
    ]
    warnings += [
        ExtractionWarning(
            "HIDDEN_LAYERS_INCLUDED",
            Severity.INFO,
            f"Capa {item.state} en el CAD «{item.layer}»: se "
            f"{'dibuja' if item.count == 1 else 'dibujan'} {_entities_text(item.count)} "
            f"{owner} por --include-hidden-layers (el DXF de origen no cambia).",
        )
        for item in included
    ]
    warnings += [
        ExtractionWarning(
            "MODELSPACE_HIDDEN_OVERLAP",
            Severity.WARNING,
            f"Capa {item.state} «{item.layer}»: {_entities_text(item.count)} suelta"
            f"{'' if item.count == 1 else 's'} en el Model Space (fuera de cualquier bloque) "
            f"sobre la zona del bloque «{block_name}»; no forma"
            f"{'' if item.count == 1 else 'n'} parte del bloque y no sale"
            f"{'' if item.count == 1 else 'n'} en este PDF.",
        )
        for item in overlap
    ]
    return warnings


def modelspace_selection_warnings(selection: ModelSpaceSelection | None) -> list[ExtractionWarning]:
    """Avisos de la selección del Model Space (cad extract-modelspace)."""
    if selection is None:
        return []
    warnings = []
    if selection.not_copied:
        warnings.append(
            ExtractionWarning(
                "MODELSPACE_NOT_COPIED",
                Severity.WARNING,
                f"{_entities_text(selection.not_copied)} de la selección no se "
                f"{'pudo' if selection.not_copied == 1 else 'pudieron'} copiar (ezdxf no sabe "
                f"copiar ese tipo) y no {'sale' if selection.not_copied == 1 else 'salen'} "
                "en el PDF.",
            )
        )
    if selection.zero_length:
        warnings.append(
            ExtractionWarning(
                "MODELSPACE_ZERO_LENGTH",
                Severity.INFO,
                f"{_entities_text(selection.zero_length)} de longitud nula (líneas o "
                "polilíneas que en el PDF serían un punto suelto) no se "
                f"{'dibuja' if selection.zero_length == 1 else 'dibujan'}.",
            )
        )
    if selection.clipped:
        warnings.append(
            ExtractionWarning(
                "MODELSPACE_CLIPPED",
                Severity.INFO,
                f"Recorte a la zona: {_entities_text(selection.clipped_entities)} "
                f"{'recortada' if selection.clipped_entities == 1 else 'recortadas'} "
                f"({selection.clip_pieces} {'tramo' if selection.clip_pieces == 1 else 'tramos'}"
                f") y {selection.outside_removed} que solo tocaban la zona con su caja, quitadas."
                " La página mide la zona.",
            )
        )
    crossing = sum(count for _kind, count in selection.not_clipped)
    if crossing:
        kinds = ", ".join(f"{kind} {count}" for kind, count in selection.not_clipped)
        warnings.append(
            ExtractionWarning(
                "MODELSPACE_NOT_CLIPPED",
                Severity.WARNING,
                f"{_entities_text(crossing)} {'cruza' if crossing == 1 else 'cruzan'} el borde de "
                f"la zona y {'entra entera' if crossing == 1 else 'entran enteras'} porque no se "
                f"{'puede' if crossing == 1 else 'pueden'} recortar ({kinds}); la página se amplía "
                "para que quepan.",
            )
        )
    if selection.without_bounds:
        one = selection.without_bounds == 1
        warnings.append(
            ExtractionWarning(
                "MODELSPACE_WITHOUT_BOUNDS",
                Severity.WARNING,
                f"{_entities_text(selection.without_bounds)} sin caja calculable no se "
                f"{'pudo' if one else 'pudieron'} situar respecto a la zona pedida y no se "
                f"{'incluyó' if one else 'incluyeron'}.",
            )
        )
    return warnings


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
    extracted_at: str | None = None,
    exporter_version: str | None = None,
    block_definition_handle: str | None = None,
    selection_method: SelectionMethod | None = None,
    user_units_override: str | None = None,
    content_size_mm: tuple[float, float] | None = None,
    page_size_mm: tuple[float, float] | None = None,
    margin_mm: float | None = None,
    transformations: tuple[TransformationStep, ...] = (),
    nested_blocks: tuple[NestedBlockInfo, ...] = (),
    fonts: tuple[FontUse, ...] = (),
    hatches: HatchSummary | None = None,
    total_references: int | None = None,
    hidden_layers: tuple[HiddenLayerContent, ...] = (),
    included_hidden_layers: tuple[HiddenLayerContent, ...] = (),
    modelspace_hidden_overlap: tuple[HiddenLayerContent, ...] = (),
    modelspace_selection: ModelSpaceSelection | None = None,
) -> ExtractedDetail:
    """Construye el detalle y clasifica cada entidad según la fidelidad existente.

    El documento recibido debe ser el DXF ya extraído, cuyo Model Space contiene solo
    el INSERT seleccionado. Pasar el DXF de origen completo incorporaría las entidades
    sueltas de su Model Space. Las dependencias requeridas son solo las usadas por la
    geometría del bloque seleccionado y sus bloques anidados.

    ``unsupported`` usa las mismas reglas y motivos que ``inspect_fidelity``;
    ``simplified`` refleja las políticas de HATCH y sus umbrales; ``transformed``
    marca texto cuya fuente figura como sustituta en el informe de fidelidad.
    El resto queda ``conserved``. ``omitted`` no se asigna aquí: tras exportar con
    --omit-unsupported, ``mark_omitted_entities`` convierte en ``omitted`` las no
    soportadas que el renderizador omitió; sin esa opción, toda omisión aborta la
    exportación. Las entidades no visibles (capa apagada o congelada) no abortan:
    el exportador las cuenta y las informa como aviso.
    """
    selected_reference = SelectedReference(
        occurrence=reference.order,
        handle=reference.handle,
        layer=reference.layer,
        insertion_xyz=reference.insert,
        rotation_deg=reference.rotation,
        scale_xyz=(reference.xscale, reference.yscale, reference.zscale),
        total_occurrences=total_references,
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
    detail_size = (
        (
            bounds.max_xyz[0] - bounds.min_xyz[0],
            bounds.max_xyz[1] - bounds.min_xyz[1],
        )
        if bounds is not None
        else None
    )
    physical_scale_guaranteed = None
    physical_scale_note = None
    if nominal_denominator is not None:
        physical_scale_guaranteed = unit_verification.verified and not unit_verification.warnings
        if not physical_scale_guaranteed:
            physical_scale_note = (
                "; ".join(unit_verification.warnings)
                if unit_verification.warnings
                else f"Unidades sin verificar: {unit_verification.status}"
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
    warnings += modelspace_selection_warnings(modelspace_selection)
    warnings += hidden_layer_warnings(
        block_name,
        hidden_layers,
        included_hidden_layers,
        modelspace_hidden_overlap,
        modelspace=modelspace_selection is not None,
    )
    if fidelity_report is not None and fidelity_report.missing_glyphs:
        warnings.append(
            ExtractionWarning(
                "GLYPHS_MISSING",
                Severity.WARNING,
                missing_glyphs_message(fidelity_report.missing_glyphs),
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
        context = PolicyContext(hatch_mode, hatch_threshold)
        for index, item in enumerate(iter_detail_entities_with_visibility(document)):
            entity = item.entity
            entity_type = entity.dxftype()
            layer = str(entity.dxf.get("layer", "0") or "0")
            facts = entity_facts(
                entity,
                dispatch,
                document,
                hatch_mode,
                visible=item.visible,
                hidden_by_ancestor=item.hidden_by_ancestor,
            )
            decision = decide(facts, context)
            status, reason = decision.status, decision.reason
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
        model_hidden = sum(
            row.count for row in inventory if row.status is EntityStatus.NOT_VISIBLE
        )
        if model_hidden != fidelity_report.hidden_total:
            mismatches.append(
                f"entidades no visibles: inventario {model_hidden}, "
                f"informe {fidelity_report.hidden_total}"
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
        extracted_at=extracted_at,
        exporter_version=exporter_version,
        block_definition_handle=block_definition_handle,
        selection_method=selection_method,
        user_units_override=user_units_override,
        detail_size=detail_size,
        output_factor_mm_per_unit=factor_mm_per_drawing_unit,
        content_size_mm=content_size_mm,
        page_size_mm=page_size_mm,
        margin_mm=margin_mm,
        physical_scale_guaranteed=physical_scale_guaranteed,
        physical_scale_note=physical_scale_note,
        transformations=transformations,
        nested_blocks=nested_blocks,
        fonts=fonts,
        hatches=hatches,
        hidden_layers=hidden_layers,
        included_hidden_layers=included_hidden_layers,
        modelspace_hidden_overlap=modelspace_hidden_overlap,
        modelspace_selection=modelspace_selection,
    )
