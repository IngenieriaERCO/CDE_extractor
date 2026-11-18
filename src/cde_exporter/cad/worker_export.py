"""Orden ``export`` del trabajador (docs/protocolo_trabajador.md, sección 5).

Exporta varias referencias del DXF ya cargado, cada una con su escala y su nombre de paquete,
con el mismo flujo que ``extract-block`` (``block_commands.run_extract_block``) y sin volver a
leer el DXF:
- Antes de empezar se comprueba todo (referencias, escalas, nombres, rutas, archivos que ya
  existen, opciones); si algo falla, error y no se exporta nada.
- Los trabajos se hacen en orden; si uno falla, los demás siguen y el que falla no deja
  archivos. Cada paquete se publica de forma atómica (manifiesto el último).
- Avance por etapas (``run_hooks``) y cancelación entre etapas y mientras se dibuja.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

from cde_exporter.cad import block_commands, run_hooks
from cde_exporter.cad import worker_protocol as protocol
from cde_exporter.cad.block_commands import BlockingNoticesError
from cde_exporter.cad.dxf_session import clean_temporaries
from cde_exporter.cad.file_names import PACKAGE_NAME_MAX, PACKAGE_PATH_MAX, is_package_name
from cde_exporter.cad.hidden_layers import ALL_LAYERS, loose_summary
from cde_exporter.cad.manifest import MANIFEST_SUFFIX
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.pdf_checks import PdfValidationError
from cde_exporter.cad.project_commands import (
    SUMMARY_NAME,
    OmittedItem,
    PlannedPackage,
    _package_row,
    _reference_row,
    _write_json_atomic,
    project_summary,
)
from cde_exporter.cad.scale import parse_nominal_scale
from cde_exporter.cad.worker_protocol import Request, WorkerError
from cde_exporter.config import get_settings

# Peso de cada etapa en el avance de un trabajo (dibujar es lo que más tarda).
STAGE_WEIGHTS = {
    "extract": 0.10,
    "analyze": 0.10,
    "draw": 0.50,
    "pdf": 0.15,
    "checks": 0.10,
    "publish": 0.05,
}
PROGRESS_INTERVAL = 0.25
HATCH_MODES = ("normal", "outline", "solid", "ignore", "auto")
DEFAULT_OPTIONS = {
    "hatch_mode": "auto",
    "hatch_threshold": 50000,
    "solids": None,
    "omit_unsupported": False,
    "text_layer": True,
    "include_hidden_layers": None,
    "font_substitutions": None,
    "font_folder": None,
    "font_fallback": None,
    "overwrite": False,
    "write_summary": False,
}


@dataclass
class _Job:
    id: str
    block: dict  # el de ``load`` (blocks[])
    scale: str
    package: str
    status: str = "pending"
    result: dict | None = None
    detail: object = None  # detalle finalizado (para resumen_proyecto.json)


def _bad(message: str, **details) -> WorkerError:
    return WorkerError(protocol.BAD_REQUEST, message, details=details)


def _options(data: dict, session) -> dict:
    """Opciones comunes, con sus valores por defecto y comprobadas."""
    given = data.get("options", {}) or {}
    if not isinstance(given, dict):
        raise _bad("«options» debe ser un objeto JSON {…}.")
    options = {**DEFAULT_OPTIONS, **{k: v for k, v in given.items() if k in DEFAULT_OPTIONS}}
    if "units" in given and given["units"] != session.units:
        raise _bad(
            f"«units» ({given['units']!r}) no es el de «load» ({session.units!r}).",
            option="units",
        )
    if "margin_mm" in given:
        margin = given["margin_mm"]
        if (
            isinstance(margin, bool)
            or not isinstance(margin, (int, float))
            or not math.isclose(float(margin), session.margin_mm)
        ):
            raise _bad(
                f"«margin_mm» ({margin!r}) no es el de «load» ({session.margin_mm:g}).",
                option="margin_mm",
            )
    if options["hatch_mode"] not in HATCH_MODES:
        raise _bad("«hatch_mode» debe ser uno de: " + ", ".join(HATCH_MODES) + ".")
    threshold = options["hatch_threshold"]
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
        raise _bad("«hatch_threshold» debe ser un entero ≥ 0.")
    if options["solids"] not in (None, "footprint"):
        raise _bad('«solids» debe ser null o "footprint".')
    for key in ("omit_unsupported", "text_layer", "overwrite", "write_summary"):
        if not isinstance(options[key], bool):
            raise _bad(f"«{key}» debe ser true o false.")
    for key in ("font_substitutions", "font_folder", "font_fallback"):
        value = options[key]
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise _bad(f"«{key}» debe ser null o una ruta.")
        path = Path(value)
        if key == "font_substitutions" and not path.is_file():
            raise _bad(f"No existe el archivo de «{key}»: {value}", option=key)
        if key == "font_folder" and not path.is_dir():
            raise _bad(f"No existe la carpeta de «{key}»: {value}", option=key)
    layers = options["include_hidden_layers"]
    if layers is not None:
        if (
            not isinstance(layers, list)
            or not layers
            or not all(isinstance(item, str) and item.strip() for item in layers)
        ):
            raise _bad('«include_hidden_layers» debe ser null, ["*"] o una lista de capas.')
        if any("," in item for item in layers):
            raise _bad("Un nombre de capa de «include_hidden_layers» no puede llevar comas.")
        existing = {str(layer.dxf.name).casefold() for layer in session.loaded.document.layers}
        unknown = [
            item for item in layers if item != ALL_LAYERS and item.casefold() not in existing
        ]
        if unknown:
            raise _bad(
                "Capas que no existen en el DXF: " + ", ".join(f"«{n}»" for n in unknown) + ".",
                layers=unknown,
            )
    return options


def _outputs(output_dir: Path, package: str) -> list[Path]:
    paths = [
        output_dir / f"{package}.pdf",
        output_dir / f"{package}.txt",
        output_dir / f"{package}{MANIFEST_SUFFIX}",
    ]
    if block_commands._keep_intermediates():
        paths += [output_dir / f"{package}.dxf", output_dir / f"{package}.svg"]
    return paths


def plan_export(data: dict, session, find_reference) -> tuple[Path, list[_Job], dict]:
    """Comprueba el pedido entero antes de exportar nada."""
    raw_dir = data.get("output_dir")
    if not isinstance(raw_dir, str) or not raw_dir:
        raise WorkerError(protocol.OUTPUT_INVALID, "Falta «output_dir» (carpeta de salida).")
    output_dir = Path(raw_dir)
    if not output_dir.is_absolute():
        raise WorkerError(
            protocol.OUTPUT_INVALID, f"«output_dir» debe ser una ruta absoluta: {raw_dir}"
        )
    if output_dir.exists() and not output_dir.is_dir():
        raise WorkerError(protocol.OUTPUT_INVALID, f"«output_dir» no es una carpeta: {raw_dir}")
    options = _options(data, session)
    raw_jobs = data.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise _bad("«jobs» debe ser una lista con al menos un trabajo.")
    jobs: list[_Job] = []
    ids: set[str] = set()
    packages: dict[str, str] = {}
    source = session.loaded.path.resolve()
    for index, raw in enumerate(raw_jobs, start=1):
        if not isinstance(raw, dict):
            raise _bad(f"El trabajo {index} debe ser un objeto JSON {{…}}.")
        job_id = raw.get("job", f"t{index}")
        if not isinstance(job_id, str) or not job_id or job_id in ids:
            raise _bad(f"El trabajo {index} necesita un «job» de texto único.", job=job_id)
        ids.add(job_id)
        try:
            block = find_reference(session.inventory, raw)
        except WorkerError as failure:
            failure.details = {**failure.details, "job": job_id}
            raise
        scale = raw.get("scale")
        try:
            if not isinstance(scale, str):
                raise CadProcessingError("falta la escala 1:N")
            parse_nominal_scale(scale)
        except (CadProcessingError, ValueError) as failure:
            raise _bad(f"Escala no válida en «{job_id}»: {failure}", job=job_id) from failure
        package = raw.get("package", block["file_stem"])
        if not isinstance(package, str) or not is_package_name(package):
            raise WorkerError(
                protocol.OUTPUT_INVALID,
                f"Nombre de paquete no válido en «{job_id}»: {package!r}.",
                "Use solo letras sin acentos, números, punto, guion y guion bajo, sin empezar "
                f"por punto y con {PACKAGE_NAME_MAX} caracteres como mucho.",
                {"job": job_id, "package": package},
            )
        if package.casefold() in packages:
            raise WorkerError(
                protocol.OUTPUT_INVALID,
                f"Los trabajos «{packages[package.casefold()]}» y «{job_id}» usan el mismo "
                f"paquete «{package}».",
                details={"job": job_id, "package": package},
            )
        packages[package.casefold()] = job_id
        longest = output_dir / f"{package}{MANIFEST_SUFFIX}"
        if len(str(longest)) > PACKAGE_PATH_MAX:
            raise WorkerError(
                protocol.OUTPUT_INVALID,
                f"La ruta de salida de «{job_id}» pasa de {PACKAGE_PATH_MAX} caracteres "
                f"({len(str(longest))}).",
                "Use una carpeta de salida más corta o un nombre de paquete más corto.",
                {"job": job_id, "path": str(longest)},
            )
        for path in _outputs(output_dir, package):
            if path.resolve() == source:
                raise WorkerError(
                    protocol.OUTPUT_INVALID,
                    "La salida no puede sobrescribir el DXF de origen.",
                    details={"job": job_id, "path": str(path)},
                )
            if path.exists() and not options["overwrite"]:
                raise WorkerError(
                    protocol.OUTPUT_EXISTS,
                    f"El archivo de salida ya existe: {path}",
                    "Use otro nombre de paquete u «overwrite»: true.",
                    {"job": job_id, "path": str(path)},
                )
        jobs.append(_Job(job_id, block, scale.strip(), package))
    summary = output_dir / SUMMARY_NAME
    if options["write_summary"] and summary.exists() and not options["overwrite"]:
        raise WorkerError(
            protocol.OUTPUT_EXISTS,
            f"El archivo de salida ya existe: {summary}",
            "Use «overwrite»: true o «write_summary»: false.",
            {"path": str(summary)},
        )
    return output_dir, jobs, options


def _block_args(session, job: _Job, output_dir: Path, options: dict):
    """Argumentos de ``extract-block`` para un trabajo (los valores por defecto del CLI)."""
    from cde_exporter.cli import build_parser

    block = job.block
    args = build_parser().parse_args(
        [
            "cad",
            "extract-block",
            str(session.loaded.path),
            "--block-name",
            block["name"],
            "--output-prefix",
            str(output_dir / job.package),
            "--scale",
            job.scale,
        ]
    )
    layers = options["include_hidden_layers"]
    args.occurrence = block["occurrence"] if block["total_occurrences"] > 1 else None
    args.units = session.units
    args.margin_mm = session.margin_mm
    args.hatch_mode = options["hatch_mode"]
    args.hatch_threshold = options["hatch_threshold"]
    args.solids = options["solids"]
    args.omit_unsupported = options["omit_unsupported"]
    args.no_text_layer = not options["text_layer"]
    # Fuentes: lo que no se pida sale de la configuración (.env), como en ``cli.main``.
    settings = get_settings()
    args.font_substitutions = options["font_substitutions"] or (
        str(settings.font_substitutions) if settings.font_substitutions else None
    )
    args.font_folder = options["font_folder"] or (
        str(settings.font_folder) if settings.font_folder else None
    )
    args.font_fallback = options["font_fallback"] or settings.font_fallback
    args.overwrite = options["overwrite"]
    args.include_hidden_layers = (
        None if layers is None else (ALL_LAYERS if ALL_LAYERS in layers else ",".join(layers))
    )
    args.source_pruning = session.loaded.pruned
    args.source_digest = (session.loaded.sha256, session.loaded.bytes)
    return args


class _ExportProgress:
    """Eventos ``progress`` de ``export``: avance total sobre todos los trabajos, solo sube."""

    def __init__(self, send, request: Request, total_jobs: int, state) -> None:
        self.send, self.request, self.total, self.state = send, request, total_jobs, state
        self.percent = 0.0
        self.last = 0.0
        self.job: _Job | None = None
        self.index = 0
        self.stage = None
        self.order = list(STAGE_WEIGHTS)

    def __call__(self, stage: str, fraction: float, text: str, force: bool = False) -> None:
        self.state.stage = stage
        position = self.order.index(stage)
        done = sum(STAGE_WEIGHTS[name] for name in self.order[:position])
        job_fraction = done + STAGE_WEIGHTS[stage] * min(max(fraction, 0.0), 1.0)
        percent = max(self.percent, 100 * (self.index - 1 + job_fraction) / self.total)
        now = time.perf_counter()
        if not force and stage == self.stage and now - self.last < PROGRESS_INTERVAL:
            return
        self.stage, self.last, self.percent = stage, now, percent
        self.send(
            protocol.progress(
                self.request.id,
                {
                    "job": self.job.id if self.job else None,
                    "job_index": self.index,
                    "job_total": self.total,
                    "stage": stage,
                    "stage_index": position + 1,
                    "stage_total": len(self.order),
                    "percent": round(percent, 1),
                    "message": text,
                },
            )
        )


def _manifest_facts(output_dir: Path, package: str) -> dict:
    data = json.loads((output_dir / f"{package}{MANIFEST_SUFFIX}").read_text(encoding="utf-8"))
    notices = [notice for group in data["notices"]["by_category"].values() for notice in group]
    return {
        "validation_status": data["validation"]["status"],
        "physical_scale_guaranteed": data["scale"]["physical_scale_guaranteed"],
        "page_size_mm": data["pdf"]["page_size_mm"],
        "notices": notices,
    }


def _job_row(job: _Job, output_dir: Path, seconds: float | None, error=None, facts=None) -> dict:
    published = job.status == "published"
    return {
        "job": job.id,
        "ref": job.block["ref"],
        "handle": job.block["handle"],
        "status": job.status,
        "package": {
            "pdf": str(output_dir / f"{job.package}.pdf"),
            "txt": str(output_dir / f"{job.package}.txt"),
            "manifest": str(output_dir / f"{job.package}{MANIFEST_SUFFIX}"),
        }
        if published
        else None,
        "validation_status": facts["validation_status"] if facts else None,
        "physical_scale_guaranteed": facts["physical_scale_guaranteed"] if facts else None,
        "page_size_mm": facts["page_size_mm"] if facts else None,
        "notices": facts["notices"] if facts else [],
        "error": error,
        "seconds": None if seconds is None else round(seconds, 1),
    }


def _failure(error: BaseException) -> dict:
    """Error de un trabajo (sección 8, errores por trabajo)."""
    if isinstance(error, BlockingNoticesError):
        return {
            "code": "CRITICAL_NOTICE",
            "message": str(error),
            "details": {
                "notices": [
                    {"code": notice.code, "message": notice.message} for notice in error.notices
                ]
            },
        }
    if isinstance(error, CadProcessingError) and isinstance(error.__cause__, PdfValidationError):
        return {"code": "VALIDATION_FAILED", "message": str(error), "details": {}}
    if isinstance(error, (CadProcessingError, OSError, ValueError, MemoryError)):
        return {"code": "EXPORT_FAILED", "message": str(error), "details": {}}
    return {
        "code": "EXPORT_FAILED",
        "message": f"Error CAD inesperado ({type(error).__name__}): {error}",
        "details": {"type": type(error).__name__},
    }


def _totals(jobs: list[_Job]) -> dict:
    return {
        status: sum(1 for job in jobs if job.status == status)
        for status in ("published", "failed", "cancelled")
    }


def _write_summary(session, output_dir: Path, jobs: list[_Job]) -> Path:
    published, failed = [], []
    for job in jobs:
        block = job.block
        package = PlannedPackage(
            block["name"],
            block["occurrence"],
            block["total_occurrences"],
            block["handle"],
            job.package,
            job.scale,
            None,
        )
        if job.status == "published":
            published.append(_package_row(package, job.result["validation_status"], job.detail))
        else:
            reason = (job.result or {}).get("error") or {"message": job.status}
            failed.append({**_reference_row(package), "reason": reason["message"]})
    omitted = tuple(
        OmittedItem(item["name"], item["occurrence"], item["total_occurrences"], item["reason"])
        for item in session.inventory["omitted"]
    )
    document = session.loaded.document
    summary = project_summary(
        session.loaded.path,
        published,
        omitted,
        failed,
        loose_summary(document),
        session.loaded.pruned,
        default_scale=None,
        scale_map=None,
    )
    path = output_dir / SUMMARY_NAME
    _write_json_atomic(path, summary)
    return path


def run_export(request: Request, session, *, send, state, check, cancelled_type, session_check):
    """Hace los trabajos en orden. Devuelve el ``data`` de la respuesta, o lanza
    ``WorkerError`` (``CANCELLED`` o ``DXF_CHANGED``) con ``details.jobs``."""
    from cde_exporter.cad.worker import find_reference

    output_dir, jobs, options = plan_export(request.data, session, find_reference)
    output_dir.mkdir(parents=True, exist_ok=True)
    clean_temporaries(output_dir)  # «.cde-tmp-» de trabajadores que ya no están
    progress = _ExportProgress(send, request, len(jobs), state)
    rows: dict[str, dict] = {}

    def stop(code: str, message: str, advice: str | None = None) -> WorkerError:
        for job in jobs:
            if job.status in ("pending", "running"):
                job.status = "cancelled"
            rows.setdefault(job.id, _job_row(job, output_dir, None))
        totals = _totals(jobs)
        return WorkerError(
            code,
            message.format(**totals),
            advice,
            {"jobs": [rows[job.id] for job in jobs], "totals": totals},
        )

    for index, job in enumerate(jobs, start=1):
        try:
            check()
        except cancelled_type:
            raise stop(
                protocol.CANCELLED,
                "Exportación cancelada: {published} publicados, {cancelled} cancelados.",
            ) from None
        changed = session_check()
        if changed is not None:
            raise stop(
                protocol.DXF_CHANGED,
                changed + " Paquetes publicados antes del cambio: {published}.",
                "Vuelva a cargarlo con «load».",
            )
        job.status = "running"
        progress.job, progress.index = job, index
        name = job.block["name"]
        definition = session.loaded.document.blocks.get(name)
        total = max(1, (len(definition) if definition is not None else 0))
        drawn = 0

        def on_stage(stage: str, job=job) -> None:
            check()
            progress(stage, 0.0, f"{job.block['ref']}: {stage}", force=True)

        def on_entity(depth: int, job=job, total=total) -> None:
            nonlocal drawn
            check()
            if depth == 1:
                drawn += 1
                progress(
                    "draw",
                    drawn / total,
                    f"Dibujando {job.block['ref']}: {min(drawn, total)} de {total} entidades",
                )

        started = time.perf_counter()
        details: list = []
        try:
            with run_hooks.hooks(on_stage, on_entity):
                block_commands.run_extract_block(
                    _block_args(session, job, output_dir, options),
                    session.loaded.document,
                    details,
                )
        except cancelled_type:
            job.status = "cancelled"
            rows[job.id] = _job_row(job, output_dir, time.perf_counter() - started)
            raise stop(
                protocol.CANCELLED,
                "Exportación cancelada: {published} publicados, {cancelled} cancelados.",
            ) from None
        except KeyboardInterrupt:
            raise
        except Exception as error:  # noqa: BLE001 - un trabajo no detiene a los demás
            job.status = "failed"
            failure = _failure(error)
            job.result = {"error": failure}
            rows[job.id] = _job_row(job, output_dir, time.perf_counter() - started, failure)
            continue
        job.status = "published"
        job.detail = details[-1] if details else None
        facts = _manifest_facts(output_dir, job.package)
        job.result = facts
        rows[job.id] = _job_row(job, output_dir, time.perf_counter() - started, None, facts)
        progress("publish", 1.0, f"{job.block['ref']}: publicado", force=True)
    summary = _write_summary(session, output_dir, jobs) if options["write_summary"] else None
    return {
        "output_dir": str(output_dir),
        "jobs": [rows[job.id] for job in jobs],
        "summary": None if summary is None else str(summary),
        "totals": _totals(jobs),
    }
