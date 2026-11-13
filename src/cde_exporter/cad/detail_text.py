"""Generación determinista del informe de extracción y fidelidad en texto plano."""

from __future__ import annotations

import textwrap
from pathlib import PureWindowsPath

from cde_exporter.cad.detail_model import (
    ADVISORY_CHECKS,
    EntityStatus,
    ExtractedDetail,
    ResultStatus,
    SelectionMethod,
)
from cde_exporter.cad.manifest import MANIFEST_SUFFIX
from cde_exporter.cad.notices import FONT_ORIGINS, collect_notices

_WIDTH = 100
_STATUS_NAMES = {
    EntityStatus.CONSERVED: "conservadas",
    EntityStatus.TRANSFORMED: "transformadas",
    EntityStatus.SIMPLIFIED: "simplificadas",
    EntityStatus.OMITTED: "omitidas",
    EntityStatus.UNSUPPORTED: "no soportadas",
    EntityStatus.NOT_VISIBLE: "no visibles",
}
_SEVERITY_LABELS = {"info": "info", "warning": "advertencia", "critical": "CRÍTICA"}
_SELECTION_NAMES = {
    SelectionMethod.AUTOMATIC: "automática",
    SelectionMethod.OCCURRENCE: "por ocurrencia",
    SelectionMethod.HANDLE: "por handle",
    SelectionMethod.ALL: "todas",
}
_UNIT_NAMES = {
    "mm": "milímetros",
    "cm": "centímetros",
    "m": "metros",
    "in": "pulgadas",
    "ft": "pies",
}
def _number(value: float | int) -> str:
    return f"{value:.3f}"


def _point(values: tuple[float, ...]) -> str:
    return "(" + ", ".join(_number(value) for value in values) + ")"


def _line(label: str, value: str, indent: str = "") -> list[str]:
    """Devuelve las líneas de «etiqueta: valor»; sin etiqueta, solo el valor."""
    text = f"{label}: {value}" if label else value
    return textwrap.wrap(
        text,
        width=_WIDTH,
        initial_indent=indent,
        subsequent_indent=indent + "  ",
        break_long_words=True,
        break_on_hyphens=False,
    ) or [f"{indent}{label}:" if label else indent]


def _section(title: str) -> list[str]:
    return ["", title, "-" * len(title)]


def _path_name(path: str) -> str:
    normalized = path.replace("\\", "/")
    return normalized.rsplit("/", 1)[-1] or PureWindowsPath(path).name or "no disponible"


def _status_name(status: EntityStatus) -> str:
    return _STATUS_NAMES[status]


def _format_identification(detail: ExtractedDetail) -> list[str]:
    path = detail.source.path
    when = (detail.extracted_at or "no disponible").replace("T", " ", 1)
    lines = _section("A. IDENTIFICACIÓN DE LA EXTRACCIÓN")
    lines += _line("Archivo DXF de origen", _path_name(path))
    lines += _line("Ruta de origen", path or "no disponible")
    lines += _line("Fecha y hora", when)
    lines += _line("Versión del exportador", detail.exporter_version or "no disponible")
    return lines


def _format_block(detail: ExtractedDetail) -> list[str]:
    reference = detail.reference
    method = _SELECTION_NAMES.get(detail.selection_method, "no disponible")
    lines = _section("B. IDENTIFICACIÓN DEL BLOQUE")
    lines += _line("Nombre del bloque", detail.source.block_name or "no disponible")
    lines += _line("Handle de la definición", detail.block_definition_handle or "no disponible")
    occurrence = str(reference.occurrence)
    if reference.total_occurrences is not None:
        occurrence += f" de {reference.total_occurrences}"
    lines += _line("Ocurrencia", occurrence)
    lines += _line("Método de selección", method)
    lines += _line("Handle de la referencia", reference.handle or "no disponible")
    lines += _line("Capa de la referencia", reference.layer or "no disponible")
    selection = detail.modelspace_selection
    if selection is not None:
        lines += _line(
            "Origen",
            "Model Space (cad extract-modelspace): la selección se agrupa en un bloque de "
            "trabajo que solo existe en la copia",
        )
        lines += _line("Capas pedidas", ", ".join(selection.layers) or "todas")
        lines += _line("Capas excluidas", ", ".join(selection.exclude_layers) or "ninguna")
        if selection.window is None:
            zone = "todo el Model Space"
        else:
            zone = "(" + ", ".join(f"{value:.3f}" for value in selection.window) + ")"
            if selection.window_block:
                zone += f", caja del bloque «{selection.window_block}»"
        lines += _line("Zona (xmin, ymin, xmax, ymax)", zone)
        lines += _line("Entidades seleccionadas", str(selection.selected))
        lines += _line("No copiadas", str(selection.not_copied))
        lines += _line("De longitud nula (no se dibujan)", str(selection.zero_length))
        lines += _line("Sin caja (fuera de la zona)", str(selection.without_bounds))
        if selection.clipped:
            crossing = ", ".join(f"{kind} {count}" for kind, count in selection.not_clipped)
            lines += _line(
                "Recorte a la zona",
                f"{selection.clipped_entities} recortadas ({selection.clip_pieces} tramos), "
                f"{selection.outside_removed} quitadas por quedar fuera, sin recortar (enteras): "
                f"{crossing or 'ninguna'}",
            )
        elif selection.window is not None:
            lines += _line("Recorte a la zona", "no (--no-clip): las entidades entran enteras")
    return lines


def _format_units_scale(detail: ExtractedDetail) -> list[str]:
    units = detail.units
    declared = _UNIT_NAMES.get(units.name, units.name) or "no disponible"
    code = "no disponible" if units.declared_code is None else str(units.declared_code)
    lines = _section("C. UNIDADES Y ESCALA")
    lines += _line("Unidades declaradas", f"{declared} (código {code})")
    lines += _line("Estado de verificación", units.status or "no disponible")
    lines += _line(
        "Unidades indicadas por el usuario", detail.user_units_override or "no se indicaron"
    )
    if detail.scale is None:
        nominal = factor = "no aplica"
    else:
        nominal = f"1:{detail.scale.nominal_denominator:.10g}"
        factor_value = detail.scale.factor_mm_per_drawing_unit
        factor = (
            "no disponible" if factor_value is None else f"{_number(factor_value)} mm/unidad CAD"
        )
    lines += _line("Escala nominal", nominal)
    lines += _line("Factor aplicado", factor)
    if detail.detail_size is None:
        detail_size = "no disponible"
    else:
        detail_size = (
            f"{_number(detail.detail_size[0])} × {_number(detail.detail_size[1])} unidades CAD"
        )
    lines += _line("Tamaño del detalle (ancho × alto)", detail_size)
    if detail.bounds is None:
        lines += _line("Caja geométrica (mínimo a máximo)", "no disponible")
    else:
        lines += _line("Caja geométrica, mínimo", _point(detail.bounds.min_xyz))
        lines += _line("Caja geométrica, máximo", _point(detail.bounds.max_xyz))
    content = (
        "no disponible"
        if detail.content_size_mm is None
        else f"{_number(detail.content_size_mm[0])} × {_number(detail.content_size_mm[1])} mm"
    )
    page = (
        "no disponible"
        if detail.page_size_mm is None
        else f"{_number(detail.page_size_mm[0])} × {_number(detail.page_size_mm[1])} mm"
    )
    margin = "no disponible" if detail.margin_mm is None else f"{_number(detail.margin_mm)} mm"
    lines += _line("Tamaño del contenido en PDF", content)
    lines += _line("Tamaño de página PDF", page)
    lines += _line("Margen", margin)
    if detail.physical_scale_guaranteed is True:
        physical = "garantizada"
    elif detail.physical_scale_guaranteed is False:
        physical = "NO GARANTIZADA"
        if detail.physical_scale_note:
            physical += f"; motivo: {detail.physical_scale_note}"
    else:
        physical = "no disponible"
    lines += _line("Escala física", physical)
    return lines


def _format_transformations(detail: ExtractedDetail) -> list[str]:
    lines = _section("D. TRANSFORMACIONES APLICADAS")
    if detail.transformations:
        for step in detail.transformations:
            if step.kind == "rotation" and step.values:
                values = f"{_number(step.values[0])}°"
            elif step.values:
                values = _point(step.values)
            else:
                values = "no disponible"
            lines += _line(step.description or step.kind, values)
    else:
        lines += _line("Transformaciones", "no disponible")

    nested = detail.nested_blocks
    lines += _line("Bloques anidados", str(len(nested)))
    max_depth = max((block.max_depth for block in nested), default=0)
    lines += _line("Profundidad máxima", str(max_depth) if nested else "no aplica")
    if not nested:
        lines += _line("Detalle de bloques anidados", "no hay bloques anidados")
        return lines

    normal = sorted(
        (block for block in nested if not block.name.startswith("*")), key=lambda b: b.name
    )
    anonymous = sorted(
        (block for block in nested if block.name.startswith("*")), key=lambda b: b.name
    )
    for block in normal[:10]:
        lines += _line(
            "Bloque anidado",
            f"{block.name}; profundidad {block.max_depth}; referencias {block.reference_count}",
        )
    if len(normal) > 10:
        lines += _line("Bloques normales omitidos", str(len(normal) - 10))
    if anonymous:
        names = ", ".join(block.name for block in anonymous)
        lines += _line("Bloques anónimos", f"{len(anonymous)} ({names})")
    return lines


def _format_inventory(detail: ExtractedDetail) -> list[str]:
    totals = {status: 0 for status in EntityStatus}
    for row in detail.inventory:
        totals[row.status] += row.count
    lines = _section("E. ENTIDADES CAD PROCESADAS")
    lines += _line("Total de entidades", str(sum(totals.values())))
    for status in EntityStatus:
        if status is EntityStatus.NOT_VISIBLE and not totals[status]:
            continue  # solo se informa si hay alguna
        value = str(totals[status])
        if status is EntityStatus.OMITTED and totals[status]:
            value += "; omitidas por --omit-unsupported (ezdxf no puede dibujarlas)"
        elif status is EntityStatus.OMITTED:
            value += (
                "; toda omisión del renderizador detiene la extracción, "
                "salvo entidades no visibles"
            )
        lines += _line(_STATUS_NAMES[status].capitalize(), value)
    lines += ["Tipo | Capa | Estado | Cantidad"]
    for row in sorted(
        detail.inventory, key=lambda item: (item.entity_type, item.layer, item.status.value)
    ):
        lines += _line(
            "",
            f"{row.entity_type} | {row.layer} | {_status_name(row.status)} | {row.count}",
            indent="  ",
        )
    if not detail.inventory:
        lines += _line("Inventario", "no disponible")
    return lines


def _format_fidelity(detail: ExtractedDetail) -> list[str]:
    lines = _section("F. FIDELIDAD GRÁFICA E INCIDENCIAS")
    substituted = [font for font in detail.fonts if not font.found]
    if substituted:
        for font in sorted(substituted, key=lambda item: (item.requested, item.resolved)):
            origin = FONT_ORIGINS.get(font.origin or "")
            lines += _line(
                "Fuente sustituida",
                f"{font.requested} → {font.resolved}; usos {font.uses}; no se encontró"
                + (f"; {origin}" if origin else ""),
            )
        total_uses = sum(font.uses for font in substituted)
        lines += _line("Total de usos de fuentes sustituidas", str(total_uses))
    else:
        lines += _line("Fuentes sustituidas", "no hay")
    if detail.hatches is None:
        lines += _line("HATCH", "no disponible")
    else:
        lines += _line("HATCH total", str(detail.hatches.total))
        lines += _line("HATCH simplificados", str(detail.hatches.simplified))
        if detail.hatches.mode:
            lines += _line("Modo de rayado", detail.hatches.mode)
        if detail.hatches.groups:
            lines += ["Tratamiento de los HATCH (tipo | patrón | escala | tratamiento | cantidad)"]
            for group in detail.hatches.groups:
                scale = "no disponible" if group.scale is None else f"{group.scale:g}"
                treatment = group.operation
                if group.reason:
                    treatment += f"; motivo: {group.reason}"
                lines += _line(
                    "",
                    f"{group.kind} | {group.pattern} | {scale} | {treatment} | {group.count}",
                    indent="  ",
                )
        if detail.hatches.details:
            for hatch in detail.hatches.details:
                scale = "no disponible" if hatch.scale is None else _number(hatch.scale)
                reason = hatch.reason or "no disponible"
                operation = f"; operación: {hatch.operation}" if hatch.operation else ""
                lines += _line(
                    "Simplificación HATCH",
                    f"capa {hatch.layer}, patrón {hatch.pattern}, escala {scale}"
                    f"{operation}; motivo: {reason}",
                )
        elif detail.hatches.simplified and not detail.hatches.groups:
            lines += _line("Motivo de simplificación HATCH", "no disponible")

    groups: dict[tuple[str, str, str, str], int] = {}
    for item in detail.dispositions:
        if item.status is EntityStatus.CONSERVED:
            continue
        key = (
            item.entity_type,
            item.layer,
            _status_name(item.status),
            item.reason or "no disponible",
        )
        groups[key] = groups.get(key, 0) + 1
    lines += ["Entidades no conservadas (tipo | capa | estado | motivo | cantidad)"]
    ordered_groups = sorted(groups.items())
    for key, count in ordered_groups[:30]:
        entity_type, layer, status, reason = key
        lines += _line("", f"{entity_type} | {layer} | {status} | {reason} | {count}", indent="  ")
    if len(ordered_groups) > 30:
        lines += _line("", f"… y {len(ordered_groups) - 30} grupos más", indent="  ")
    elif not ordered_groups:
        lines += _line("", "no hay entidades no conservadas", indent="  ")

    if detail.dependencies:
        lines += ["Dependencias externas"]
        for dependency in sorted(detail.dependencies, key=lambda item: (item.kind, item.reference)):
            resolved = "resuelta" if dependency.resolved else "no resuelta"
            lines += _line(
                "", f"{dependency.kind} | {dependency.reference} | {resolved}", indent="  "
            )
    else:
        lines += _line("Dependencias externas", "no hay")
    if detail.warnings:
        lines += ["Advertencias del modelo"]
        for warning in detail.warnings:
            severity = _SEVERITY_LABELS.get(warning.severity.value, "advertencia")
            lines += _line("", f"{warning.code} ({severity}): {warning.message}", indent="  ")
    else:
        lines += _line("Advertencias del modelo", "no hay")
    return lines


def _format_result(detail: ExtractedDetail) -> list[str]:
    lines = _section("G. RESULTADO")
    result = detail.result
    if result is None:
        lines += _line("Resultado", "Resultado pendiente: el informe se generó antes de exportar")
        return lines
    status = "COMPLETA" if result.status is ResultStatus.COMPLETE else "COMPLETA CON ADVERTENCIAS"
    lines += _line("PDF", result.pdf_name or "no disponible")
    if result.pdf_name:
        stem = result.pdf_name.removesuffix(".pdf")
        lines += _line("Manifiesto", f"{stem}{MANIFEST_SUFFIX}")
    lines += _line("Estado final", status)
    for check in result.validations:
        if check.passed:
            marker = "[OK]"
        else:
            marker = "[AVISO]" if check.name in ADVISORY_CHECKS else "[FALLÓ]"
        lines += _line("", f"{marker} {check.name}: {check.detail or 'no disponible'}")
    lines += ["A tener en cuenta"]
    # La misma lista de avisos que el manifiesto (notices.collect_notices).
    notes = [notice.text for notice in collect_notices(detail)]
    if notes:
        for note in notes:
            lines += _line("", f"- {note}", indent="  ")
    else:
        lines += _line("", "no hay advertencias", indent="  ")
    return lines


def format_detail_report(detail: ExtractedDetail) -> str:
    """Devuelve el informe completo de extracción sin realizar operaciones de E/S."""
    title = "INFORME DE EXTRACCIÓN Y FIDELIDAD"
    lines = ["=" * len(title), title, "=" * len(title)]
    lines += _format_identification(detail)
    lines += _format_block(detail)
    lines += _format_units_scale(detail)
    lines += _format_transformations(detail)
    lines += _format_inventory(detail)
    lines += _format_fidelity(detail)
    lines += _format_result(detail)
    return "\n".join(lines) + "\n"
