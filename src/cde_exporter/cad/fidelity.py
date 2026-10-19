"""Informe de fidelidad de la exportación DXF con ezdxf drawing."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import ezdxf
from ezdxf.addons.drawing import Frontend, RenderContext, config, svg
from ezdxf.addons.drawing.config import HatchPolicy
from ezdxf.document import Drawing
from ezdxf.fonts import fonts
from ezdxf.protocols import SupportsVirtualEntities

from cde_exporter.cad.exporters import _estimate_hatch_segments
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.reader import read_dxf

_HATCH_POLICIES = {
    "normal": HatchPolicy.NORMAL,
    "outline": HatchPolicy.SHOW_OUTLINE,
    "solid": HatchPolicy.SHOW_SOLID,
    "ignore": HatchPolicy.IGNORE,
    "auto": HatchPolicy.NORMAL,
}
_TEXT_TYPES = {"TEXT", "MTEXT", "DIMENSION", "ATTRIB"}
_INLINE_FONT = re.compile(r"\\f([^|;]+)(?:\|[^;]*)?;", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FidelityReport:
    entity_counts: dict[tuple[str, str], int]
    unsupported_counts: dict[tuple[str, str, str], int]
    fonts: dict[tuple[str, str, str, bool], int]
    hatches: dict[tuple[str, str, str], int]
    hatch_mode: str
    simplified_hatches: int
    lineweight_policy: str
    hatch_simplifications: tuple[tuple[str, str, str, int], ...] = ()

    @property
    def entity_total(self) -> int:
        return sum(self.entity_counts.values())


def _entities_with_nested_blocks(document: Drawing):
    def visit(entity, block_stack: tuple[str, ...]):
        yield entity
        if entity.dxftype() == "INSERT":
            yield from entity.attribs
            name = entity.dxf.name
            if name in block_stack:
                return
            try:
                block = document.blocks[name]
            except ezdxf.DXFError:
                return
            for child in block:
                yield from visit(child, (*block_stack, name))

    for entity in document.modelspace():
        yield from visit(entity, ())


def _style_and_font(entity, document: Drawing) -> tuple[str, str, str, bool]:
    if entity.dxftype() == "DIMENSION":
        dimstyle = document.dimstyles.get(entity.dxf.get("dimstyle", "Standard"))
        style_name = str(dimstyle.dxf.get("dimtxsty", "Standard"))
    else:
        style_name = str(entity.dxf.get("style", "Standard") or "Standard")
    try:
        style = document.styles.get(style_name)
    except ezdxf.DXFError:
        return style_name, "(estilo no encontrado)", "(sin fuente)", False
    raw_name = str(style.dxf.get("font", "") or "")
    inline = (
        _INLINE_FONT.findall(str(entity.dxf.get("text", ""))) if entity.dxftype() == "MTEXT" else []
    )
    if inline:
        raw_name = inline[0]
    if not raw_name:
        try:
            xdata = entity.get_xdata("ACAD")
            raw_name = next((str(tag.value) for tag in xdata if isinstance(tag.value, str)), "")
        except (ezdxf.DXFError, AttributeError):
            pass
    if (
        raw_name
        and not fonts.is_shx_font_name(raw_name)
        and not raw_name.lower().endswith((".ttf", ".otf"))
    ):
        matched = fonts.find_best_match(family=raw_name)
        if matched is None:
            fallback = fonts.font_manager.get_font_face(fonts.font_manager.fallback_font_name())
            return style_name, raw_name, fallback.filename, False
        return style_name, raw_name, matched.filename, True
    if not raw_name:
        return style_name, "(fuente por defecto)", "(fuente por defecto)", True
    face = fonts.get_entity_font_face(entity, document)
    if face.family:
        matched_face = fonts.font_manager.find_best_match(face)
        available = matched_face is not None
        resolved = (
            matched_face.filename
            if matched_face is not None
            else fonts.font_manager.get_font_face(fonts.font_manager.fallback_font_name()).filename
        )
    else:
        candidate = fonts.map_shx_to_ttf(raw_name) if fonts.is_shx_font_name(raw_name) else raw_name
        available = fonts.font_manager.has_font(candidate)
        resolved = fonts.resolve_font_face(raw_name).filename
    return style_name, raw_name, resolved, available


def _hatch_type(entity) -> str:
    if entity.has_gradient_data:
        return "degradado"
    if entity.dxf.get("solid_fill", 0) or entity.dxf.get("pattern_name", "").upper() == "SOLID":
        return "sólido"
    return "patrón"


def inspect_fidelity(
    source: str | Path, hatch_mode: str = "normal", hatch_threshold: int = 50000
) -> FidelityReport:
    if hatch_mode not in _HATCH_POLICIES:
        raise CadProcessingError(f"Modo de rayado no válido: {hatch_mode}")
    document = read_dxf(source)
    # ezdxf 1.4.4 no expone un registro público de tipos dibujables. La tabla
    # _dispatch es la misma que UniversalFrontend usa en draw_entity().
    frontend = Frontend(RenderContext(document), svg.SVGBackend())
    dispatch = frontend._dispatch
    entities: Counter[tuple[str, str]] = Counter()
    unsupported: Counter[tuple[str, str, str]] = Counter()
    text_fonts: Counter[tuple[str, str, str, bool]] = Counter()
    hatches: Counter[tuple[str, str, str]] = Counter()
    simplified_rows = []

    for entity in _entities_with_nested_blocks(document):
        kind = entity.dxftype()
        layer = str(entity.dxf.get("layer", "0") or "0")
        entities[(kind, layer)] += 1
        proxy = bool(getattr(entity, "proxy_graphic", None))
        virtual = isinstance(entity, SupportsVirtualEntities)
        if kind == "IMAGE":
            unsupported[(kind, layer, "requiere archivo raster externo")] += 1
        elif kind in {"ACAD_PROXY_ENTITY", "MLEADER", "MULTILEADER"}:
            reason = "proxy gráfico disponible" if proxy else "proxy gráfico ausente"
            unsupported[(kind, layer, reason)] += 1
        elif kind not in dispatch and not virtual:
            unsupported[(kind, layer, "sin soporte de dibujo")] += 1
        if kind in _TEXT_TYPES:
            text_fonts[_style_and_font(entity, document)] += 1
        if kind == "HATCH":
            hatch_kind = _hatch_type(entity)
            pattern = str(entity.dxf.get("pattern_name", "SOLID") or "SOLID")
            scale = f"{float(entity.dxf.get('pattern_scale', 1.0)):g}"
            hatches[(hatch_kind, pattern, scale)] += 1
            estimate = _estimate_hatch_segments(entity) if hatch_mode == "auto" else 0
            if estimate > hatch_threshold:
                simplified_rows.append((layer, pattern, scale, estimate))

    hatch_total = sum(hatches.values())
    if hatch_mode in {"normal", "auto"}:
        simplified = 0
    elif hatch_mode == "solid":
        simplified = sum(count for (kind, _, _), count in hatches.items() if kind != "sólido")
    else:
        simplified = hatch_total
    return FidelityReport(
        entity_counts=dict(sorted(entities.items())),
        unsupported_counts=dict(sorted(unsupported.items())),
        fonts=dict(sorted(text_fonts.items())),
        hatches=dict(sorted(hatches.items())),
        hatch_mode=hatch_mode,
        lineweight_policy=config.Configuration().lineweight_policy.name,
        simplified_hatches=len(simplified_rows) if hatch_mode == "auto" else simplified,
        hatch_simplifications=tuple(simplified_rows),
    )


def format_fidelity_report(report: FidelityReport) -> str:
    lines = [
        "Informe de fidelidad del dibujo vectorial",
        f"Entidades contabilizadas (incluye bloques anidados): {report.entity_total}",
        "",
        "Conteo por tipo y capa:",
    ]
    lines.extend(
        f"  {kind} | {layer} | {count}" for (kind, layer), count in report.entity_counts.items()
    )
    if report.hatch_simplifications:
        lines.append(
            "HATCH simplificados en modo auto (capa | patrón | escala | tramos estimados):"
        )
        lines.extend(
            f"  {layer} | {pattern} | {scale} | {estimate}"
            for layer, pattern, scale, estimate in report.hatch_simplifications
        )
    lines.append("\nTipos no soportados o con soporte condicionado:")
    lines.extend(
        f"  {kind} | {layer} | {reason} | {count}"
        for (kind, layer, reason), count in report.unsupported_counts.items()
    )
    if not report.unsupported_counts:
        lines.append("  Ninguno detectado por la tabla de dibujo de ezdxf.")
    lines.append("\nTexto y fuentes:")
    lines.extend(
        f"  estilo {style} | fuente DXF {source} | resuelta {resolved} | "
        f"{'encontrada' if found else 'sustituta'} | {count} entidad(es)"
        for (style, source, resolved, found), count in report.fonts.items()
    )
    lines.append("\nHATCH:")
    lines.extend(
        f"  {kind} | patrón {pattern} | escala {scale} | {count}"
        for (kind, pattern, scale), count in report.hatches.items()
    )
    lines.extend(
        (
            f"Modo de rayado: {report.hatch_mode}",
            f"HATCH simplificados por el modo: {report.simplified_hatches}",
            f"Política de grosor de línea: {report.lineweight_policy}",
        )
    )
    return "\n".join(lines) + "\n"


def write_fidelity_report(
    source: str | Path,
    destination: str | Path,
    hatch_mode: str,
    *,
    overwrite: bool = False,
    hatch_threshold: int = 50000,
) -> FidelityReport:
    path = Path(destination)
    report = inspect_fidelity(source, hatch_mode, hatch_threshold)
    if path.exists() and not overwrite:
        raise CadProcessingError(f"El archivo de salida ya existe: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_fidelity_report(report), encoding="utf-8", newline="\n")
    return report


def print_fidelity_summary(report: FidelityReport, destination: str | Path) -> None:
    unsupported = sum(report.unsupported_counts.values())
    missing_fonts = sum(count for (_, _, _, found), count in report.fonts.items() if not found)
    print(
        f"Fidelidad: {report.entity_total} entidades; {unsupported} sin soporte o "
        f"condicionadas; {missing_fonts} usos de fuentes sustitutas; "
        f"{sum(report.hatches.values())} HATCH, {report.simplified_hatches} simplificados; "
        f"grosor {report.lineweight_policy}."
    )
    print(f"Informe de fidelidad: {destination}")
    if report.hatch_simplifications:
        for layer, pattern, scale, estimate in report.hatch_simplifications:
            print(
                f"HATCH simplificado: capa {layer}, patrón {pattern}, escala {scale}, "
                f"~{estimate} tramos."
            )
    missing = Counter()
    for (_style, family, _resolved, found), count in report.fonts.items():
        if not found:
            missing[family] += count
    if missing:
        print(
            "Fuentes no encontradas sin sustitución: "
            + ", ".join(f"{name} ({count})" for name, count in sorted(missing.items()))
        )
