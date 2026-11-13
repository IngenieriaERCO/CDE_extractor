"""Lista única de avisos de una extracción: la usan el informe TXT y el manifiesto JSON.

Cada aviso tiene código, severidad, categoría, mensaje y, si lo hay, un consejo. Se generan
en un solo sitio (``collect_notices``) a partir del modelo del detalle, para que el TXT y el
manifiesto cuenten siempre lo mismo. Solo usa la biblioteca estándar, como ``detail_model``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from cde_exporter.cad.detail_model import (
    SEARCHABLE_TEXT_CHECK,
    ExtractedDetail,
    Severity,
)


class NoticeSeverity(StrEnum):
    INFO = "info"  # informa; no cambia el estado final
    WARNING = "warning"  # se publica; el estado final pasa a "completa con advertencias"
    CRITICAL = "critical"  # bloquea: no se publica nada (código 1)


# Severidades que impiden publicar; el resto solo informa o avisa.
BLOCKING_SEVERITIES = frozenset({NoticeSeverity.CRITICAL})


class NoticeCategory(StrEnum):
    FONTS = "fonts"
    HATCH = "hatch"
    DEPENDENCIES = "dependencies"
    UNITS = "units"
    TEXT = "text"
    RENDERER = "renderer"
    SCALE = "scale"
    ENTITIES = "entities"  # geometría: sin caja, omitida, datos inválidos, huellas


@dataclass(frozen=True, slots=True)
class Notice:
    code: str
    severity: NoticeSeverity
    category: NoticeCategory
    message: str
    advice: str | None = None

    @property
    def text(self) -> str:
        """Mensaje y consejo, como se muestran en "A tener en cuenta"."""
        return f"{self.message} {self.advice}" if self.advice else self.message


_UNITS_ADVICE = "Confirme las unidades del dibujo y, si las conoce, indíquelas con --units."

# Categoría y consejo de cada código de aviso del modelo (``ExtractedDetail.warnings``).
WARNING_CODES: dict[str, tuple[NoticeCategory, str | None]] = {
    "UNITS_UNVERIFIED": (NoticeCategory.UNITS, _UNITS_ADVICE),
    "UNITS_SUSPECT": (NoticeCategory.UNITS, _UNITS_ADVICE),
    "GLYPHS_MISSING": (
        NoticeCategory.FONTS,
        "Indique con --font-substitutions una fuente que contenga esos caracteres.",
    ),
    "HATCH_DEGRADED": (
        NoticeCategory.HATCH,
        "Si necesita ver el patrón, revise su escala en el CAD o use --hatch-mode outline.",
    ),
    "DEPENDENCY_UNRESOLVED_XREF": (
        NoticeCategory.DEPENDENCIES,
        "Inserte la referencia externa en el dibujo (BIND en AutoCAD) o deje el archivo "
        "referenciado junto al DXF, y vuelva a exportar el DXF.",
    ),
    "DEPENDENCY_UNRESOLVED_IMAGE": (NoticeCategory.DEPENDENCIES, None),
    "DEPENDENCY_UNRESOLVED_UNDERLAY": (NoticeCategory.DEPENDENCIES, None),
    "DEPENDENCY_UNRESOLVED_BLOCK": (
        NoticeCategory.DEPENDENCIES,
        "El dibujo usa un bloque cuya definición no está en el DXF; revíselo en el CAD "
        "(p. ej. con AUDIT) y vuelva a exportar el DXF.",
    ),
    "TEXT_NOT_SEARCHABLE": (NoticeCategory.TEXT, None),
    "DRAWING_SCALE_MISMATCH": (NoticeCategory.SCALE, None),
    "ENTITIES_WITHOUT_BOUNDS": (
        NoticeCategory.ENTITIES,
        "Revise que no falte contenido en los bordes del PDF.",
    ),
    "UNSUPPORTED_OMITTED": (
        NoticeCategory.ENTITIES,
        "Revise en el CAD si esa geometría es necesaria en el PDF.",
    ),
    "ENTITIES_INVALID_DATA": (NoticeCategory.ENTITIES, None),
    "SOLIDS_AS_FOOTPRINT": (
        NoticeCategory.ENTITIES,
        "La huella muestra la planta del sólido, no su volumen ni su altura.",
    ),
    "SOLIDS_ACIS_OWN_READER": (
        NoticeCategory.ENTITIES,
        "Compare una muestra de huellas con el CAD.",
    ),
    "SOLIDS_NOT_CONVERTED": (NoticeCategory.ENTITIES, None),
    "HIDDEN_LAYERS_CONTENT": (
        NoticeCategory.ENTITIES,
        "Si debe salir en el PDF, enciéndala o descongélela en el CAD, o use "
        "--include-hidden-layers.",
    ),
    "HIDDEN_LAYERS_INCLUDED": (NoticeCategory.ENTITIES, None),
    "MODELSPACE_NOT_COPIED": (
        NoticeCategory.ENTITIES,
        "Revise en el CAD si esas entidades son necesarias en el PDF.",
    ),
    "MODELSPACE_ZERO_LENGTH": (NoticeCategory.ENTITIES, None),
    "MODELSPACE_CLIPPED": (NoticeCategory.ENTITIES, None),
    "MODELSPACE_NOT_CLIPPED": (
        NoticeCategory.ENTITIES,
        "Solo se recortan líneas y polilíneas de tramos rectos; use --no-clip para que todo "
        "entre entero.",
    ),
    "SOURCE_UNUSED_BLOCKS_SKIPPED": (NoticeCategory.ENTITIES, None),
    "MODELSPACE_WITHOUT_BOUNDS": (
        NoticeCategory.ENTITIES,
        "Para incluirlas, exporte sin --window o amplíe la zona en el CAD.",
    ),
    "MODELSPACE_HIDDEN_OVERLAP": (
        NoticeCategory.ENTITIES,
        "--include-hidden-layers no la incluye, porque no está en el bloque. Si debe salir, "
        "inclúyala en el bloque en el CAD.",
    ),
    "FIDELITY_RECONCILIATION": (NoticeCategory.ENTITIES, None),
}

# Códigos de los avisos que no vienen de ``ExtractedDetail.warnings``.
FONT_SUBSTITUTED = "FONT_SUBSTITUTED"
PHYSICAL_SCALE_NOT_GUARANTEED = "PHYSICAL_SCALE_NOT_GUARANTEED"
TEXT_AS_PATHS = "TEXT_AS_PATHS"
TEXT_LAYER = "TEXT_LAYER"
RENDERER_MESSAGE = "RENDERER_MESSAGE"

# Cómo se eligió cada fuente sustituta (``FontUse.origin``), en palabras.
FONT_ORIGINS = {
    "configurada": "sustitución configurada (JSON --font-substitutions)",
    "respaldo automático": "sustitución automática (fuente de respaldo)",
}


def _font_notice(requested: str, resolved: str, uses: int, origin: str | None) -> Notice:
    texts = "1 texto" if uses == 1 else f"{uses} textos"
    how = FONT_ORIGINS.get(origin or "")
    return Notice(
        FONT_SUBSTITUTED,
        # Una sustitución no pierde contenido: informa. Si a la sustituta le faltan
        # caracteres, el aviso GLYPHS_MISSING (warning) lo dice aparte.
        NoticeSeverity.INFO,
        NoticeCategory.FONTS,
        f"Fuente sustituida: {requested} se dibuja con {resolved} en {texts}"
        + (f" ({how})." if how else "."),
    )


def _text_notice(count: int, searchable: bool) -> Notice:
    entities = "1 entidad de texto" if count == 1 else f"{count} entidades de texto"
    if searchable:
        message = (
            f"El texto del PDF ({entities}: TEXT, MTEXT, ATTRIB y DIMENSION) está dibujado como "
            "trazados vectoriales y lleva encima una capa de texto invisible: se ve igual que en "
            "el CAD y se puede buscar, seleccionar y copiar. Al copiar, los saltos de línea "
            "siguen los cortes de línea del dibujo (no los párrafos del CAD), y la caja de "
            "búsqueda sigue la anchura de la fuente usada en el dibujo, que puede ser una "
            "sustituta."
        )
        return Notice(TEXT_LAYER, NoticeSeverity.INFO, NoticeCategory.TEXT, message)
    message = (
        f"El texto del PDF ({entities}: TEXT, MTEXT, ATTRIB y DIMENSION) está dibujado como "
        "trazados vectoriales: se ve igual que en el CAD, pero no se puede seleccionar, copiar "
        "ni buscar."
    )
    return Notice(TEXT_AS_PATHS, NoticeSeverity.INFO, NoticeCategory.TEXT, message)


def _severity(severity: Severity) -> NoticeSeverity:
    return NoticeSeverity(severity.value)


def collect_notices(detail: ExtractedDetail) -> tuple[Notice, ...]:
    """Todos los avisos del detalle, en el orden en que los muestra "A tener en cuenta".

    Orden: avisos del modelo, fuentes sustituidas, escala física no garantizada, nota del
    texto y mensajes del renderizador. La nota del texto y los mensajes del renderizador
    solo existen cuando el detalle tiene resultado (tras exportar y validar).
    """
    notices = []
    for warning in detail.warnings:
        category, advice = WARNING_CODES.get(warning.code, (NoticeCategory.ENTITIES, None))
        notices.append(
            Notice(warning.code, _severity(warning.severity), category, warning.message, advice)
        )
    for font in sorted(
        (font for font in detail.fonts if not font.found),
        key=lambda item: (item.requested, item.resolved),
    ):
        notices.append(_font_notice(font.requested, font.resolved, font.uses, font.origin))
    if detail.physical_scale_guaranteed is False and detail.physical_scale_note:
        notices.append(
            Notice(
                PHYSICAL_SCALE_NOT_GUARANTEED,
                NoticeSeverity.WARNING,
                NoticeCategory.SCALE,
                f"Escala física no garantizada: {detail.physical_scale_note}",
            )
        )
    result = detail.result
    if result is None:
        return tuple(notices)
    text_entities = sum(font.uses for font in detail.fonts)
    if text_entities:
        searchable = any(check.name == SEARCHABLE_TEXT_CHECK for check in result.validations)
        notices.append(_text_notice(text_entities, searchable))
    for item in result.warnings_after_export:
        notices.append(
            Notice(
                RENDERER_MESSAGE,
                NoticeSeverity.WARNING,
                NoticeCategory.RENDERER,
                f"Aviso del renderizador (ezdxf): {item}",
            )
        )
    return tuple(notices)


def blocking_notices(detail: ExtractedDetail) -> tuple[Notice, ...]:
    """Avisos que impiden publicar (severidad en ``BLOCKING_SEVERITIES``)."""
    return tuple(
        notice for notice in collect_notices(detail) if notice.severity in BLOCKING_SEVERITIES
    )


def blocking_message(notices: tuple[Notice, ...]) -> str:
    """Mensaje para la terminal: qué falta y qué hacer, sin publicar ningún archivo."""
    count = len(notices)
    head = (
        "La extracción se detuvo por 1 aviso crítico"
        if count == 1
        else f"La extracción se detuvo por {count} avisos críticos"
    )
    return f"{head}: " + " ".join(f"{notice.text}" for notice in notices) + (
        " No se generaron archivos."
    )
