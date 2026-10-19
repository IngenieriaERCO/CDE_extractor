"""Configuración opcional de fuentes para el dibujo ezdxf 1.4.4."""

from __future__ import annotations

import json
import platform
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import ezdxf
from ezdxf.fonts import fonts
from ezdxf.fonts.font_manager import get_ttf_font_face
from fontTools.ttLib import TTFont

from cde_exporter.cad.models import CadProcessingError

_INLINE_FONT = re.compile(r"\\[fF]([^|;]+)(?:\|[^;]*)?;", re.IGNORECASE)
_ACTIVE_SUBSTITUTIONS: dict[str, tuple[str, str, str, int]] = {}


def active_substitutions() -> dict[str, tuple[str, str, str, int]]:
    return dict(_ACTIVE_SUBSTITUTIONS)


def _fallback_path(name: str, folder: str | None) -> Path:
    candidates = [Path(name).expanduser()]
    if folder:
        candidates.append(Path(folder).expanduser() / name)
    system = platform.system()
    if system == "Windows":
        candidates.append(Path("C:/Windows/Fonts") / name)
    elif system == "Darwin":
        candidates.extend((Path("/System/Library/Fonts") / name, Path("/Library/Fonts") / name))
    else:
        candidates.extend(
            (
                Path("/usr/share/fonts/truetype/msttcorefonts") / name,
                Path("/usr/share/fonts/truetype/msttcorefonts/Arial.ttf"),
            )
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise CadProcessingError(f"No se encontró la fuente de respaldo '{name}'")


def _font_family(filename: str) -> str:
    import logging

    logger = logging.getLogger(
        "fontTools.ttLib.tables._n_a_m_e"
    )

    class _ExpectedNameTableMessage(logging.Filter):
        def filter(self, record):
            return (
                "name table stringOffset incorrect"
                not in record.getMessage()
            )

    message_filter = _ExpectedNameTableMessage()
    logger.addFilter(message_filter)
    font = None

    try:
        font = TTFont(filename, lazy=True)

        if "name" not in font:
            return ""

        for record in font["name"].names:
            if record.nameID == 1:
                return record.toUnicode().casefold()

    except (OSError, KeyError, ValueError):
        return ""

    finally:
        if font is not None:
            font.close()
        logger.removeFilter(message_filter)

    return ""
def _requests(source: str | Path) -> Counter[str]:
    document = ezdxf.readfile(source)
    result: Counter[str] = Counter()
    for entity in document.entitydb.values():
        if entity is None or entity.dxftype() not in {"TEXT", "MTEXT", "ATTRIB", "DIMENSION"}:
            continue
        inline = (
            _INLINE_FONT.findall(str(entity.dxf.get("text", "")))
            if entity.dxftype() == "MTEXT"
            else []
        )
        result.update(inline)
        xdata_font = ""
        try:
            xdata = entity.get_xdata("ACAD")
            for tag in xdata:
                if isinstance(tag.value, str):
                    xdata_font = tag.value
                    break
        except (ezdxf.DXFError, AttributeError):
            pass
        if xdata_font:
            result[xdata_font] += 1
        elif not inline:
            if entity.dxftype() == "DIMENSION":
                dimstyle_name = entity.dxf.get("dimstyle", "Standard")
                try:
                    dimstyle = document.dimstyles.get(dimstyle_name)
                    style_name = dimstyle.dxf.get("dimtxsty", "Standard")
                except (ezdxf.DXFError, AttributeError):
                    style_name = "Standard"
            else:
                style_name = entity.dxf.get("style", "Standard")

            try:
                raw = str(document.styles.get(style_name).dxf.get("font", "") or "")
            except (ezdxf.DXFError, AttributeError):
                raw = ""

            if raw:
                result[raw] += 1
    return result


def configure_fonts(
    substitutions: str | None,
    folder: str | None,
    source: str | Path | None = None,
    fallback: str = "arial.ttf",
) -> Callable[[], None]:
    """Genera sustituciones para solicitudes no resueltas y las instala en ezdxf."""
    global _ACTIVE_SUBSTITUTIONS
    if folder:
        path = Path(folder).expanduser()
        if not path.is_dir():
            raise CadProcessingError(f"No existe la carpeta de fuentes: {path}")
        fonts.font_manager.scan_folder(path)
    mapping = {}
    if substitutions:
        path = Path(substitutions)
        try:
            mapping = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CadProcessingError(
                f"No se pudo leer la configuración de fuentes: {error}"
            ) from error
    if not isinstance(mapping, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()
    ):
        raise CadProcessingError(
            "La configuración de fuentes debe ser un objeto JSON familia→fuente"
        )
    faces = {}
    counts = _requests(source) if source else Counter()
    configured = {key.casefold(): (key, value, "configurada") for key, value in mapping.items()}
    fallback_path = _fallback_path(fallback, folder) if counts else None
    automatic = {}
    if fallback_path:
        fonts.font_manager._font_cache.add_entry(
                fallback_path, get_ttf_font_face(fallback_path)
            )
        for family, count in counts.items():
            if family.casefold() in configured:
                continue
            if not fonts.is_shx_font_name(family):
                matched = fonts.find_best_match(family=Path(family).stem)
                if (
                    matched
                    and matched.filename
                    and _font_family(matched.filename) == Path(family).stem.casefold()
                ):
                    continue
            automatic[family.casefold()] = (
                family,
                fallback_path.name,
                "respaldo automático",
                count,
            )
    combined = dict(configured)
    combined.update(
        {key: (name, target, origin) for key, (name, target, origin, _count) in automatic.items()}
    )
    for family, target, _origin in combined.values():
        target_path = Path(target).expanduser()
        if target_path.is_file():
            fonts.font_manager._font_cache.add_entry(
                target_path, get_ttf_font_face(target_path)
            )
            target = target_path.name
        face = fonts.resolve_font_face(target)
        if not face.filename:
            raise CadProcessingError(f"No se pudo resolver la fuente sustituta '{target}'")
        faces[family.casefold()] = face
    _ACTIVE_SUBSTITUTIONS = {
        key: (
            name,
            faces[key].filename,
            origin,
            automatic.get(key, ("", "", "", counts.get(name, 0)))[3],
        )
        for key, (name, _target, origin) in combined.items()
    }
    for name, filename, origin, count in _ACTIVE_SUBSTITUTIONS.values():
        if origin == "respaldo automático":
            print(f"Fuente sustituida: {name} → {filename} | {origin} | {count} texto(s)")
    original = fonts.find_best_match

    def find_best_match(*, family="sans-serif", style="Regular", weight=400, width=5, italic=False):
        face = faces.get(str(family).casefold())
        if face is not None:
            return face
        return original(family=family, style=style, weight=weight, width=width, italic=italic)

    fonts.find_best_match = find_best_match

    def restore() -> None:
        global _ACTIVE_SUBSTITUTIONS
        fonts.find_best_match = original
        _ACTIVE_SUBSTITUTIONS = {}

    return restore
