"""Comandos CLI para catálogo y extracción por bloque."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import ezdxf
from ezdxf import units

from cde_exporter import __version__
from cde_exporter.cad import run_hooks
from cde_exporter.cad.block_catalog import format_block_catalog, list_referenced_blocks
from cde_exporter.cad.block_extractor import (
    BlockReference,
    _definition_has_unbounded,
    extract_block_reference,
    references_for_block,
)
from cde_exporter.cad.detail_builder import build_extracted_detail
from cde_exporter.cad.detail_extras import (
    font_uses,
    hatch_summary,
    nested_blocks,
    transformation_steps,
)
from cde_exporter.cad.detail_model import (
    DRAWING_SCALE_CHECK,
    FOOTPRINT_REASON,
    EntityStatus,
    ExtractionWarning,
    SelectionMethod,
    Severity,
    degraded_hatches_message,
    finalize_detail,
    mark_degraded_hatches,
    mark_footprint_solids,
    mark_omitted_entities,
)
from cde_exporter.cad.detail_text import format_detail_report
from cde_exporter.cad.dxf_session import make_temporary
from cde_exporter.cad.exporters import export_vector_outputs, instancing_enabled, xobjects_enabled
from cde_exporter.cad.fidelity import (
    hidden_skip_events,
    iter_detail_entities,
    print_fidelity_summary,
    write_fidelity_report,
)
from cde_exporter.cad.font_config import configure_fonts
from cde_exporter.cad.hidden_layers import (
    hidden_layer_counts,
    hidden_overlap,
    option_value,
    parse_layer_selection,
    reveal_layers,
)
from cde_exporter.cad.large_dxf import (
    is_large,
    prune_unused_blocks,
    pruning_message,
    pruning_warning,
)
from cde_exporter.cad.manifest import (
    MANIFEST_SUFFIX,
    PdfFacts,
    RunOptions,
    build_manifest,
    validate_manifest,
)
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.notices import (
    NoticeSeverity,
    blocking_message,
    blocking_notices,
    collect_notices,
)
from cde_exporter.cad.pdf_checks import (
    PdfValidationError,
    advisory_warnings,
    build_extraction_result,
    check_manifest_coherence,
    pdf_page_facts,
    validate_output_pdf,
)
from cde_exporter.cad.reader import InvalidEntity, entity_bounds, read_dxf, safe_extents
from cde_exporter.cad.scale import (
    output_factor,
    parse_nominal_scale,
    verify_drawing_units,
)
from cde_exporter.cad.solids import FootprintResult, footprint_document


def _is_interactive() -> bool:
    """Indica si hay una persona en la terminal que pueda responder una pregunta."""
    stdin = sys.stdin
    return stdin is not None and stdin.isatty()


def _prompt_nominal_scale(args, document, references, selected) -> float:
    """Pide la escala nominal de salida una vez identificada la referencia."""
    ref = selected[0]
    block_definition = document.blocks[args.block_name]
    block_units_code = int(block_definition.block_record.dxf.get("units", 0) or 0)
    # Las entidades que no se pueden medir se avisan después, en el flujo principal.
    block_extents, _invalid = safe_extents(block_definition, block=args.block_name)
    max_block_dimension = (
        max(
            abs(float(block_extents.extmax.x - block_extents.extmin.x)),
            abs(float(block_extents.extmax.y - block_extents.extmin.y)),
            abs(float(block_extents.extmax.z - block_extents.extmin.z)),
        )
        if block_extents.has_data
        else None
    )
    verification = verify_drawing_units(
        int(document.header.get("$INSUNITS", 0) or 0),
        block_units_code,
        max_block_dimension=max_block_dimension,
        insert_scale=(ref.xscale, ref.yscale, ref.zscale),
        insert_position=ref.insert,
        override=args.units,
    )
    print(
        f"Referencia: ocurrencia {ref.order} de {len(references)} | handle "
        f"{ref.handle or '(sin handle)'} | capa {ref.layer}"
    )
    if len(selected) > 1:
        print(f"La misma escala se aplicará a las {len(selected)} referencias seleccionadas.")
    print(f"Unidades: {verification.unit_name} ({verification.status})")
    if not verification.verified:
        print(
            "ADVERTENCIA: unidades no verificadas; la escala física NO quedará garantizada. "
            "Puede indicar las unidades con --units (mm, cm, m, in, ft).",
            file=sys.stderr,
        )
    entities = [
        entity
        for entity in document.modelspace()
        if entity.dxftype() == "INSERT" and entity.dxf.name == args.block_name
    ]
    extents, _invalid = safe_extents([entities[ref.order - 1]])
    if extents.has_data:
        width = float(extents.extmax.x - extents.extmin.x)
        height = float(extents.extmax.y - extents.extmin.y)
        print(f"Tamaño del detalle: {width:.3f} × {height:.3f} unidades CAD")
        factor = verification.millimeters_per_unit
        examples = "; ".join(
            f"1:{n} = {width * factor / n:.1f} × {height * factor / n:.1f} mm"
            for n in (20, 50, 100, 500)
        )
        print(f"Medida física del detalle según la escala: {examples}")
    while True:
        try:
            answer = input(
                "Escala nominal de salida (formato 1:N, por ejemplo 1:20, 1:50, 1:100): "
            )
        except EOFError as error:
            raise CadProcessingError("No se indicó la escala nominal; use --scale 1:N.") from error
        try:
            return parse_nominal_scale(answer.strip())
        except ValueError:
            print(
                "Escala no válida. Escriba la escala como 1:N, con N mayor que cero "
                "(por ejemplo 1:50)."
            )


def run_blocks(args) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    rows = list_referenced_blocks(
        args.drawing,
        include_anonymous=args.include_anonymous,
        filter_text=args.filter or "",
    )
    content = format_block_catalog(rows, include_handles=args.handles)
    if args.output:
        destination, source = Path(args.output), Path(args.drawing)
        if destination.resolve() == source.resolve():
            raise CadProcessingError("El informe no puede sobrescribir el DXF de origen")
        if destination.exists():
            raise CadProcessingError(f"El archivo de salida ya existe: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8", newline="\n")
        print(f"Informe UTF-8: {destination}")
    else:
        sys.stdout.write(content)
    return 0


def _legacy_format_references(references: tuple[BlockReference, ...]) -> str:
    return "\n".join(
        f"  ocurrencia {ref.order} | handle {ref.handle or '(sin handle)'} | capa {ref.layer} | "
        f"rotación {ref.rotation:g}° | escala ({ref.xscale:g}, {ref.yscale:g}, {ref.zscale:g}) | "
        f"inserción {', '.join(f'{value:g}' for value in ref.insert)}"
        for ref in references
    )


def _format_insert(point: tuple[float, float, float]) -> str:
    return ", ".join(f"{value:.3f}" for value in point)


def _format_references(references: tuple[BlockReference, ...]) -> str:
    return "\n".join(
        f"  ocurrencia {ref.order} | handle {ref.handle or '(sin handle)'} | capa {ref.layer} | "
        f"punto de inserci\u00f3n ({_format_insert(ref.insert)}) | "
        f"rotaci\u00f3n {ref.rotation:g} | escala "
        f"({ref.xscale:g}, {ref.yscale:g}, {ref.zscale:g})"
        for ref in references
    )


def _select_references(args, references: tuple[BlockReference, ...]) -> tuple[BlockReference, ...]:
    if args.all:
        return references
    if args.occurrence is not None:
        if args.occurrence < 1 or args.occurrence > len(references):
            raise CadProcessingError(
                f"La ocurrencia {args.occurrence} no existe (hay {len(references)} referencia(s))."
            )
        return (references[args.occurrence - 1],)
    if args.handle is not None:
        matches = tuple(
            ref for ref in references if (ref.handle or "").casefold() == args.handle.casefold()
        )
        if len(matches) == 1:
            return matches
        if not matches:
            raise CadProcessingError(f"No hay referencias con handle '{args.handle}'.")
        raise CadProcessingError(
            f"Handle '{args.handle}' duplicado; elige --occurrence N. Referencias:\n"
            + _format_references(matches)
        )
    if len(references) == 1:
        return references
    raise CadProcessingError(
        f"El bloque tiene {len(references)} referencias; no se extrajo ninguna. "
        f"Usa --occurrence N para elegir una. Referencias:\n{_format_references(references)}"
    )


def _fit_parameters(
    args,
    source: Path,
    block_name: str,
    occurrence: int,
    extracted,
    bounds_xyz=None,
    source_document=None,
) -> tuple[float | None, tuple[float, float] | None, bool]:
    """``bounds_xyz`` (caja ya calculada del documento que se dibuja) sustituye a la caja
    de la referencia en el DXF de origen; se usa con --solids footprint."""
    if args.scale_factor is not None:
        return args.scale_factor, tuple(args.page_size_mm) if args.page_size_mm else None, False
    if bounds_xyz is not None:
        bounds = (bounds_xyz[0][0], bounds_xyz[0][1], bounds_xyz[1][0], bounds_xyz[1][1])
    else:
        document = source_document if source_document is not None else read_dxf(source)
        references = [
            entity
            for entity in document.modelspace()
            if entity.dxftype() == "INSERT" and entity.dxf.name == block_name
        ]
        bounds = entity_bounds(references[occurrence - 1])
    if bounds is None:
        return None, tuple(args.page_size_mm) if args.page_size_mm else None, True
    width, height = max(bounds[2] - bounds[0], 1e-9), max(bounds[3] - bounds[1], 1e-9)
    units_code = extracted.units_code
    verified = units_code != 0
    mm_per_unit = (
        units.conversion_factor(units_code, ezdxf.units.InsertUnits.Millimeters)
        if verified
        else 1.0
    )
    if args.page_size_mm:
        page = tuple(args.page_size_mm)
        scale = min(
            (page[0] - 2 * args.margin_mm) / width,
            (page[1] - 2 * args.margin_mm) / height,
        )
    else:
        scale = mm_per_unit
        page = (width * scale + 2 * args.margin_mm, height * scale + 2 * args.margin_mm)
    if scale <= 0:
        raise CadProcessingError("La geometría no cabe en la página con el margen indicado")
    return scale, page, not verified


def _nominal_scale_parameters(args, bounds_xyz, verification, denominator: float):
    """Calcula dimensiones desde la caja efectiva; INSERT anidados están transformados.

    ``bounds_xyz`` es la caja ya calculada con ``_effective_bounds_xyz`` sobre el
    documento que se dibuja, para no recorrer la geometría otra vez.
    """
    if bounds_xyz is None:
        raise CadProcessingError("No se pudo calcular la caja geométrica del detalle")
    box = (bounds_xyz[0][0], bounds_xyz[0][1], bounds_xyz[1][0], bounds_xyz[1][1])
    width, height = box[2] - box[0], box[3] - box[1]
    factor = verification.millimeters_per_unit / denominator
    content = width * factor, height * factor
    required = content[0] + 2 * args.margin_mm, content[1] + 2 * args.margin_mm
    page = tuple(args.page_size_mm) if args.page_size_mm else required
    if page[0] + 1e-8 < required[0] or page[1] + 1e-8 < required[1]:
        raise CadProcessingError(
            f"El detalle necesita una página de {required[0]:.3f} × {required[1]:.3f} mm "
            f"con márgenes; se indicó {page[0]:.3f} × {page[1]:.3f} mm. "
            "No se generaron archivos."
        )
    return factor, page, box, content


def _validate_nominal_page_sizes(
    args, document, references, selected, block_units_code, max_block_dimension, denominator
) -> None:
    if not args.page_size_mm:
        return
    insert_entities = [
        entity
        for entity in document.modelspace()
        if entity.dxftype() == "INSERT" and entity.dxf.name == args.block_name
    ]
    drawing_units = int(document.header.get("$INSUNITS", 0) or 0)
    for ref in selected:
        verification = verify_drawing_units(
            drawing_units,
            block_units_code,
            max_block_dimension=max_block_dimension,
            insert_scale=(ref.xscale, ref.yscale, ref.zscale),
            insert_position=ref.insert,
            override=args.units,
        )
        extents, _invalid = safe_extents([insert_entities[ref.order - 1]])
        if not extents.has_data:
            continue
        width = float(extents.extmax.x - extents.extmin.x)
        height = float(extents.extmax.y - extents.extmin.y)
        factor = verification.millimeters_per_unit / denominator
        required = (
            width * factor + 2 * args.margin_mm,
            height * factor + 2 * args.margin_mm,
        )
        page = tuple(args.page_size_mm)
        if page[0] + 1e-8 < required[0] or page[1] + 1e-8 < required[1]:
            raise CadProcessingError(
                f"El detalle necesita una página de {required[0]:.3f} × {required[1]:.3f} mm "
                f"con márgenes; se indicó {page[0]:.3f} × {page[1]:.3f} mm. "
                "No se generaron archivos."
            )


def _effective_bounds_xyz(document):
    """Caja efectiva (mínimo y máximo, con Z) del Model Space del DXF extraído.

    Devuelve también las entidades que no se pudieron medir por datos inválidos.
    """
    extents, invalid = safe_extents(document.modelspace())
    if not extents.has_data:
        return None, invalid
    return (
        (float(extents.extmin.x), float(extents.extmin.y), float(extents.extmin.z)),
        (float(extents.extmax.x), float(extents.extmax.y), float(extents.extmax.z)),
    ), invalid


def _keep_intermediates() -> bool:
    """Opción de desarrollo: conserva el DXF extraído y el SVG junto al PDF y al TXT."""
    value = os.environ.get("CDE_KEEP_INTERMEDIATES", "")
    return value.strip().lower() in {"1", "true", "yes", "si", "sí"}


def _move_file(source: Path, destination: Path) -> None:
    try:
        os.replace(source, destination)
    except OSError:
        shutil.copy2(source, destination)
        source.unlink(missing_ok=True)


def _publish_files(pairs: list[tuple[Path, Path]], overwrite: bool) -> None:
    """Mueve los archivos preparados a su destino sin dejar un resultado a medias.

    Si algún movimiento falla, se retiran los archivos ya publicados y se restauran
    los que había antes (cuando se usa --overwrite).
    """
    published: list[Path] = []
    backups: dict[Path, Path] = {}
    try:
        for staged, destination in pairs:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if not overwrite:
                    raise CadProcessingError(
                        f"El archivo de salida ya existe: {destination}. "
                        "Usa --overwrite para reemplazarlo."
                    )
                backup = staged.with_name(staged.name + ".previo")
                _move_file(destination, backup)
                backups[destination] = backup
            _move_file(staged, destination)
            published.append(destination)
    except BaseException:
        for destination in published:
            destination.unlink(missing_ok=True)
        for destination, backup in backups.items():
            if backup.exists():
                _move_file(backup, destination)
        raise


def read_source_dxf(source: Path, work_parent: Path):
    """Lee el DXF de origen. Si es muy grande (``large_dxf.is_large``), lo lee desde una copia
    temporal sin el contenido de los bloques que no se usan, hecha por partes dentro de
    ``work_parent`` (la carpeta de salida) y borrada en cuanto se ha leído. El DXF de origen no
    se modifica. Devuelve el documento y el informe de la copia (None si no se hizo).
    """
    if not is_large(source):
        return read_dxf(source), None
    created = not work_parent.exists()
    work_parent.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(dir=work_parent))
    try:
        copy = folder / f"{source.stem}_sin_bloques_sin_usar.dxf"
        print(
            f"DXF grande ({source.stat().st_size / 1_000_000:.1f} MB): se prepara una copia "
            "temporal sin los bloques que no se usan…"
        )
        sys.stdout.flush()
        report = prune_unused_blocks(source, copy)
        if not report.emptied:
            return read_dxf(source), None
        print(pruning_message(report))
        return read_dxf(copy), report
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        if created:
            with contextlib.suppress(OSError):
                work_parent.rmdir()  # solo si quedó vacía (p. ej. con --list)


def run_extract_block(args, document=None, details: list | None = None) -> int:
    """``document`` permite reutilizar el DXF de origen ya leído (extract-all-blocks lo lee
    una sola vez para todos los bloques); no se modifica. Si se pasa ``details``, se le
    añade el detalle finalizado de cada referencia publicada."""
    font_restorers: list = []
    staging: list[Path] = []
    try:
        return _run_extract_block(args, font_restorers, staging, document, details)
    finally:
        for restore in font_restorers:
            restore()
        for folder in staging:
            shutil.rmtree(folder, ignore_errors=True)


@contextlib.contextmanager
def _staging_folder(parent: Path, staging: list[Path]):
    """Carpeta de trabajo de una referencia, dentro de la carpeta de salida.

    No se borra al salir del bloque, sino al terminar la orden (``run_extract_block``): con
    --all, los archivos de todas las referencias esperan ahí hasta publicarse juntos.
    """
    folder = make_temporary(parent)  # «.cde-tmp-…» con el PID que la creó
    staging.append(folder)
    yield str(folder)


def _outputs(prefix: Path, ref, all_references: bool, keep: bool) -> dict[str, Path]:
    """Archivos que publica una referencia: PDF, TXT, manifiesto y, en desarrollo, DXF y SVG."""
    order_prefix = prefix.with_name(f"{prefix.name}_{ref.order:03d}") if all_references else prefix
    outputs = {
        "pdf": order_prefix.with_suffix(".pdf"),
        "txt": order_prefix.with_suffix(".txt"),
        "manifest": order_prefix.with_name(f"{order_prefix.name}{MANIFEST_SUFFIX}"),
    }
    if keep:
        outputs["dxf"] = order_prefix.with_suffix(".dxf")
        outputs["svg"] = order_prefix.with_suffix(".svg")
    return outputs


# Avisos de capas ocultas que se repiten en la terminal: (código, ¿por stderr?).
_HIDDEN_TERMINAL_CODES = {
    "HIDDEN_LAYERS_CONTENT": False,
    "HIDDEN_LAYERS_INCLUDED": False,
    "MODELSPACE_HIDDEN_OVERLAP": True,
}


def _run_extract_block(
    args, font_restorers: list, staging: list[Path], document=None, details: list | None = None
) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    source, prefix = Path(args.drawing), Path(args.output_prefix)
    if args.scale is not None and args.scale_factor is not None:
        raise CadProcessingError("No combines --scale con --scale-factor")
    nominal_denominator = parse_nominal_scale(args.scale) if args.scale is not None else None
    if prefix.suffix:
        raise CadProcessingError("--output-prefix debe ser una ruta base sin extensión")
    if args.all and (args.handle is not None or args.occurrence is not None):
        raise CadProcessingError("--all no se combina con --occurrence ni --handle")
    pruned = getattr(args, "source_pruning", None)
    if document is None:
        document, pruned = read_source_dxf(source, prefix.parent)
    if args.block_name not in document.blocks:
        from cde_exporter.cad.block_extractor import _similar_names

        raise CadProcessingError(
            f"No existe el bloque '{args.block_name}'.{_similar_names(document, args.block_name)}"
        )
    # Capas que se dibujan aunque estén ocultas (solo en la copia de trabajo).
    reveal = parse_layer_selection(
        document,
        getattr(args, "include_hidden_layers", None),
        strict=getattr(args, "modelspace_selection", None) is None,
    )
    references = references_for_block(document, args.block_name)
    if not references:
        raise CadProcessingError(
            f"El bloque '{args.block_name}' no tiene referencias en modelspace"
        )
    selected = _select_references(args, references)
    if nominal_denominator is None and args.scale_factor is None:
        if _is_interactive():
            nominal_denominator = _prompt_nominal_scale(args, document, references, selected)
        else:
            print(
                "Aviso: no se indicó --scale; el PDF no tendrá una escala nominal. "
                "Use --scale 1:N para fijarla.",
                file=sys.stderr,
            )
    block_definition = document.blocks[args.block_name]
    block_definition_handle = str(block_definition.block_record.dxf.handle)
    if args.all:
        selection_method = SelectionMethod.ALL
    elif args.occurrence is not None:
        selection_method = SelectionMethod.OCCURRENCE
    elif args.handle is not None:
        selection_method = SelectionMethod.HANDLE
    else:
        selection_method = SelectionMethod.AUTOMATIC
    block_units_code = int(block_definition.block_record.dxf.get("units", 0) or 0)
    # Etapa «extract» (run_hooks): la caja del bloque, que puede tardar, y la extracción.
    run_hooks.stage("extract")
    block_extents, source_invalid = safe_extents(block_definition, block=args.block_name)
    max_block_dimension = (
        max(
            abs(float(block_extents.extmax.x - block_extents.extmin.x)),
            abs(float(block_extents.extmax.y - block_extents.extmin.y)),
            abs(float(block_extents.extmax.z - block_extents.extmin.z)),
        )
        if block_extents.has_data
        else None
    )
    if nominal_denominator is not None:
        _validate_nominal_page_sizes(
            args,
            document,
            references,
            selected,
            block_units_code,
            max_block_dimension,
            nominal_denominator,
        )
    keep = _keep_intermediates()
    # Con --all, todo o nada: se comprueban los destinos de todas las referencias antes de
    # preparar ninguna, y se publica cuando todas están preparadas.
    for ref in selected:
        for output in _outputs(prefix, ref, args.all, keep).values():
            if output.resolve() == source.resolve():
                raise CadProcessingError("La salida no puede sobrescribir el DXF original")
            if output.exists() and not args.overwrite:
                raise CadProcessingError(
                    f"El archivo de salida ya existe: {output}. Usa --overwrite para reemplazarlo."
                )
    staged_pairs: list[tuple[Path, Path]] = []
    staged_manifests: list[tuple[Path, Path]] = []
    after_publish: list[tuple[str, bool]] = []
    for ref in selected:
        order_prefix = prefix.with_name(f"{prefix.name}_{ref.order:03d}") if args.all else prefix
        destinations = _outputs(prefix, ref, args.all, keep)
        pdf_destination = destinations["pdf"]
        txt_destination = destinations["txt"]
        manifest_destination = destinations["manifest"]
        dxf_output = order_prefix.with_suffix(".dxf")
        svg_output = order_prefix.with_suffix(".svg")
        prefix.parent.mkdir(parents=True, exist_ok=True)
        # La carpeta de trabajo dura hasta el final de la orden (se borra en run_extract_block).
        with _staging_folder(prefix.parent, staging) as temp_name:
            work = Path(temp_name)
            temp_dxf = work / f"{order_prefix.name}.dxf"
            temp_fidelity = work / "fidelidad.txt"
            temp_svg = work / f"{order_prefix.name}.svg"
            temp_pdf = work / f"{order_prefix.name}.pdf"
            temp_report = work / f"{order_prefix.name}.txt"
            temp_manifest = work / manifest_destination.name
            result = extract_block_reference(
                source,
                args.block_name,
                references.index(ref) + 1,
                temp_dxf,
                overwrite=True,
                document=document,
            )
            extracted_document = read_dxf(temp_dxf)
            hidden, included = _hidden_layers(extracted_document, temp_dxf, reveal)
            if not font_restorers:
                # Las fuentes se configuran ya validados el bloque y la escala, y solo con
                # el detalle extraído; todas las referencias comparten la misma definición.
                font_restorers.append(
                    configure_fonts(
                        args.font_substitutions,
                        args.font_folder,
                        extracted_document,
                        args.font_fallback,
                    )
                )
            run_hooks.stage("analyze")
            fidelity_report = write_fidelity_report(
                temp_dxf,
                temp_fidelity,
                args.hatch_mode,
                overwrite=True,
                hatch_threshold=args.hatch_threshold,
            )
            print_fidelity_summary(fidelity_report, txt_destination)
            scale_note = ""
            content_size = None
            geometry_page_size = None
            if nominal_denominator is not None:
                verification = verify_drawing_units(
                    result.units_code,
                    block_units_code,
                    max_block_dimension=max_block_dimension,
                    insert_scale=(ref.xscale, ref.yscale, ref.zscale),
                    insert_position=ref.insert,
                    override=args.units,
                )
                factors = tuple(
                    output_factor(
                        verification.millimeters_per_unit, nominal_denominator, abs(axis_scale)
                    )
                    for axis_scale in (ref.xscale, ref.yscale, ref.zscale)
                )
                factor_text = (
                    f"{factors[0]:g} mm/unidad CAD"
                    if factors[0] == factors[1] == factors[2]
                    else f"X={factors[0]:g}, Y={factors[1]:g}, Z={factors[2]:g} mm/unidad CAD"
                )
                scale_note = (
                    f"Escala nominal: 1:{nominal_denominator:g} | {factor_text} | "
                    f"unidades {verification.unit_name} | estado: {verification.status}"
                )
                if not verification.verified:
                    scale_note += " | ADVERTENCIA: escala física NO garantizada"
                if verification.warnings:
                    scale_note += " | indicios: " + "; ".join(verification.warnings)
            transform = (
                f"Ocurrencia usada: {ref.order} de {len(references)} | handle "
                f"{ref.handle or '(sin handle)'} | capa {ref.layer} | "
                f"punto ({_format_insert(ref.insert)}) | "
                f"rotación {ref.rotation:g}° | "
                f"escala ({ref.xscale:g}, {ref.yscale:g}, {ref.zscale:g})"
            )
            print(transform)
            if scale_note:
                print(scale_note)
            # Con --solids footprint se dibuja una copia de render; la caja, el contenido
            # y la página salen de ella para coincidir con el PDF. El inventario sigue
            # usando el DXF extraído, donde los 3DSOLID conservan su tipo y su handle.
            render_dxf, render_document, footprints = temp_dxf, extracted_document, None
            if args.solids == "footprint":
                render_dxf = work / f"{order_prefix.name}_render.dxf"
                footprints = footprint_document(
                    temp_dxf,
                    render_dxf,
                    millimeters_per_unit_on_paper=_paper_factor(
                        args,
                        result,
                        nominal_denominator,
                        verification if nominal_denominator is not None else None,
                    ),
                )
                render_document = read_dxf(render_dxf)
            if footprints is not None or source_invalid:
                # «Sin caja» se midió en el original; con huellas, o si el original tenía
                # entidades que no se podían medir, se mide de nuevo en lo que se dibuja.
                result = dataclasses.replace(
                    result,
                    unbounded=_definition_has_unbounded(render_document, args.block_name, set()),
                )
            # La caja se calcula una sola vez, sobre lo que se dibuja, y se reutiliza para la
            # sección C, la página y la comprobación de que el dibujo cabe en el exportador.
            effective_bounds_xyz, render_invalid = _effective_bounds_xyz(render_document)
            # extract-modelspace con recorte: la página es la ventana, no la caja de lo dibujado
            # (lo recortado puede no tocar los cuatro bordes).
            page_window = getattr(args, "page_window", None)
            if page_window is not None and effective_bounds_xyz is not None:
                effective_bounds_xyz = (
                    (page_window[0], page_window[1], effective_bounds_xyz[0][2]),
                    (page_window[2], page_window[3], effective_bounds_xyz[1][2]),
                )
            cad_size = (
                None
                if effective_bounds_xyz is None
                else (
                    effective_bounds_xyz[1][0] - effective_bounds_xyz[0][0],
                    effective_bounds_xyz[1][1] - effective_bounds_xyz[0][1],
                )
            )
            # Entidades sueltas del Model Space de origen, en capas ocultas, sobre el detalle.
            overlap = hidden_overlap(
                document,
                None
                if effective_bounds_xyz is None
                else (
                    effective_bounds_xyz[0][0],
                    effective_bounds_xyz[0][1],
                    effective_bounds_xyz[1][0],
                    effective_bounds_xyz[1][1],
                ),
            )
            if nominal_denominator is not None:
                scale, geometry_page_size, geometry_box, content_size = _nominal_scale_parameters(
                    args, effective_bounds_xyz, verification, nominal_denominator
                )
                page_size = geometry_page_size
                geometry_note = (
                    "Caja geométrica efectiva CAD (xmin, ymin, xmax, ymax): "
                    f"({geometry_box[0]:.3f}, {geometry_box[1]:.3f}, "
                    f"{geometry_box[2]:.3f}, {geometry_box[3]:.3f}); "
                    f"contenido físico: {content_size[0]:.3f} × {content_size[1]:.3f} mm; "
                    f"página: {page_size[0]:.3f} × {page_size[1]:.3f} mm; "
                    f"factor aplicado a caja efectiva: {scale:g} mm/unidad CAD."
                )
                print(geometry_note)
                unknown_units = not verification.verified
            else:
                scale, page_size, unknown_units = _fit_parameters(
                    args,
                    source,
                    args.block_name,
                    ref.order,
                    result,
                    bounds_xyz=effective_bounds_xyz if footprints is not None else None,
                    source_document=document,
                )
                verification = verify_drawing_units(
                    result.units_code,
                    block_units_code,
                    max_block_dimension=max_block_dimension,
                    insert_scale=(ref.xscale, ref.yscale, ref.zscale),
                    insert_position=ref.insert,
                    override=args.units,
                )
            detail = build_extracted_detail(
                source_path=source,
                block_name=args.block_name,
                reference=ref,
                extraction=result,
                unit_verification=verification,
                nominal_denominator=nominal_denominator,
                factor_mm_per_drawing_unit=(scale if nominal_denominator is not None else None),
                effective_bounds_xyz=effective_bounds_xyz,
                document=extracted_document,
                fidelity_report=fidelity_report,
                hatch_mode=args.hatch_mode,
                hatch_threshold=args.hatch_threshold,
                transformations=transformation_steps(ref),
                nested_blocks=nested_blocks(extracted_document),
                fonts=font_uses(fidelity_report),
                hatches=hatch_summary(fidelity_report),
                total_references=len(references),
                include_dependencies=True,
                extracted_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                exporter_version=__version__,
                block_definition_handle=block_definition_handle,
                selection_method=selection_method,
                user_units_override=args.units,
                content_size_mm=content_size,
                page_size_mm=geometry_page_size,
                margin_mm=args.margin_mm if nominal_denominator is not None else None,
                hidden_layers=hidden,
                included_hidden_layers=included,
                modelspace_hidden_overlap=overlap,
                modelspace_selection=getattr(args, "modelspace_selection", None),
            )
            if pruned is not None:
                detail = dataclasses.replace(
                    detail, warnings=(*detail.warnings, pruning_warning(pruned))
                )
            if footprints is not None:
                detail = mark_footprint_solids(
                    detail,
                    footprints.converted,
                    footprints.failure_reasons,
                    footprints.max_error_mm,
                    len(footprints.own_transform),
                )
            # Un aviso crítico (p. ej. XREF sin resolver) se conoce antes de dibujar: se detiene
            # aquí para no gastar el tiempo del dibujo.
            _raise_if_blocking(detail)
            sys.stdout.flush()  # para que los avisos de stderr salgan en orden
            if result.unbounded:
                print(
                    "Aviso: el bloque contiene entidades sin caja; la página se ajusta a la "
                    "geometría calculable.",
                    file=sys.stderr,
                )
            if unknown_units:
                print(
                    "Aviso: unidades no verificadas; el ajuste indicado no representa una "
                    "escala física.",
                    file=sys.stderr,
                )
            if scale is None:
                print(
                    "Aviso: no se pudo calcular una escala 1:N porque no hay una caja "
                    "calculable para el bloque.",
                    file=sys.stderr,
                )
            if args.hatch_mode != "normal":
                print(f"Rayados simplificados: modo --hatch-mode {args.hatch_mode}.")
            run_hooks.stage("draw")
            try:
                export_report = export_vector_outputs(
                    render_dxf,
                    temp_svg,
                    temp_pdf,
                    overwrite=True,
                    page_size_mm=page_size,
                    margin_mm=args.margin_mm,
                    scale_factor=scale,
                    exact_page_size=nominal_denominator is not None,
                    hatch_mode=args.hatch_mode,
                    hatch_threshold=args.hatch_threshold,
                    omit_unsupported=args.omit_unsupported,
                    suggest_solids_footprint=args.solids is None,
                    content_size=cad_size,
                    # El SVG solo se publica con CDE_KEEP_INTERMEDIATES; si no, no se dibuja.
                    draw_svg=keep,
                    text_layer=not args.no_text_layer,
                    render_box=page_window,
                )
            except CadProcessingError as error:
                if footprints is None or not footprints.failures:
                    raise
                raise CadProcessingError(
                    f"{error} 3DSOLID que no se pudieron convertir en huella: "
                    f"{_failure_text(footprints)}."
                ) from error
            detail = mark_omitted_entities(detail, export_report.omitted)
            # Conciliación en tiempo de ejecución: lo que ezdxf descartó por no ser visible
            # debe coincidir con lo que el inventario clasificó como tal en lo que se dibuja.
            expected_hidden = hidden_skip_events(render_document)
            if expected_hidden != export_report.invisible:
                detail = dataclasses.replace(
                    detail,
                    warnings=(
                        *detail.warnings,
                        ExtractionWarning(
                            "FIDELITY_RECONCILIATION",
                            Severity.WARNING,
                            "No coincide la conciliación de entidades no visibles "
                            f"(modelo {expected_hidden}, renderizador {export_report.invisible}).",
                        ),
                    ),
                )
            if export_report.degraded:
                degraded = _degraded_rows(extracted_document, export_report.degraded)
                detail = mark_degraded_hatches(detail, degraded)
                sys.stdout.flush()
                print(f"Aviso: {degraded_hatches_message(degraded)}", file=sys.stderr)
            # Se decide tras dibujar, para no afirmar que una entidad se dibuja si se omitió.
            invalid_messages = _invalid_data_messages(
                document, source_invalid, render_invalid, export_report.omitted
            )
            if invalid_messages:
                detail = dataclasses.replace(
                    detail,
                    warnings=(
                        *detail.warnings,
                        *(
                            ExtractionWarning("ENTITIES_INVALID_DATA", Severity.WARNING, message)
                            for message in invalid_messages
                        ),
                    ),
                )
                sys.stdout.flush()
                for message in invalid_messages:
                    print(f"Aviso: {message}", file=sys.stderr)
            drawing_scale = (
                (scale, export_report.drawn_size, nominal_denominator)
                if nominal_denominator is not None and export_report.drawn_size is not None
                else None
            )
            run_hooks.stage("checks")
            checks = validate_output_pdf(
                temp_pdf, geometry_page_size, export_report.text_lines, drawing_scale
            )
            # Comprobaciones de aviso que no pasan (texto no buscable, escala medida distinta):
            # se publica igual, con advertencia.
            check_warnings = advisory_warnings(checks)
            if check_warnings:
                detail = dataclasses.replace(detail, warnings=(*detail.warnings, *check_warnings))
                sys.stdout.flush()
                for warning in check_warnings:
                    print(f"Aviso: {warning.message}", file=sys.stderr)
            detail = _confirm_physical_scale(detail, checks)
            _raise_if_blocking(detail)  # también los que hubieran aparecido al dibujar
            try:
                # El estado sale de la lista única de avisos: "con advertencias" si hay alguno
                # warning o critical (las notas informativas no cuentan).
                extraction_result = build_extraction_result(
                    pdf_destination.name,
                    checks,
                    tuple(
                        notice
                        for notice in collect_notices(detail)
                        if notice.severity is not NoticeSeverity.INFO
                    ),
                    export_report.warnings,
                )
            except PdfValidationError as error:
                failed = "; ".join(f"{check.name}: {check.detail}" for check in error.checks)
                raise CadProcessingError(
                    f"La validación del PDF falló; no se publicó ningún archivo. {failed}"
                ) from error
            detail = finalize_detail(detail, extraction_result)
            if details is not None:
                details.append(detail)
            temp_report.write_text(format_detail_report(detail), encoding="utf-8", newline="\n")
            # Manifiesto v1: validado contra su esquema, escrito y comprobado contra el PDF.
            manifest = _checked_manifest(source, temp_pdf, detail, export_report, args)
            temp_manifest.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            coherence = check_manifest_coherence(temp_manifest, temp_pdf)
            if not coherence.passed:
                raise CadProcessingError(
                    f"{coherence.name}: {coherence.detail} No se generaron archivos."
                )
            pairs = [(temp_pdf, pdf_destination), (temp_report, txt_destination)]
            if keep:
                pairs += [(render_dxf, dxf_output), (temp_svg, svg_output)]
            # Todo o nada: se publica cuando todas las referencias están preparadas.
            staged_pairs += pairs
            staged_manifests.append((temp_manifest, manifest_destination))
        if args.scale_factor is None and nominal_denominator is None and scale is not None:
            mm_per_unit = (
                1.0
                if unknown_units
                else units.conversion_factor(result.units_code, ezdxf.units.InsertUnits.Millimeters)
            )
            print(f"Escala aproximada: 1:{mm_per_unit / scale:.6g}")
        print(
            f"Bloque: {args.block_name} | orden: {ref.order} | handle: "
            f"{ref.handle or '(sin handle)'} | capa: {ref.layer} | inserción: "
            f"{_format_insert(ref.insert)} | entidades de definición: "
            f"{result.definition_entity_count}"
        )
        # Lo que se dice tras publicar: (texto, ¿por stderr?).
        created = [pdf_destination, txt_destination]
        if keep:
            created += [dxf_output, svg_output]
        after_publish += [(f"Creado: {path}", False) for path in created]
        after_publish.append((f"Creado: {manifest_destination}", False))
        if footprints is not None and (footprints.converted or footprints.failures):
            drawn = sum(
                1
                for item in detail.dispositions
                if item.status is EntityStatus.TRANSFORMED and item.reason == FOOTPRINT_REASON
            )
            distinct = len(footprints.converted)
            distinct_text = "sólido distinto" if distinct == 1 else "sólidos distintos"
            own = len(footprints.own_transform)
            own_text = f"; {own} con el lector propio del transform ACIS" if own else ""
            after_publish.append(
                (
                    f"Huellas: {drawn} 3DSOLID {'dibujado' if drawn == 1 else 'dibujados'} como "
                    f"huella 2D ({distinct} {distinct_text}{own_text}).",
                    False,
                )
            )
            if footprints.failures:
                after_publish.append(
                    (f"Aviso: 3DSOLID sin huella: {_failure_text(footprints)}.", False)
                )
        if export_report.omitted:
            after_publish.append((_omitted_terminal_line(export_report.omitted), True))
        for warning in detail.warnings:
            to_stderr = _HIDDEN_TERMINAL_CODES.get(warning.code)
            if to_stderr is not None:
                text = f"Aviso: {warning.message}" if to_stderr else warning.message
                after_publish.append((text, to_stderr))
    # Orden de publicación: PDF y TXT (y los intermedios de desarrollo) de todas las
    # referencias y, al final, los manifiestos. Un PDF sin su manifiesto es un paquete
    # incompleto (``manifest.verify_package``).
    run_hooks.stage("publish")
    _publish_files([*staged_pairs, *staged_manifests], args.overwrite)
    for text, to_stderr in after_publish:
        if to_stderr:
            # stdout puede tener búfer (p. ej. redirigido); se vacía para que el aviso salga último.
            sys.stdout.flush()
        print(text, file=sys.stderr if to_stderr else sys.stdout)
    return 0


_INVALID_MESSAGE_LIMIT = 10


def _describe_invalid(item: InvalidEntity) -> str:
    where = f"capa {item.layer}" + (f", bloque {item.block}" if item.block else "")
    return f"{item.entity_type} {item.handle or '(sin handle)'} ({where})"


def _repair_note(entity, document) -> str:
    """Explica cómo se dibuja una entidad que falló en el original y no en el extraído.

    Al copiar, ezdxf deja en blanco los handles que no apuntan a un objeto válido; si el
    override de DIMSTYLE señalaba como estilo de texto algo que no lo es, se usa el de
    por defecto.
    """
    try:
        tags = list(entity.get_xdata_list("ACAD", "DSTYLE"))
    except Exception:  # noqa: BLE001 - sin override legible no se puede precisar más
        tags = []
    for current, following in zip(tags, tags[1:], strict=False):
        if current.code == 1070 and current.value == 340 and following.code == 1005:
            target = document.entitydb.get(following.value)
            if target is None or target.dxftype() != "STYLE":
                return "Se dibuja con el estilo de texto por defecto."
    return "Se dibuja con los datos corregidos al extraer."


def _invalid_data_messages(
    document,
    source_invalid: tuple[InvalidEntity, ...],
    render_invalid: tuple[InvalidEntity, ...],
    omitted: tuple[tuple[str, str, int], ...],
) -> list[str]:
    """Un aviso por entidad excluida de la medición, con lo que pasa realmente con ella.

    Las del original se relacionan con las del DXF dibujado por tipo y capa (los handles
    cambian al extraer). Si en el dibujado sigue fallando o el dibujo la omitió, no se
    afirma que se dibuje.
    """
    still_invalid = Counter((item.entity_type, item.layer) for item in render_invalid)
    omitted_keys = Counter({(kind, layer): count for kind, layer, count in omitted})
    messages = []
    for item in source_invalid:
        key = (item.entity_type, item.layer)
        if omitted_keys[key]:
            omitted_keys[key] -= 1
            if still_invalid[key]:
                still_invalid[key] -= 1
            note = "No se pudo dibujar y se omitió (--omit-unsupported)."
        elif still_invalid[key]:
            still_invalid[key] -= 1
            note = "En el DXF extraído tampoco se puede medir; consulte el inventario."
        else:
            note = _repair_note(item.entity, document)
        messages.append(
            f"Se excluyó de la medición 1 entidad con datos inválidos: "
            f"{_describe_invalid(item)}. {note}"
        )
    for item in render_invalid:
        key = (item.entity_type, item.layer)
        if still_invalid[key] <= 0:
            continue
        still_invalid[key] -= 1
        messages.append(
            f"Se excluyó de la medición 1 entidad con datos inválidos en el DXF extraído: "
            f"{_describe_invalid(item)}; consulte el inventario."
        )
    if len(messages) > _INVALID_MESSAGE_LIMIT:
        hidden = len(messages) - _INVALID_MESSAGE_LIMIT
        messages = messages[:_INVALID_MESSAGE_LIMIT] + [
            f"… y {hidden} {'entidad' if hidden == 1 else 'entidades'} más con datos inválidos."
        ]
    return messages


def _hidden_layers(extracted_document, extracted_path: Path, reveal: frozenset[str] | None):
    """Capas ocultas del detalle extraído y, con --include-hidden-layers, las que se muestran.

    Las capas pedidas se encienden y descongelan en la copia de trabajo (que se guarda de
    nuevo, porque el resto del flujo la lee del disco); el DXF de origen no cambia. Devuelve
    (lo que sigue oculto, lo que se incluyó), cada uno por capa.
    """
    hidden = hidden_layer_counts(extracted_document)
    if reveal is None:
        return hidden, ()
    revealed = {name.casefold() for name in reveal_layers(extracted_document, reveal)}
    if not revealed:
        return hidden, ()
    extracted_document.saveas(extracted_path)
    included = tuple(item for item in hidden if item.layer.casefold() in revealed)
    return hidden_layer_counts(extracted_document), included


class BlockingNoticesError(CadProcessingError):
    """La extracción se detuvo por avisos críticos (``notices.BLOCKING_SEVERITIES``)."""

    def __init__(self, notices) -> None:
        super().__init__(blocking_message(notices))
        self.notices = tuple(notices)


def _raise_if_blocking(detail) -> None:
    """Sin archivos y código 1 si hay algún aviso crítico (``notices.BLOCKING_SEVERITIES``)."""
    notices = blocking_notices(detail)
    if notices:
        raise BlockingNoticesError(notices)


def _run_options(args) -> RunOptions:
    return RunOptions(
        hatch_mode=args.hatch_mode,
        hatch_threshold=args.hatch_threshold,
        solids=args.solids,
        omit_unsupported=bool(args.omit_unsupported),
        text_layer=not args.no_text_layer,
        font_substitutions=args.font_substitutions,
        font_folder=args.font_folder,
        font_fallback=args.font_fallback,
        margin_mm=args.margin_mm,
        include_hidden_layers=option_value(getattr(args, "include_hidden_layers", None)),
        units=args.units,
        instancing=instancing_enabled(),
        xobject=xobjects_enabled(),
        clip=getattr(getattr(args, "modelspace_selection", None), "clipped", None),
        exporter_version=__version__,
    )


def _checked_manifest(source: Path, pdf: Path, detail, export_report, args) -> dict:
    """Manifiesto del detalle finalizado; si no cumple su esquema, no se publica nada."""
    page_count, page_size = pdf_page_facts(pdf)
    manifest = build_manifest(
        detail,
        source_dxf=source,
        pdf_file=pdf,
        pdf=PdfFacts(
            page_count, page_size, export_report.drawn_bounds, export_report.cad_to_page_pt
        ),
        options=_run_options(args),
        # El trabajador ya calculó el hash en «load»: no se vuelve a leer el DXF.
        dxf_digest=getattr(args, "source_digest", None),
    )
    problems = validate_manifest(manifest)
    if problems:
        raise CadProcessingError(
            "El manifiesto generado no cumple su esquema (fallo del programa): "
            + "; ".join(problems[:5])
        )
    return manifest


def _confirm_physical_scale(detail, checks):
    """La escala física solo queda "garantizada" si la escala medida en el PDF coincide."""
    if detail.physical_scale_guaranteed is not True:
        return detail
    check = next((item for item in checks if item.name == DRAWING_SCALE_CHECK), None)
    if check is not None and check.passed:
        return detail
    note = (
        "no se pudo medir la escala del dibujo en el PDF"
        if check is None
        else "la escala medida en el PDF no coincide con la nominal (ver «Escala del dibujo»)"
    )
    return dataclasses.replace(
        detail, physical_scale_guaranteed=False, physical_scale_note=note
    )


def _failure_text(footprints: FootprintResult) -> str:
    return "; ".join(f"{reason} ({count})" for reason, count in footprints.failure_reasons)


def _paper_factor(args, result, nominal_denominator, verification) -> float:
    """mm de papel por unidad CAD para estimar el error de huella antes de exportar.

    Sin escala nominal ni --scale-factor se supone 1:1; si después la página se ajusta
    a un tamaño menor, el error real es menor que el estimado.
    """
    if nominal_denominator is not None:
        return verification.millimeters_per_unit / nominal_denominator
    if args.scale_factor is not None:
        return args.scale_factor
    if result.units_code:
        return units.conversion_factor(result.units_code, ezdxf.units.InsertUnits.Millimeters)
    return 1.0


def _degraded_rows(
    document, degraded: tuple[tuple[str, str, str, str, float, str], ...]
) -> tuple[tuple[int, str | None, str, str, float, str], ...]:
    """Añade a cada HATCH rellenado su índice en el inventario (por handle, en orden)."""
    positions: dict[str, list[int]] = {}
    for index, entity in enumerate(iter_detail_entities(document)):
        if entity.dxftype() == "HATCH":
            positions.setdefault(str(entity.dxf.get("handle") or ""), []).append(index)
    rows = []
    for _type, layer, handle, pattern, scale, reason in degraded:
        free = positions.get(handle) or []
        rows.append((free.pop(0) if free else 0, handle or None, layer, pattern, scale, reason))
    return tuple(rows)


def _omitted_terminal_line(omitted: tuple[tuple[str, str, int], ...]) -> str:
    total = sum(count for _type, _layer, count in omitted)
    if len(omitted) == 1:
        groups = f"{omitted[0][0]} en {omitted[0][1]}"
    else:
        groups = "; ".join(f"{kind} en {layer}: {count}" for kind, layer, count in omitted[:3])
        hidden = len(omitted) - 3
        if hidden > 0:
            groups += f"; y {hidden} {'grupo' if hidden == 1 else 'grupos'} más"
    if total == 1:
        return f"Aviso: se omitió 1 entidad no soportada ({groups}); no aparece en el PDF."
    return f"Aviso: se omitieron {total} entidades no soportadas ({groups}); no aparecen en el PDF."