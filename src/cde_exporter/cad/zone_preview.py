"""Vista previa PNG de una zona del Model Space (orden ``cad preview-modelspace``).

Sirve para elegir la zona y las capas antes de exportar con ``cad extract-modelspace``:
- Vista: todo el Model Space; un trozo con ``--view``; o, con ``--window`` y sin ``--view``, la
  zona con un 25 % de contexto por cada lado. La zona se marca con un rectángulo rojo. En la
  vista de todo el Model Space se marcan en azul los grupos de dibujo (separados por huecos de
  más del 0,5 % del lado mayor) y se da la caja de cada uno, lista para ``--view``.
- Cajas: la de un INSERT sale de la de su definición, calculada una vez por bloque
  (``inventory._Bounds``); es mucho más rápida que la exacta de ``select_modelspace`` y puede
  ser algo mayor (solo hace que se dibuje alguna entidad de más fuera de la vista).
- Capas: el mismo filtro que la exportación (``--layers``, ``--exclude-layers`` y
  ``--include-hidden-layers``), también dentro de los bloques: una capa que no se ve no se
  dibuja en ningún nivel, salvo la «0», que toma la capa del INSERT. La vista no recorta: lo que
  cruza el borde de la zona se ve entero (la exportación lo corta).
- Coordenadas: cuadrícula con las coordenadas CAD en los bordes y, en el JSON, la matriz
  ``cad_to_px`` (la misma convención que la vista previa del trabajador, ``preview.py``).
- Con ``--scale 1:N``: tamaño de la zona (o de la vista) en la hoja, con los márgenes, y si cabe
  en una plancha A1, en una franja al pie del PNG y en el JSON.
- Dibujo ligero, como ``preview.py``: texto como rectángulos, rayados rellenos, sin tipos de
  línea y sin 3DSOLID.
- Archivos: ``<salida>.png`` y ``<salida>.json`` al lado (el JSON, el último); se sobrescriben.
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import ezdxf
from ezdxf.addons.drawing import RenderContext
from ezdxf.addons.drawing.properties import LayoutProperties, layer_key
from ezdxf.addons.drawing.recorder import Recorder
from PIL import Image, ImageDraw, ImageFont

from cde_exporter.cad import block_commands
from cde_exporter.cad.exporters import xobjects_enabled
from cde_exporter.cad.hidden_layers import layer_state, parse_layer_selection
from cde_exporter.cad.inventory import _Bounds
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.modelspace_commands import (
    A1_MM,
    _check_requested_layers,
    _is_zero_length,
    _patterns,
    _points_bounds,
    _visible,
    _window,
)
from cde_exporter.cad.preview import (
    ALWAYS_REPORTED,
    MAX_LONG_SIDE_PX,
    MIN_LONG_SIDE_PX,
    _configuration,
    _PreviewFrontend,
    _show_hidden_layers,
    paint,
)
from cde_exporter.cad.reader import entity_bounds
from cde_exporter.cad.scale import parse_nominal_scale

FORMAT = "cde-zone-preview"
VERSION = 1
CONTEXT = 0.25  # con --window y sin --view: contexto alrededor de la zona (fracción por lado)
ZONE_COLOR = (220, 0, 0, 255)
GRID_COLOR = (215, 222, 230, 255)
LABEL_COLOR = (90, 100, 115, 255)
FOOTER_PX = 26
GROUP_COLOR = (0, 110, 200, 255)
GROUP_GAP = 0.005  # hueco que separa grupos de dibujo: fracción del lado mayor de la vista
GROUPS_LIMIT = 15
FULL_VIEW_PAD = 0.03  # vista de todo el Model Space: aire por lado (fracción del lado mayor)
_UNIT_CODES = {"mm": 4, "cm": 5, "m": 6, "in": 1, "ft": 2}


def _layer_name(entity) -> str:
    return str(entity.dxf.get("layer", "0") or "0")


# ----------------------------------------------------------------- selección rápida


@dataclass
class ViewSelection:
    entities: list  # lo que toca la vista (sin filtrar capas), en el orden del Model Space
    boxes: list  # caja de cada una (None si no tiene)
    layers: Counter  # entidades por capa
    without_bounds: int
    box: tuple[float, float, float, float] | None  # caja de todo lo que tiene caja


def _box_of(cache: _Bounds, entity) -> tuple[float, float, float, float] | None:
    """Caja XY rápida: la de un INSERT sale de la de su definición (calculada una vez por
    bloque, con lo de las capas ocultas) llevada por su matriz, más sus atributos; contiene la
    exacta y puede ser algo mayor. Lo demás, como en ``select_modelspace``."""
    if (
        entity.dxftype() == "INSERT"
        and int(entity.dxf.get("row_count", 1) or 1) <= 1
        and int(entity.dxf.get("column_count", 1) or 1) <= 1
    ):
        try:
            box, _exact, _hidden = cache.reference(entity, True)
        except Exception:  # noqa: BLE001 - datos inválidos: se prueba la caja exacta
            box = None
        if box is not None and box.has_data:
            x0, y0, x1, y1 = box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y
            for attrib in entity.attribs:  # también los invisibles o en capas ocultas
                extra = entity_bounds(attrib)
                if extra is not None:
                    x0, y0 = min(x0, extra[0]), min(y0, extra[1])
                    x1, y1 = max(x1, extra[2]), max(y1, extra[3])
            return (x0, y0, x1, y1)
    return entity_bounds(entity) or _points_bounds(entity)


def modelspace_boxes(document, check=None) -> list[tuple[object, tuple | None, bool]]:
    """(entidad, caja rápida o None, es un punto) de cada entidad del Model Space que ezdxf
    interpreta, en su orden. Se calcula una vez por DXF (el trabajador la guarda en la
    sesión)."""
    cache = _Bounds(document)
    rows = []
    for number, entity in enumerate(document.modelspace()):
        if check is not None and number % 500 == 0:
            check()
        if not entity.dxf.is_supported("layer"):
            continue
        if _is_zero_length(entity):
            rows.append((entity, None, True))
            continue
        rows.append((entity, _box_of(cache, entity), False))
    return rows


def _touches(box, window) -> bool:
    return not (
        box[2] < window[0] or box[0] > window[2] or box[3] < window[1] or box[1] > window[3]
    )


def zone_candidates(boxes, window) -> list:
    """Lo que puede tocar ``window`` según la caja rápida (que contiene la exacta), más lo que
    no tiene caja y los puntos: ``select_modelspace(..., candidates=…)`` da así lo mismo que
    mirando todo el Model Space, sin calcular la caja exacta de todo."""
    return [entity for entity, box, point in boxes if point or box is None or _touches(box, window)]


def select_view(document, view, boxes=None) -> ViewSelection:
    """Entidades del Model Space que tocan ``view`` (todas, sin vista), sin filtrar capas.
    ``boxes``: las de ``modelspace_boxes`` si ya se calcularon."""
    if boxes is None:
        boxes = modelspace_boxes(document)
    entities, found, layers = [], [], Counter()
    unbounded = 0
    xmin = ymin = math.inf
    xmax = ymax = -math.inf
    for entity, box, point in boxes:
        if point:
            continue
        if box is None:
            unbounded += 1
            if view is not None:
                continue
        elif view is not None and not _touches(box, view):
            continue
        entities.append(entity)
        found.append(box)
        layers[_layer_name(entity)] += 1
        if box is not None:
            xmin, ymin = min(xmin, box[0]), min(ymin, box[1])
            xmax, ymax = max(xmax, box[2]), max(ymax, box[3])
    total = None if xmin == math.inf else (xmin, ymin, xmax, ymax)
    return ViewSelection(entities, found, layers, unbounded, total)


def _split(items: list, axis: int, gap: float) -> list[list]:
    """Parte ``items`` (cajas con su índice) donde hay un hueco mayor que ``gap`` en el eje."""
    ordered = sorted(items, key=lambda item: item[0][axis])
    parts, current, reach = [], [], -math.inf
    for item in ordered:
        if current and item[0][axis] > reach + gap:
            parts.append(current)
            current, reach = [], -math.inf
        current.append(item)
        reach = max(reach, item[0][axis + 2])
    if current:
        parts.append(current)
    return parts


def drawing_groups(boxes: list, gap: float, limit: int = GROUPS_LIMIT) -> list[dict]:
    """Grupos de dibujo separados por huecos mayores que ``gap`` (cortes sucesivos en X y en
    Y), de más a menos entidades: para elegir ``--view`` en un Model Space con dibujos lejanos
    entre sí."""
    items = [(box, index) for index, box in enumerate(boxes) if box is not None]
    pending, done = [items] if items else [], []
    while pending:
        group = pending.pop()
        parts = _split(group, 0, gap)
        if len(parts) == 1:
            parts = _split(group, 1, gap)
        if len(parts) == 1:
            done.append(group)
        else:
            pending.extend(parts)
    rows = []
    for group in done:
        rows.append(
            {
                "box": [
                    min(item[0][0] for item in group),
                    min(item[0][1] for item in group),
                    max(item[0][2] for item in group),
                    max(item[0][3] for item in group),
                ],
                "entities": len(group),
            }
        )
    rows.sort(key=lambda row: (-row["entities"], row["box"][0], row["box"][1]))
    for number, row in enumerate(rows, start=1):
        row["number"] = number
    return rows[:limit]


def hide_layers(ctx: RenderContext, document, layers, exclude_layers) -> list[str]:
    """Oculta, solo en este dibujo, las capas que no se ven por ``layers``/``exclude_layers``
    (en todos los niveles; un INSERT anidado en esa capa desaparece entero). La «0» no se
    oculta: dentro de un bloque toma la capa del INSERT. Devuelve las capas ocultadas."""
    hidden = []
    if not layers and not exclude_layers:
        return hidden
    for layer in document.layers:
        name = str(layer.dxf.name)
        if name == "0" or _visible(name, layers, exclude_layers):
            continue
        properties = ctx.layers.get(layer_key(name))
        if properties is not None:
            properties.is_visible = False
            hidden.append(name)
    return hidden


def draw_entities(
    document,
    entities,
    *,
    layers=(),
    exclude_layers=(),
    hidden_layers=None,
    check=None,
    progress=None,
    plot_style=None,
    print_transparency: bool = True,
):
    """Graba el dibujo de ``entities`` (del Model Space) con el filtro de capas en todos los
    niveles. ``check`` se llama en cada entidad (cancelar) y ``progress(etapa, fracción,
    texto)`` en las del primer nivel; ``plot_style`` y ``print_transparency`` como en la vista
    previa de un bloque. Devuelve (player, omitidas por tipo)."""
    chosen = {id(entity) for entity in entities}
    total = max(1, len(chosen))
    done = 0

    def on_entity(depth: int) -> None:
        nonlocal done
        if check is not None:
            check()
        if depth == 0 and progress is not None:
            done += 1
            progress("draw", done / total, f"Dibujando el Model Space: {done} de {total}")

    backend = Recorder()
    ctx = RenderContext(document)
    _show_hidden_layers(ctx, document, hidden_layers)
    hide_layers(ctx, document, layers, exclude_layers)
    properties = LayoutProperties.from_layout(document.modelspace())
    properties.set_colors("#ffffff")
    frontend = _PreviewFrontend(
        ctx,
        backend,
        config=_configuration(),
        on_entity=on_entity,
        plot_style=plot_style,
        print_transparency=print_transparency,
    )
    frontend.xobjects = frontend.instancing and xobjects_enabled()
    with contextlib.redirect_stdout(io.StringIO()):
        frontend.draw_layout(
            document.modelspace(),
            layout_properties=properties,
            filter_func=lambda entity: id(entity) in chosen,
        )
    omitted: Counter = Counter({kind: 0 for kind in ALWAYS_REPORTED})
    for (kind, _layer), count in frontend.omitted.items():
        omitted[kind] += count
    return backend.player(), dict(sorted(omitted.items()))


# ----------------------------------------------------------------- vista y papel


def _view(args, document, window):
    """Caja que se pinta: --view, la zona con contexto o todo el Model Space (None: se calcula
    con lo que hay)."""
    if args.view:
        xmin, ymin, xmax, ymax = (float(value) for value in args.view)
        if not (xmin < xmax and ymin < ymax):
            raise CadProcessingError("--view necesita XMIN < XMAX e YMIN < YMAX.")
        return (xmin, ymin, xmax, ymax)
    if window is not None:
        dx = (window[2] - window[0]) * CONTEXT
        dy = (window[3] - window[1]) * CONTEXT
        return (window[0] - dx, window[1] - dy, window[2] + dx, window[3] + dy)
    return None


def mm_per_unit(document, units: str | None) -> tuple[float | None, bool]:
    """Milímetros por unidad CAD (None si no se sabe) y si vienen declaradas (--units o
    $INSUNITS)."""
    code = _UNIT_CODES[units] if units else int(document.header.get("$INSUNITS", 0) or 0)
    if not code:
        return None, False
    return ezdxf.units.conversion_factor(code, ezdxf.units.InsertUnits.Millimeters), True


def paper_size(box, factor: float, denominator: float, margin_mm: float) -> dict:
    """Tamaño en la hoja de ``box`` a 1:``denominator``: el dibujo y la página con márgenes."""
    content = (
        (box[2] - box[0]) * factor / denominator,
        (box[3] - box[1]) * factor / denominator,
    )
    page = (content[0] + 2 * margin_mm, content[1] + 2 * margin_mm)
    fits = (page[0] <= A1_MM[0] and page[1] <= A1_MM[1]) or (
        page[0] <= A1_MM[1] and page[1] <= A1_MM[0]
    )
    return {
        "content_mm": [round(value, 3) for value in content],
        "page_mm": [round(value, 3) for value in page],
        "margin_mm": margin_mm,
        "fits_a1": fits,
    }


def _number(value: float, decimals: int) -> str:
    return f"{value:,.{decimals}f}".replace(",", " ").replace(".", ",")


def paper_text(scale: str, paper: dict, what: str) -> str:
    width, height = paper["content_mm"]
    page_w, page_h = paper["page_mm"]
    return (
        f"{what} a {scale}: {_number(width, 1)} × {_number(height, 1)} mm; hoja "
        f"{_number(page_w, 1)} × {_number(page_h, 1)} mm con márgenes de "
        f"{paper['margin_mm']:g} mm; {'cabe' if paper['fits_a1'] else 'NO cabe'} en A1"
    )


# ----------------------------------------------------------------- dibujo del PNG


def grid_step(extent: float, lines: int = 8) -> float:
    """Paso «redondo» (1, 2 o 5 × 10^k) que da unas ``lines`` líneas en ``extent``."""
    if extent <= 0:
        return 1.0
    raw = extent / lines
    power = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 5, 10):
        if raw <= factor * power:
            return factor * power
    return 10 * power


def _font(size: int):
    """Arial (tiene «×» y las tildes); si no está instalada, la fuente de Pillow."""
    for name in ("arial.ttf", "DejaVuSans.ttf"):
        with contextlib.suppress(OSError):
            return ImageFont.truetype(name, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow antiguo
        return ImageFont.load_default()


def _label(draw: ImageDraw.ImageDraw, xy, text: str, font, anchor: str) -> None:
    box = draw.textbbox(xy, text, font=font, anchor=anchor)
    draw.rectangle((box[0] - 2, box[1] - 1, box[2] + 2, box[3] + 1), fill=(255, 255, 255, 230))
    draw.text(xy, text, fill=LABEL_COLOR, font=font, anchor=anchor)


def compose(
    drawing: Image.Image, cad_to_px, view, zone, footer: str | None, groups=()
) -> Image.Image:
    """Fondo blanco, cuadrícula con coordenadas, el dibujo encima, los grupos de dibujo en
    azul (numerados), la zona en rojo y, si hay, la franja del pie (debajo de la imagen: no
    cambia ``cad_to_px``)."""
    width, height = drawing.size
    a, _b, _c, d, e, f = cad_to_px
    extra = FOOTER_PX if footer else 0
    image = Image.new("RGBA", (width, height + extra), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    step = grid_step(max(view[2] - view[0], view[3] - view[1]))
    decimals = max(0, -math.floor(math.log10(step) + 1e-9))
    font = _font(12)
    xs = [k * step for k in range(math.ceil(view[0] / step), math.floor(view[2] / step) + 1)]
    ys = [k * step for k in range(math.ceil(view[1] / step), math.floor(view[3] / step) + 1)]
    for x in xs:
        px = a * x + e
        draw.line((px, 0, px, height), fill=GRID_COLOR, width=1)
    for y in ys:
        py = d * y + f
        draw.line((0, py, width, py), fill=GRID_COLOR, width=1)
    image.alpha_composite(drawing)
    draw = ImageDraw.Draw(image)
    group_font = _font(14)
    for group in groups:
        x0, y0, x1, y1 = group["box"]
        left, right = a * x0 + e, a * x1 + e
        top, bottom = d * y1 + f, d * y0 + f
        # Un grupo diminuto en la imagen se marca con un cuadrado mínimo para que se vea.
        if right - left < 8:
            middle = (left + right) / 2
            left, right = middle - 4, middle + 4
        if bottom - top < 8:
            middle = (top + bottom) / 2
            top, bottom = middle - 4, middle + 4
        draw.rectangle((left, top, right, bottom), outline=GROUP_COLOR, width=1)
        text = f"G{group['number']}"
        if right + 3 + draw.textlength(text, font=group_font) <= width:
            draw.text((right + 3, top), text, fill=GROUP_COLOR, font=group_font, anchor="lt")
        else:
            draw.text((left - 3, top), text, fill=GROUP_COLOR, font=group_font, anchor="rt")
    if zone is not None:
        left, right = a * zone[0] + e, a * zone[2] + e
        top, bottom = d * zone[3] + f, d * zone[1] + f
        draw.rectangle((left, top, right, bottom), outline=ZONE_COLOR, width=2)
    for x in xs:
        _label(draw, (a * x + e, 2), _number(x, decimals), font, "mt")
    for y in ys:
        _label(draw, (2, d * y + f), _number(y, decimals), font, "lm")
    if footer:
        draw.rectangle((0, height, width, height + extra), fill=(245, 246, 248, 255))
        draw.line((0, height, width, height), fill=GRID_COLOR, width=1)
        draw.text(
            (6, height + extra / 2), footer, fill=(30, 30, 30, 255), font=_font(13), anchor="lm"
        )
    return image


# ----------------------------------------------------------------- orden


def _layer_rows(document, counts: Counter, layers, exclude_layers, reveal) -> list[dict]:
    """Cada capa del DXF: entidades del Model Space en la vista, estado en el CAD y si se ve."""
    rows = []
    for layer in sorted(document.layers, key=lambda item: str(item.dxf.name).casefold()):
        name = str(layer.dxf.name)
        state = layer_state(layer)
        revealed = state is not None and reveal is not None and name.casefold() in reveal
        requested = _visible(name, layers, exclude_layers)
        rows.append(
            {
                "name": name,
                "entities_in_view": counts.get(name, 0),
                "cad_state": state,
                "shown": requested and (state is None or revealed),
            }
        )
    return rows


def _write(path: Path, write) -> None:
    temporary = path.with_name(f".cde-tmp-{os.getpid()}-{path.name}")
    try:
        write(temporary)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def run_preview_modelspace(args) -> int:
    source = Path(args.drawing)
    output = Path(args.output)
    if output.suffix.lower() != ".png":
        raise CadProcessingError("--output debe ser un archivo .png")
    px = int(args.px)
    if not MIN_LONG_SIDE_PX <= px <= MAX_LONG_SIDE_PX:
        raise CadProcessingError(f"--px debe estar entre {MIN_LONG_SIDE_PX} y {MAX_LONG_SIDE_PX}.")
    if not math.isfinite(args.margin_mm) or args.margin_mm < 0:
        raise CadProcessingError("El margen debe ser un número finito mayor o igual que cero")
    denominator = parse_nominal_scale(args.scale) if args.scale else None
    output.parent.mkdir(parents=True, exist_ok=True)
    document, _pruned = block_commands.read_source_dxf(source, output.parent)
    layers, exclude = _patterns(args.layers), _patterns(args.exclude_layers)
    reveal = parse_layer_selection(document, args.include_hidden_layers)
    _check_requested_layers(document, layers, reveal)
    zone, _block = _window(args, document)
    view = _view(args, document, zone)
    # Una sola pasada por las cajas: todo lo que toca la vista; el filtro de capas, después.
    everything = select_view(document, view)
    groups: list[dict] = []
    if view is None:
        if everything.box is None:
            raise CadProcessingError("El Model Space no tiene nada con caja que mostrar.")
        box = everything.box
        extent = max(box[2] - box[0], box[3] - box[1])
        groups = drawing_groups(everything.boxes, extent * GROUP_GAP)
        # Con aire alrededor, para que lo de los bordes (y sus etiquetas) se vea entero.
        pad = max(extent, 1.0) * FULL_VIEW_PAD
        view = (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)
    entities = [
        entity for entity in everything.entities if _visible(_layer_name(entity), layers, exclude)
    ]
    print(
        f"Vista de {source.name}: {len(entities)} de {len(everything.entities)} entidades del "
        "Model Space en la vista"
        + (f" (capas: {', '.join(layers)})" if layers else "")
        + (f" (sin: {', '.join(exclude)})" if exclude else "")
        + "."
    )
    player, omitted = draw_entities(
        document, entities, layers=layers, exclude_layers=exclude, hidden_layers=reveal
    )
    extent = max(view[2] - view[0], view[3] - view[1])
    picture = paint(player, px, extent_hint=extent, frame=view)

    factor, declared = mm_per_unit(document, args.units)
    paper = None
    footer = None
    if denominator is not None and factor is not None:
        what = "Zona" if zone is not None else "Vista"
        paper = paper_size(zone if zone is not None else view, factor, denominator, args.margin_mm)
        footer = paper_text(args.scale, paper, what)
    elif denominator is not None:
        footer = f"Escala {args.scale}: tamaño en la hoja no calculable (unidades sin declarar)"
    image = compose(picture.image, picture.cad_to_px, view, zone, footer, groups)

    data = {
        "format": FORMAT,
        "version": VERSION,
        "source": {"file": source.name},
        "image": {
            "file": output.name,
            "width_px": image.size[0],
            "height_px": image.size[1],
            "footer_px": FOOTER_PX if footer else 0,
        },
        "view": list(view),
        "zone": list(zone) if zone is not None else None,
        "cad_to_px": picture.cad_to_px,
        "units": {"mm_per_unit": factor, "declared": declared},
        "scale": args.scale,
        "paper": paper,
        "layers": _layer_rows(document, everything.layers, layers, exclude, reveal),
        "filters": {
            "layers": list(layers),
            "exclude_layers": list(exclude),
            "include_hidden_layers": sorted(reveal) if reveal is not None else None,
        },
        "entities": {
            "in_view": len(everything.entities),
            "drawn": len(entities),
            "without_bounds": everything.without_bounds,
        },
        "omitted": {kind: count for kind, count in omitted.items() if count},
        "groups": groups,
    }
    _write(output, lambda path: image.save(path, format="PNG"))
    meta = output.with_suffix(".json")
    _write(
        meta,
        lambda path: path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        ),
    )
    print(f"Vista: X {view[0]:.3f} a {view[2]:.3f}, Y {view[1]:.3f} a {view[3]:.3f}.")
    if zone is not None:
        print(f"Zona (en rojo): X {zone[0]:.3f} a {zone[2]:.3f}, Y {zone[1]:.3f} a {zone[3]:.3f}.")
    if footer:
        print(footer + ".")
    if len(groups) > 1:
        print("Grupos de dibujo (en azul; para ver uno, pase su caja a --view):")
        for group in groups:
            x0, y0, x1, y1 = group["box"]
            print(
                f"  G{group['number']}: {group['entities']} entidades; "
                f"--view {x0:.3f} {y0:.3f} {x1:.3f} {y1:.3f}"
            )
    if data["omitted"]:
        print(
            "No dibujado en la vista previa: "
            + ", ".join(f"{kind} {count}" for kind, count in data["omitted"].items())
            + "."
        )
    if everything.without_bounds:
        print(f"Sin caja (no se pueden situar ni dibujar): {everything.without_bounds}.")
    print(f"Escrito: {output} ({image.size[0]} × {image.size[1]} px) y {meta.name}.")
    return 0


__all__ = [
    "compose",
    "draw_entities",
    "drawing_groups",
    "grid_step",
    "hide_layers",
    "mm_per_unit",
    "paper_size",
    "modelspace_boxes",
    "run_preview_modelspace",
    "select_view",
    "zone_candidates",
]
