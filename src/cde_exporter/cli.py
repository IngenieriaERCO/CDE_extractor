"""Interfaz de línea de comandos del exportador vectorial."""

import argparse
import logging
import os
import sys
import tempfile
import traceback
from collections.abc import Sequence
from pathlib import Path

from cde_exporter import __version__
from cde_exporter.cad import (
    CadProcessingError,
    CadWindow,
    diagnose_unbounded_entities,
    export_vector_outputs,
    extract_dxf_region,
    inspect_dxf,
)
from cde_exporter.cad.block_commands import run_blocks, run_extract_block
from cde_exporter.cad.fidelity import print_fidelity_summary, write_fidelity_report
from cde_exporter.cad.font_config import configure_fonts
from cde_exporter.cad.manifest import manifest_path_for, verify_package
from cde_exporter.cad.project_commands import run_extract_all_blocks
from cde_exporter.config import get_settings
from cde_exporter.converters.pdf_to_svg import PDFConversionError, convert_page_to_svg
from cde_exporter.validators.pdf_validator import validate_pdf

_NO_TEXT_LAYER_HELP = (
    "No añade al PDF la capa de texto invisible: el texto queda solo como trazados "
    "(no se puede buscar, seleccionar ni copiar)"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cde-exporter",
        description="Valida archivos PDF y exporta páginas a SVG vectorial.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    validate_command = commands.add_parser("validate", help="Valida un archivo PDF")
    validate_command.add_argument("pdf", help="Ruta del PDF que se validará")

    export_command = commands.add_parser("export", help="Exporta una página PDF a SVG")
    export_command.add_argument("pdf", help="Ruta del PDF de origen")
    export_command.add_argument(
        "--page", type=int, required=True, help="Índice de página basado en cero"
    )
    export_command.add_argument("--output", required=True, help="Ruta del SVG de salida")

    cad_commands = commands.add_parser("cad", help="Inspecciona y extrae dibujos DXF")
    cad_subcommands = cad_commands.add_subparsers(dest="cad_command", required=True)

    blocks_command = cad_subcommands.add_parser(
        "blocks", help="Lista bloques referenciados en el modelspace"
    )
    blocks_command.add_argument("drawing", help="Ruta del dibujo DXF")
    blocks_command.add_argument("--include-anonymous", action="store_true")
    blocks_command.add_argument(
        "--handles", action="store_true", help="Muestra handles por ocurrencia"
    )
    blocks_command.add_argument(
        "--filter", help="Filtra nombres sin distinguir mayúsculas ni tildes"
    )
    blocks_command.add_argument("--output", help="Guarda el informe UTF-8 en una ruta nueva")

    block_extract_command = cad_subcommands.add_parser(
        "extract-block", help="Extrae una o varias referencias de bloque"
    )
    block_extract_command.add_argument("drawing", help="Ruta del dibujo DXF")
    block_extract_command.add_argument(
        "--block-name", required=True, help="Nombre exacto del bloque"
    )
    selection_group = block_extract_command.add_mutually_exclusive_group()
    selection_group.add_argument("--occurrence", type=int, help="Orden de referencia desde 1")
    selection_group.add_argument("--handle", help="Handle de la referencia")
    selection_group.add_argument("--all", action="store_true", help="Extrae todas las referencias")
    block_extract_command.add_argument("--output-prefix", required=True)
    block_extract_command.add_argument("--format", choices=("all", "svg", "pdf"), default="all")
    block_extract_command.add_argument(
        "--page-size-mm", nargs=2, type=float, metavar=("WIDTH", "HEIGHT")
    )
    block_extract_command.add_argument("--margin-mm", type=float, default=5.0)
    block_extract_command.add_argument("--scale-factor", type=float)
    block_extract_command.add_argument("--scale", help="Escala nominal, por ejemplo 1:50")
    block_extract_command.add_argument(
        "--units", choices=("mm", "cm", "m", "in", "ft"),
        help="Declara o corrige las unidades del dibujo",
    )
    block_extract_command.add_argument(
        "--hatch-mode",
        choices=("normal", "outline", "solid", "ignore", "auto"),
        default="auto",
    )
    block_extract_command.add_argument("--overwrite", action="store_true")
    block_extract_command.add_argument(
        "--omit-unsupported",
        action="store_true",
        help="Omite (y declara) las entidades que ezdxf no puede dibujar en vez de detenerse",
    )
    block_extract_command.add_argument(
        "--solids",
        choices=("footprint",),
        help="footprint: dibuja cada 3DSOLID convertible como su huella en planta",
    )
    block_extract_command.add_argument("--hatch-threshold", type=int, default=50000)
    block_extract_command.add_argument("--font-substitutions", help="JSON UTF-8 familia→fuente")
    block_extract_command.add_argument("--font-folder", help="Carpeta de fuentes del proyecto")
    block_extract_command.add_argument(
        "--font-fallback", help="Fuente de respaldo (default: arial.ttf)"
    )
    block_extract_command.add_argument(
        "--no-text-layer", action="store_true", help=_NO_TEXT_LAYER_HELP
    )

    project_command = cad_subcommands.add_parser(
        "extract-all-blocks",
        help="Exporta cada bloque del Model Space como su propio paquete (un proyecto)",
    )
    project_command.add_argument("drawing", help="Ruta del dibujo DXF")
    project_command.add_argument(
        "--output-dir", required=True, help="Carpeta del proyecto donde quedan los paquetes"
    )
    project_command.add_argument("--scale", help="Escala por defecto de todos los bloques (1:N)")
    project_command.add_argument(
        "--scale-map", help='JSON UTF-8 con la escala de cada bloque: {"bloque": "1:N"}'
    )
    project_command.add_argument(
        "--list",
        action="store_true",
        help="Solo muestra qué se exportaría, con su escala y su tamaño; no crea paquetes",
    )
    project_command.add_argument(
        "--write-scale-map",
        metavar="JSON",
        help="Escribe una plantilla de escalas con todos los bloques (implica --list)",
    )
    project_command.add_argument("--overwrite", action="store_true")
    project_command.add_argument(
        "--units", choices=("mm", "cm", "m", "in", "ft"),
        help="Declara o corrige las unidades del dibujo",
    )
    project_command.add_argument(
        "--page-size-mm", nargs=2, type=float, metavar=("WIDTH", "HEIGHT")
    )
    project_command.add_argument("--margin-mm", type=float, default=5.0)
    project_command.add_argument(
        "--hatch-mode",
        choices=("normal", "outline", "solid", "ignore", "auto"),
        default="auto",
    )
    project_command.add_argument("--hatch-threshold", type=int, default=50000)
    project_command.add_argument(
        "--omit-unsupported",
        action="store_true",
        help="Omite (y declara) las entidades que ezdxf no puede dibujar en vez de detenerse",
    )
    project_command.add_argument(
        "--solids",
        choices=("footprint",),
        help="footprint: dibuja cada 3DSOLID convertible como su huella en planta",
    )
    project_command.add_argument("--font-substitutions", help="JSON UTF-8 familia→fuente")
    project_command.add_argument("--font-folder", help="Carpeta de fuentes del proyecto")
    project_command.add_argument(
        "--font-fallback", help="Fuente de respaldo (default: arial.ttf)"
    )
    project_command.add_argument("--no-text-layer", action="store_true", help=_NO_TEXT_LAYER_HELP)

    verify_command = cad_subcommands.add_parser(
        "verify-package",
        help="Comprueba que el paquete de un detalle (PDF + manifiesto) está completo y coherente",
    )
    verify_command.add_argument("path", help="Ruta del PDF o de su .manifest.json")

    inspect_command = cad_subcommands.add_parser(
        "inspect", help="Muestra unidades, capas, entidades y extensiones de un DXF"
    )
    inspect_command.add_argument("drawing", help="Ruta del dibujo DXF")

    diagnose_command = cad_subcommands.add_parser(
        "diagnose", help="Lista entidades sin caja y sus posiciones aproximadas"
    )
    diagnose_command.add_argument("drawing", help="Ruta del dibujo DXF")
    diagnose_command.add_argument(
        "--window",
        nargs=4,
        type=float,
        metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
        help="Cuenta posiciones XY dentro y fuera de esta ventana",
    )
    diagnose_command.add_argument(
        "--output",
        help="Escribe el informe en UTF-8 en esta ruta nueva",
    )

    extract_command = cad_subcommands.add_parser(
        "extract", help="Extrae una ventana XY a DXF, SVG y PDF independientes"
    )
    extract_command.add_argument("drawing", help="Ruta del dibujo DXF")
    extract_command.add_argument(
        "--window",
        nargs=4,
        type=float,
        required=True,
        metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
        help="Ventana en coordenadas XY del dibujo",
    )
    extract_command.add_argument(
        "--output-prefix",
        required=True,
        help="Prefijo sin extensión para los archivos de salida",
    )
    extract_command.add_argument(
        "--format",
        choices=("all", "svg", "pdf"),
        default="all",
        help="Formato vectorial que se publicará; el DXF independiente siempre se crea",
    )
    extract_command.add_argument(
        "--hatch-mode",
        choices=("normal", "outline", "solid", "ignore", "auto"),
        default="auto",
        help="Política de rayados al dibujar (predeterminada: auto)",
    )
    extract_command.add_argument(
        "--page-size-mm",
        nargs=2,
        type=float,
        metavar=("WIDTH", "HEIGHT"),
        help="Ancho y alto de página en milímetros; el orden define la orientación",
    )
    extract_command.add_argument(
        "--margin-mm",
        type=float,
        default=5.0,
        help="Margen de página en milímetros (predeterminado: 5)",
    )
    extract_command.add_argument(
        "--scale-factor",
        type=float,
        help="Milímetros de página por unidad CAD; requiere unidades CAD verificadas",
    )
    extract_command.add_argument(
        "--overwrite",
        action="store_true",
        help="Permite reemplazar explícitamente archivos de salida existentes",
    )
    extract_command.add_argument(
        "--allow-unbounded",
        action="store_true",
        help="Permite continuar y registra las entidades sin caja en un informe",
    )
    extract_command.add_argument("--hatch-threshold", type=int, default=50000)
    extract_command.add_argument("--font-substitutions", help="JSON UTF-8 familia→fuente")
    extract_command.add_argument("--font-folder", help="Carpeta de fuentes del proyecto")
    extract_command.add_argument("--font-fallback", help="Fuente de respaldo (default: arial.ttf)")
    extract_command.add_argument("--no-text-layer", action="store_true", help=_NO_TEXT_LAYER_HELP)
    return parser


def _format_extents(extents: tuple[float, float, float, float] | None) -> str:
    if extents is None:
        return "no calculables"
    return ", ".join(f"{value:g}" for value in extents)


def _format_cad_diagnosis(report) -> str:
    lines = [f"Entidades diagnosticadas: {len(report.entities)}"]
    if report.entities:
        lines.append("Orden | tipo | capa | handle | posición aproximada")
        for entity in report.entities:
            position = (
                ", ".join(f"{value:g}" for value in entity.position)
                if entity.position is not None
                else entity.position_note
            )
            lines.append(
                f"{entity.modelspace_order} | {entity.entity_type} | {entity.layer} | "
                f"{entity.handle or '(sin handle)'} | {position}"
            )
            if entity.position is not None:
                lines.append(f"  Referencia: {entity.position_note}")
    lines.append("Conteo por tipo:")
    lines.extend(f"  {name}: {count}" for name, count in report.counts_by_type.items())
    lines.append("Conteo por capa:")
    lines.extend(f"  {name}: {count}" for name, count in report.counts_by_layer.items())
    if report.inside_window is not None:
        lines.extend(
            (
                f"Posiciones dentro de --window: {report.inside_window}",
                f"Posiciones fuera de --window: {report.outside_window}",
                f"Entidades sin posición: {report.without_position}",
            )
        )
    lines.append(f"INSERT con contenido anidado sin caja: {len(report.block_inserts)}")
    for insert in report.block_inserts:
        position = ", ".join(f"{value:g}" for value in insert.insert_position)
        lines.append(
            f"{insert.modelspace_order} | INSERT | {insert.layer} | "
            f"{insert.handle or '(sin handle)'} | bloque {insert.block_name} | inserción {position}"
        )
        nested = ", ".join(f"{name}={count}" for name, count in insert.unbounded_by_type.items())
        lines.append(f"  Entidades anidadas sin caja: {nested}")
        if report.inside_window is not None:
            insertion_status = "dentro" if insert.insert_inside_window else "fuera"
            if insert.partial_bounds is None:
                partial_status = "no calculable"
            else:
                partial_status = "toca" if insert.partial_bounds_touch_window else "no toca"
            lines.append(f"  Punto de inserción: {insertion_status} de --window")
            lines.append(f"  Caja parcial calculable: {partial_status} --window")
            if insert.partial_bounds is not None:
                bounds = ", ".join(f"{value:g}" for value in insert.partial_bounds)
                lines.append(f"  Extensión parcial XY: {bounds}")
    lines.append("Conteo anidado sin caja por tipo (definiciones de bloque únicas):")
    lines.extend(f"  {name}: {count}" for name, count in report.nested_counts_by_type.items())
    lines.append(f"Total anidado sin caja: {sum(report.nested_counts_by_type.values())}")
    lines.append("Conteo directo de modelspace sin caja:")
    lines.extend(f"  {name}: {count}" for name, count in report.modelspace_counts_by_type.items())
    lines.append(f"Total como aviso de cad inspect: {report.inspect_total}")
    lines.append("Conteo por tipo como aviso de cad inspect:")
    lines.extend(f"  {name}: {count}" for name, count in report.inspect_counts_by_type.items())
    return "\n".join(lines) + "\n"


def _format_unbounded_extraction_report(report) -> str:
    lines = [
        "Informe de entidades sin caja",
        "Estas entidades no pudieron validarse por coordenadas.",
        "El PDF/SVG puede no dibujar ciertos tipos de entidad o fuentes.",
        "",
        "Orden | tipo | capa | handle | bloque | estado",
    ]
    for entity in report.unbounded_entities:
        lines.append(
            f"{entity.modelspace_order} | {entity.entity_type} | {entity.layer} | "
            f"{entity.handle or '(sin handle)'} | {entity.block_name or '-'} | {entity.status}"
        )
    included = sum(
        entity.status == "incluido en la selección" for entity in report.unbounded_entities
    )
    unverified = sum(entity.status == "no verificado" for entity in report.unbounded_entities)
    lines.extend(("", f"Incluidas en la selección: {included}", f"No verificadas: {unverified}"))
    return "\n".join(lines) + "\n"


def _run_cad(args: argparse.Namespace) -> int:
    def restore_fonts() -> None:
        return None

    try:
        # extract-block configura las fuentes por su cuenta, tras validar el bloque y la
        # escala y solo con el detalle extraído (ver run_extract_block).
        if args.cad_command == "extract":
            restore_fonts = configure_fonts(
                args.font_substitutions, args.font_folder, args.drawing, args.font_fallback
            )
        if args.cad_command == "verify-package":
            problems = verify_package(args.path)
            if problems:
                print("Paquete NO válido:", file=sys.stderr)
                for problem in problems:
                    print(f"  - {problem}", file=sys.stderr)
                return 1
            print(f"Paquete completo y coherente: {manifest_path_for(args.path)}")
            return 0
        if args.cad_command == "inspect":
            result = inspect_dxf(args.drawing)
            print(f"DXF: {result.path}")
            print(f"Entidades en modelspace: {result.entity_count}")
            print(f"Unidades: {result.units_name} (código {result.units_code})")
            print(f"Extensiones XY: {_format_extents(result.extents)}")
            print("Entidades por tipo:")
            for name, count in result.entity_types.items():
                print(f"  {name}: {count}")
            print("Entidades por capa:")
            for name, count in result.layers.items():
                print(f"  {name}: {count}")
            print(f"Bloques definidos: {', '.join(result.block_names) or '(ninguno)'}")
            if result.entities_without_bounds:
                print(
                    "Advertencia: hay entidades cuyas cajas no se pudieron calcular: "
                    f"{result.entities_without_bounds}",
                    file=sys.stderr,
                )
            return 0

        if args.cad_command == "diagnose":
            window = CadWindow(*args.window) if args.window else None
            report = diagnose_unbounded_entities(args.drawing, window)
            content = _format_cad_diagnosis(report)
            if args.output:
                destination = Path(args.output)
                if destination.resolve() == Path(args.drawing).resolve():
                    raise CadProcessingError("El informe no puede sobrescribir el dibujo original")
                if destination.exists():
                    raise CadProcessingError(f"El archivo de salida ya existe: {destination}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8", newline="\n")
                print(f"Informe UTF-8: {destination}")
            else:
                sys.stdout.write(content)
            return 0

        if args.cad_command == "blocks":
            return run_blocks(args)

        if args.cad_command == "extract-block":
            return run_extract_block(args)

        if args.cad_command == "extract-all-blocks":
            return run_extract_all_blocks(args)

        source = Path(args.drawing)
        prefix = Path(args.output_prefix)
        if prefix.suffix:
            raise CadProcessingError(
                "--output-prefix debe ser una ruta base sin extensión; "
                "se crearán archivos con las extensiones correspondientes."
            )
        window = CadWindow(*args.window)
        selected_formats = {
            "all": (".svg", ".pdf"),
            "svg": (".svg",),
            "pdf": (".pdf",),
        }[args.format]
        outputs = [prefix.with_suffix(".dxf")] + [
            prefix.with_suffix(suffix) for suffix in selected_formats
        ]
        report_destination = prefix.with_name(f"{prefix.name}_omitidas.txt")
        fidelity_destination = prefix.with_name(f"{prefix.name}_fidelidad.txt")
        checked_outputs = outputs + [fidelity_destination]
        if args.allow_unbounded:
            checked_outputs.append(report_destination)
        for destination in checked_outputs:
            if destination.resolve() == source.resolve():
                raise CadProcessingError("La salida no puede sobrescribir el dibujo original")
            if destination.exists() and not args.overwrite:
                raise CadProcessingError(
                    f"El archivo de salida ya existe: {destination}. "
                    "Usa --overwrite para reemplazarlo."
                )

        prefix.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=prefix.parent) as temporary_directory:
            staged_prefix = Path(temporary_directory) / prefix.name
            dxf_path = staged_prefix.with_suffix(".dxf")
            svg_path = staged_prefix.with_suffix(".svg")
            pdf_path = staged_prefix.with_suffix(".pdf")
            extraction = extract_dxf_region(
                source,
                window,
                dxf_path,
                overwrite=True,
                allow_unbounded=args.allow_unbounded,
            )
            staged_by_suffix = {
                ".dxf": dxf_path,
                ".svg": svg_path,
                ".pdf": pdf_path,
            }
            if args.allow_unbounded:
                staged_report = Path(temporary_directory) / report_destination.name
                staged_report.write_text(
                    _format_unbounded_extraction_report(extraction)
                    + (
                        f"\nRayados simplificados: modo {args.hatch_mode}.\n"
                        if args.hatch_mode != "normal"
                        else ""
                    ),
                    encoding="utf-8",
                    newline="\n",
                )
            if args.overwrite:
                os.replace(dxf_path, outputs[0])
                if args.allow_unbounded:
                    os.replace(staged_report, report_destination)
            else:
                dxf_path.rename(outputs[0])
                if args.allow_unbounded:
                    staged_report.rename(report_destination)
            fidelity_report = write_fidelity_report(
                outputs[0],
                fidelity_destination,
                args.hatch_mode,
                overwrite=args.overwrite,
                hatch_threshold=args.hatch_threshold,
            )
            print_fidelity_summary(fidelity_report, fidelity_destination)
            if args.hatch_mode != "normal":
                print(f"Rayados simplificados: modo --hatch-mode {args.hatch_mode}.")
            export_report = export_vector_outputs(
                outputs[0],
                svg_path,
                pdf_path,
                overwrite=True,
                page_size_mm=tuple(args.page_size_mm) if args.page_size_mm else None,
                margin_mm=args.margin_mm,
                scale_factor=args.scale_factor,
                hatch_mode=args.hatch_mode,
                hatch_threshold=args.hatch_threshold,
                text_layer=not args.no_text_layer,
            )
            for warning in export_report.warnings:
                print(f"Advertencia de renderizado: {warning}", file=sys.stderr)
            if args.allow_unbounded:
                included_count = sum(
                    item.status == "incluido en la selección"
                    for item in extraction.unbounded_entities
                )
                unverified_count = sum(
                    item.status == "no verificado" for item in extraction.unbounded_entities
                )
                print(
                    "Aviso: entidades sin caja: "
                    f"{included_count} incluidas, {unverified_count} no verificadas. "
                    "Consulta el informe UTF-8."
                )
                print(
                    "Advertencia: estas entidades no pudieron validarse por coordenadas; "
                    "el PDF/SVG puede no dibujar ciertos tipos o fuentes.",
                    file=sys.stderr,
                )
            for destination in outputs[1:]:
                temporary_path = staged_by_suffix[destination.suffix.lower()]
                if args.overwrite:
                    os.replace(temporary_path, destination)
                else:
                    temporary_path.rename(destination)

        if args.scale_factor is not None and extraction.units_code == 0:
            print(
                "Advertencia: el DXF declara unidades desconocidas; "
                "el factor aplicado no garantiza una escala física.",
                file=sys.stderr,
            )
        print(
            f"Entidades extraídas: {extraction.extracted_entity_count} "
            f"de {extraction.source_entity_count}."
        )
        for destination in outputs:
            print(f"Creado: {destination}")
        print(f"Creado: {fidelity_destination}")
        if args.allow_unbounded:
            print(f"Creado: {report_destination}")
        return 0
    except (CadProcessingError, OSError, ValueError, MemoryError) as error:
        print(f"Error CAD: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - nada inesperado debe mostrar un traceback
        if os.environ.get("CDE_DEBUG", "").strip().lower() in {"1", "true", "yes", "si", "sí"}:
            traceback.print_exc()
        print(
            f"Error CAD inesperado ({type(error).__name__}): {error}. "
            "No se generaron archivos. Para ver el detalle técnico, ejecute con CDE_DEBUG=1.",
            file=sys.stderr,
        )
        return 1
    finally:
        restore_fonts()


def main(argv: Sequence[str] | None = None) -> int:
    # Antes de leer los argumentos: --help imprime y termina dentro de parse_args, y la
    # consola de Windows (cp1252) no puede escribir caracteres como «→».
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    if args.command == "cad" and args.cad_command == "extract-block":
        if args.scale is not None and args.scale_factor is not None:
            print("Error CAD: No combines --scale con --scale-factor", file=sys.stderr)
            return 1
        if args.scale is not None:
            from cde_exporter.cad.scale import parse_nominal_scale

            try:
                parse_nominal_scale(args.scale)
            except ValueError as error:
                print(f"Error CAD: {error}", file=sys.stderr)
                return 1
    settings = get_settings()
    if args.command == "cad" and args.cad_command in {
        "extract",
        "extract-block",
        "extract-all-blocks",
    }:
        args.font_substitutions = args.font_substitutions or (
            str(settings.font_substitutions) if settings.font_substitutions else None
        )
        args.font_folder = args.font_folder or (
            str(settings.font_folder) if settings.font_folder else None
        )
        args.font_fallback = getattr(args, "font_fallback", None) or settings.font_fallback
    logging.basicConfig(level=settings.log_level)
    logging.getLogger("ezdxf").setLevel(logging.WARNING)

    if args.command == "cad":
        return _run_cad(args)

    if args.command == "validate":
        result = validate_pdf(args.pdf)
        if not result.valid:
            print(f"PDF no válido: {result.error}", file=sys.stderr)
            return 1
        print(f"PDF válido: {result.page_count} página(s)")
        return 0

    try:
        output = convert_page_to_svg(args.pdf, args.page, args.output)
    except PDFConversionError as error:
        print(f"Error de exportación: {error}", file=sys.stderr)
        return 1

    print(f"SVG exportado: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
