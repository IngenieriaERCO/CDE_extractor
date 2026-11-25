"""Inventario de un DXF para el trabajador (orden ``load``) y ``--list --json``.

Da, sin exportar nada (docs/protocolo_trabajador.md, sección 3):
- cada referencia exportable del Model Space con su caja **por definición** (cada bloque se mide
  una vez y se transforma por la matriz de cada INSERT), en el marco único: Model Space en WCS
  con el INSERT aplicado. La caja incluye la huella de los 3DSOLID y no lo que está en capas
  apagadas o congeladas (eso va en ``bounds_with_hidden``);
- las referencias que no se exportan, con su motivo;
- la tabla de capas con los colores efectivos (resueltos como al dibujar) y su uso por bloque;
- lo suelto del Model Space.
"""

from __future__ import annotations

import math
import os
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from ezdxf import bbox
from ezdxf.addons.drawing import RenderContext
from ezdxf.addons.drawing.properties import Properties, layer_key
from ezdxf.colors import aci2rgb
from ezdxf.math import BoundingBox

from cde_exporter.cad.fidelity import iter_detail_entities_with_visibility
from cde_exporter.cad.file_names import package_name
from cde_exporter.cad.hidden_layers import STATE_VISIBLE, layer_state, loose_summary
from cde_exporter.cad.project_commands import (
    REASON_ANONYMOUS,
    REASON_NO_DEFINITION,
    REASON_NOTHING_VISIBLE,
    REASON_XREF,
    _has_visible_content,
    _is_xref,
)
from cde_exporter.cad.reader import dxf_attr, is_unknown_entity
from cde_exporter.cad.scale import OVERRIDE_UNITS, verify_drawing_units
from cde_exporter.cad.solids import solid_footprint

TEXT_TYPES = frozenset({"TEXT", "MTEXT", "ATTRIB"})
# Bloques anónimos «*U» del Model Space que se ofrecen para exportar: los que dibujan al menos
# tantas entidades visibles (las mesas de PVcase, tablas de Civil 3D…). Los menores (etiquetas,
# cotas, piezas de Civil 3D, a miles) siguen en «omitted».
ANON_MIN_ENV = "CDE_ANON_MIN_ENTITIES"
DEFAULT_ANON_MIN_ENTITIES = 100


def anon_min_entities() -> int:
    """Entidades visibles que necesita un «*U» del Model Space para ofrecerse
    (CDE_ANON_MIN_ENTITIES o 100)."""
    value = os.environ.get(ANON_MIN_ENV, "").strip()
    try:
        return max(0, int(value)) if value else DEFAULT_ANON_MIN_ENTITIES
    except ValueError:
        return DEFAULT_ANON_MIN_ENTITIES


def _is_offered_anonymous(name: str) -> bool:
    return name.upper().startswith("*U")


def _visible_count(document, insert) -> int:
    """Entidades que dibujaría ``insert`` (sin contar los INSERT anidados)."""
    return sum(
        1
        for item in iter_detail_entities_with_visibility(document, roots=[insert])
        if item.visible and item.entity.dxftype() != "INSERT"
    )


def _anonymous_reason(minimum: int) -> str:
    return (
        f"bloque anónimo «*U» con menos de {minimum} entidades visibles (etiquetas, cotas o "
        f"piezas de Civil 3D; umbral {ANON_MIN_ENV} o «anonymous_min_entities» de «load»)"
    )


_ANGLE_TOLERANCE = 1e-9

# Avance: (etapa, fracción 0–1 de la etapa, mensaje); cancelación: lanza si se pidió.
Progress = Callable[[str, float, str], None]
Check = Callable[[], None]


def _noop_progress(stage: str, fraction: float, text: str) -> None:
    return None


def _noop_check() -> None:
    return None


@dataclass
class _Box:
    """Caja de una definición en sus coordenadas, si es exacta y cuántas ocultas tiene."""

    box: BoundingBox = field(default_factory=BoundingBox)
    exact: bool = True
    hidden: int = 0


def _right_angle(degrees: float) -> bool:
    remainder = math.fmod(abs(degrees), 90.0)
    return remainder < _ANGLE_TOLERANCE or 90.0 - remainder < _ANGLE_TOLERANCE


def _flat(entity) -> bool:
    extrusion = entity.dxf.get("extrusion", (0, 0, 1))
    return abs(extrusion[0]) < 1e-12 and abs(extrusion[1]) < 1e-12 and extrusion[2] > 0


class _Bounds:
    """Cajas por definición, cacheadas por (bloque, capa heredada por lo de la capa «0», con
    o sin capas ocultas)."""

    def __init__(self, document) -> None:
        self.document = document
        self.hidden_layers = {
            str(layer.dxf.name).casefold() for layer in document.layers if layer_state(layer)
        }
        self.cache: dict[tuple[str, str, bool], _Box] = {}
        self.solids: dict[str, tuple[float, float, float, float] | None] = {}
        # Se llama en cada entidad de una definición (cancelar, o ceder el turno a un pedido
        # mientras se prepara la vista previa en segundo plano: preview_index.Gate).
        self.check = None

    def _hidden(self, layer: str) -> bool:
        return layer.casefold() in self.hidden_layers

    def _solid(self, solid) -> tuple[float, float, float, float] | None:
        handle = str(solid.dxf.get("handle") or "")
        if not handle:
            # Sin handle (entidad virtual) no se guarda en caché: su ``id`` podría pasar a otro
            # sólido cuando este se libere.
            try:
                hull, _z, _half, _own = solid_footprint(solid)
            except Exception:  # noqa: BLE001 - un sólido que no se puede leer no cuenta
                return None
            xs, ys = [p[0] for p in hull], [p[1] for p in hull]
            return (min(xs), min(ys), max(xs), max(ys))
        if handle not in self.solids:
            try:
                hull, _z, _half, _own = solid_footprint(solid)
                xs, ys = [p[0] for p in hull], [p[1] for p in hull]
                self.solids[handle] = (min(xs), min(ys), max(xs), max(ys))
            except Exception:  # noqa: BLE001 - un sólido que no se puede leer no cuenta
                self.solids[handle] = None
        return self.solids[handle]

    def entity_box(self, entity) -> BoundingBox | None:
        if entity.dxftype() == "3DSOLID":
            box = self._solid(entity)
            return None if box is None else BoundingBox([(box[0], box[1]), (box[2], box[3])])
        try:
            extents = bbox.extents([entity])
        except Exception:  # noqa: BLE001 - datos inválidos: no cuentan para la caja
            return None
        return extents if extents.has_data else None

    def definition(self, name: str, inherited: str, include_hidden: bool, stack=()) -> _Box:
        key = (name, inherited.casefold(), include_hidden)
        if key in self.cache:
            return self.cache[key]
        result = _Box()
        if name in stack or name not in self.document.blocks:
            return result
        for entity in self.document.blocks[name]:
            if self.check is not None:
                self.check()
            kind = entity.dxftype()
            if kind == "ATTDEF" or is_unknown_entity(entity) or dxf_attr(entity, "invisible", 0):
                continue  # las de tipos desconocidos no tienen caja (se avisan aparte)
            own = str(dxf_attr(entity, "layer", "0") or "0")
            layer = inherited if own == "0" else own
            if kind == "INSERT":
                child = self.definition(entity.dxf.name, layer, include_hidden, (*stack, name))
                result.hidden += child.hidden
                if child.box.has_data:
                    result.box.extend(
                        entity.matrix44().transform_vertices(child.box.cube_vertices())
                    )
                    rotated = not (_right_angle(entity.dxf.get("rotation", 0.0)) and _flat(entity))
                    result.exact = result.exact and child.exact and not rotated
                for attrib in entity.attribs:
                    if attrib.dxf.get("invisible", 0) or self._hidden(attrib.dxf.layer):
                        continue
                    extents = self.entity_box(attrib)
                    if extents is not None:
                        result.box.extend([extents.extmin, extents.extmax])
                continue
            if self._hidden(layer):
                result.hidden += 1
                if not include_hidden:
                    continue
            box = self.entity_box(entity)
            if box is not None:
                result.box.extend([box.extmin, box.extmax])
            elif kind == "3DSOLID":
                result.exact = False  # su huella no se pudo calcular: no está en la caja
        self.cache[key] = result
        return result

    def reference(self, insert, include_hidden: bool) -> tuple[BoundingBox, bool, int]:
        """Caja de un INSERT del Model Space en WCS, si es exacta y sus entidades ocultas."""
        layer = str(insert.dxf.get("layer", "0") or "0")
        child = self.definition(insert.dxf.name, layer, include_hidden)
        box = BoundingBox()
        if child.box.has_data:
            box.extend(insert.matrix44().transform_vertices(child.box.cube_vertices()))
        exact = child.exact and _right_angle(insert.dxf.get("rotation", 0.0)) and _flat(insert)
        for attrib in insert.attribs:
            if not attrib.dxf.get("invisible", 0) and not self._hidden(attrib.dxf.layer):
                extents = self.entity_box(attrib)
                if extents is not None:
                    box.extend([extents.extmin, extents.extmax])
        return box, exact, child.hidden


def _rgb(color: str) -> str:
    return color[:7].lower()


def _walk(document, insert):
    """Recorre todo lo que dibuja ``insert`` (con bloques anidados), con las reglas del
    dibujo: (entidad, visible, capa que la oculta o None, color efectivo o None)."""
    ctx = RenderContext(document)
    ctx.set_current_layout(document.modelspace())
    # Fondo blanco, como el PDF: el color 7 se resuelve a negro.
    ctx.current_layout_properties.set_colors("#ffffff")

    def visit(entity, stack: tuple[str, ...], hidden_above: bool):
        if is_unknown_entity(entity):
            # Sin capa ni visibilidad (ezdxf fallaría al resolverla): no se dibuja.
            yield entity, False, None, None
            return
        visible = not hidden_above and ctx.resolve_visible(entity)
        hiding = None
        if not visible and not hidden_above and entity.dxftype() != "INSERT":
            properties = ctx.layers.get(layer_key(ctx.resolve_layer(entity)))
            if properties is not None and not properties.is_visible:
                hiding = str(properties.layer)
        color = None
        if visible and entity.dxftype() != "INSERT":
            try:
                color = _rgb(ctx.resolve_color(entity))
            except Exception:  # noqa: BLE001 - un color raro no detiene el inventario
                color = None
        yield entity, visible, hiding, color
        if entity.dxftype() != "INSERT":
            return
        pushed = False
        if visible:
            state = Properties()
            state.layer = ctx.resolve_layer(entity)
            ctx.push_state(state)
            pushed = True
        try:
            for attrib in entity.attribs:
                yield from visit(attrib, stack, hidden_above or not visible)
            name = entity.dxf.name
            if name in stack or name not in document.blocks:
                return
            for child in document.blocks[name]:
                if child.dxftype() == "ATTDEF":
                    continue
                yield from visit(child, (*stack, name), hidden_above or not visible)
        finally:
            if pushed:
                ctx.pop_state()

    yield from visit(insert, (), False)


def _units(document, override: str | None) -> dict:
    code = int(document.header.get("$INSUNITS", 0) or 0)
    verification = verify_drawing_units(
        code,
        0,
        max_block_dimension=None,
        insert_scale=(1, 1, 1),
        insert_position=(0, 0, 0),
        override=override,
    )
    return {
        "declared_code": code,
        "name": verification.unit_name,
        "millimeters_per_unit": verification.millimeters_per_unit
        if verification.verified
        else None,
        "status": verification.status,
        "verified": verification.verified,
        "warnings": list(verification.warnings),
    }


def _reference_units(document, insert, override: str | None) -> dict:
    block = document.blocks.get(insert.dxf.name)
    block_units = int(block.block_record.dxf.get("units", 0) or 0) if block is not None else 0
    verification = verify_drawing_units(
        int(document.header.get("$INSUNITS", 0) or 0),
        block_units,
        max_block_dimension=None,
        insert_scale=(
            float(insert.dxf.get("xscale", 1)),
            float(insert.dxf.get("yscale", 1)),
            float(insert.dxf.get("zscale", 1)),
        ),
        insert_position=tuple(float(value) for value in insert.dxf.insert),
        override=override,
    )
    return {
        "name": verification.unit_name,
        "millimeters_per_unit": verification.millimeters_per_unit
        if verification.verified
        else None,
        "verified": verification.verified,
        "status": verification.status,
        "warnings": list(verification.warnings),
    }


def _bounds_json(box: BoundingBox) -> dict | None:
    if not box.has_data:
        return None
    return {
        "min": [round(box.extmin.x, 6), round(box.extmin.y, 6)],
        "max": [round(box.extmax.x, 6), round(box.extmax.y, 6)],
    }


def _layer_color(layer) -> dict:
    true_color = layer.dxf.get("true_color")
    if true_color is not None:
        r, g, b = (true_color >> 16) & 255, (true_color >> 8) & 255, true_color & 255
        return {"aci": None, "rgb": f"#{r:02x}{g:02x}{b:02x}"}
    aci = abs(int(layer.dxf.get("color", 7)))
    if aci == 7:
        return {"aci": 7, "rgb": "#000000"}  # como sale sobre papel blanco
    rgb = aci2rgb(aci) if 0 < aci < 256 else None
    return {"aci": aci, "rgb": None if rgb is None else "#{:02x}{:02x}{:02x}".format(*rgb)}


def build_inventory(
    document,
    *,
    units: str | None = None,
    margin_mm: float = 5.0,
    include_layers: bool = True,
    progress: Progress = _noop_progress,
    check: Check = _noop_check,
    source_copy=None,
    anonymous_min_entities: int | None = None,
) -> dict:
    """``blocks``, ``omitted``, ``layers``, ``modelspace_loose``, ``units`` y ``margin_mm``
    del documento (el resto de ``load``, como el DXF y el hash, lo añade el trabajador).

    ``anonymous_min_entities``: umbral de los «*U» ofrecidos; si es ``None``, el de
    ``CDE_ANON_MIN_ENTITIES`` (o 100)."""
    if anonymous_min_entities is not None and (
        isinstance(anonymous_min_entities, bool)
        or not isinstance(anonymous_min_entities, int)
        or anonymous_min_entities < 0
    ):
        raise ValueError(f"Umbral de anónimos no válido: {anonymous_min_entities}")
    if units is not None and units not in OVERRIDE_UNITS:
        raise ValueError(f"Unidades no válidas: {units}")
    inserts: dict[str, list] = defaultdict(list)
    for entity in document.modelspace():
        if entity.dxftype() == "INSERT":
            inserts[entity.dxf.name].append(entity)
    bounds = _Bounds(document)
    blocks: list[dict] = []
    omitted: list[dict] = []
    usage: dict[str, list] = defaultdict(list)
    colors: dict[str, Counter] = defaultdict(Counter)
    used_stems: set[str] = set()
    total = sum(len(entities) for entities in inserts.values()) or 1
    done = 0

    minimum = anon_min_entities() if anonymous_min_entities is None else anonymous_min_entities

    def omit(name, entity, occurrence, count, reason, visible=None) -> None:
        item = {
            "name": name,
            "occurrence": occurrence,
            "total_occurrences": count,
            "handle": str(entity.dxf.handle),
            "reason": reason,
        }
        if visible is not None:
            item["anonymous"] = True
            item["visible_entities"] = visible
        omitted.append(item)

    for name, entities in inserts.items():
        count = len(entities)
        reason = None
        anonymous = _is_offered_anonymous(name) and name in document.blocks
        if name.startswith("*") and not anonymous:
            reason = REASON_ANONYMOUS
        elif name not in document.blocks:
            reason = REASON_NO_DEFINITION
        elif _is_xref(document.blocks[name]):
            reason = REASON_XREF
        base = package_name(name)
        for occurrence, insert in enumerate(entities, start=1):
            check()
            done += 1
            progress("blocks", done / total, f"Bloque «{name}» ({done} de {total})")
            if reason is not None:
                omit(name, insert, occurrence, count, reason)
                continue
            if anonymous:
                visible = _visible_count(document, insert)
                if visible < max(minimum, 1):
                    omit(name, insert, occurrence, count, _anonymous_reason(minimum), visible)
                    continue
            elif not _has_visible_content(document, insert):
                omit(name, insert, occurrence, count, REASON_NOTHING_VISIBLE)
                continue
            blocks.append(
                _reference(
                    document,
                    insert,
                    name,
                    occurrence,
                    count,
                    base,
                    bounds,
                    units,
                    used_stems,
                    usage,
                    colors,
                    anonymous,
                )
            )
    layers = []
    if include_layers:
        progress("layers", 0.0, "Capas")
        for layer in document.layers:
            name = str(layer.dxf.name)
            linetype = document.linetypes.get(layer.dxf.get("linetype", "Continuous"))
            weight = int(layer.dxf.get("lineweight", -3))
            layers.append(
                {
                    "name": name,
                    "color": _layer_color(layer),
                    "effective_colors": [
                        {"rgb": rgb, "entities": n}
                        for rgb, n in colors[name.casefold()].most_common()
                    ],
                    "lineweight_mm": weight / 100 if weight >= 0 else None,
                    "linetype": {
                        "name": str(layer.dxf.get("linetype", "Continuous")),
                        "description": str(linetype.dxf.get("description", ""))
                        if linetype is not None
                        else "",
                    },
                    "state": {
                        "on": layer.is_on(),
                        "frozen": layer.is_frozen(),
                        "plot": bool(layer.dxf.get("plot", 1)),
                    },
                    "description": str(getattr(layer, "description", "") or ""),
                    "usage": usage.get(name.casefold(), []),
                }
            )
        progress("layers", 1.0, "Capas")
    return {
        "units": _units(document, units),
        "margin_mm": margin_mm,
        "anonymous_min_entities": minimum,
        "blocks": blocks,
        "omitted": omitted,
        "layers": layers,
        "modelspace_loose": _loose(document, source_copy),
        "unknown_entities": unknown_entity_summary(document),
    }


def _loose(document, source_copy) -> list[dict]:
    """Lo suelto del Model Space por capa y estado. Con la lectura selectiva
    (``source_copy.selective``) no está en el documento: sale del resumen que se hizo al
    copiar, con los vértices, los tipos y una caja aproximada."""
    if source_copy is None or not getattr(source_copy, "selective", False):
        return loose_summary(document)
    states = {str(layer.dxf.name).casefold(): layer_state(layer) for layer in document.layers}
    return [
        {
            "layer": item.layer,
            "state": states.get(item.layer.casefold()) or STATE_VISIBLE,
            "entities": item.entities,
            "vertices": item.vertices,
            "types": item.types,
            "bounds_approx": None
            if item.bounds is None
            else {
                "min": [round(item.bounds[0], 3), round(item.bounds[1], 3)],
                "max": [round(item.bounds[2], 3), round(item.bounds[3], 3)],
            },
        }
        for item in source_copy.loose
    ]


def unknown_entity_summary(document) -> list[dict]:
    """Entidades de tipos que ezdxf no sabe leer, por tipo: sueltas en el Model Space y en las
    definiciones de bloque (cada definición una vez). No se dibujan en ningún PDF."""
    loose: Counter = Counter()
    nested: Counter = Counter()
    for entity in document.modelspace():
        if is_unknown_entity(entity):
            loose[entity.dxftype()] += 1
    for block in document.blocks:
        if block.is_any_layout:
            continue  # el Model Space y el Paper Space no son definiciones de bloque
        for entity in block:
            if is_unknown_entity(entity):
                nested[entity.dxftype()] += 1
    return [
        {"type": kind, "modelspace": loose[kind], "in_blocks": nested[kind]}
        for kind in sorted(set(loose) | set(nested))
    ]


def _reference(
    document,
    insert,
    name,
    occurrence,
    count,
    base,
    bounds,
    units,
    used_stems,
    usage,
    colors,
    anonymous=False,
) -> dict:
    box, exact, hidden = bounds.reference(insert, include_hidden=False)
    with_hidden = None
    if hidden:
        hidden_box, hidden_exact, _ = bounds.reference(insert, include_hidden=True)
        with_hidden = _bounds_json(hidden_box)
        if with_hidden is not None:
            with_hidden["exact"] = hidden_exact
    reference_units = _reference_units(document, insert, units)
    size = [round(box.size.x, 6), round(box.size.y, 6)] if box.has_data else None
    factor = reference_units["millimeters_per_unit"]
    size_m = (
        [round(value * factor / 1000, 6) for value in size]
        if size is not None and factor is not None
        else None
    )
    # Recorrido con las reglas del dibujo: cuentas, lo oculto, uso y colores por capa.
    kinds: Counter = Counter()
    unknown: Counter = Counter()
    per_layer: dict[str, Counter] = defaultdict(Counter)
    entities = hidden_entities = visible_entities = 0
    for entity, visible, hiding, color in _walk(document, insert):
        if entity is insert:
            continue
        kind = entity.dxftype()
        if is_unknown_entity(entity):
            unknown[kind] += 1
            continue
        entities += 1
        if visible and kind != "INSERT":
            visible_entities += 1
        kinds[kind] += 1
        if hiding is not None:
            hidden_entities += 1
        layer = (
            str(entity.dxf.get("layer", "0") or "0") if entity.dxf.is_supported("layer") else "0"
        )
        per_layer[layer][kind] += 1
        if visible and color is not None:
            colors[layer.casefold()][color] += 1
    ref = f"{name}#{occurrence}"
    for layer, types in per_layer.items():
        usage[layer.casefold()].append({"ref": ref, "types": dict(types.most_common())})
    insert_layer = str(insert.dxf.get("layer", "0") or "0")
    # Un «*U» no tiene un nombre útil (AutoCAD lo renumera al guardar): se nombra por la capa y
    # el handle de su INSERT, que no cambian.
    label = f"{insert_layer}_{insert.dxf.handle}" if anonymous else name
    if anonymous:
        stem = package_name(label)
    else:
        stem = base if count == 1 else f"{base}_{occurrence:03d}"
    candidate, suffix = stem, 2
    while candidate.casefold() in used_stems:
        candidate, suffix = f"{stem}_{suffix}", suffix + 1
    used_stems.add(candidate.casefold())
    return {
        "ref": ref,
        "name": name,
        # Bloque anónimo «*U» del Model Space (el Componedor los muestra aparte) y su nombre
        # legible; en los demás, «label» es el nombre del bloque.
        "anonymous": anonymous,
        "label": label,
        "handle": str(insert.dxf.handle),
        "occurrence": occurrence,
        "total_occurrences": count,
        "layer": insert_layer,
        "insert": {
            "point": [round(float(value), 6) for value in insert.dxf.insert],
            "rotation_deg": float(insert.dxf.get("rotation", 0.0)),
            "scale": [
                float(insert.dxf.get("xscale", 1)),
                float(insert.dxf.get("yscale", 1)),
                float(insert.dxf.get("zscale", 1)),
            ],
        },
        "bounds": None if not box.has_data else {**_bounds_json(box), "exact": exact},
        "bounds_with_hidden": with_hidden,
        "size_cad": size,
        "size_m": size_m,
        "units": reference_units,
        "entities": entities,
        "visible_entities": visible_entities,
        "contains": {
            "solids_3d": kinds["3DSOLID"],
            "regions": kinds["REGION"],
            "proxies": kinds["ACAD_PROXY_ENTITY"],
            "texts": sum(kinds[kind] for kind in TEXT_TYPES),
            "hatches": kinds["HATCH"],
            "images": kinds["IMAGE"],
        },
        "hidden_layer_entities": hidden_entities,
        # De tipos que ezdxf no sabe leer (complementos): no se dibujan ni cuentan en la caja.
        "unknown_entities": [
            {"type": kind, "count": count} for kind, count in sorted(unknown.items())
        ],
        "file_stem": candidate,
    }


def page_size_mm(block: dict, scale_denominator: float, margin_mm: float) -> list[float] | None:
    """Tamaño de página a 1:N (``size_m × 1000 ÷ N + 2 × margen`` por eje)."""
    if block.get("size_m") is None:
        return None
    return [round(value * 1000 / scale_denominator + 2 * margin_mm, 3) for value in block["size_m"]]
