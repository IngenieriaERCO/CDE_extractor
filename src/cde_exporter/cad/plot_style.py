"""Tabla de estilos de impresión por color (CTB) y transparencia de rellenos al imprimir.

Solo se usa cuando se pide (``--plot-style`` / ``plot_style``; ``--no-print-transparency`` /
``print_transparency: false``); sin esas opciones el dibujo es el de siempre.

**CTB.** AutoCAD imprime con la CTB de la presentación: cada ACI (1–255) puede tener un color
propio, escala de grises, tramado (``screen``) y un grosor propio. El ``RenderContext`` de ezdxf
acepta ``ctb=``, pero no sirve tal cual (ezdxf 1.4.4): usa la CTB solo como paleta ACI → RGB,
deja el ACI 7 siempre negro o blanco, no aplica escala de grises ni tramado y busca el grosor
con el ACI escrito en la entidad (``entity.dxf.color``), así que en las entidades PorCapa o
PorBloque (casi todas) nunca lo aplica. Por eso la CTB se aplica aquí, sobre las propiedades
ya resueltas de cada entidad (``Frontend.push_property_override_function``), con el ACI
resuelto (``properties.pen``: PorCapa y PorBloque ya aplicados):

- solo a las entidades cuyo color sale de un ACI (el de la paleta de AutoCAD para ese ACI);
  las de color verdadero (RGB) conservan su color y su grosor;
- color propio de la CTB si lo tiene; después escala de grises (luminancia) y tramado
  (``screen`` %: 100 = color tal cual, 0 = blanco);
- grosor propio de la CTB si lo tiene (si es «del objeto», el de la entidad);
- la transparencia de la entidad (canal alfa) no se toca.

La lectura del archivo es propia: ``acadctb.load`` de ezdxf falla con las CTB cuya descripción
no está en UTF-8 (las de AutoCAD en español traen «ñ» en cp1252).

**Transparencia.** Con ``print_transparency`` desactivado, los HATCH con transparencia alta
(``HIGH_TRANSPARENCY``, 80 % o más: veladuras casi invisibles) no se dibujan, como en el PDF de
AutoCAD de PRGD; el resto se dibuja igual que hoy.
"""

from __future__ import annotations

import hashlib
import zlib
from dataclasses import dataclass
from pathlib import Path

from ezdxf.addons import acadctb
from ezdxf.colors import aci2rgb

from cde_exporter.cad.models import CadProcessingError

# Transparencia (0–1) a partir de la cual un HATCH no se imprime sin --print-transparency.
HIGH_TRANSPARENCY = 0.80
_HEADER_BYTES = 60
_CTB_HEADER = b"PIAFILEVERSION"


@dataclass(frozen=True, slots=True)
class PenStyle:
    """Lo que la CTB hace con un ACI."""

    color: tuple[int, int, int] | None  # None: color del objeto
    grayscale: bool
    screen: int  # 0–100
    lineweight_mm: float | None  # None: grosor del objeto

    @property
    def changes_anything(self) -> bool:
        return (
            self.color is not None
            or self.grayscale
            or self.screen != 100
            or self.lineweight_mm is not None
        )


@dataclass(frozen=True, slots=True)
class PlotStyleTable:
    name: str  # nombre del archivo (nunca la ruta)
    sha256: str
    pens: tuple[PenStyle, ...]  # índice = ACI; el 0 no se usa

    def describe(self) -> dict:
        """Lo que se guarda en el manifiesto."""
        return {"name": self.name, "sha256": self.sha256}

    def pen(self, aci: int) -> PenStyle | None:
        return self.pens[aci] if 1 <= aci <= 255 else None


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("cp1252", errors="replace")


def load_plot_style(path: str | Path) -> PlotStyleTable:
    """Lee una CTB (tabla de estilos por color). Error claro si no existe o no es una CTB."""
    source = Path(path)
    if source.suffix.lower() == ".stb":
        raise CadProcessingError(
            f"«{source.name}» es una tabla de estilos con nombre (.stb); solo se admiten las "
            "tablas por color (.ctb)."
        )
    try:
        raw = source.read_bytes()
    except OSError as error:
        raise CadProcessingError(
            f"No se pudo leer la tabla de estilos de impresión «{source}»: {error}"
        ) from error
    if not raw.startswith(_CTB_HEADER) or b"CTBVER" not in raw[:_HEADER_BYTES]:
        raise CadProcessingError(
            f"«{source.name}» no es una tabla de estilos de impresión por color (.ctb) de AutoCAD."
        )
    try:
        text = _decode(zlib.decompress(raw[_HEADER_BYTES:]).rstrip(b"\0"))
        table = acadctb.ColorDependentPlotStyles()
        table.parse(text)
        pens = [PenStyle(None, False, 100, None)]
        for aci in range(1, 256):
            style = table[aci]
            lineweight = table.get_lineweight(aci)
            pens.append(
                PenStyle(
                    None if style.has_object_color() else tuple(style.color),
                    bool(style.grayscale),
                    max(0, min(100, int(style.screen))),
                    None if lineweight is None else round(float(lineweight), 4),
                )
            )
    except (zlib.error, ValueError, KeyError, IndexError, TypeError) as error:
        raise CadProcessingError(
            f"La tabla de estilos «{source.name}» está dañada o no se puede leer: {error}"
        ) from error
    return PlotStyleTable(source.name, hashlib.sha256(raw).hexdigest(), tuple(pens))


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def plotted_rgb(pen: PenStyle, rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """El color con que la CTB imprime ``rgb`` (color propio, grises y tramado)."""
    r, g, b = pen.color if pen.color is not None else rgb
    if pen.grayscale:
        r = g = b = round(0.299 * r + 0.587 * g + 0.114 * b)
    if pen.screen != 100:
        r, g, b = (round(255 - (255 - value) * pen.screen / 100) for value in (r, g, b))
    return r, g, b


def apply_plot_style(table: PlotStyleTable, properties, default_color: str) -> None:
    """Cambia ``properties`` (ya resueltas) según la CTB. ``default_color`` es el del ACI 7."""
    aci = int(properties.pen)
    pen = table.pen(aci)
    if pen is None or not pen.changes_anything:
        return
    color = str(properties.color)
    if len(color) < 7:
        return
    palette = default_color[:7] if aci == 7 else _hex(tuple(aci2rgb(aci)))
    plotted = _hex(plotted_rgb(pen, _rgb(palette)))
    current = color[:7].lower()
    if current == palette.lower():
        properties.color = plotted + color[7:]
    elif current != plotted:
        return  # color verdadero: la CTB no lo cambia
    # (Si ya es el de la CTB, viene PorBloque de un INSERT ya convertido: solo falta el grosor.)
    if pen.lineweight_mm is not None:
        properties.lineweight = pen.lineweight_mm


def transparency(color: str) -> float:
    """Transparencia (0–1) del color resuelto ``#RRGGBBAA`` (0 si no lleva alfa)."""
    if len(color) < 9:
        return 0.0
    return 1.0 - int(color[7:9], 16) / 255


def is_high_transparency(color: str) -> bool:
    return transparency(color) >= HIGH_TRANSPARENCY - 1e-9
