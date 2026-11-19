"""Tabla de estilos (CTB) derivada de un PDF impreso por AutoCAD.

Cuando no se tiene la CTB con que se imprimió un plano, se puede reconstruir su tabla de
colores comparando el DXF con el PDF que imprimió AutoCAD:

1. Del DXF, para cada capa efectiva (la del INSERT para lo que va en la capa «0» de un bloque,
   como hace AutoCAD al crear las capas del PDF), qué ACI resuelto se dibuja (PorCapa y PorBloque
   ya aplicados) y qué colores verdaderos (RGB) (``drawn_pens``).
2. Del PDF, los colores de trazo y relleno de cada capa (contenido opcional) (``pdf_layer_colors``).
3. ``derive_colors``, por pasadas: en cada capa se quitan los colores verdaderos del DXF (salen tal
   cual), los de los ACI ya medidos y el blanco (fondo de textos y máscaras); si queda **un solo
   ACI sin medir** y algún color, ese ACI se imprimió con el color dominante que queda. Se
   repite mientras salgan ACI nuevos. Si varias capas dan colores distintos para el mismo ACI,
   gana el que confirman más capas (a igualdad, el de más trazos) y se anota el conflicto.
4. ``plot_style.write_ctb`` escribe la CTB: el color medido en los ACI medidos (si es distinto del
   de la paleta) y «color del objeto» en los demás. Los grosores no se derivan (del objeto).

Limitaciones: un ACI que solo aparece en capas con otro ACI sin medir no se puede medir; dos ACI
que imprimen el mismo color en la misma capa se confunden (el segundo queda sin medir).

Uso: ``python -m cde_exporter.cad.ctb_derive PLANO.dxf PLANO_AutoCAD.pdf SALIDA.ctb``; escribe
también ``SALIDA.json`` con las pruebas de cada ACI.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

RGB = tuple[int, int, int]
# Parte mínima del color dominante (entre los que quedan en la capa) para aceptarlo.
MIN_SHARE = 0.80
# El blanco no es prueba sobre papel blanco: en el PDF suele ser el fondo de un texto (máscara)
# o un WIPEOUT, no el color de la entidad.
PAPER = (255, 255, 255)


@dataclass
class Derivation:
    colors: dict[int, RGB] = field(default_factory=dict)  # ACI medido → color impreso
    # ACI → [(capa, color, trazos de ese color, trazos que quedaban en la capa, pasada)]
    evidence: dict[int, list[tuple[str, RGB, int, int, int]]] = field(default_factory=dict)
    conflicts: dict[int, list[tuple[str, RGB, int]]] = field(default_factory=dict)
    used: Counter = field(default_factory=Counter)  # ACI → entidades dibujadas en el DXF

    @property
    def unmeasured(self) -> list[int]:
        return sorted(aci for aci in self.used if aci not in self.colors)


def layer_key(name: str) -> str:
    """Nombre de capa comparable entre el DXF y el PDF (PyMuPDF puede estropear los acentos)."""
    return "".join(char if ord(char) < 128 else "?" for char in name).casefold()


def derive_colors(
    pens: dict[str, Counter], pdf_colors: dict[str, Counter], min_share: float = MIN_SHARE
) -> Derivation:
    """``pens``: capa → Counter de ACI (int) y colores verdaderos (tupla RGB) dibujados;
    ``pdf_colors``: capa → Counter de colores RGB del PDF (trazos y rellenos)."""
    result = Derivation()
    by_key = {layer_key(name): colors for name, colors in pdf_colors.items()}
    layers = []
    for name, counter in pens.items():
        acis = {key for key in counter if isinstance(key, int)}
        trues = {key for key in counter if not isinstance(key, int)}
        for aci in acis:
            result.used[aci] += counter[aci]
        colors = by_key.get(layer_key(name))
        if colors:
            layers.append((name, acis, trues, colors))
    step = 0
    while True:
        step += 1
        candidates: dict[int, list[tuple[str, RGB, int, int]]] = defaultdict(list)
        for name, acis, trues, colors in layers:
            unknown = [aci for aci in acis if aci not in result.colors]
            if len(unknown) != 1:
                continue
            taken = trues | {result.colors[aci] for aci in acis if aci in result.colors} | {PAPER}
            left = Counter({color: n for color, n in colors.items() if color not in taken})
            total = sum(left.values())
            if not total:
                continue
            color, count = left.most_common(1)[0]
            if count / total >= min_share:
                candidates[unknown[0]].append((name, color, count, total))
        if not candidates:
            return result
        for aci, found in candidates.items():
            # Gana el color que confirman más capas; a igualdad, el de más trazos.
            votes: dict[RGB, list[int]] = defaultdict(lambda: [0, 0])
            for _name, color, count, _total in found:
                votes[color][0] += 1
                votes[color][1] += count
            color = max(votes, key=lambda item: tuple(votes[item]))
            result.colors[aci] = color
            result.evidence[aci] = [(*item, step) for item in found if item[1] == color]
            others = [(name, other, count) for name, other, count, _t in found if other != color]
            if others:
                result.conflicts[aci] = others


def drawn_pens(document) -> dict[str, Counter]:
    """Capa efectiva → Counter de ACI resueltos (int) y colores verdaderos (RGB) que se dibujan
    en el Model Space y en las presentaciones (``properties.layer`` y ``properties.pen``)."""
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.recorder import Recorder

    pens: dict[str, Counter] = defaultdict(Counter)

    class _Collector(Frontend):
        def draw_entity(self, entity, properties) -> None:
            kind = entity.dxftype()
            if kind == "INSERT":
                super().draw_entity(entity, properties)  # abre el bloque
                return
            if kind == "VIEWPORT":
                return
            color = str(properties.color)
            if _has_true_color(entity, self.ctx, properties):
                pens[properties.layer][
                    (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))
                ] += 1
            else:
                pens[properties.layer][int(properties.pen)] += 1

        def log_message(self, message: str) -> None:
            pass

    collector = _Collector(RenderContext(document), Recorder())
    collector.draw_layout(document.modelspace())
    for layout in document.layouts:
        if layout.name != "Model":
            collector.draw_layout(layout)
    return dict(pens)


def _has_true_color(entity, ctx, properties) -> bool:
    """¿El color resuelto no sale de un ACI? (color verdadero propio, de la capa o PorBloque)."""
    from ezdxf.colors import aci2rgb

    aci = int(properties.pen)
    if not 1 <= aci <= 255:
        return True
    color = str(properties.color)[:7].lower()
    palette = (
        ctx.current_layout_properties.default_color[:7]
        if aci == 7
        else "#{:02x}{:02x}{:02x}".format(*aci2rgb(aci))
    )
    return color != palette.lower()


def pdf_layer_colors(pdf_path: str | Path, page: int = 0) -> dict[str, Counter]:
    """Capa (contenido opcional) → Counter de colores RGB de trazos y rellenos del PDF."""
    import pymupdf

    colors: dict[str, Counter] = defaultdict(Counter)
    with pymupdf.open(pdf_path) as document:
        for item in document[page].get_drawings():
            layer = item.get("layer") or ""
            for key in ("color", "fill"):
                value = item.get(key)
                if value:
                    colors[layer][tuple(round(channel * 255) for channel in value)] += 1
    return dict(colors)


def report(derivation: Derivation) -> dict:
    """Las pruebas de cada ACI, para el JSON que acompaña a la CTB."""
    from ezdxf.colors import aci2rgb

    return {
        "measured": {
            str(aci): {
                "palette": list(aci2rgb(aci)),
                "plotted": list(color),
                "evidence": [
                    {"layer": name, "count": count, "of": total, "pass": step}
                    for name, _color, count, total, step in derivation.evidence[aci]
                ],
                "conflicts": [
                    {"layer": name, "color": list(other), "count": count}
                    for name, other, count in derivation.conflicts.get(aci, [])
                ],
            }
            for aci, color in sorted(derivation.colors.items())
        },
        "unmeasured": {
            str(aci): {"palette": list(aci2rgb(aci)), "entities": derivation.used[aci]}
            for aci in derivation.unmeasured
        },
    }


def main(argv: list[str] | None = None) -> int:
    import ezdxf

    from cde_exporter.cad.plot_style import load_plot_style, write_ctb

    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 3:
        print("Uso: python -m cde_exporter.cad.ctb_derive PLANO.dxf PLANO.pdf SALIDA.ctb")
        return 2
    dxf_path, pdf_path, output = (Path(item) for item in arguments)
    derivation = derive_colors(drawn_pens(ezdxf.readfile(dxf_path)), pdf_layer_colors(pdf_path))
    description = (
        f"Derivada del PDF de AutoCAD {pdf_path.name} y de {dxf_path.name} (cde_exporter): "
        f"{len(derivation.colors)} ACI medidos, {len(derivation.unmeasured)} sin medir "
        "(color del objeto); grosores del objeto"
    )
    write_ctb(derivation.colors, output, description)
    data = {"ctb": output.name, "sha256": load_plot_style(output).sha256, **report(derivation)}
    output.with_name(output.stem + ".json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(description)
    for aci, color in sorted(derivation.colors.items()):
        print(f"  ACI {aci}: {color}")
    print("  Sin medir:", ", ".join(map(str, derivation.unmeasured)) or "ninguno")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
