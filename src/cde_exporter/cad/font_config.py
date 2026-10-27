"""Configuración opcional de fuentes para el dibujo ezdxf 1.4.4."""

from __future__ import annotations

import json
import platform
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import ezdxf
from ezdxf.document import Drawing
from ezdxf.fonts import fonts
from ezdxf.fonts.font_manager import get_ttf_font_face

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


def register_font_file(path: Path) -> None:
    """Hace que ezdxf conozca un archivo de fuente concreto por su nombre de archivo.

    ezdxf 1.4 no ofrece una API pública para añadir un solo archivo: ``scan_folder`` escanea
    toda una carpeta y puede cambiar la fuente que se resuelve para otras familias. Por eso se
    usa la caché interna del administrador de fuentes, y solo aquí. Si una versión futura de
    ezdxf la cambia, falla con un mensaje claro en lugar de un AttributeError.
    """
    try:
        add_entry = fonts.font_manager._font_cache.add_entry
    except AttributeError as error:
        raise CadProcessingError(
            f"La versión de ezdxf instalada ({ezdxf.__version__}) cambió el administrador de "
            "fuentes que usa el exportador, que está probado con ezdxf 1.4.x. Instale una "
            "versión 1.4.x de ezdxf o actualice el exportador."
        ) from error
    try:
        face = get_ttf_font_face(path)
    except Exception as error:  # noqa: BLE001 - fontTools puede lanzar cualquier error
        raise CadProcessingError(f"No se pudo leer la fuente '{path}': {error}") from error
    add_entry(path, face)


_FONT_FILE_SUFFIXES = (".ttf", ".otf", ".ttc", ".shx")


def is_shx_request(name: str) -> bool:
    """Solo los archivos .shx son fuentes de trazo; un nombre sin extensión es una familia.

    ``ezdxf.fonts.is_shx_font_name`` da True para cualquier nombre que no acabe en .ttf, así
    que no distingue la familia "Verdana" de una fuente SHX.
    """
    return name.casefold().endswith(".shx")


def font_stem(name: str) -> str:
    """Nombre de la fuente sin la extensión de archivo (los nombres de familia no cambian)."""
    lowered = name.casefold()
    for suffix in _FONT_FILE_SUFFIXES:
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


def is_installed_font(requested: str, matched) -> bool:
    """Indica si la fuente que ezdxf encontró es la pedida y no una aproximada.

    ``find_best_match`` devuelve la más parecida; solo se acepta si su familia o su nombre de
    archivo coinciden con lo pedido (``matched.filename`` es el nombre, no una ruta).
    """
    if matched is None or not matched.filename:
        return False
    return (
        str(matched.family).casefold() == font_stem(requested).casefold()
        or str(matched.filename).casefold() == requested.casefold()
    )


def font_is_installed(name: str) -> bool:
    """Indica si ezdxf encuentra exactamente esa fuente (por archivo TrueType o por familia)."""
    if is_shx_request(name):
        return False
    if name.casefold().endswith((".ttf", ".otf", ".ttc")) and fonts.font_manager.has_font(name):
        return True
    return is_installed_font(name, fonts.find_best_match(family=font_stem(name)))


def _requests(source: str | Path | Drawing) -> Counter[str]:
    document = source if isinstance(source, Drawing) else ezdxf.readfile(source)
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
    source: str | Path | Drawing | None = None,
    fallback: str = "arial.ttf",
) -> Callable[[], None]:
    """Genera sustituciones para solicitudes no resueltas y las instala en ezdxf.

    ``source`` puede ser una ruta o un documento ya leído (por ejemplo, el detalle
    extraído), para no volver a leer el DXF.
    """
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
        register_font_file(fallback_path)
        for family, count in counts.items():
            if family.casefold() in configured:
                continue
            if font_is_installed(family):
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
            register_font_file(target_path)
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
