"""Comandos CLI para catálogo y extracción por bloque."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import ezdxf
from ezdxf import bbox, units

from cde_exporter import __version__
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
    FOOTPRINT_REASON,
    EntityStatus,
    SelectionMethod,
    detail_to_dict,
    finalize_detail,
    mark_footprint_solids,
    mark_omitted_entities,
)
from cde_exporter.cad.detail_text import format_detail_report
from cde_exporter.cad.exporters import export_vector_outputs
from cde_exporter.cad.fidelity import print_fidelity_summary, write_fidelity_report
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.pdf_checks import (
    PdfValidationError,
    build_extraction_result,
    validate_output_pdf,
)
from cde_exporter.cad.reader import entity_bounds, read_dxf
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
    block_extents = bbox.extents(block_definition)
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
    extents = bbox.extents([entities[ref.order - 1]])
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
    args, source: Path, block_name: str, occurrence: int, extracted, bounds_document=None
) -> tuple[float | None, tuple[float, float] | None, bool]:
    if args.scale_factor is not None:
        return args.scale_factor, tuple(args.page_size_mm) if args.page_size_mm else None, False
    if bounds_document is not None:
        box = _effective_bounds_xyz(bounds_document)
        bounds = None if box is None else (box[0][0], box[0][1], box[1][0], box[1][1])
    else:
        document = read_dxf(source)
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


def _nominal_scale_parameters(args, extracted, verification, denominator: float):
    """Calcula dimensiones desde la caja efectiva; INSERT anidados están transformados."""
    extents = bbox.extents(extracted.modelspace())
    if not extents.has_data:
        raise CadProcessingError("No se pudo calcular la caja geométrica del detalle")
    box = tuple(float(value) for value in (
        extents.extmin.x, extents.extmin.y, extents.extmax.x, extents.extmax.y
    ))
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
        extents = bbox.extents([insert_entities[ref.order - 1]])
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


def _detail_json_output(path: str | None, occurrence: int, multiple: bool) -> Path | None:
    if path is None:
        return None
    destination = Path(path)
    if multiple:
        destination = destination.with_name(
            f"{destination.stem}_{occurrence:03d}{destination.suffix}"
        )
    return destination


def _effective_bounds_xyz(document):
    """Caja efectiva (mínimo y máximo, con Z) del Model Space del DXF extraído."""
    extents = bbox.extents(document.modelspace())
    if not extents.has_data:
        return None
    return (
        (float(extents.extmin.x), float(extents.extmin.y), float(extents.extmin.z)),
        (float(extents.extmax.x), float(extents.extmax.y), float(extents.extmax.z)),
    )


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


def run_extract_block(args) -> int:
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
    document = read_dxf(source)
    if args.block_name not in document.blocks:
        from cde_exporter.cad.block_extractor import _similar_names

        raise CadProcessingError(
            f"No existe el bloque '{args.block_name}'.{_similar_names(document, args.block_name)}"
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
    block_extents = bbox.extents(block_definition)
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
    for ref in selected:
        order_prefix = prefix.with_name(f"{prefix.name}_{ref.order:03d}") if args.all else prefix
        pdf_destination = order_prefix.with_suffix(".pdf")
        txt_destination = order_prefix.with_suffix(".txt")
        dxf_output = order_prefix.with_suffix(".dxf")
        svg_output = order_prefix.with_suffix(".svg")
        detail_json_output = _detail_json_output(
            args.detail_json, ref.order, args.all or len(selected) > 1
        )
        outputs = [pdf_destination, txt_destination]
        if keep:
            outputs += [dxf_output, svg_output]
        if detail_json_output is not None:
            outputs.append(detail_json_output)
        for output in outputs:
            if output.resolve() == source.resolve():
                raise CadProcessingError("La salida no puede sobrescribir el DXF original")
            if output.exists() and not args.overwrite:
                raise CadProcessingError(
                    f"El archivo de salida ya existe: {output}. Usa --overwrite para reemplazarlo."
                )
        prefix.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            dir=prefix.parent, ignore_cleanup_errors=True
        ) as temp_name:
            work = Path(temp_name)
            temp_dxf = work / f"{order_prefix.name}.dxf"
            temp_fidelity = work / "fidelidad.txt"
            temp_svg = work / f"{order_prefix.name}.svg"
            temp_pdf = work / f"{order_prefix.name}.pdf"
            temp_report = work / f"{order_prefix.name}.txt"
            temp_json = work / "detalle.json"
            result = extract_block_reference(
                source,
                args.block_name,
                references.index(ref) + 1,
                temp_dxf,
                overwrite=True,
            )
            extracted_document = read_dxf(temp_dxf)
            effective_bounds_xyz = _effective_bounds_xyz(extracted_document)
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
                effective_bounds_xyz = _effective_bounds_xyz(render_document)
                result = dataclasses.replace(
                    result,
                    unbounded=_definition_has_unbounded(render_document, args.block_name, set()),
                )
            if nominal_denominator is not None:
                scale, geometry_page_size, geometry_box, content_size = _nominal_scale_parameters(
                    args, render_document, verification, nominal_denominator
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
                    bounds_document=render_document if footprints is not None else None,
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
            )
            if footprints is not None:
                detail = mark_footprint_solids(
                    detail,
                    footprints.converted,
                    footprints.failure_reasons,
                    footprints.max_error_mm,
                )
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
                )
            except CadProcessingError as error:
                if footprints is None or not footprints.failures:
                    raise
                raise CadProcessingError(
                    f"{error} 3DSOLID que no se pudieron convertir en huella: "
                    f"{_failure_text(footprints)}."
                ) from error
            detail = mark_omitted_entities(detail, export_report.omitted)
            checks = validate_output_pdf(temp_pdf, geometry_page_size)
            try:
                extraction_result = build_extraction_result(
                    pdf_destination.name,
                    checks,
                    detail.warnings,
                    export_report.warnings,
                )
            except PdfValidationError as error:
                failed = "; ".join(f"{check.name}: {check.detail}" for check in error.checks)
                raise CadProcessingError(
                    f"La validación del PDF falló; no se publicó ningún archivo. {failed}"
                ) from error
            detail = finalize_detail(detail, extraction_result)
            temp_report.write_text(format_detail_report(detail), encoding="utf-8", newline="\n")
            pairs = [(temp_pdf, pdf_destination), (temp_report, txt_destination)]
            if keep:
                pairs += [(render_dxf, dxf_output), (temp_svg, svg_output)]
            if detail_json_output is not None:
                try:
                    with temp_json.open("w", encoding="utf-8", newline="\n") as stream:
                        json.dump(detail_to_dict(detail), stream, indent=2, ensure_ascii=False)
                        stream.write("\n")
                except (OSError, TypeError, ValueError) as error:
                    raise CadProcessingError(
                        f"No se pudo escribir el JSON del detalle '{detail_json_output}': {error}"
                    ) from error
                pairs.append((temp_json, detail_json_output))
            _publish_files(pairs, args.overwrite)
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
        print(f"Creado: {pdf_destination}")
        print(f"Creado: {txt_destination}")
        if keep:
            print(f"Creado: {dxf_output}")
            print(f"Creado: {svg_output}")
        if detail_json_output is not None:
            print(f"JSON del detalle creado: {detail_json_output}")
        if footprints is not None:
            drawn = sum(
                1
                for item in detail.dispositions
                if item.status is EntityStatus.TRANSFORMED and item.reason == FOOTPRINT_REASON
            )
            distinct = len(footprints.converted)
            distinct_text = "sólido distinto" if distinct == 1 else "sólidos distintos"
            print(
                f"Huellas: {drawn} 3DSOLID {'dibujado' if drawn == 1 else 'dibujados'} como "
                f"huella 2D ({distinct} {distinct_text})."
            )
            if footprints.failures:
                print(f"Aviso: 3DSOLID sin huella: {_failure_text(footprints)}.")
        if export_report.omitted:
            # stdout puede tener búfer (p. ej. redirigido); se vacía para que el aviso salga último.
            sys.stdout.flush()
            print(_omitted_terminal_line(export_report.omitted), file=sys.stderr)
    return 0


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