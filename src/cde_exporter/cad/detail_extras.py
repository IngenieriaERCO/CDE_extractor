"""Funciones puras para completar los campos extra del modelo de detalle."""

from __future__ import annotations

from collections import Counter

import ezdxf
from ezdxf.document import Drawing

from cde_exporter.cad.detail_model import (
    FontUse,
    HatchDetail,
    HatchGroup,
    HatchSummary,
    NestedBlockInfo,
    SelectedReference,
    TransformationStep,
)
from cde_exporter.cad.fidelity import FidelityReport


def _reference_values(
    reference: object,
) -> tuple[tuple[float, ...], float, tuple[float, ...]]:
    """Lee inserción, rotación y escala de un SelectedReference o de un BlockReference."""
    if hasattr(reference, "scale_xyz"):
        insertion = reference.insertion_xyz
        rotation = reference.rotation_deg
        scale = reference.scale_xyz
    else:
        insertion = reference.insert
        rotation = reference.rotation
        scale = (reference.xscale, reference.yscale, reference.zscale)
    return (
        tuple(float(value) for value in insertion),
        float(rotation),
        tuple(float(value) for value in scale),
    )


def transformation_steps(reference: SelectedReference) -> tuple[TransformationStep, ...]:
    """Describe la inserción, rotación y escala de una referencia seleccionada.

    Acepta un SelectedReference o un BlockReference del flujo de extracción.
    """
    insertion, rotation, scale = _reference_values(reference)
    x, y, z = scale
    uniform = x == y == z
    scale_description = "Escala uniforme aplicada" if uniform else "Escala no uniforme aplicada"
    return (
        TransformationStep("insertion", "Punto de inserción (X, Y, Z)", insertion),
        TransformationStep("rotation", "Rotación aplicada (grados)", (rotation,)),
        TransformationStep("scale", scale_description, scale),
    )


def font_uses(report: FidelityReport) -> tuple[FontUse, ...]:
    """Resume exactamente los usos de fuente contabilizados por el informe."""
    totals: Counter[tuple[str, str, bool]] = Counter()
    for (_style, requested, resolved, found), uses in report.fonts.items():
        totals[(requested, resolved, found)] += uses
    return tuple(
        FontUse(requested, resolved, found, uses)
        for (requested, resolved, found), uses in sorted(totals.items())
    )


_SOLID_INSTEAD_OF_PATTERN = "relleno sólido en lugar del patrón"
_KEPT = "conservado"


def _hatch_groups(report: FidelityReport) -> tuple[HatchGroup, ...]:
    """Reparte los HATCH por (tipo, patrón, escala) según el tratamiento del modo de rayado."""
    mode = report.hatch_mode
    over_threshold = Counter(
        (pattern, scale) for _layer, pattern, scale, _estimate in report.hatch_simplifications
    )
    groups = []
    for (kind, pattern, scale), count in report.hatches.items():
        simplified = 0
        operation = reason = ""
        if mode == "auto" and kind == "patrón":
            simplified = min(count, over_threshold[(pattern, scale)])
            operation = _SOLID_INSTEAD_OF_PATTERN
            reason = f"estimación de tramos superior a --hatch-threshold {report.hatch_threshold}"
        elif mode == "solid" and kind != "sólido":
            simplified = count
            operation = f"relleno sólido en lugar del {kind}"
            reason = "--hatch-mode solid"
        elif mode == "outline":
            simplified = count
            operation = "solo contorno, sin relleno ni patrón"
            reason = "--hatch-mode outline"
        elif mode == "ignore":
            simplified = count
            operation = "no se dibuja"
            reason = "--hatch-mode ignore"
        for number, action, text, why in (
            (count - simplified, "conserved", _KEPT, None),
            (simplified, "simplified", operation, reason),
        ):
            if number:
                groups.append(
                    HatchGroup(kind, pattern, float(scale), number, action, text, why)
                )
    return tuple(groups)


def hatch_summary(report: FidelityReport) -> HatchSummary | None:
    """Resume los hatch presentes, su tratamiento y las simplificaciones registradas."""
    total = sum(report.hatches.values())
    if not total:
        return None
    details = tuple(
        HatchDetail(
            layer=layer,
            pattern=pattern,
            scale=float(scale),
            action="simplified",
            reason=f"Simplificado por estimación de {estimate} tramos.",
            operation=_SOLID_INSTEAD_OF_PATTERN,
        )
        for layer, pattern, scale, estimate in report.hatch_simplifications
    )
    return HatchSummary(
        total,
        report.simplified_hatches,
        details,
        groups=_hatch_groups(report),
        mode=report.hatch_mode,
    )


def nested_blocks(document: Drawing) -> tuple[NestedBlockInfo, ...]:
    """Cuenta los INSERT de bloques anidados en el Model Space del DXF extraído."""
    max_depth: dict[str, int] = {}
    reference_count: Counter[str] = Counter()

    def visit(block_name: str, depth: int, stack: tuple[str, ...]) -> None:
        if block_name in stack:
            return
        try:
            block = document.blocks[block_name]
        except (ezdxf.DXFError, KeyError):
            return
        next_stack = (*stack, block_name)
        for entity in block:
            if entity.dxftype() != "INSERT":
                continue
            name = str(entity.dxf.get("name", "") or "")
            if not name:
                continue
            child_depth = depth + 1
            max_depth[name] = max(max_depth.get(name, 0), child_depth)
            reference_count[name] += 1
            visit(name, child_depth, next_stack)

    # Comparte el criterio de descenso por INSERT y protección de ciclos con iter_detail_entities.
    for entity in document.modelspace():
        if entity.dxftype() == "INSERT":
            visit(str(entity.dxf.get("name", "") or ""), 0, ())

    return tuple(
        NestedBlockInfo(name, max_depth[name], reference_count[name])
        for name in sorted(max_depth)
    )
