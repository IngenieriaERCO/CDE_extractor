"""Nombres de archivo seguros en Windows a partir de nombres de bloque.

Solo usa la biblioteca estándar.
"""

from __future__ import annotations

import hashlib
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


# Nombres de paquete del trabajador (docs/protocolo_trabajador.md, 5.1): solo ASCII seguro.
PACKAGE_NAME_MAX = 60
PACKAGE_PATH_MAX = 240
_PACKAGE_ALLOWED = re.compile(r"[^A-Za-z0-9._-]+")
PACKAGE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")


def package_name(name: str, limit: int = PACKAGE_NAME_MAX) -> str:
    """Nombre de paquete válido para ``name``: letras sin acentos, números, punto, guion y
    guion bajo, sin empezar por punto y de ``limit`` caracteres como mucho.

    Los acentos y la eñe pasan a su letra sin tilde; lo demás, a ``_``. Si hay que recortar,
    se añade ``_`` y 6 cifras hexadecimales del SHA-1 del nombre original, para que dos
    nombres largos distintos no acaben iguales.
    """
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    stem = _PACKAGE_ALLOWED.sub("_", plain).strip("._")
    stem = re.sub(r"_{2,}", "_", stem) or FALLBACK_STEM
    if stem.upper() in _RESERVED:
        stem += "_"
    if len(stem) > limit:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
        stem = f"{stem[: limit - 7].rstrip('._-')}_{digest}"
    return stem


def is_package_name(value: str, limit: int = PACKAGE_NAME_MAX) -> bool:
    """¿Cumple ``value`` las reglas de nombre de paquete (sin mirar la ruta)?"""
    return (
        bool(PACKAGE_NAME_PATTERN.match(value))
        and len(value) <= limit
        and value.upper() not in _RESERVED
    )


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
