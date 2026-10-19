"""Configuración opcional de fuentes para el dibujo ezdxf 1.4.4."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from ezdxf.fonts import fonts

from cde_exporter.cad.models import CadProcessingError


def configure_fonts(substitutions: str | None, folder: str | None) -> Callable[[], None]:
    """Registra una carpeta y aplica un mapa familia→archivo al resolver de ezdxf."""
    if folder:
        path = Path(folder).expanduser()
        if not path.is_dir():
            raise CadProcessingError(f"No existe la carpeta de fuentes: {path}")
        fonts.font_manager.scan_folder(path)
    if not substitutions:
        return lambda: None
    path = Path(substitutions)
    try:
        mapping = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CadProcessingError(f"No se pudo leer la configuración de fuentes: {error}") from error
    if not isinstance(mapping, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()
    ):
        raise CadProcessingError(
            "La configuración de fuentes debe ser un objeto JSON familia→fuente"
        )
    faces = {}
    for family, target in mapping.items():
        target_path = Path(target).expanduser()
        if target_path.is_file():
            fonts.font_manager.scan_folder(target_path.parent)
            target = target_path.name
        face = fonts.resolve_font_face(target)
        if not face.filename:
            raise CadProcessingError(f"No se pudo resolver la fuente sustituta '{target}'")
        faces[family.casefold()] = face
    original = fonts.find_best_match

    def find_best_match(*, family="sans-serif", style="Regular", weight=400, width=5, italic=False):
        face = faces.get(str(family).casefold())
        if face is not None:
            return face
        return original(family=family, style=style, weight=weight, width=width, italic=italic)

    fonts.find_best_match = find_best_match

    def restore() -> None:
        fonts.find_best_match = original

    return restore
