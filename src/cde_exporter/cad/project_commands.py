"""Orden ``cad extract-all-blocks``: un paquete por bloque del Model Space (un proyecto).

Cada referencia de bloque se exporta con el mismo flujo de ``cad extract-block``
(``block_commands.run_extract_block``) y se publica por su cuenta: si una falla, las
demás se publican igual. Los bloques anónimos, sin definición, XREF o sin nada visible se
omiten con su motivo. Al final se escribe ``resumen_proyecto.json`` en la carpeta.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cde_exporter import __version__
from cde_exporter.cad import block_commands
from cde_exporter.cad.block_extractor import references_for_block
from cde_exporter.cad.fidelity import iter_detail_entities_with_visibility
from cde_exporter.cad.file_names import safe_file_stem, unique_stem
from cde_exporter.cad.hidden_layers import STATE_VISIBLE, loose_summary, parse_layer_selection
from cde_exporter.cad.manifest import MANIFEST_SUFFIX
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.reader import safe_extents
from cde_exporter.cad.scale import parse_nominal_scale, verify_drawing_units

SUMMARY_NAME = "resumen_proyecto.json"
SUMMARY_DOCUMENT_TYPE = "cde_exporter.project_summary"
SUMMARY_SCHEMA_VERSION = "1"

REASON_ANONYMOUS = "bloque anónimo (lo crea AutoCAD para cotas, rayados o bloques dinámicos)"
REASON_NO_DEFINITION = "la referencia apunta a un bloque sin definición en el DXF"
REASON_XREF = "referencia externa (XREF): su contenido no está en este DXF"
REASON_NOTHING_VISIBLE = "sin entidades visibles (vacío, o todo apagado, congelado o invisible)"

_STATUS_TEXT = {"complete": "correcto", "complete_with_warnings": "con advertencias"}
# En la terminal el motivo de un fallo se recorta (ezdxf puede listar miles de entidades);
# resumen_proyecto.json lo guarda completo.
_REASON_TERMINAL_LIMIT = 300


def _short(reason: str) -> str:
    if len(reason) <= _REASON_TERMINAL_LIMIT:
        return reason
    return reason[:_REASON_TERMINAL_LIMIT].rstrip(" ,") + f"… (completo en {SUMMARY_NAME})"


# Entidades que no son geometría propia: un INSERT solo agrupa; un ATTDEF de la definición
# no se dibuja en la referencia (se dibuja su ATTRIB).
_NOT_GEOMETRY = frozenset({"INSERT", "ATTDEF"})


@dataclass(frozen=True, slots=True)
class PlannedPackage:
    """Una referencia que se va a exportar como paquete propio."""

    block_name: str
    occurrence: int
    total_occurrences: int
    handle: str | None
    stem: str
    scale: str | None
    insert: object


@dataclass(frozen=True, slots=True)
class OmittedItem:
    block_name: str
    occurrence: int | None
    total_occurrences: int
    reason: str


@dataclass(frozen=True, slots=True)
class ProjectPlan:
    packages: tuple[PlannedPackage, ...]
    omitted: tuple[OmittedItem, ...]
    exportable_blocks: tuple[str, ...]


def _debug() -> bool:
    return os.environ.get("CDE_DEBUG", "").strip().lower() in {"1", "true", "yes", "si", "sí"}


def _scale_text(value: str) -> str:
    return f"1:{parse_nominal_scale(value):g}"


def read_scale_map(path: Path) -> dict[str, str]:
    """Lee el JSON ``{"bloque": "1:N"}``; devuelve las escalas normalizadas a ``1:N``."""
    try:
        # utf-8-sig: el Bloc de notas y PowerShell pueden guardar el archivo con BOM.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise CadProcessingError(f"No existe el mapa de escalas: {path}") from error
    except json.JSONDecodeError as error:
        raise CadProcessingError(
            f"El mapa de escalas {path.name} no es un JSON válido (línea {error.lineno}, "
            f"columna {error.colno}): {error.msg}"
        ) from error
    if not isinstance(data, dict):
        raise CadProcessingError(
            f'El mapa de escalas {path.name} debe ser un objeto JSON: {{"bloque": "1:N"}}'
        )
    scales: dict[str, str] = {}
    problems = []
    for name, value in data.items():
        if not isinstance(value, str):
            problems.append(f'«{name}»: la escala debe ir entre comillas, por ejemplo "1:50"')
            continue
        try:
            scales[name] = _scale_text(value)
        except ValueError:
            problems.append(f"«{name}»: escala no válida «{value}» (use 1:N, por ejemplo 1:50)")
    if problems:
        raise CadProcessingError(
            f"El mapa de escalas {path.name} tiene errores: " + "; ".join(problems)
        )
    return scales


def _has_visible_content(document, insert) -> bool:
    """Si la referencia dibujaría algo, con las mismas reglas de visibilidad del dibujo."""
    return any(
        item.visible and item.entity.dxftype() not in _NOT_GEOMETRY
        for item in iter_detail_entities_with_visibility(document, roots=[insert])
    )


def _is_xref(layout) -> bool:
    block = layout.block
    return bool(getattr(block, "is_xref", False) or getattr(block, "is_xref_overlay", False))


def plan_project(document, scale_map: dict[str, str], default_scale: str | None) -> ProjectPlan:
    """Clasifica las referencias del Model Space: paquetes que salen y omitidos con motivo.

    El orden es el de la primera referencia de cada bloque en el DXF.
    """
    inserts: dict[str, list] = {}
    for entity in document.modelspace():
        if entity.dxftype() == "INSERT":
            inserts.setdefault(entity.dxf.name, []).append(entity)
    by_casefold = {name.casefold(): scale for name, scale in scale_map.items()}
    unknown = [
        name for name in scale_map if name.casefold() not in {block.casefold() for block in inserts}
    ]
    if unknown:
        raise CadProcessingError(
            "El mapa de escalas nombra bloques que no tienen referencias en el Model Space: "
            + ", ".join(f"«{name}»" for name in unknown)
            + ". Revise que el nombre esté escrito exactamente igual (use --list)."
        )
    used: set[str] = {Path(SUMMARY_NAME).stem.casefold()}
    packages: list[PlannedPackage] = []
    omitted: list[OmittedItem] = []
    exportable: list[str] = []
    for name, entities in inserts.items():
        total = len(entities)
        if name.startswith("*"):
            omitted.append(OmittedItem(name, None, total, REASON_ANONYMOUS))
            continue
        if name not in document.blocks:
            omitted.append(OmittedItem(name, None, total, REASON_NO_DEFINITION))
            continue
        if _is_xref(document.blocks[name]):
            omitted.append(OmittedItem(name, None, total, REASON_XREF))
            continue
        visible = [_has_visible_content(document, entity) for entity in entities]
        if not any(visible):
            omitted.append(OmittedItem(name, None, total, REASON_NOTHING_VISIBLE))
            continue
        exportable.append(name)
        base = unique_stem(safe_file_stem(name), total, used)
        scale = by_casefold.get(name.casefold(), default_scale)
        for ref, entity, shows in zip(
            references_for_block(document, name), entities, visible, strict=True
        ):
            if not shows:
                omitted.append(OmittedItem(name, ref.order, total, REASON_NOTHING_VISIBLE))
                continue
            stem = base if total == 1 else f"{base}_{ref.order:03d}"
            packages.append(PlannedPackage(name, ref.order, total, ref.handle, stem, scale, entity))
    return ProjectPlan(tuple(packages), tuple(omitted), tuple(exportable))


def _label(block_name: str, occurrence: int | None, total: int) -> str:
    if occurrence is None or total <= 1:
        return f"«{block_name}»"
    return f"«{block_name}» (ocurrencia {occurrence} de {total})"


def _package_paths(output_dir: Path, stem: str, keep: bool) -> list[Path]:
    paths = [
        output_dir / f"{stem}.pdf",
        output_dir / f"{stem}.txt",
        output_dir / f"{stem}{MANIFEST_SUFFIX}",
    ]
    if keep:
        paths += [output_dir / f"{stem}.dxf", output_dir / f"{stem}.svg"]
    return paths


def _size_text(document, package: PlannedPackage, units_override: str | None) -> str:
    """Tamaño de la referencia en unidades CAD y en mm a su escala (para --list)."""
    extents, _invalid = safe_extents([package.insert])
    if not extents.has_data:
        return "sin caja medible"
    width = float(extents.extmax.x - extents.extmin.x)
    height = float(extents.extmax.y - extents.extmin.y)
    layout = document.blocks[package.block_name]
    block_extents, _invalid = safe_extents(layout, block=package.block_name)
    max_dimension = (
        max(
            abs(float(block_extents.extmax.x - block_extents.extmin.x)),
            abs(float(block_extents.extmax.y - block_extents.extmin.y)),
            abs(float(block_extents.extmax.z - block_extents.extmin.z)),
        )
        if block_extents.has_data
        else None
    )
    dxf = package.insert.dxf
    verification = verify_drawing_units(
        int(document.header.get("$INSUNITS", 0) or 0),
        int(layout.block_record.dxf.get("units", 0) or 0),
        max_block_dimension=max_dimension,
        insert_scale=(
            float(dxf.get("xscale", 1)),
            float(dxf.get("yscale", 1)),
            float(dxf.get("zscale", 1)),
        ),
        insert_position=tuple(float(value) for value in dxf.insert),
        override=units_override,
    )
    text = f"{width:.3f} × {height:.3f} {verification.unit_name}"
    if package.scale is not None:
        factor = verification.millimeters_per_unit / parse_nominal_scale(package.scale)
        text += f" → {width * factor:.1f} × {height * factor:.1f} mm"
        if not verification.verified:
            text += " (unidades no verificadas)"
    return text


def _print_plan(document, plan: ProjectPlan, args) -> None:
    print(f"Paquetes que se exportarían: {len(plan.packages)}")
    for package in plan.packages:
        scale = package.scale or "(sin escala)"
        print(
            f"  - {_label(package.block_name, package.occurrence, package.total_occurrences)}"
            f" → {package.stem}.pdf | {scale} | "
            f"{_size_text(document, package, args.units)}"
        )
    _print_omitted(plan.omitted)
    _print_loose(loose_summary(document))


def _print_loose(loose: list[dict]) -> None:
    """Lo suelto del Model Space (fuera de cualquier bloque): esta orden no lo exporta."""
    if not loose:
        return
    visible = sum(row["entities"] for row in loose if row["state"] == STATE_VISIBLE)
    hidden = [row for row in loose if row["state"] != STATE_VISIBLE]
    print(
        f"Entidades sueltas del Model Space (fuera de cualquier bloque; no se exportan): "
        f"{visible} visibles, {sum(row['entities'] for row in hidden)} en capas ocultas"
    )
    for row in hidden:
        print(f"  - capa {row['state']} «{row['layer']}»: {row['entities']}")


def _hidden_rows(items) -> list[dict]:
    return [{"layer": item.layer, "state": item.state, "entities": item.count} for item in items]


def _print_omitted(omitted) -> None:
    print(f"Omitidos: {len(omitted)}")
    for item in omitted:
        print(
            f"  - {_label(item.block_name, item.occurrence, item.total_occurrences)}: {item.reason}"
        )


def _write_json_atomic(path: Path, data: dict) -> None:
    """Escribe el JSON en un temporal de la misma carpeta y lo mueve a su sitio."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _write_scale_template(
    path: Path,
    source: Path,
    plan: ProjectPlan,
    scale_map: dict[str, str],
    default: str | None,
    overwrite: bool,
) -> None:
    if path.suffix.lower() != ".json":
        raise CadProcessingError("--write-scale-map debe terminar en .json")
    if path.resolve() == source.resolve():
        raise CadProcessingError("La plantilla no puede sobrescribir el DXF original")
    if path.exists() and not overwrite:
        raise CadProcessingError(
            f"La plantilla ya existe: {path}. Use --overwrite para reemplazarla."
        )
    by_casefold = {name.casefold(): scale for name, scale in scale_map.items()}
    missing = [
        name for name in plan.exportable_blocks if by_casefold.get(name.casefold(), default) is None
    ]
    if missing:
        raise CadProcessingError(
            "Para escribir la plantilla indique la escala por defecto con --scale 1:N."
        )
    template = {name: by_casefold.get(name.casefold(), default) for name in plan.exportable_blocks}
    _write_json_atomic(path, template)
    print(f"Plantilla de escalas: {path} ({len(template)} bloque(s))")


def _block_args(args, package: PlannedPackage, output_dir: Path) -> argparse.Namespace:
    """Argumentos de ``extract-block`` para exportar una referencia del proyecto."""
    values = dict(vars(args))
    values.update(
        cad_command="extract-block",
        block_name=package.block_name,
        occurrence=package.occurrence if package.total_occurrences > 1 else None,
        handle=None,
        all=False,
        output_prefix=str(output_dir / package.stem),
        scale=package.scale,
        scale_factor=None,
    )
    return argparse.Namespace(**values)


def _published_status(output_dir: Path, stem: str) -> str:
    manifest = output_dir / f"{stem}{MANIFEST_SUFFIX}"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    return str(data["validation"]["status"])


def run_extract_all_blocks(args) -> int:
    source, output_dir = Path(args.drawing), Path(args.output_dir)
    if output_dir.exists() and not output_dir.is_dir():
        raise CadProcessingError(f"--output-dir no es una carpeta: {output_dir}")
    default_scale = _scale_text(args.scale) if args.scale is not None else None
    scale_map = read_scale_map(Path(args.scale_map)) if args.scale_map else {}
    document, pruned = block_commands.read_source_dxf(source, output_dir)
    args.source_pruning = pruned  # cada paquete lo declara en su TXT y su manifiesto
    # Una capa pedida con --include-hidden-layers que no existe detiene la orden aquí.
    parse_layer_selection(document, getattr(args, "include_hidden_layers", None))
    plan = plan_project(document, scale_map, default_scale)
    print(f"Proyecto: {source.stem} | DXF: {source.name} | carpeta: {output_dir}")
    if args.list or args.write_scale_map:
        _print_plan(document, plan, args)
        if args.write_scale_map:
            _write_scale_template(
                Path(args.write_scale_map),
                source,
                plan,
                scale_map,
                default_scale,
                args.overwrite,
            )
        return 0
    if not plan.packages:
        _print_omitted(plan.omitted)
        raise CadProcessingError("El DXF no tiene bloques que exportar; no se generaron archivos.")
    without_scale = sorted({p.block_name for p in plan.packages if p.scale is None})
    if without_scale:
        raise CadProcessingError(
            "Falta la escala de: "
            + ", ".join(f"«{name}»" for name in without_scale)
            + ". Indique --scale 1:N (escala por defecto) o inclúyalos en --scale-map. "
            "No se generaron archivos."
        )
    keep = block_commands._keep_intermediates()
    summary_path = output_dir / SUMMARY_NAME
    # Antes de exportar nada: ningún archivo de salida puede pisar el DXF ni, sin
    # --overwrite, un archivo que ya exista.
    for path in [summary_path] + [
        item for package in plan.packages for item in _package_paths(output_dir, package.stem, keep)
    ]:
        if path.resolve() == source.resolve():
            raise CadProcessingError("La salida no puede sobrescribir el DXF original")
        if path.exists() and not args.overwrite:
            raise CadProcessingError(
                f"El archivo de salida ya existe: {path}. Use --overwrite para reemplazarlo. "
                "No se generaron archivos."
            )
    output_dir.mkdir(parents=True, exist_ok=True)
    published: list[dict] = []
    failed: list[dict] = []
    for index, package in enumerate(plan.packages, start=1):
        label = _label(package.block_name, package.occurrence, package.total_occurrences)
        print(f"\n[{index}/{len(plan.packages)}] Bloque {label} → {package.stem} | {package.scale}")
        sys.stdout.flush()
        details: list = []
        try:
            block_commands.run_extract_block(
                _block_args(args, package, output_dir), document, details
            )
            status = _published_status(output_dir, package.stem)
        except KeyboardInterrupt:
            raise
        except (CadProcessingError, OSError, ValueError, MemoryError) as error:
            reason = str(error)
        except Exception as error:  # noqa: BLE001 - un bloque no debe detener a los demás
            if _debug():
                traceback.print_exc()
            reason = f"Error CAD inesperado ({type(error).__name__}): {error}"
        else:
            published.append(_package_row(package, status, details[-1] if details else None))
            continue
        sys.stdout.flush()
        print(f"FALLÓ {label}: {_short(reason)}", file=sys.stderr)
        failed.append({**_reference_row(package), "reason": reason})
    loose = loose_summary(document)
    summary = {
        "document_type": SUMMARY_DOCUMENT_TYPE,
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "project": source.stem,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "exporter_version": __version__,
        "source_dxf": source.name,
        "default_scale": default_scale,
        "scale_map": Path(args.scale_map).name if args.scale_map else None,
        "totals": {
            "packages": len(published),
            "omitted": len(plan.omitted),
            "failed": len(failed),
            "hidden_layer_entities": sum(
                row["entities"] for package in published for row in package["hidden_layers"]
            ),
            "modelspace_loose_entities": sum(row["entities"] for row in loose),
            "modelspace_loose_hidden_entities": sum(
                row["entities"] for row in loose if row["state"] != STATE_VISIBLE
            ),
        },
        "packages": published,
        "omitted": [
            {
                "block_name": item.block_name,
                "occurrence": item.occurrence,
                "total_occurrences": item.total_occurrences,
                "reason": item.reason,
            }
            for item in plan.omitted
        ],
        "failed": failed,
        # Entidades del Model Space fuera de cualquier bloque, por capa y estado de la capa.
        "modelspace_loose": loose,
        # DXF muy grande leído desde una copia sin bloques sin usar (null si no hizo falta).
        "source_copy": None
        if pruned is None
        else {
            "source_bytes": pruned.source_bytes,
            "copy_bytes": pruned.copy_bytes,
            "seconds": round(pruned.seconds, 1),
            "emptied_blocks": [
                {"name": item.name, "entities": item.entities, "bytes": item.bytes}
                for item in pruned.emptied
            ],
        },
    }
    _write_json_atomic(summary_path, summary)
    _print_summary(source, plan, published, failed, summary_path, loose)
    if failed:
        sys.stdout.flush()
        print(
            f"Error CAD: {len(failed)} paquete(s) fallaron y no se publicaron; "
            f"los demás sí (ver {SUMMARY_NAME}).",
            file=sys.stderr,
        )
        return 1
    return 0


def _reference_row(package: PlannedPackage) -> dict:
    return {
        "block_name": package.block_name,
        "occurrence": package.occurrence,
        "total_occurrences": package.total_occurrences,
        "handle": package.handle,
        "file_stem": package.stem,
        "scale": package.scale,
    }


def _package_row(package: PlannedPackage, status: str, detail=None) -> dict:
    return {
        **_reference_row(package),
        "pdf": f"{package.stem}.pdf",
        "txt": f"{package.stem}.txt",
        "manifest": f"{package.stem}{MANIFEST_SUFFIX}",
        "status": status,
        # Capas apagadas o congeladas: lo que no se dibujó del bloque, lo que se dibujó con
        # --include-hidden-layers y lo suelto del Model Space sobre la zona del bloque.
        "hidden_layers": _hidden_rows(detail.hidden_layers if detail else ()),
        "included_hidden_layers": _hidden_rows(detail.included_hidden_layers if detail else ()),
        "modelspace_hidden_overlap": _hidden_rows(
            detail.modelspace_hidden_overlap if detail else ()
        ),
    }


def _hidden_text(row: dict) -> str:
    parts = []
    hidden = sum(item["entities"] for item in row["hidden_layers"])
    if hidden:
        parts.append(f"{hidden} entidades en capas ocultas sin dibujar")
    included = sum(item["entities"] for item in row["included_hidden_layers"])
    if included:
        parts.append(f"{included} de capas ocultas incluidas")
    overlap = sum(item["entities"] for item in row["modelspace_hidden_overlap"])
    if overlap:
        parts.append(f"AVISO: {overlap} sueltas ocultas en el Model Space encima")
    return "".join(f" | {part}" for part in parts)


def _print_summary(
    source: Path, plan: ProjectPlan, published, failed, summary_path, loose=()
) -> None:
    total = len(plan.packages)
    print(f"\n=== Resumen del proyecto «{source.stem}» ===")
    print(f"Paquetes publicados: {len(published)} de {total}")
    for row in published:
        label = _label(row["block_name"], row["occurrence"], row["total_occurrences"])
        status = _STATUS_TEXT.get(row["status"], row["status"])
        print(f"  - {label} → {row['pdf']} | {row['scale']} | {status}{_hidden_text(row)}")
    _print_hidden_layers(published)
    _print_omitted(plan.omitted)
    print(f"Fallidos: {len(failed)}")
    for row in failed:
        label = _label(row["block_name"], row["occurrence"], row["total_occurrences"])
        print(f"  - {label}: {_short(row['reason'])}")
    _print_loose(list(loose))
    print(f"Resumen: {summary_path}")


def _print_hidden_layers(published) -> None:
    """Capas apagadas o congeladas con contenido de los bloques publicados, agrupadas."""
    groups: dict[tuple[str, str], list] = {}
    for row in published:
        label = _label(row["block_name"], row["occurrence"], row["total_occurrences"])
        for item in row["hidden_layers"]:
            group = groups.setdefault((item["layer"], item["state"]), [0, []])
            group[0] += item["entities"]
            group[1].append(label)
    if not groups:
        return
    print("Capas ocultas con contenido de los bloques (no se dibujó; --include-hidden-layers):")
    for (layer, state), (entities, labels) in sorted(groups.items()):
        where = ", ".join(labels[:3]) + (f" y {len(labels) - 3} más" if len(labels) > 3 else "")
        print(f"  - capa {state} «{layer}»: {entities} entidades en {where}")
