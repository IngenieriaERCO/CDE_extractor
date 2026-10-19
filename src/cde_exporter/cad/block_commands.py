"""Comandos CLI para catálogo y extracción por bloque."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import ezdxf
from ezdxf import units

from cde_exporter.cad.block_catalog import format_block_catalog, list_referenced_blocks
from cde_exporter.cad.block_extractor import (
    BlockReference,
    extract_block_reference,
    references_for_block,
)
from cde_exporter.cad.exporters import export_vector_outputs
from cde_exporter.cad.fidelity import print_fidelity_summary, write_fidelity_report
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.reader import entity_bounds, read_dxf


def run_blocks(args) -> int:
    rows = list_referenced_blocks(
        args.drawing,
        include_anonymous=args.include_anonymous,
        filter_text=args.filter or "",
    )
    content = format_block_catalog(rows)
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


def _format_references(references: tuple[BlockReference, ...]) -> str:
    return "\n".join(
        f"  orden {ref.order} | capa {ref.layer} | handle {ref.handle or '(sin handle)'} | "
        f"inserciÃ³n {', '.join(f'{value:g}' for value in ref.insert)}"
        for ref in references
    )


def _select_references(args, references: tuple[BlockReference, ...]) -> tuple[BlockReference, ...]:
    if args.all:
        if args.occurrence is not None or args.handle is not None:
            raise CadProcessingError("--all no se combina con --occurrence ni --handle")
        return references
    if args.occurrence is not None and args.handle is not None:
        raise CadProcessingError("Indica --occurrence o --handle, no ambos")
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
            f"El handle '{args.handle}' está duplicado; usa --occurrence. Referencias:\n"
            + _format_references(matches)
        )
    if len(references) == 1:
        return references
    raise CadProcessingError(
        f"El bloque tiene {len(references)} referencias; especifica --occurrence, --handle "
        f"o --all. Referencias:\n{_format_references(references)}"
    )


def _fit_parameters(
    args, source: Path, order: int, extracted
) -> tuple[float | None, tuple[float, float] | None, bool]:
    if args.scale_factor is not None:
        return args.scale_factor, tuple(args.page_size_mm) if args.page_size_mm else None, False
    document = read_dxf(source)
    reference = list(document.modelspace())[order - 1]
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


def run_extract_block(args) -> int:
    source, prefix = Path(args.drawing), Path(args.output_prefix)
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
    formats = {"all": (".svg", ".pdf"), "svg": (".svg",), "pdf": (".pdf",)}[args.format]
    for ref in selected:
        order_prefix = prefix.with_name(f"{prefix.name}_{ref.order:03d}") if args.all else prefix
        dxf_output = order_prefix.with_suffix(".dxf")
        fidelity_destination = order_prefix.with_name(f"{order_prefix.name}_fidelidad.txt")
        vector_outputs = [order_prefix.with_suffix(ext) for ext in formats]
        for output in (dxf_output, fidelity_destination, *vector_outputs):
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
        fidelity_report = write_fidelity_report(
            dxf_output,
            fidelity_destination,
            args.hatch_mode,
            overwrite=args.overwrite,
            hatch_threshold=args.hatch_threshold,
        )
        print_fidelity_summary(fidelity_report, fidelity_destination)
        scale, page_size, unknown_units = _fit_parameters(args, source, ref.order, result)
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
        if args.scale_factor is None and scale is not None:
            mm_per_unit = (
                1.0
                if unknown_units
                else units.conversion_factor(result.units_code, ezdxf.units.InsertUnits.Millimeters)
            )
            print(f"Escala aproximada: 1:{mm_per_unit / scale:.6g}")
        print(
            f"Bloque: {args.block_name} | orden: {ref.order} | handle: "
            f"{ref.handle or '(sin handle)'} | capa: {ref.layer} | inserción: "
            f"{', '.join(f'{value:g}' for value in ref.insert)} | entidades de definición: "
            f"{result.definition_entity_count}"
        )
        print(f"Creado: {dxf_output}")
        print(f"Creado: {fidelity_destination}")
        for ext in formats:
            print(f"Creado: {order_prefix.with_suffix(ext)}")
    return 0
