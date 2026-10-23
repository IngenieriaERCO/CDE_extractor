"""Funciones puras para completar los campos extra del modelo de detalle."""

from __future__ import annotations

from collections import Counter

import ezdxf
from ezdxf.document import Drawing

from cde_exporter.cad.detail_model import (
    FontUse,
    HatchDetail,
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


def hatch_summary(report: FidelityReport) -> HatchSummary | None:
    """Resume los hatch presentes y las simplificaciones registradas."""
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
        )
        for layer, pattern, scale, estimate in report.hatch_simplifications
    )
    return HatchSummary(total, report.simplified_hatches, details)


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
