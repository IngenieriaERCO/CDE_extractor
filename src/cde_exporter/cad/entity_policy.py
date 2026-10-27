"""Política de representación por tipo de entidad (solo biblioteca estándar).

Una sola tabla decide qué se hace con cada entidad del detalle y por qué: conservarla,
transformarla (fuente sustituida), simplificarla (HATCH), no dibujarla porque no se ve en el CAD
o declararla no soportada. Quien conoce ezdxf (``fidelity.py``) solo reúne los *hechos* de cada
entidad (``EntityFacts``); ``decide`` los convierte en un ``Decision``. El inventario, el informe
de fidelidad y las pistas de error usan estas mismas reglas.

Pendiente: REGION es "no soportada" (ezdxf no la dibuja); se podría dibujar su contorno con
``ezdxf.acis``, como se hace con los 3DSOLID (``--solids footprint``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from cde_exporter.cad.detail_model import EntityStatus

# Motivo con el que se trata como no soportada una entidad cuyo procesamiento falla.
INVALID_DATA = "datos inválidos"

TEXT_TYPES = frozenset({"TEXT", "MTEXT", "DIMENSION", "ATTRIB"})

# Entidades compuestas cuya geometría ezdxf genera a partir de estilos y overrides: si no puede
# generarla, son no soportadas por datos inválidos.
PROBED_COMPOSITES = frozenset(
    {"LEADER", "DIMENSION", "ARC_DIMENSION", "LARGE_RADIAL_DIMENSION", "MLINE", "ACAD_TABLE"}
)

# ezdxf las dibuja con sus propios datos (no necesitan gráfico proxy): se conservan si generan
# geometría.
GEOMETRY_FROM_DATA_TYPES = frozenset({"MLEADER", "MULTILEADER"})

UNSUPPORTED_DRAWING = "sin soporte de dibujo"
NO_GEOMETRY = "sin geometría dibujable"
HIDDEN_OWN = "No visible en el CAD: capa apagada o congelada, o entidad marcada como invisible."
HIDDEN_BY_ANCESTOR = "No visible en el CAD: está dentro de un INSERT marcado como invisible."

HINT_OMIT_UNSUPPORTED = (
    ". Puede usar --omit-unsupported en cad extract-block para continuar sin ellas."
)
HINT_SOLIDS_FOOTPRINT = (
    ". Puede usar en cad extract-block --solids footprint (dibujar los 3DSOLID como huella 2D) "
    "o --omit-unsupported (omitirlos)."
)


@dataclass(frozen=True, slots=True)
class TypePolicy:
    """Tratamiento propio de un tipo, independiente de lo que ezdxf sepa dibujar."""

    status: EntityStatus
    cause: str | None


# Tipos con tratamiento propio. El resto se decide por lo que ezdxf puede dibujar
# (``EntityFacts.drawable``) y por las reglas de HATCH y de texto de ``decide``.
TYPE_POLICY: dict[str, TypePolicy] = {
    "IMAGE": TypePolicy(EntityStatus.UNSUPPORTED, "requiere archivo raster externo"),
    "ACAD_PROXY_ENTITY": TypePolicy(EntityStatus.UNSUPPORTED, None),  # depende del proxy
    "MLEADER": TypePolicy(EntityStatus.CONSERVED, None),
    "MULTILEADER": TypePolicy(EntityStatus.CONSERVED, None),
}


@dataclass(frozen=True, slots=True)
class EntityFacts:
    """Lo observado de una entidad; ``fidelity.entity_facts`` lo reúne con ezdxf."""

    kind: str
    visible: bool = True
    hidden_by_ancestor: bool = False
    drawable: bool = True  # ezdxf sabe dibujar este tipo
    has_proxy: bool = False
    has_geometry: bool = True  # solo se consulta en GEOMETRY_FROM_DATA_TYPES
    invalid_error: str | None = None  # "datos inválidos: ..." si ezdxf no puede leerla
    hatch_kind: str | None = None  # "sólido", "patrón" o "degradado"
    hatch_estimate: int = 0  # tramos estimados (solo en modo auto)
    # (estilo, fuente pedida, fuente resuelta, encontrada) de los textos
    font: tuple[str, str, str, bool] | None = None


@dataclass(frozen=True, slots=True)
class PolicyContext:
    hatch_mode: str = "normal"
    hatch_threshold: int = 50000


@dataclass(frozen=True, slots=True)
class Decision:
    status: EntityStatus
    reason: str | None = None  # texto para el inventario (sección F del TXT)
    cause: str | None = None  # causa corta de una no soportada (informe de fidelidad)


def hatch_simplification_reason(
    hatch_kind: str, estimate: int, hatch_mode: str, hatch_threshold: int
) -> str | None:
    """Motivo por el que el modo de rayado simplifica este HATCH, o None si se conserva."""
    if hatch_mode == "auto":
        if estimate > hatch_threshold:
            return (
                f"HATCH simplificado por --hatch-threshold {hatch_threshold}; "
                f"estimación de {estimate} tramos"
            )
        return None
    if hatch_mode == "solid" and hatch_kind != "sólido":
        return "HATCH convertido a relleno sólido por --hatch-mode solid"
    if hatch_mode == "outline":
        return "HATCH mostrado solo como contorno por --hatch-mode outline"
    if hatch_mode == "ignore":
        return "HATCH omitido deliberadamente por --hatch-mode ignore"
    return None


def _unsupported(cause: str) -> Decision:
    reason = (
        f"{cause[0].upper()}{cause[1:]}."
        if cause.startswith(INVALID_DATA)
        else f"Tipo no soportado o condicionado: {cause}."
    )
    return Decision(EntityStatus.UNSUPPORTED, reason, cause)


def decide(facts: EntityFacts, context: PolicyContext | None = None) -> Decision:
    """Tratamiento de una entidad: estado, motivo y causa. Es la única fuente de estas reglas."""
    context = context or PolicyContext()
    kind = facts.kind
    if not facts.visible:
        return Decision(
            EntityStatus.NOT_VISIBLE, HIDDEN_BY_ANCESTOR if facts.hidden_by_ancestor else HIDDEN_OWN
        )
    if facts.invalid_error:
        return _unsupported(facts.invalid_error)
    policy = TYPE_POLICY.get(kind)
    if policy is not None and policy.status is EntityStatus.UNSUPPORTED:
        if policy.cause is not None:
            return _unsupported(policy.cause)
        return _unsupported(
            "proxy gráfico disponible" if facts.has_proxy else "proxy gráfico ausente"
        )
    if kind in GEOMETRY_FROM_DATA_TYPES:
        return Decision(EntityStatus.CONSERVED) if facts.has_geometry else _unsupported(NO_GEOMETRY)
    if not facts.drawable:
        return _unsupported(UNSUPPORTED_DRAWING)
    if kind == "HATCH" and facts.hatch_kind is not None:
        reason = hatch_simplification_reason(
            facts.hatch_kind, facts.hatch_estimate, context.hatch_mode, context.hatch_threshold
        )
        if reason:
            return Decision(EntityStatus.SIMPLIFIED, reason)
    if kind in TEXT_TYPES and facts.font is not None and not facts.font[3]:
        _style, source_font, resolved_font, _found = facts.font
        return Decision(
            EntityStatus.TRANSFORMED,
            f"Fuente sustituida: {source_font} se representa con {resolved_font}.",
        )
    return Decision(EntityStatus.CONSERVED)


def unsupported_hint(skipped_types: Iterable[str], suggest_solids_footprint: bool) -> str:
    """Pista para el usuario cuando la extracción se detiene por entidades no soportadas."""
    if suggest_solids_footprint and "3DSOLID" in set(skipped_types):
        return HINT_SOLIDS_FOOTPRINT
    return HINT_OMIT_UNSUPPORTED
