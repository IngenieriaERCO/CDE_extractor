"""Informe de fidelidad de la exportación DXF con ezdxf drawing."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import ezdxf
from ezdxf import bbox
from ezdxf.addons.drawing import Frontend, RenderContext, config, svg
from ezdxf.addons.drawing.config import HatchPolicy
from ezdxf.addons.drawing.properties import Properties
from ezdxf.document import Drawing
from ezdxf.fonts import fonts
from ezdxf.protocols import SupportsVirtualEntities, virtual_entities
from ezdxf.tools.text import fast_plain_mtext

from cde_exporter.cad.detail_model import EntityStatus
from cde_exporter.cad.entity_policy import (
    GEOMETRY_FROM_DATA_TYPES,
    PROBED_COMPOSITES,
    TEXT_TYPES,
    EntityFacts,
    PolicyContext,
    decide,
)
from cde_exporter.cad.exporters import _estimate_hatch_segments
from cde_exporter.cad.font_config import (
    active_substitutions,
    font_is_installed,
    is_installed_font,
    is_shx_request,
)
from cde_exporter.cad.models import CadProcessingError, invalid_data_reason
from cde_exporter.cad.reader import read_dxf

_HATCH_POLICIES = {
    "normal": HatchPolicy.NORMAL,
    "outline": HatchPolicy.SHOW_OUTLINE,
    "solid": HatchPolicy.SHOW_SOLID,
    "ignore": HatchPolicy.IGNORE,
    "auto": HatchPolicy.NORMAL,
}
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
    hatch_threshold: int = 50000
    # Origen de cada sustitución de fuente: familia pedida (en minúsculas) → "configurada" o
    # "respaldo automático".
    font_origins: dict[str, str] = field(default_factory=dict)
    # Caracteres que la fuente sustituta no tiene: (archivo de fuente, carácter) → textos.
    missing_glyphs: dict[tuple[str, str], int] = field(default_factory=dict)
    # Entidades contabilizadas que no se dibujan por no ser visibles en el CAD.
    hidden_total: int = 0

    @property
    def entity_total(self) -> int:
        return sum(self.entity_counts.values())


def iter_detail_entities(document: Drawing):
    """Recorre entidades del detalle, incluidos INSERT anidados y sus ATTRIB."""
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


_entities_with_nested_blocks = iter_detail_entities


@dataclass(frozen=True, slots=True)
class EntityVisibility:
    entity: object
    visible: bool
    # No se ve porque un INSERT que la contiene está marcado como invisible.
    hidden_by_ancestor: bool = False
    # Veces que ezdxf la recorre (un INSERT en matriz repite su contenido en cada celda).
    weight: int = 1


def iter_detail_entities_with_visibility(document: Drawing):
    """Como ``iter_detail_entities``, indicando si ezdxf dibujaría cada entidad.

    Aplica las reglas del exportador con el mismo ``RenderContext``: un INSERT solo depende de
    su indicador de invisible (el estado de su capa no cuenta); el resto, de su capa
    (apagada, congelada), de su indicador de invisible y, en la capa "0" de un bloque, de la
    capa del INSERT que lo contiene. El contenido de un INSERT invisible no se recorre al
    dibujar, pero aquí se sigue contando (marcado ``hidden_by_ancestor``).
    """
    ctx = RenderContext(document)

    def visit(entity, block_stack: tuple[str, ...], hidden_above: bool, weight: int):
        visible = not hidden_above and ctx.resolve_visible(entity)
        yield EntityVisibility(entity, visible, hidden_above, weight)
        if entity.dxftype() != "INSERT":
            return
        pushed = False
        if visible:
            state = Properties()
            state.layer = ctx.resolve_layer(entity)
            ctx.push_state(state)
            pushed = True
        try:
            hidden = hidden_above or not visible
            inner_weight = weight * max(1, entity.mcount) if visible else weight
            for attrib in entity.attribs:
                yield from visit(attrib, block_stack, hidden, inner_weight)
            name = entity.dxf.name
            if name in block_stack:
                return
            try:
                block = document.blocks[name]
            except ezdxf.DXFError:
                return
            for child in block:
                yield from visit(child, (*block_stack, name), hidden, inner_weight)
        finally:
            if pushed:
                ctx.pop_state()

    for entity in document.modelspace():
        yield from visit(entity, (), False, 1)


def hidden_skip_events(document: Drawing) -> int:
    """Entidades que ezdxf descarta una a una por no ser visibles al dibujar el documento.

    Las que están dentro de un INSERT invisible no cuentan: ezdxf no llega a recorrerlas.
    """
    return sum(
        item.weight
        for item in iter_detail_entities_with_visibility(document)
        if not item.visible and not item.hidden_by_ancestor
    )


def fidelity_dispatch(document: Drawing):
    """Devuelve la tabla de tipos que el Frontend de fidelidad puede dibujar."""
    return Frontend(RenderContext(document), svg.SVGBackend())._dispatch


def entity_facts(
    entity,
    dispatch,
    document: Drawing,
    hatch_mode: str = "normal",
    *,
    visible: bool = True,
    hidden_by_ancestor: bool = False,
) -> EntityFacts:
    """Reúne con ezdxf lo observado de una entidad; ``entity_policy.decide`` decide qué hacer.

    Las entidades compuestas de ``PROBED_COMPOSITES`` se miden y los MULTILEADER se
    descomponen: si ezdxf no puede generar su geometría son datos inválidos. Solo esos tipos,
    para no recorrer la geometría de todas las entidades.
    """
    kind = entity.dxftype()
    if not visible:
        return EntityFacts(kind, visible=False, hidden_by_ancestor=hidden_by_ancestor)
    drawable = kind in dispatch or isinstance(entity, SupportsVirtualEntities)
    invalid_error = None
    has_geometry = True
    try:
        if kind in PROBED_COMPOSITES and drawable:
            bbox.extents([entity])
        elif kind in GEOMETRY_FROM_DATA_TYPES:
            has_geometry = isinstance(entity, SupportsVirtualEntities) and any(
                True for _ in virtual_entities(entity)
            )
    except Exception as error:  # noqa: BLE001 - ezdxf puede lanzar cualquier error
        invalid_error = invalid_data_reason(error)
    hatch_kind = None
    estimate = 0
    if kind == "HATCH":
        hatch_kind = _hatch_type(entity)
        estimate = _estimate_hatch_segments(entity) if hatch_mode == "auto" else 0
    # Con datos inválidos tampoco se leen sus fuentes.
    font = (
        _style_and_font(entity, document)
        if kind in TEXT_TYPES and invalid_error is None
        else None
    )
    return EntityFacts(
        kind,
        drawable=drawable,
        has_proxy=bool(getattr(entity, "proxy_graphic", None)),
        has_geometry=has_geometry,
        invalid_error=invalid_error,
        hatch_kind=hatch_kind,
        hatch_estimate=estimate,
        font=font,
    )


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
    configured = active_substitutions().get(raw_name.casefold()) if raw_name else None
    if configured:
        return style_name, raw_name, configured[1], False
    is_file_name = raw_name.lower().endswith((".ttf", ".otf"))
    if raw_name and not is_shx_request(raw_name) and not is_file_name:
        matched = fonts.find_best_match(family=raw_name)
        if not is_installed_font(raw_name, matched):
            fallback = fonts.font_manager.get_font_face(fonts.font_manager.fallback_font_name())
            return style_name, raw_name, fallback.filename, False
        return style_name, raw_name, matched.filename, True
    if not raw_name:
        return style_name, "(fuente por defecto)", "(fuente por defecto)", True
    face = fonts.get_entity_font_face(entity, document)
    if is_file_name:
        # Archivo TrueType pedido por nombre: ezdxf lo resuelve por ese nombre.
        if font_is_installed(raw_name):
            return style_name, raw_name, face.filename, True
        fallback = fonts.font_manager.get_font_face(fonts.font_manager.fallback_font_name())
        return style_name, raw_name, fallback.filename, False
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


def _text_content(entity) -> str:
    """Texto visible de la entidad, sin códigos de formato; vacío si no se puede leer."""
    try:
        if entity.dxftype() == "DIMENSION":
            override = str(entity.dxf.get("text", "") or "").replace("<>", "")
            return fast_plain_mtext(override) if override.strip() else ""
        return entity.plain_text()
    except Exception:  # noqa: BLE001 - un texto con datos inválidos no detiene el informe
        return ""


class _GlyphChecker:
    """Busca los caracteres de un texto que la fuente sustituta no tiene."""

    def __init__(self) -> None:
        self.missing: Counter[tuple[str, str]] = Counter()
        self._codepoints: dict[str, frozenset[int] | None] = {}
        self._by_text: dict[tuple[str, str], frozenset[str]] = {}

    def _font_codepoints(self, name: str) -> frozenset[int] | None:
        if name not in self._codepoints:
            codepoints = None
            if fonts.font_manager.has_font(name):
                try:
                    codepoints = frozenset(fonts.font_manager.get_ttf_font(name).getBestCmap())
                except Exception:  # noqa: BLE001 - fuente ilegible: no se puede comprobar
                    pass
            self._codepoints[name] = codepoints
        return self._codepoints[name]

    def check(self, entity, resolved_font: str) -> None:
        name = Path(str(resolved_font)).name
        if not name.casefold().endswith((".ttf", ".otf", ".ttc")):
            return
        codepoints = self._font_codepoints(name)
        if codepoints is None:
            return
        text = _text_content(entity)
        key = (name, text)
        if key not in self._by_text:
            self._by_text[key] = frozenset(
                char
                for char in text
                if ord(char) >= 32 and not char.isspace() and ord(char) not in codepoints
            )
        for char in self._by_text[key]:
            self.missing[(name, char)] += 1


def missing_glyphs_message(missing: dict[tuple[str, str], int]) -> str:
    """Aviso de los caracteres que la fuente sustituta no tiene (agrupado por fuente)."""
    by_font: dict[str, list[tuple[str, int]]] = {}
    for (font, char), uses in sorted(missing.items()):
        by_font.setdefault(font, []).append((char, uses))
    parts = []
    for font, items in by_font.items():
        shown = ", ".join(
            f"«{char}» U+{ord(char):04X} ({uses} {'texto' if uses == 1 else 'textos'})"
            for char, uses in items[:8]
        )
        if len(items) > 8:
            shown += f", … y {len(items) - 8} más"
        noun = "carácter" if len(items) == 1 else "caracteres"
        parts.append(f"{font}: {len(items)} {noun} sin glifo ({shown})")
    return (
        "La fuente sustituta no tiene todos los caracteres del texto; pueden salir vacíos o "
        f"como un recuadro en el PDF. {'; '.join(parts)}."
    )


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
    dispatch = fidelity_dispatch(document)
    entities: Counter[tuple[str, str]] = Counter()
    unsupported: Counter[tuple[str, str, str]] = Counter()
    text_fonts: Counter[tuple[str, str, str, bool]] = Counter()
    hatches: Counter[tuple[str, str, str]] = Counter()
    simplified_rows = []
    glyphs = _GlyphChecker()
    context = PolicyContext(hatch_mode, hatch_threshold)

    hidden_total = 0
    for item in iter_detail_entities_with_visibility(document):
        entity = item.entity
        facts = entity_facts(
            entity,
            dispatch,
            document,
            hatch_mode,
            visible=item.visible,
            hidden_by_ancestor=item.hidden_by_ancestor,
        )
        decision = decide(facts, context)
        kind = facts.kind
        layer = str(entity.dxf.get("layer", "0") or "0")
        entities[(kind, layer)] += 1
        if decision.status is EntityStatus.NOT_VISIBLE:
            hidden_total += 1
        if decision.status is EntityStatus.UNSUPPORTED:
            unsupported[(kind, layer, decision.cause)] += 1
        if facts.font is not None:
            text_fonts[facts.font] += 1
            if not facts.font[3]:  # fuente sustituta: ¿tiene todos los caracteres del texto?
                glyphs.check(entity, facts.font[2])
        if facts.hatch_kind is not None:
            pattern = str(entity.dxf.get("pattern_name", "SOLID") or "SOLID")
            scale = f"{float(entity.dxf.get('pattern_scale', 1.0)):g}"
            hatches[(facts.hatch_kind, pattern, scale)] += 1
            if facts.hatch_estimate > hatch_threshold:
                simplified_rows.append((layer, pattern, scale, facts.hatch_estimate))

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
        hatch_threshold=hatch_threshold,
        font_origins={key: item[2] for key, item in active_substitutions().items()},
        missing_glyphs=dict(sorted(glyphs.missing.items())),
        hidden_total=hidden_total,
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
    substitutions = active_substitutions()
    if substitutions:
        lines.append("Sustituciones aplicadas (familia | fuente usada | origen | textos):")
        lines.extend(
            f"  {family} | {filename} | {origin} | {count}"
            for family, filename, origin, count in substitutions.values()
        )
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
            "Fuentes que requieren sustitución: "
            + ", ".join(f"{name} ({count})" for name, count in sorted(missing.items()))
        )
    if report.missing_glyphs:
        print(f"Aviso: {missing_glyphs_message(report.missing_glyphs)}")
