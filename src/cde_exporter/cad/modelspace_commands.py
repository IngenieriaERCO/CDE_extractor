"""Orden «cad extract-modelspace»: exporta lo que hay en el Model Space, sin elegir un bloque.

Se elige qué entra (todo, por capas o por zona) y se agrupa en un bloque de trabajo que solo
existe en una copia en memoria. Luego se exporta con el mismo flujo que «cad extract-block»
(PDF, TXT y manifiesto, mismas comprobaciones y publicación atómica). El DXF de origen no se
modifica.

Selección (sin recorte):
- Por capas: ``--layers`` y ``--exclude-layers`` (separadas por comas, con comodines ``*`` y
  ``?``, sin distinguir mayúsculas). Un INSERT se elige por su propia capa y entra entero.
- Por zona: ``--window`` o ``--window-from-block``. Entra cada entidad cuya caja XY toca la
  zona, entera. Las que no tienen caja no se pueden situar: no entran y se avisa.
- Las líneas y polilíneas de longitud nula no se dibujan (serían un punto suelto) y se avisa.
- Lo que está en capas ocultas entra en la selección pero no se dibuja (se avisa), salvo con
  --include-hidden-layers. Si --layers solo pide capas ocultas, la orden se detiene.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import Counter
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

import ezdxf
from ezdxf import xref
from ezdxf.document import Drawing

from cde_exporter.cad import block_commands
from cde_exporter.cad.block_extractor import (
    _multileader_mapping_once,
    _restore_default_resources,
)
from cde_exporter.cad.detail_model import ModelSpaceSelection
from cde_exporter.cad.hidden_layers import layer_state, parse_layer_selection
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.reader import entity_bounds
from cde_exporter.cad.scale import parse_nominal_scale

# Nombre del bloque de trabajo que agrupa la selección (con _2, _3… si ya existe).
WORKING_BLOCK = "Model_Space"
# Longitud XY (unidades CAD) por debajo de la cual una línea o polilínea es «nula».
ZERO_LENGTH = 1e-9
# Plancha A1 (mm), para decir en --list si el dibujo cabe.
A1_MM = (841.0, 594.0)

_LINEAR_TYPES = frozenset({"LINE", "LWPOLYLINE", "POLYLINE"})


def _patterns(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (value or "").split(",") if part.strip())


def _matches(layer: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(layer.casefold(), pattern.casefold()) for pattern in patterns)


def _xy_points(entity) -> list[tuple[float, float]]:
    kind = entity.dxftype()
    if kind == "LINE":
        return [(entity.dxf.start.x, entity.dxf.start.y), (entity.dxf.end.x, entity.dxf.end.y)]
    if kind == "LWPOLYLINE":
        return [(x, y) for x, y, *_rest in entity.get_points("xy")]
    if kind == "POLYLINE" and not (entity.is_polygon_mesh or entity.is_poly_face_mesh):
        return [(point.x, point.y) for point in entity.points()]
    return []


def _vertex_count(entity) -> int:
    kind = entity.dxftype()
    if kind == "LWPOLYLINE":
        return len(entity)
    if kind == "POLYLINE":
        return len(entity.vertices)
    return 2 if kind == "LINE" else 0


def _points_bounds(entity) -> tuple[float, float, float, float] | None:
    """Caja XY de los vértices de una línea o polilínea. Respaldo para las que ezdxf no sabe
    medir: en un DXF real, polilíneas de 2 o 3 mm con coordenadas de millones de metros."""
    try:
        points = _xy_points(entity)
    except Exception:  # noqa: BLE001 - datos inválidos: sin caja
        return None
    if not points:
        return None
    xs, ys = [x for x, _y in points], [y for _x, y in points]
    return min(xs), min(ys), max(xs), max(ys)


def _is_zero_length(entity) -> bool:
    """Línea o polilínea (no malla) cuya longitud XY es nula: en el PDF sería un punto."""
    if entity.dxftype() not in _LINEAR_TYPES:
        return False
    if entity.dxftype() == "POLYLINE" and (entity.is_polygon_mesh or entity.is_poly_face_mesh):
        return False
    try:
        points = _xy_points(entity)
    except Exception:  # noqa: BLE001 - datos inválidos: que lo decida el dibujo
        return False
    if not points:
        return False
    length = sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False))
    return length < ZERO_LENGTH


@dataclass(frozen=True, slots=True)
class Selection:
    entities: tuple
    layers: Counter  # entidades elegidas por capa
    vertices: int
    zero_length: int
    without_bounds: int
    box: tuple[float, float, float, float] | None  # caja de lo elegido con caja


def select_modelspace(
    document: Drawing,
    layers: tuple[str, ...],
    exclude_layers: tuple[str, ...],
    window: tuple[float, float, float, float] | None,
) -> Selection:
    """Entidades del Model Space que cumplen las capas y la zona pedidas (sin recortar)."""
    chosen, by_layer = [], Counter()
    vertices = zero = unbounded = 0
    xmin = ymin = math.inf
    xmax = ymax = -math.inf
    for entity in document.modelspace():
        if not entity.dxf.is_supported("layer"):
            continue  # entidades que ezdxf no interpreta (p. ej. GEOMAPIMAGE): no se copian
        layer = str(entity.dxf.get("layer", "0") or "0")
        if layers and not _matches(layer, layers):
            continue
        if exclude_layers and _matches(layer, exclude_layers):
            continue
        if _is_zero_length(entity):
            # Un punto suelto: se cuenta si está en la zona (o si no hay zona).
            x, y = _xy_points(entity)[0]
            if window is None or (window[0] <= x <= window[2] and window[1] <= y <= window[3]):
                zero += 1
            continue
        bounds = entity_bounds(entity) or _points_bounds(entity)
        if window is not None:
            if bounds is None:
                unbounded += 1
                continue
            if not (
                bounds[0] <= window[2]
                and window[0] <= bounds[2]
                and bounds[1] <= window[3]
                and window[1] <= bounds[3]
            ):
                continue
        chosen.append(entity)
        by_layer[layer] += 1
        vertices += _vertex_count(entity)
        if bounds is not None:
            xmin, ymin = min(xmin, bounds[0]), min(ymin, bounds[1])
            xmax, ymax = max(xmax, bounds[2]), max(ymax, bounds[3])
    box = (xmin, ymin, xmax, ymax) if xmin <= xmax else None
    return Selection(tuple(chosen), by_layer, vertices, zero, unbounded, box)


def _window(args, document: Drawing) -> tuple[tuple[float, float, float, float] | None, str | None]:
    if args.window and args.window_from_block:
        raise CadProcessingError("Use --window o --window-from-block, no los dos.")
    if args.window:
        xmin, ymin, xmax, ymax = (float(value) for value in args.window)
        if not (xmin < xmax and ymin < ymax):
            raise CadProcessingError("--window necesita XMIN < XMAX e YMIN < YMAX.")
        return (xmin, ymin, xmax, ymax), None
    if not args.window_from_block:
        if args.occurrence is not None:
            raise CadProcessingError("--occurrence solo se usa con --window-from-block.")
        return None, None
    name = args.window_from_block
    inserts = [
        entity
        for entity in document.modelspace()
        if entity.dxftype() == "INSERT" and entity.dxf.name.casefold() == name.casefold()
    ]
    if not inserts:
        raise CadProcessingError(
            f"El bloque «{name}» no tiene referencias en el Model Space (--window-from-block)."
        )
    if args.occurrence is None and len(inserts) > 1:
        raise CadProcessingError(
            f"El bloque «{name}» tiene {len(inserts)} referencias; indique cuál con --occurrence N."
        )
    occurrence = args.occurrence or 1
    if not 1 <= occurrence <= len(inserts):
        raise CadProcessingError(
            f"La ocurrencia {occurrence} no existe (hay {len(inserts)} referencia(s))."
        )
    bounds = entity_bounds(inserts[occurrence - 1])
    if bounds is None:
        raise CadProcessingError(f"No se pudo calcular la caja del bloque «{name}».")
    return bounds, inserts[occurrence - 1].dxf.name


def _check_requested_layers(
    document: Drawing, layers: tuple[str, ...], reveal: frozenset[str] | None
) -> None:
    """--layers debe nombrar alguna capa; si todas las que nombra están ocultas, hace falta
    --include-hidden-layers (decisión de Luis Angel: nunca se incluyen por su cuenta)."""
    if not layers:
        return
    matched = [layer for layer in document.layers if _matches(str(layer.dxf.name), layers)]
    if not matched:
        raise CadProcessingError(
            "Ninguna capa del DXF coincide con --layers ("
            + ", ".join(f"«{pattern}»" for pattern in layers)
            + "). No se generaron archivos."
        )
    hidden = [layer for layer in matched if layer_state(layer) is not None]
    revealed = [
        layer for layer in hidden if reveal is not None and layer.dxf.name.casefold() in reveal
    ]
    if len(hidden) == len(matched) and not revealed:
        names = ", ".join(f"«{layer.dxf.name}» ({layer_state(layer)})" for layer in hidden)
        raise CadProcessingError(
            f"Las capas pedidas están ocultas en el CAD: {names}. Para exportarlas use "
            "--include-hidden-layers. No se generaron archivos."
        )


def _unique_block_name(document: Drawing) -> str:
    name, number = WORKING_BLOCK, 2
    while name in document.blocks:
        name, number = f"{WORKING_BLOCK}_{number}", number + 1
    return name


def working_document(document: Drawing, entities: tuple) -> tuple[Drawing, str, int]:
    """Copia las entidades elegidas a un DXF nuevo y las agrupa en un bloque de trabajo.

    Devuelve el documento, el nombre del bloque y cuántas entidades no se pudieron copiar.
    Las coordenadas no cambian: el bloque tiene su base en (0, 0, 0) y se inserta ahí, así
    que la matriz CAD → página del manifiesto sigue en coordenadas del Model Space de origen.
    """
    wanted = {id(entity) for entity in entities}
    target = ezdxf.new(document.dxfversion)
    loader = xref.Loader(document, target)
    loader.load_modelspace(filter_fn=lambda candidate: id(candidate) in wanted)
    with _multileader_mapping_once():
        loader.execute()
    _restore_default_resources(document, target)
    target.header["$INSUNITS"] = int(document.header.get("$INSUNITS", 0) or 0)
    modelspace = target.modelspace()
    copied = list(modelspace)
    name = _unique_block_name(target)
    block = target.blocks.new(name)
    for entity in copied:
        modelspace.move_to_layout(entity, block)
    modelspace.add_blockref(name, (0, 0, 0))
    return target, name, len(entities) - len(copied)


def _print_list(
    selection: Selection, document: Drawing, args, reveal: frozenset[str] | None
) -> None:
    states = {str(layer.dxf.name).casefold(): layer_state(layer) for layer in document.layers}
    print(
        f"Entidades seleccionadas: {len(selection.entities)} | vértices de líneas y "
        f"polilíneas: {selection.vertices}"
    )
    for layer, count in sorted(selection.layers.items(), key=lambda item: item[0].casefold()):
        state = states.get(layer.casefold())
        if not state:
            note = ""
        elif reveal is not None and layer.casefold() in reveal:
            note = f" ({state} en el CAD: se dibuja por --include-hidden-layers)"
        else:
            note = f" ({state}: no se dibuja)"
        print(f"  - capa «{layer}»: {count}{note}")
    if selection.zero_length:
        print(f"De longitud nula (no se dibujarían): {selection.zero_length}")
    if selection.without_bounds:
        print(f"Sin caja (no se pueden situar en la zona): {selection.without_bounds}")
    if selection.box is None:
        print("Tamaño: no calculable (ninguna entidad con caja).")
        return
    width, height = selection.box[2] - selection.box[0], selection.box[3] - selection.box[1]
    print(
        f"Caja CAD (xmin, ymin, xmax, ymax): ({selection.box[0]:.3f}, {selection.box[1]:.3f}, "
        f"{selection.box[2]:.3f}, {selection.box[3]:.3f}); {width:.3f} × {height:.3f} unidades"
    )
    code = int(document.header.get("$INSUNITS", 0) or 0)
    if args.units:
        code = {"mm": 4, "cm": 5, "m": 6, "in": 1, "ft": 2}[args.units]
    if not code or args.scale is None:
        print("Tamaño en papel: indique --scale 1:N (y unidades declaradas) para calcularlo.")
        return
    factor = ezdxf.units.conversion_factor(code, ezdxf.units.InsertUnits.Millimeters)
    denominator = parse_nominal_scale(args.scale)
    paper = (
        width * factor / denominator + 2 * args.margin_mm,
        height * factor / denominator + 2 * args.margin_mm,
    )
    fits = (paper[0] <= A1_MM[0] and paper[1] <= A1_MM[1]) or (
        paper[0] <= A1_MM[1] and paper[1] <= A1_MM[0]
    )
    print(
        f"Página a {args.scale} (con márgenes de {args.margin_mm:g} mm): {paper[0]:.1f} × "
        f"{paper[1]:.1f} mm; {'cabe' if fits else 'NO cabe'} en una plancha A1 "
        f"({A1_MM[0]:g} × {A1_MM[1]:g} mm)."
    )


def run_extract_modelspace(args) -> int:
    source = Path(args.drawing)
    if Path(args.output_prefix).suffix:
        raise CadProcessingError("--output-prefix debe ser una ruta base sin extensión")
    document, pruned = block_commands.read_source_dxf(source, Path(args.output_prefix).parent)
    layers, exclude = _patterns(args.layers), _patterns(args.exclude_layers)
    reveal = parse_layer_selection(document, args.include_hidden_layers)
    _check_requested_layers(document, layers, reveal)
    window, window_block = _window(args, document)
    selection = select_modelspace(document, layers, exclude, window)
    print(
        f"Model Space de {source.name}: {len(selection.entities)} entidades seleccionadas"
        + (f" (capas: {', '.join(layers)})" if layers else "")
        + (f" (sin: {', '.join(exclude)})" if exclude else "")
        + ("" if window is None else " en la zona pedida")
        + "."
    )
    if args.list:
        _print_list(selection, document, args, reveal)
        return 0
    if not selection.entities:
        raise CadProcessingError(
            "La selección del Model Space está vacía; no se generaron archivos."
        )
    working, block_name, not_copied = working_document(document, selection.entities)
    sys.stdout.flush()
    values = dict(vars(args))
    values.update(
        block_name=block_name,
        occurrence=None,
        handle=None,
        all=False,
        scale_factor=None,
        source_pruning=pruned,
        modelspace_selection=ModelSpaceSelection(
            layers=layers,
            exclude_layers=exclude,
            window=window,
            window_block=window_block,
            selected=len(selection.entities),
            not_copied=not_copied,
            zero_length=selection.zero_length,
            without_bounds=selection.without_bounds,
        ),
    )
    return block_commands.run_extract_block(argparse.Namespace(**values), working)
