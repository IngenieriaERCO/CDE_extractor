"""Capas apagadas o congeladas: qué ocultan y cómo incluirlas a propósito.

- Dentro del detalle: ``hidden_layer_counts`` cuenta, por capa, las entidades que no se dibujan
  porque su capa (o, en la capa "0" de un bloque, la del INSERT que la contiene) está apagada
  o congelada. Usa las mismas reglas que el dibujo (``fidelity.iter_detail_entities_with_
  visibility``).
- En el Model Space: ``loose_modelspace_entities`` recoge las entidades sueltas (todo lo que
  no es un INSERT, es decir, lo que no está en ningún bloque) con el estado de su capa;
  ``hidden_overlap`` dice cuáles, en capas ocultas, están sobre la zona de un detalle.
- ``reveal_layers`` enciende y descongela capas en la copia de trabajo del detalle
  (--include-hidden-layers). El DXF de origen nunca se modifica.
"""

from __future__ import annotations

import weakref
from collections import Counter
from dataclasses import dataclass

from ezdxf.document import Drawing

from cde_exporter.cad.detail_model import HiddenLayerContent
from cde_exporter.cad.fidelity import iter_detail_entities_with_visibility
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.reader import entity_bounds

STATE_OFF = "apagada"
STATE_FROZEN = "congelada"
STATE_OFF_AND_FROZEN = "apagada y congelada"
STATE_VISIBLE = "visible"

# Valor de --include-hidden-layers sin lista de capas: todas las capas ocultas.
ALL_LAYERS = "*"


def layer_state(layer) -> str | None:
    """Estado de una capa de la tabla LAYER: None si se ve."""
    off = not layer.is_on()
    frozen = layer.is_frozen()
    if off and frozen:
        return STATE_OFF_AND_FROZEN
    if frozen:
        return STATE_FROZEN
    if off:
        return STATE_OFF
    return None


def _states(document: Drawing) -> dict[str, str | None]:
    return {str(layer.dxf.name).casefold(): layer_state(layer) for layer in document.layers}


def _contents(counts: Counter, states: dict[str, str | None]) -> tuple[HiddenLayerContent, ...]:
    return tuple(
        HiddenLayerContent(layer, states.get(layer.casefold()) or STATE_OFF, count)
        for layer, count in sorted(counts.items(), key=lambda item: item[0].casefold())
    )


def hidden_layer_counts(document: Drawing) -> tuple[HiddenLayerContent, ...]:
    """Entidades del Model Space de ``document`` (el detalle extraído) ocultas por su capa.

    Cuenta cada vez que la entidad aparecería en el dibujo, como el inventario del TXT; no
    cuenta lo que está dentro de un INSERT invisible ni lo marcado como invisible.
    """
    counts: Counter = Counter()
    for item in iter_detail_entities_with_visibility(document):
        if item.hidden_layer is not None:
            counts[item.hidden_layer] += 1
    return _contents(counts, _states(document))


@dataclass(frozen=True, slots=True)
class LooseEntity:
    """Entidad suelta del Model Space: capa, estado de la capa y caja XY (solo si oculta)."""

    entity_type: str
    layer: str
    state: str
    bounds: tuple[float, float, float, float] | None


_LOOSE_CACHE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def loose_modelspace_entities(document: Drawing) -> tuple[LooseEntity, ...]:
    """Entidades del Model Space que no son un INSERT (no están en ningún bloque).

    Solo se mide la caja de las que están en una capa oculta (son las que pueden faltar en el
    PDF sin que nadie lo note). Se calcula una vez por documento: extract-all-blocks lo
    reutiliza para todos los paquetes.
    """
    cached = _LOOSE_CACHE.get(document)
    if cached is not None:
        return cached
    # Las entidades que ezdxf no interpreta (p. ej. GEOMAPIMAGE) no tienen capa: no pueden
    # estar en una capa oculta y ezdxf no sabría decidir si se ven.
    roots = [
        entity
        for entity in document.modelspace()
        if entity.dxftype() != "INSERT" and entity.dxf.is_supported("layer")
    ]
    states = _states(document)
    result = []
    for item in iter_detail_entities_with_visibility(document, roots=roots):
        entity = item.entity
        layer = str(entity.dxf.get("layer", "0") or "0")
        if item.hidden_layer is not None:
            state = states.get(item.hidden_layer.casefold()) or STATE_OFF
            result.append(
                LooseEntity(entity.dxftype(), item.hidden_layer, state, entity_bounds(entity))
            )
        else:
            result.append(LooseEntity(entity.dxftype(), layer, STATE_VISIBLE, None))
    loose = tuple(result)
    _LOOSE_CACHE[document] = loose
    return loose


def _overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def hidden_overlap(
    document: Drawing, box: tuple[float, float, float, float] | None
) -> tuple[HiddenLayerContent, ...]:
    """Entidades sueltas en capas ocultas cuya caja toca ``box`` (xmin, ymin, xmax, ymax)."""
    if box is None:
        return ()
    counts: Counter = Counter()
    states: dict[str, str] = {}
    for item in loose_modelspace_entities(document):
        if item.state != STATE_VISIBLE and item.bounds is not None and _overlaps(item.bounds, box):
            counts[item.layer] += 1
            states[item.layer.casefold()] = item.state
    return _contents(counts, states)


def loose_summary(document: Drawing) -> list[dict]:
    """Entidades sueltas del Model Space por capa y estado (para ``resumen_proyecto.json``)."""
    counts: Counter = Counter(
        (item.layer, item.state) for item in loose_modelspace_entities(document)
    )
    return [
        {"layer": layer, "state": state, "entities": count}
        for (layer, state), count in sorted(
            counts.items(), key=lambda item: (item[0][0].casefold(), item[0][1])
        )
    ]


def parse_layer_selection(
    document: Drawing, value: str | None, *, strict: bool = True
) -> frozenset[str] | None:
    """Capas pedidas con --include-hidden-layers (sin mayúsculas).

    Devuelve None si no se usó la opción. Sin lista (``ALL_LAYERS``) devuelve todas las capas
    del documento. Una capa que no existe en el DXF detiene la orden antes de crear nada
    (salvo con ``strict=False``: la copia de trabajo de extract-modelspace solo tiene las capas
    de lo elegido, y los nombres ya se comprobaron en el DXF de origen).
    """
    if value is None:
        return None
    existing = {str(layer.dxf.name).casefold(): str(layer.dxf.name) for layer in document.layers}
    if value.strip() == ALL_LAYERS:
        return frozenset(existing)
    names = [name.strip() for name in value.split(",") if name.strip()]
    if not names:
        raise CadProcessingError(
            "--include-hidden-layers necesita nombres de capa separados por comas, o nada "
            "para incluir todas las capas ocultas."
        )
    missing = [name for name in names if name.casefold() not in existing]
    if missing and strict:
        raise CadProcessingError(
            "No existe en el DXF la capa "
            + ", ".join(f"«{name}»" for name in missing)
            + " (--include-hidden-layers). No se generaron archivos."
        )
    return frozenset(name.casefold() for name in names)


def option_value(value: str | None) -> tuple[str, ...] | None:
    """Lo pedido con --include-hidden-layers, tal como se guarda en el manifiesto."""
    if value is None:
        return None
    if value.strip() == ALL_LAYERS:
        return (ALL_LAYERS,)
    return tuple(name.strip() for name in value.split(",") if name.strip())


def reveal_layers(document: Drawing, selection: frozenset[str]) -> tuple[str, ...]:
    """Enciende y descongela las capas ocultas de ``selection`` en ``document``.

    ``document`` debe ser la copia de trabajo del detalle, nunca el DXF de origen. Devuelve
    los nombres de las capas que estaban ocultas y se han mostrado.
    """
    revealed = []
    for layer in document.layers:
        name = str(layer.dxf.name)
        if name.casefold() in selection and layer_state(layer) is not None:
            layer.on()
            layer.thaw()
            revealed.append(name)
    return tuple(revealed)
