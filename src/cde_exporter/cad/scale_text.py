"""Escalas escritas en los textos del dibujo («ESC 1:25», «ESCALA: 1:25», «ESC-1:10»…).

Lo usa la orden ``texts`` del trabajador (``zone_texts``) para proponer la escala de una zona.
Solo propone: la escala la confirma siempre el usuario. Solo usa la biblioteca estándar.

Reglas:
- Hace falta la palabra ``ESC`` o ``ESCALA`` (sin distinguir mayúsculas ni tildes; admite
  ``ESC.``). Un «1:25» suelto no cuenta: podría ser una pendiente o una proporción.
- Tras la palabra, una o más relaciones ``1:N`` o ``1/N``, con ``:``, ``-``, ``.`` o ``=``
  delante y separadas por ``,``, ``;``, ``/``, ``Y`` o guiones: «ESC 1:25 Y 1:50» da dos.
- Una relación puede llevar el eje delante (``H``, ``V``, ``HORIZONTAL``, ``VERTICAL``), como en
  los perfiles: «ESC. H 1:1000 V 1:100».
- ``N`` admite decimales con punto o coma (``1:2.5``) y miles con punto (``1:1.000`` = 1000).
- Propuesta (``propose``): solo si en la zona hay una única escala y sin eje; con varias, o con
  ejes H/V, se devuelven todas para que el usuario elija; sin ninguna, nada.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Fuentes de cada escala candidata que se devuelven como mucho (el total va en «count»).
SOURCES_LIMIT = 10

_KEYWORD = re.compile(r"\bESC(?:ALA)?S?\b\.?")
_NUMBER = r"(\d{1,3}(?:\.\d{3})+(?![\d.,])|\d+(?:[.,]\d+)?)"
_AXIS = r"(?:(HORIZONTAL|HOR|VERTICAL|VERT|H|V)\b\.?[\s:=]*)?"
_FIRST = re.compile(r"[\s:.\-–=]*" + _AXIS + r"1\s*[:/]\s*" + _NUMBER)
_NEXT = re.compile(r"(?:\s*[,;/\-–]\s*|\s+Y\s+|\s+)" + _AXIS + r"1\s*[:/]\s*" + _NUMBER)


@dataclass(frozen=True)
class ScaleMatch:
    denominator: float
    axis: str | None  # "H", "V" o None

    @property
    def scale(self) -> str:
        return f"1:{self.denominator:g}"


def normalize(text: str) -> str:
    """Mayúsculas y sin tildes (Ó → O), para comparar."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).upper()


def _denominator(raw: str) -> float | None:
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw):
        raw = raw.replace(".", "")
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        return None
    return value if value > 0 else None


def _axis(raw: str | None) -> str | None:
    return None if raw is None else raw[0]


def find_scales(text: str) -> list[ScaleMatch]:
    """Escalas escritas en ``text``, en orden y sin repetir."""
    found: list[ScaleMatch] = []
    normal = normalize(text)
    for keyword in _KEYWORD.finditer(normal):
        match = _FIRST.match(normal, keyword.end())
        while match is not None:
            denominator = _denominator(match.group(2))
            if denominator is not None:
                item = ScaleMatch(denominator, _axis(match.group(1)))
                if item not in found:
                    found.append(item)
            match = _NEXT.match(normal, match.end())
    return found


def propose(texts: list[dict]) -> tuple[list[dict], str | None]:
    """Escalas candidatas de una lista de textos (los de ``zone_texts``, con «text», «x», «y»,
    «height», «layer» y «handle»). Devuelve (candidatas, escala propuesta o None).

    Cada candidata: ``{"scale", "denominator", "axis", "count", "sources"}``, ordenadas por
    veces que sale y, a igualdad, por la altura del texto más grande."""
    groups: dict[tuple[float, str | None], dict] = {}
    for item in texts:
        for match in find_scales(item["text"]):
            group = groups.setdefault(
                (match.denominator, match.axis),
                {
                    "scale": match.scale,
                    "denominator": match.denominator,
                    "axis": match.axis,
                    "count": 0,
                    "sources": [],
                    "_height": 0.0,
                },
            )
            group["count"] += 1
            group["_height"] = max(group["_height"], float(item.get("height") or 0.0))
            if len(group["sources"]) < SOURCES_LIMIT:
                group["sources"].append(
                    {key: item.get(key) for key in ("text", "x", "y", "layer", "handle")}
                )
    ordered = sorted(groups.values(), key=lambda group: (-group["count"], -group["_height"]))
    for group in ordered:
        del group["_height"]
    proposed = None
    if len(ordered) == 1 and ordered[0]["axis"] is None:
        proposed = ordered[0]["scale"]
    return ordered, proposed
