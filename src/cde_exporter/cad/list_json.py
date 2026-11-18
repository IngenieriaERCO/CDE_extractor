"""``extract-all-blocks --list --json``: el inventario de ``load`` sin trabajador.

Escribe por stdout (UTF-8) un solo documento JSON con el mismo contenido que el ``data`` de
``load`` (docs/protocolo_trabajador.md, sección 11), sin ``session``. Con ``--scale`` o
``--scale-map``, cada bloque lleva además ``scale`` y ``page_size_mm``. Todo lo demás va por
stderr. Un error también sale como JSON por stdout (``{"error": {...}}``) y el código es 1.
"""

from __future__ import annotations

import contextlib
import json
import sys
import tempfile
from pathlib import Path

from cde_exporter.cad import worker_protocol as protocol
from cde_exporter.cad.dxf_session import load_dxf
from cde_exporter.cad.inventory import build_inventory, page_size_mm
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.scale import parse_nominal_scale
from cde_exporter.cad.worker import load_response
from cde_exporter.cad.worker_protocol import WorkerError


def _scales(args, blocks: list[dict]) -> dict[str, str]:
    """Escala de cada nombre de bloque (sin mayúsculas) según --scale y --scale-map."""
    from cde_exporter.cad.project_commands import _scale_text, read_scale_map

    default = _scale_text(args.scale) if args.scale is not None else None
    scale_map = read_scale_map(Path(args.scale_map)) if args.scale_map else {}
    names = {block["name"].casefold() for block in blocks}
    unknown = [name for name in scale_map if name.casefold() not in names]
    if unknown:
        raise WorkerError(
            protocol.BAD_REQUEST,
            "El mapa de escalas nombra bloques que no se exportan: "
            + ", ".join(f"«{name}»" for name in unknown)
            + ".",
        )
    by_name = {name.casefold(): scale for name, scale in scale_map.items()}
    return {name: by_name.get(name, default) for name in names}


def _document(args) -> dict:
    source = Path(args.drawing)
    if not source.is_file():
        raise WorkerError(protocol.DXF_NOT_FOUND, f"No existe el DXF: {source}")
    try:
        loaded = load_dxf(source, Path(tempfile.gettempdir()))
    except MemoryError as failure:
        raise WorkerError(
            protocol.DXF_TOO_LARGE, "No hay memoria suficiente para leer este DXF."
        ) from failure
    except (CadProcessingError, OSError, ValueError) as failure:
        raise WorkerError(protocol.DXF_INVALID, f"No se pudo leer el DXF: {failure}") from failure
    inventory = build_inventory(
        loaded.document,
        units=args.units,
        margin_mm=float(args.margin_mm),
        include_layers=not args.no_layers,
    )
    data = load_response(loaded, inventory)
    if args.scale is not None or args.scale_map:
        try:
            scales = _scales(args, data["blocks"])
        except CadProcessingError as failure:
            raise WorkerError(protocol.BAD_REQUEST, str(failure)) from failure
        for block in data["blocks"]:
            scale = scales.get(block["name"].casefold())
            block["scale"] = scale
            block["page_size_mm"] = (
                page_size_mm(block, parse_nominal_scale(scale), float(args.margin_mm))
                if scale is not None
                else None
            )
    return data


def run_list_json(args) -> int:
    if not args.list:
        print("Error CAD: --json solo se usa con --list.", file=sys.stderr)
        return 1
    output = sys.stdout
    code = 0
    with contextlib.redirect_stdout(sys.stderr):
        try:
            payload = _document(args)
        except WorkerError as failure:
            payload, code = {"error": failure.payload()}, 1
    output.write(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
    output.flush()
    return code
