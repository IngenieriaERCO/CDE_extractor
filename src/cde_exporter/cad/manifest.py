"""Manifiesto JSON v1 de un detalle extraído: el contrato técnico que consume el componedor.

``build_manifest`` lo construye a partir del detalle finalizado y de los datos medidos del
PDF; ``validate_manifest`` comprueba su estructura con un esquema propio (sin jsonschema).
Solo usa la biblioteca estándar, como ``detail_model``. El formato está documentado en
``docs/manifiesto.md``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from cde_exporter.cad.detail_model import (
    EntityStatus,
    ExtractedDetail,
    check_blocks,
    detail_to_dict,
)
from cde_exporter.cad.notices import (
    Notice,
    NoticeCategory,
    NoticeSeverity,
    collect_notices,
)

DOCUMENT_TYPE = "cde-detail-manifest"
SCHEMA_VERSION = "1.0"
# El manifiesto se publica junto al PDF como <prefijo>.manifest.json, el último de los tres.
MANIFEST_SUFFIX = ".manifest.json"
_MM_PER_POINT = 25.4 / 72
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Opciones con las que se ejecutó la extracción (nombres de archivo, nunca rutas)."""

    hatch_mode: str
    hatch_threshold: int
    solids: str | None
    omit_unsupported: bool
    text_layer: bool
    font_substitutions: str | None
    font_folder: str | None
    font_fallback: str | None
    margin_mm: float


@dataclass(frozen=True, slots=True)
class PdfFacts:
    """Lo medido en el PDF final: páginas, tamaño de la primera página y lo dibujado."""

    page_count: int
    page_size_mm: tuple[float, float]
    drawn_bounds: tuple[tuple[float, float], tuple[float, float]] | None
    cad_to_page_pt: tuple[float, float, float, float, float, float] | None


def file_name(path: str | Path | None) -> str | None:
    """Solo el nombre del archivo, con "/" como separador en cualquier sistema."""
    if path is None:
        return None
    return str(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or None


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def _notice(notice: Notice) -> dict:
    return {
        "code": notice.code,
        "severity": notice.severity.value,
        "message": notice.message,
        "advice": notice.advice,
    }


def _entities(detail: ExtractedDetail) -> dict:
    by_type: dict[str, Counter[str]] = {}
    for row in detail.inventory:
        by_type.setdefault(row.entity_type, Counter())[row.status.value] += row.count
    not_conserved: Counter[tuple[str, str, str, str]] = Counter()
    for item in detail.dispositions:
        if item.status is not EntityStatus.CONSERVED:
            not_conserved[(item.entity_type, item.layer, item.status.value, item.reason or "")] += 1
    hatches = detail.hatches
    return {
        "total": sum(row.count for row in detail.inventory),
        "by_type": [
            {
                "entity_type": entity_type,
                "total": sum(statuses.values()),
                "by_status": dict(sorted(statuses.items())),
            }
            for entity_type, statuses in sorted(by_type.items())
        ],
        "by_layer": [
            {
                "entity_type": row.entity_type,
                "layer": row.layer,
                "status": row.status.value,
                "count": row.count,
            }
            for row in sorted(
                detail.inventory, key=lambda item: (item.entity_type, item.layer, item.status)
            )
        ],
        "not_conserved": [
            {
                "entity_type": entity_type,
                "layer": layer,
                "status": status,
                "reason": reason or None,
                "count": count,
            }
            for (entity_type, layer, status, reason), count in sorted(not_conserved.items())
        ],
        "hatches": {
            "mode": hatches.mode if hatches else None,
            "total": hatches.total if hatches else 0,
            "simplified": hatches.simplified if hatches else 0,
            "groups": [
                {
                    "kind": group.kind,
                    "pattern": group.pattern,
                    "scale": group.scale,
                    "count": group.count,
                    "treatment": group.operation,
                    "reason": group.reason,
                }
                for group in (hatches.groups if hatches else ())
            ],
        },
    }


def _drawing(facts: PdfFacts) -> dict | None:
    if facts.drawn_bounds is None or facts.cad_to_page_pt is None:
        return None
    a, b, c, d, e, f = (value * _MM_PER_POINT for value in facts.cad_to_page_pt)
    (x0, y0), (x1, y1) = facts.drawn_bounds
    corners = [(a * x + c * y + e, b * x + d * y + f) for x in (x0, x1) for y in (y0, y1)]
    return {
        "cad_bounds": {"min": [x0, y0], "max": [x1, y1]},
        "page_bounds_mm": {
            "x0": min(x for x, _ in corners),
            "y0": min(y for _, y in corners),
            "x1": max(x for x, _ in corners),
            "y1": max(y for _, y in corners),
        },
        "cad_to_page": {
            "units": "mm",
            "origin": "top-left",
            "y_axis": "down",
            "matrix": [a, b, c, d, e, f],
        },
    }


def _scale(detail: ExtractedDetail) -> dict:
    nominal = detail.scale.nominal_denominator if detail.scale else None
    guaranteed = detail.physical_scale_guaranteed is True
    if guaranteed:
        reason = None
    elif nominal is None:
        reason = "Sin escala nominal (--scale 1:N): el PDF no tiene una escala física."
    else:
        reason = detail.physical_scale_note or "No se pudo comprobar la escala física."
    return {
        "requested": f"1:{nominal:g}" if nominal is not None else None,
        "denominator": nominal,
        "factor_mm_per_cad_unit": detail.output_factor_mm_per_unit,
        "physical_scale_guaranteed": guaranteed,
        "physical_scale_reason": reason,
    }


def build_manifest(
    detail: ExtractedDetail,
    *,
    source_dxf: str | Path,
    pdf_file: str | Path,
    pdf: PdfFacts,
    options: RunOptions,
) -> dict:
    """Manifiesto v1 del detalle finalizado (``detail.result`` presente).

    Lee ``source_dxf`` y ``pdf_file`` solo para su hash y tamaño; del DXF guarda el nombre,
    nunca la ruta.
    """
    if detail.result is None:
        raise ValueError("El manifiesto necesita el detalle finalizado (con resultado)")
    dxf_hash, dxf_bytes = _file_digest(Path(source_dxf))
    pdf_hash, pdf_bytes = _file_digest(Path(pdf_file))
    reference = detail.reference
    notices = collect_notices(detail)
    by_category = {category.value: [] for category in NoticeCategory}
    for notice in notices:
        by_category[notice.category.value].append(_notice(notice))
    severities = Counter(notice.severity.value for notice in notices)
    return {
        "document_type": DOCUMENT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "model_version": detail_to_dict(detail)["model_version"],
        "generated_at": detail.extracted_at,
        "exporter": {
            "version": detail.exporter_version,
            "ezdxf": _version("ezdxf"),
            "pymupdf": _version("PyMuPDF"),
        },
        "source": {
            "dxf_name": file_name(source_dxf),
            "dxf_sha256": dxf_hash,
            "dxf_bytes": dxf_bytes,
            "block_name": detail.source.block_name,
            "block_definition_handle": detail.block_definition_handle,
            "reference": {
                "handle": reference.handle,
                "occurrence": reference.occurrence,
                "total_occurrences": reference.total_occurrences,
                "layer": reference.layer,
                "selection_method": (
                    detail.selection_method.value if detail.selection_method else None
                ),
                "insertion_xyz": list(reference.insertion_xyz),
                "rotation_deg": reference.rotation_deg,
                "scale_xyz": list(reference.scale_xyz),
            },
        },
        "units": {
            "declared_code": detail.units.declared_code,
            "name": detail.units.name,
            "status": detail.units.status,
            "millimeters_per_unit": detail.units.millimeters_per_unit,
            "user_override": detail.user_units_override,
        },
        "scale": _scale(detail),
        "pdf": {
            "file_name": file_name(pdf_file),
            "bytes": pdf_bytes,
            "sha256": pdf_hash,
            "page_count": pdf.page_count,
            "page_size_mm": list(pdf.page_size_mm),
            "expected_page_size_mm": (
                list(detail.page_size_mm) if detail.page_size_mm is not None else None
            ),
            "margin_mm": detail.margin_mm,
            "content_size_mm": (
                list(detail.content_size_mm) if detail.content_size_mm is not None else None
            ),
            "drawing": _drawing(pdf),
        },
        "entities": _entities(detail),
        "fonts": [
            {
                "requested": font.requested,
                "resolved": font.resolved,
                "found": font.found,
                "uses": font.uses,
                "origin": font.origin,
            }
            for font in detail.fonts
        ],
        "dependencies": [
            {
                "kind": dependency.kind,
                "reference": file_name(dependency.reference),
                "required": dependency.required,
                "resolved": dependency.resolved,
                "detail": dependency.detail,
            }
            for dependency in detail.dependencies
        ],
        "notices": {
            "counts": {severity.value: severities[severity.value] for severity in NoticeSeverity},
            "by_category": by_category,
        },
        "validation": {
            "status": detail.result.status.value,
            "checks": [
                {
                    "name": check.name,
                    "passed": check.passed,
                    "blocking": check_blocks(check.name),
                    "detail": check.detail,
                }
                for check in detail.result.validations
            ],
        },
        "options": {
            "hatch_mode": options.hatch_mode,
            "hatch_threshold": options.hatch_threshold,
            "solids": options.solids,
            "omit_unsupported": options.omit_unsupported,
            "text_layer": options.text_layer,
            "font_substitutions": file_name(options.font_substitutions),
            "font_folder": file_name(options.font_folder),
            "font_fallback": file_name(options.font_fallback),
            "margin_mm": options.margin_mm,
        },
    }


# --------------------------------------------------------------------------- validación
# Esquema declarativo: un dict es un objeto con exactamente esas claves; ``_List(x)`` una
# lista de x; ``_Null(x)`` admite None; ``_Enum`` un texto de un conjunto; ``_Name`` un
# nombre de archivo (sin "\" ni "/"); ``_Hash`` un SHA-256 en hexadecimal; ``_Floats(n)`` una
# lista de n números.


@dataclass(frozen=True)
class _List:
    item: object


@dataclass(frozen=True)
class _Null:
    item: object


@dataclass(frozen=True)
class _Enum:
    values: frozenset[str]


@dataclass(frozen=True)
class _Floats:
    length: int


class _Name:
    pass


class _Hash:
    pass


_NUMBER = float
_SEVERITIES = _Enum(frozenset(severity.value for severity in NoticeSeverity))
_NOTICE = {"code": str, "severity": _SEVERITIES, "message": str, "advice": _Null(str)}
_SCHEMA = {
    "document_type": _Enum(frozenset({DOCUMENT_TYPE})),
    "schema_version": str,
    "model_version": str,
    "generated_at": _Null(str),
    "exporter": {"version": _Null(str), "ezdxf": _Null(str), "pymupdf": _Null(str)},
    "source": {
        "dxf_name": _Name(),
        "dxf_sha256": _Hash(),
        "dxf_bytes": int,
        "block_name": str,
        "block_definition_handle": _Null(str),
        "reference": {
            "handle": _Null(str),
            "occurrence": int,
            "total_occurrences": _Null(int),
            "layer": str,
            "selection_method": _Null(str),
            "insertion_xyz": _Floats(3),
            "rotation_deg": _NUMBER,
            "scale_xyz": _Floats(3),
        },
    },
    "units": {
        "declared_code": _Null(int),
        "name": str,
        "status": str,
        "millimeters_per_unit": _Null(_NUMBER),
        "user_override": _Null(str),
    },
    "scale": {
        "requested": _Null(str),
        "denominator": _Null(_NUMBER),
        "factor_mm_per_cad_unit": _Null(_NUMBER),
        "physical_scale_guaranteed": bool,
        "physical_scale_reason": _Null(str),
    },
    "pdf": {
        "file_name": _Name(),
        "bytes": int,
        "sha256": _Hash(),
        "page_count": int,
        "page_size_mm": _Floats(2),
        "expected_page_size_mm": _Null(_Floats(2)),
        "margin_mm": _Null(_NUMBER),
        "content_size_mm": _Null(_Floats(2)),
        "drawing": _Null(
            {
                "cad_bounds": {"min": _Floats(2), "max": _Floats(2)},
                "page_bounds_mm": {"x0": _NUMBER, "y0": _NUMBER, "x1": _NUMBER, "y1": _NUMBER},
                "cad_to_page": {
                    "units": _Enum(frozenset({"mm"})),
                    "origin": _Enum(frozenset({"top-left"})),
                    "y_axis": _Enum(frozenset({"down"})),
                    "matrix": _Floats(6),
                },
            }
        ),
    },
    "entities": {
        "total": int,
        "by_type": _List({"entity_type": str, "total": int, "by_status": dict}),
        "by_layer": _List({"entity_type": str, "layer": str, "status": str, "count": int}),
        "not_conserved": _List(
            {
                "entity_type": str,
                "layer": str,
                "status": str,
                "reason": _Null(str),
                "count": int,
            }
        ),
        "hatches": {
            "mode": _Null(str),
            "total": int,
            "simplified": int,
            "groups": _List(
                {
                    "kind": str,
                    "pattern": str,
                    "scale": _Null(_NUMBER),
                    "count": int,
                    "treatment": str,
                    "reason": _Null(str),
                }
            ),
        },
    },
    "fonts": _List(
        {"requested": str, "resolved": str, "found": bool, "uses": int, "origin": _Null(str)}
    ),
    "dependencies": _List(
        {
            "kind": str,
            "reference": _Null(str),
            "required": bool,
            "resolved": bool,
            "detail": _Null(str),
        }
    ),
    "notices": {
        "counts": {severity.value: int for severity in NoticeSeverity},
        "by_category": {category.value: _List(_NOTICE) for category in NoticeCategory},
    },
    "validation": {
        "status": _Enum(frozenset({"complete", "complete_with_warnings"})),
        "checks": _List({"name": str, "passed": bool, "blocking": bool, "detail": _Null(str)}),
    },
    "options": {
        "hatch_mode": str,
        "hatch_threshold": int,
        "solids": _Null(str),
        "omit_unsupported": bool,
        "text_layer": bool,
        "font_substitutions": _Null(_Name()),
        "font_folder": _Null(_Name()),
        "font_fallback": _Null(_Name()),
        "margin_mm": _NUMBER,
    },
}


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check(value: object, spec: object, where: str, errors: list[str]) -> None:
    if isinstance(spec, _Null):
        if value is not None:
            _check(value, spec.item, where, errors)
        return
    if value is None:
        errors.append(f"{where}: falta el valor (es None)")
        return
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            errors.append(f"{where}: se esperaba un objeto")
            return
        if not spec:  # dict vacío en el esquema: cualquier objeto
            return
        for key, item_spec in spec.items():
            if key not in value:
                errors.append(f"{where}.{key}: falta la clave")
            else:
                _check(value[key], item_spec, f"{where}.{key}", errors)
        for key in value.keys() - spec.keys():
            errors.append(f"{where}.{key}: clave no prevista en el esquema")
    elif spec is dict:
        if not isinstance(value, dict):
            errors.append(f"{where}: se esperaba un objeto")
    elif isinstance(spec, _List):
        if not isinstance(value, list):
            errors.append(f"{where}: se esperaba una lista")
            return
        for index, item in enumerate(value):
            _check(item, spec.item, f"{where}[{index}]", errors)
    elif isinstance(spec, _Enum):
        if value not in spec.values:
            errors.append(f"{where}: {value!r} no es uno de {sorted(spec.values)}")
    elif isinstance(spec, _Floats):
        if (
            not isinstance(value, list)
            or len(value) != spec.length
            or not all(_is_number(item) for item in value)
        ):
            errors.append(f"{where}: se esperaba una lista de {spec.length} números")
    elif isinstance(spec, _Name):
        if not isinstance(value, str) or not value or "\\" in value or "/" in value:
            errors.append(f"{where}: se esperaba un nombre de archivo, sin ruta ni '\\'")
    elif isinstance(spec, _Hash):
        if not isinstance(value, str) or not _SHA256.match(value):
            errors.append(f"{where}: se esperaba un SHA-256 en hexadecimal (64 caracteres)")
    elif spec is _NUMBER:
        if not _is_number(value):
            errors.append(f"{where}: se esperaba un número finito")
    elif spec is int:
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"{where}: se esperaba un entero")
    elif not isinstance(value, spec):
        errors.append(f"{where}: se esperaba {spec.__name__}")


def validate_manifest(data: object) -> tuple[str, ...]:
    """Errores de estructura del manifiesto; vacío si es válido.

    Además del esquema, comprueba la versión mayor del esquema (1), que el número de
    páginas sea al menos 1, que los recuentos por severidad cuadren con los avisos y que no
    haya ningún "\\" en los textos de tipo ruta.
    """
    errors: list[str] = []
    _check(data, _SCHEMA, "manifiesto", errors)
    if errors or not isinstance(data, dict):
        return tuple(errors)
    if data["schema_version"].split(".", 1)[0] != SCHEMA_VERSION.split(".", 1)[0]:
        errors.append(
            f"manifiesto.schema_version: versión {data['schema_version']} no compatible con "
            f"{SCHEMA_VERSION}"
        )
    if data["pdf"]["page_count"] < 1:
        errors.append("manifiesto.pdf.page_count: el PDF debe tener al menos una página")
    counted = Counter(
        notice["severity"]
        for notices in data["notices"]["by_category"].values()
        for notice in notices
    )
    if any(counted[severity] != count for severity, count in data["notices"]["counts"].items()):
        errors.append("manifiesto.notices.counts: no cuadra con los avisos por categoría")
    if data["validation"]["status"] == "complete" and (
        counted["warning"] or counted["critical"]
    ):
        errors.append("manifiesto.validation.status: 'complete' con avisos warning o critical")
    return tuple(errors)


def manifest_path_for(path: str | Path) -> Path:
    """Manifiesto que corresponde a un PDF (o el propio manifiesto, si ya lo es)."""
    path = Path(path)
    if path.name.endswith(MANIFEST_SUFFIX):
        return path
    return path.with_name(f"{path.stem}{MANIFEST_SUFFIX}")


def verify_package(path: str | Path) -> tuple[str, ...]:
    """Problemas del paquete publicado de un detalle; vacío si está completo y es coherente.

    ``path`` es el PDF o su manifiesto. El manifiesto se publica el último: un PDF sin
    manifiesto es un paquete incompleto (p. ej. un corte a mitad de la publicación). Si hay
    manifiesto, debe cumplir su esquema y el PDF que nombra debe existir junto a él con el
    mismo SHA-256 y los mismos bytes; si se reemplazó el PDF pero no el manifiesto, el hash
    no coincide.
    """
    manifest_file = manifest_path_for(path)
    if not manifest_file.is_file():
        return (f"Paquete incompleto: falta el manifiesto {manifest_file.name}.",)
    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return (f"No se pudo leer el manifiesto {manifest_file.name}: {error}",)
    problems = list(validate_manifest(data))
    if problems:
        return tuple(problems)
    pdf_name = data["pdf"]["file_name"]
    pdf_file = manifest_file.with_name(pdf_name)
    if Path(path).suffix.lower() == ".pdf" and Path(path).name != pdf_name:
        problems.append(f"El manifiesto describe {pdf_name}, no {Path(path).name}.")
    if not pdf_file.is_file():
        return (*problems, f"Paquete incompleto: falta el PDF {pdf_name}.")
    digest, size = _file_digest(pdf_file)
    if digest != data["pdf"]["sha256"]:
        problems.append(
            f"El SHA-256 de {pdf_name} no coincide con el del manifiesto: el PDF no es el que "
            "describe."
        )
    if size != data["pdf"]["bytes"]:
        problems.append(
            f"{pdf_name} mide {size} bytes; el manifiesto dice {data['pdf']['bytes']}."
        )
    return tuple(problems)
