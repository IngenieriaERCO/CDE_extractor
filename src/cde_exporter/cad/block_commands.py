"""Comandos CLI para catálogo y extracción por bloque."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import ezdxf
from ezdxf import bbox, units

from cde_exporter.cad.block_catalog import format_block_catalog, list_referenced_blocks
from cde_exporter.cad.block_extractor import (
    BlockReference,
    extract_block_reference,
    references_for_block,
)
from cde_exporter.cad.detail_builder import build_extracted_detail
from cde_exporter.cad.detail_model import detail_to_dict
from cde_exporter.cad.exporters import export_vector_outputs
from cde_exporter.cad.fidelity import print_fidelity_summary, write_fidelity_report
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.reader import entity_bounds, read_dxf
from cde_exporter.cad.scale import (
    output_factor,
    parse_nominal_scale,
    verify_drawing_units,
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
    args, source: Path, block_name: str, occurrence: int, extracted
) -> tuple[float | None, tuple[float, float] | None, bool]:
    if args.scale_factor is not None:
        return args.scale_factor, tuple(args.page_size_mm) if args.page_size_mm else None, False
    document = read_dxf(source)
    references = [
        entity
        for entity in document.modelspace()
        if entity.dxftype() == "INSERT" and entity.dxf.name == block_name
    ]
    reference = references[occurrence - 1]
    bounds = entity_bounds(reference)
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
    formats = {"all": (".svg", ".pdf"), "svg": (".svg",), "pdf": (".pdf",)}[args.format]
    for ref in selected:
        order_prefix = prefix.with_name(f"{prefix.name}_{ref.order:03d}") if args.all else prefix
        dxf_output = order_prefix.with_suffix(".dxf")
        fidelity_destination = order_prefix.with_name(f"{order_prefix.name}_fidelidad.txt")
        vector_outputs = [order_prefix.with_suffix(ext) for ext in formats]
        detail_json_output = _detail_json_output(
            args.detail_json, ref.order, args.all or len(selected) > 1
        )
        outputs = (dxf_output, fidelity_destination, *vector_outputs)
        if detail_json_output is not None:
            outputs += (detail_json_output,)
        for output in outputs:
            if output.resolve() == source.resolve():
                raise CadProcessingError("La salida no puede sobrescribir el DXF original")
            if output.exists() and not args.overwrite:
                raise CadProcessingError(
                    f"El archivo de salida ya existe: {output}. Usa --overwrite para reemplazarlo."
                )
        result = extract_block_reference(
            source,
            args.block_name,
            references.index(ref) + 1,
            dxf_output,
            overwrite=args.overwrite,
        )
        extracted_document = read_dxf(dxf_output)
        effective_extents = bbox.extents(extracted_document.modelspace())
        effective_bounds_xyz = (
            (
                tuple(float(value) for value in (
                    effective_extents.extmin.x,
                    effective_extents.extmin.y,
                    effective_extents.extmin.z,
                )),
                tuple(float(value) for value in (
                    effective_extents.extmax.x,
                    effective_extents.extmax.y,
                    effective_extents.extmax.z,
                )),
            )
            if effective_extents.has_data
            else None
        )
        fidelity_report = write_fidelity_report(
            dxf_output,
            fidelity_destination,
            args.hatch_mode,
            overwrite=args.overwrite,
            hatch_threshold=args.hatch_threshold,
        )
        print_fidelity_summary(fidelity_report, fidelity_destination)
        scale_note = ""
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
            f"rotación {ref.rotation:g}° | escala ({ref.xscale:g}, {ref.yscale:g}, {ref.zscale:g})"
        )
        with fidelity_destination.open("a", encoding="utf-8", newline="\n") as report_file:
            report_file.write(f"\nSelección del bloque\n{transform}\n")
        print(transform)
        if scale_note:
            with fidelity_destination.open("a", encoding="utf-8", newline="\n") as report_file:
                report_file.write(f"{scale_note}\n")
            print(scale_note)
        if nominal_denominator is not None:
            scale, page_size, geometry_box, content_size = _nominal_scale_parameters(
                args, read_dxf(dxf_output), verification, nominal_denominator
            )
            geometry_note = (
                "Caja geométrica efectiva CAD (xmin, ymin, xmax, ymax): "
                f"({geometry_box[0]:.3f}, {geometry_box[1]:.3f}, "
                f"{geometry_box[2]:.3f}, {geometry_box[3]:.3f}); "
                f"contenido físico: {content_size[0]:.3f} × {content_size[1]:.3f} mm; "
                f"página: {page_size[0]:.3f} × {page_size[1]:.3f} mm; "
                f"factor aplicado a caja efectiva: {scale:g} mm/unidad CAD."
            )
            with fidelity_destination.open("a", encoding="utf-8", newline="\n") as report_file:
                report_file.write(geometry_note + "\n")
                if result.unbounded:
                    report_file.write(
                        "Aviso: las entidades sin caja calculable se excluyeron del tamaño.\n"
                    )
                if not verification.verified:
                    report_file.write("ADVERTENCIA: escala física NO garantizada.\n")
            print(geometry_note)
            unknown_units = not verification.verified
        else:
            scale, page_size, unknown_units = _fit_parameters(
                args, source, args.block_name, ref.order, result
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
            include_dependencies=True,
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
        with tempfile.TemporaryDirectory(dir=prefix.parent) as temp_dir:
            temp_prefix = Path(temp_dir) / order_prefix.name
            export_vector_outputs(
                dxf_output,
                temp_prefix.with_suffix(".svg"),
                temp_prefix.with_suffix(".pdf"),
                overwrite=True,
                page_size_mm=page_size,
                margin_mm=args.margin_mm,
                scale_factor=scale,
                exact_page_size=nominal_denominator is not None,
                hatch_mode=args.hatch_mode,
                hatch_threshold=args.hatch_threshold,
            )
            for extension in formats:
                staged = temp_prefix.with_suffix(extension)
                destination = order_prefix.with_suffix(extension)
                if args.overwrite:
                    os.replace(staged, destination)
                else:
                    staged.rename(destination)
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
        print(f"Creado: {dxf_output}")
        print(f"Creado: {fidelity_destination}")
        for ext in formats:
            print(f"Creado: {order_prefix.with_suffix(ext)}")
        if detail_json_output is not None:
            try:
                detail_json_output.parent.mkdir(parents=True, exist_ok=True)
                mode = "w" if args.overwrite else "x"
                with detail_json_output.open(mode, encoding="utf-8", newline="\n") as stream:
                    json.dump(detail_to_dict(detail), stream, indent=2, ensure_ascii=False)
                    stream.write("\n")
            except (OSError, TypeError, ValueError) as error:
                raise CadProcessingError(
                    f"No se pudo escribir el JSON del detalle '{detail_json_output}': {error}"
                ) from error
            print(f"JSON del detalle creado: {detail_json_output}")
    return 0
