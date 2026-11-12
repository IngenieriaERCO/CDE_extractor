"""Nombres de archivo seguros en Windows a partir de nombres de bloque.

Solo usa la biblioteca estándar.
"""

from __future__ import annotations

import re
import unicodedata

# Prohibidos en Windows, más el punto: el nombre es la base de "<base>.pdf" y un punto
# se tomaría como extensión.
_FORBIDDEN = re.compile(r'[<>:"/\\|?*.\x00-\x1f\x7f]')
_SPACES = re.compile(r"\s+")
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
MAX_STEM_LENGTH = 80
FALLBACK_STEM = "bloque"


def safe_file_stem(name: str) -> str:
    """Base de nombre de archivo válida en Windows para ``name``.

    Cambia por ``_`` los caracteres prohibidos y el punto, une los espacios repetidos,
    quita los espacios de los extremos, evita los nombres reservados (CON, NUL, COM1…) y
    recorta a ``MAX_STEM_LENGTH`` caracteres. Conserva acentos y eñes.
    """
    stem = _FORBIDDEN.sub("_", unicodedata.normalize("NFC", name))
    stem = _SPACES.sub(" ", stem).strip()
    stem = stem[:MAX_STEM_LENGTH].rstrip()
    if not stem:
        return FALLBACK_STEM
    if stem.upper() in _RESERVED:
        stem += "_"
    return stem


def unique_stem(base: str, needed: int, used: set[str]) -> str:
    """Base libre en ``used`` (comparada sin mayúsculas, como Windows) y la reserva.

    ``needed`` > 1 reserva también ``<base>_001`` … ``<base>_NNN`` (un archivo por
    referencia). Si alguno ya está ocupado se prueba ``<base>_2``, ``<base>_3``…
    """
    suffix = 1
    while True:
        candidate = base if suffix == 1 else f"{base}_{suffix}"
        stems = package_stems(candidate, needed)
        if not any(stem.casefold() in used for stem in stems):
            used.update(stem.casefold() for stem in stems)
            return candidate
        suffix += 1


def package_stems(base: str, references: int) -> list[str]:
    """Bases de los paquetes de un bloque: ``base`` o ``base_001``, ``base_002``…"""
    if references <= 1:
        return [base]
    return [f"{base}_{order:03d}" for order in range(1, references + 1)]
