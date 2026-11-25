"""Zonas del Model Space en el trabajador (docs/protocolo_trabajador.md, secciones 4.1 y 5.4).

- ``preview`` con ``zone`` (o ``view``): PNG de una zona, de un trozo o de todo el Model Space,
  con el filtro de capas de la exportación (``zone_preview``), en el marco único y con la misma
  convención que la vista previa de un bloque (fondo transparente, ``cad_to_px``).
- ``export`` con trabajos de zona: ``{"zone": [...], "layers": [...], "scale": N,
  "file_stem": ...}``, exportados con el flujo de ``cad extract-modelspace``
  (``modelspace_commands.prepare_modelspace_export``) sobre el DXF ya leído. Sin nombre, se
  genera ``Zona_01``, ``Zona_02``…
- Las cajas rápidas del Model Space (``zone_preview.modelspace_boxes``) se calculan una vez por
  sesión: la primera vista previa o exportación de zona las calcula y las demás las reutilizan.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from cde_exporter.cad import worker_protocol as protocol
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.modelspace_commands import (
    _check_requested_layers,
    _visible,
    prepare_modelspace_export,
)
from cde_exporter.cad.preview import ALWAYS_REPORTED, PreviewImage, paint
from cde_exporter.cad.preview_index import paint_index
from cde_exporter.cad.scale import parse_nominal_scale
from cde_exporter.cad.worker_protocol import WorkerError
from cde_exporter.cad.zone_preview import (
    GROUP_GAP,
    _layer_name,
    _layer_rows,
    boxes_for,
    draw_entities,
    drawing_groups,
    mm_per_unit,
    paper_size,
    select_view,
    zone_candidates,
)

ZONE_NAME_PREFIX = "Zona_"
ZONE_PREVIEW_STAGES = (("boxes", 0.3), ("draw", 0.5), ("paint", 0.2))


@dataclass(frozen=True)
class ZoneRequest:
    """Zona pedida (coordenadas CAD del Model Space) y capas que se ven."""

    zone: tuple[float, float, float, float] | None
    view: tuple[float, float, float, float] | None
    layers: tuple[str, ...]
    exclude_layers: tuple[str, ...]


def _bad(message: str, **details) -> WorkerError:
    return WorkerError(protocol.BAD_REQUEST, message, details=details)


def parse_box(value, name: str) -> tuple[float, float, float, float] | None:
    """``[xmin, ymin, xmax, ymax]`` en coordenadas CAD (null: sin caja)."""
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value)
        or not all(math.isfinite(item) for item in value)
    ):
        raise _bad(f"«{name}» debe ser [xmin, ymin, xmax, ymax] con cuatro números.", key=name)
    xmin, ymin, xmax, ymax = (float(item) for item in value)
    if not (xmin < xmax and ymin < ymax):
        raise _bad(f"«{name}» necesita xmin < xmax e ymin < ymax.", key=name)
    return (xmin, ymin, xmax, ymax)


def parse_layers(value, name: str) -> tuple[str, ...]:
    """Lista de capas (admite ``*`` y ``?``, sin distinguir mayúsculas); null o [] = sin
    filtro."""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise _bad(f"«{name}» debe ser null o una lista de nombres de capa.", key=name)
    if any("," in item for item in value):
        raise _bad(f"Un nombre de capa de «{name}» no puede llevar comas.", key=name)
    return tuple(item.strip() for item in value)


def parse_scale(value, where: str) -> str:
    """Escala como ``"1:N"`` o como el número N."""
    if isinstance(value, bool):
        value = None
    if isinstance(value, (int, float)):
        text = f"1:{value:g}" if math.isfinite(value) else ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        text = ""
    try:
        if not text:
            raise ValueError('falta la escala (N o "1:N")')
        parse_nominal_scale(text)
    except (CadProcessingError, ValueError) as failure:
        raise _bad(f"Escala no válida en {where}: {failure}", scale=value) from failure
    return text


def zone_request(data: dict, document, reveal: frozenset[str] | None) -> ZoneRequest:
    """Zona, vista y capas de un pedido, comprobadas contra el DXF cargado. ``reveal``: las
    capas ocultas pedidas (``worker.hidden_layer_selection``), o None."""
    request = ZoneRequest(
        parse_box(data.get("zone"), "zone"),
        parse_box(data.get("view"), "view"),
        parse_layers(data.get("layers"), "layers"),
        parse_layers(data.get("exclude_layers"), "exclude_layers"),
    )
    try:
        _check_requested_layers(document, request.layers, reveal)
    except CadProcessingError as failure:
        raise _bad(str(failure), layers=list(request.layers)) from failure
    return request


def session_boxes(session, check=None, progress=None) -> list:
    """Cajas rápidas del Model Space de la sesión (se calculan la primera vez)."""
    if session.modelspace_boxes is None:
        if progress is not None:
            progress("boxes", 0.0, "Calculando las cajas del Model Space")
        boxes_for(session, session.loaded.document, check)
        if progress is not None:
            progress("boxes", 1.0, "Cajas del Model Space calculadas")
    return session.modelspace_boxes


# ----------------------------------------------------------------- vista previa


def preview_key(request: ZoneRequest) -> str:
    """Parte del nombre en la caché: ``ms-`` y 10 cifras del SHA-1 de zona, vista y capas."""
    text = json.dumps(
        {
            "zone": request.zone,
            "view": request.view,
            "layers": sorted(item.casefold() for item in request.layers),
            "exclude": sorted(item.casefold() for item in request.exclude_layers),
        },
        sort_keys=True,
    )
    return "ms-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def zone_paper(session, request: ZoneRequest, scale: str | None, frame) -> dict | None:
    """Tamaño en la hoja de la zona (o de la vista) a la escala pedida; None sin escala o
    sin unidades."""
    if scale is None:
        return None
    factor, _declared = mm_per_unit(session.loaded.document, session.units)
    if factor is None or frame is None:
        return None
    box = request.zone or frame
    return paper_size(box, factor, parse_nominal_scale(scale), session.margin_mm)


def draw_zone_preview(
    session,
    request: ZoneRequest,
    long_side: int,
    *,
    hidden_layers,
    plot_style,
    print_transparency: bool,
    check,
    progress,
    index=None,
) -> tuple[object, dict]:
    """Dibuja la vista previa de una zona. Devuelve (imagen, datos de la respuesta).

    Con ``index`` (la vista previa preparada, ``preview_index``), la imagen se pinta desde él
    en una fracción de segundo, con menos detalle de lejos; sin él, se dibuja con ezdxf lo que
    toca la vista (``prepared: false``)."""
    document = session.loaded.document
    boxes = session_boxes(session, check, progress)
    frame = request.view or request.zone
    everything = select_view(document, frame, boxes)
    groups: list[dict] = []
    if frame is None:
        frame = everything.box
        if frame is not None:
            extent = max(frame[2] - frame[0], frame[3] - frame[1])
            groups = drawing_groups(everything.boxes, extent * GROUP_GAP)
    entities = [
        entity
        for entity in everything.entities
        if _visible(_layer_name(entity), request.layers, request.exclude_layers)
    ]
    lod = None
    if index is not None:
        # Desde la vista previa preparada: lo omitido es el de todo el Model Space.
        omitted = {kind: 0 for kind in ALWAYS_REPORTED} | dict(index.omitted)
        if frame is None:
            picture = PreviewImage(Image.new("RGBA", (1, 1), (0, 0, 0, 0)), None, None)
            stats = {"items": 0, "dots": 0}
        else:
            picture, stats = paint_index(
                index,
                frame,
                long_side,
                lambda layer: _visible(layer, request.layers, request.exclude_layers),
                check,
            )
        lod = {"strokes": stats["items"], "as_dots": stats["dots"]}
    else:
        player, omitted = draw_entities(
            document,
            entities,
            layers=request.layers,
            exclude_layers=request.exclude_layers,
            hidden_layers=hidden_layers,
            check=check,
            progress=progress,
            plot_style=plot_style,
            print_transparency=print_transparency,
        )
        check()
        extent = None if frame is None else max(frame[2] - frame[0], frame[3] - frame[1])
        picture = paint(player, long_side, check, progress, extent, frame=frame)
    data = {
        "ref": None,
        "handle": None,
        "kind": "zone",
        "px": list(picture.image.size),
        "bounds": None
        if picture.bounds is None
        else {
            "min": [round(value, 6) for value in picture.bounds[0]],
            "max": [round(value, 6) for value in picture.bounds[1]],
        },
        "cad_to_px": picture.cad_to_px,
        "zone": None if request.zone is None else list(request.zone),
        "view": None if frame is None else [round(value, 6) for value in frame],
        "layers": list(request.layers),
        "exclude_layers": list(request.exclude_layers),
        "layer_rows": _layer_rows(
            document, everything.layers, request.layers, request.exclude_layers, hidden_layers
        ),
        "entities": {
            "in_view": len(everything.entities),
            "drawn": len(entities),
            "without_bounds": everything.without_bounds,
        },
        "groups": groups,
        "omitted": omitted,
        "prepared": index is not None,
        "lod": lod,
    }
    return picture.image, data


# ----------------------------------------------------------------- exportación


def generated_names(count: int, taken: set[str], exists) -> list[str]:
    """``count`` nombres ``Zona_01``, ``Zona_02``… que no estén en ``taken`` (sin mayúsculas)
    ni tengan ya archivos en la carpeta de salida (``exists(nombre)``)."""
    names: list[str] = []
    number = 0
    while len(names) < count:
        number += 1
        name = f"{ZONE_NAME_PREFIX}{number:02d}"
        if name.casefold() in taken or exists(name):
            continue
        names.append(name)
        taken.add(name.casefold())
    return names


def check_zone_has_content(session, zone: dict, check=None) -> None:
    """BAD_REQUEST antes de exportar nada si en la zona no hay nada de las capas que se ven
    (según la caja rápida)."""
    boxes = session_boxes(session, check)
    for entity in zone_candidates(boxes, zone["zone"]):
        if _visible(_layer_name(entity), zone["layers"], zone["exclude_layers"]):
            return
    raise _bad(
        "En la zona no hay nada de las capas que se ven.",
        zone=list(zone["zone"]),
        layers=list(zone["layers"]),
        exclude_layers=list(zone["exclude_layers"]),
    )


def zone_args(args, zone: dict):
    """Completa los argumentos de ``extract-modelspace`` con la zona y las capas."""
    args.window = list(zone["zone"])
    args.layers = ",".join(zone["layers"]) or None
    args.exclude_layers = ",".join(zone["exclude_layers"]) or None
    return args


def prepare_zone(session, args, zone: dict, check=None):
    """Elige, copia y recorta la zona en una copia de trabajo, sin releer el DXF; la selección
    exacta solo mira lo que la caja rápida sitúa en la zona. Devuelve (argumentos de
    ``extract-block``, copia de trabajo)."""
    candidates = zone_candidates(session_boxes(session, check), zone["zone"])
    prepared = prepare_modelspace_export(
        args, session.loaded.document, session.loaded.pruned, candidates
    )
    if prepared is None:  # solo con --list, que el trabajador no usa
        raise CadProcessingError("La zona no se pudo preparar.")
    return prepared


def output_exists(output_dir: Path, outputs):
    """Para ``generated_names``: si algún archivo del paquete ya existe."""

    def exists(name: str) -> bool:
        return any(path.exists() for path in outputs(output_dir, name))

    return exists
