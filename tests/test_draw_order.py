"""Orden de dibujo del DXF (ACAD_SORTENTS) en bloques y zonas, y fondo de los HATCH debajo de lo
que los cruza (draw_order.py y exporters._StrictFrontend)."""

from pathlib import Path

import ezdxf
import pymupdf
import pytest

from cde_exporter.cad.draw_order import (
    copy_block_draw_order,
    drawing_sequence,
    has_draw_order,
    set_draw_order,
)
from cde_exporter.cli import main

GRAY = (147, 149, 152)
BLUE = (0, 0, 255)


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def _concrete(layout, offset=0.0):
    """Concreto: HATCH ANSI31 rojo con fondo gris, de 10 × 10."""
    hatch = layout.add_hatch(color=1)
    hatch.set_pattern_fill("ANSI31", color=1, scale=0.5)
    hatch.paths.add_polyline_path([(offset, 0), (offset + 10, 0), (offset + 10, 10), (offset, 10)])
    hatch.bgcolor = GRAY
    return hatch


def _steel(layout, x, y):
    """Acero: un círculo relleno azul (HATCH sólido) de radio 0,5."""
    hatch = layout.add_hatch(color=5)
    hatch.paths.add_edge_path().add_arc((x, y), 0.5, 0, 360)
    return hatch


def _document():
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    document.appids.new("HATCHBACKGROUNDCOLOR")
    return document


def _order(pdf: Path) -> tuple[list[int], list[int], list[int]]:
    """Índices (orden del PDF) de los rellenos grises, de los azules y de las rayas rojas."""
    with pymupdf.open(pdf) as document:
        drawings = document[0].get_drawings()

    def near(color, rgb) -> bool:
        return color is not None and all(
            abs(a - b / 255) < 0.01 for a, b in zip(color, rgb, strict=True)
        )

    gray = [i for i, item in enumerate(drawings) if near(item.get("fill"), GRAY)]
    blue = [i for i, item in enumerate(drawings) if near(item.get("fill"), BLUE)]
    red = [i for i, item in enumerate(drawings) if near(item.get("color"), (255, 0, 0))]
    return gray, blue, red


def _extract_block(source: Path, prefix: Path, name: str) -> Path:
    code = main(
        [
            "cad",
            "extract-block",
            str(source),
            "--block-name",
            name,
            "--scale",
            "1:100",
            "--output-prefix",
            str(prefix),
        ]
    )
    assert code == 0
    return prefix.with_suffix(".pdf")


# ----------------------------------------------------------------- funciones


def test_set_draw_order_writes_a_table_that_gives_that_order() -> None:
    document = _document()
    modelspace = document.modelspace()
    first, second, third = (modelspace.add_line((0, i), (1, i)) for i in range(3))
    assert not has_draw_order(modelspace)
    assert drawing_sequence(modelspace) == [first, second, third]

    set_draw_order(modelspace, [third, first, second])

    assert has_draw_order(modelspace)
    assert drawing_sequence(modelspace) == [third, first, second]


def test_the_block_draw_order_is_copied_also_to_renamed_anonymous_blocks() -> None:
    source = _document()
    named = source.blocks.new("CON_TABLA")
    lines = [named.add_line((0, i), (1, i)) for i in range(3)]
    named.add_circle((0, 0), 1)
    set_draw_order(named, [named[3], *lines])
    target = _document()
    copy = target.blocks.new("CON_TABLA")
    for i in range(3):
        copy.add_line((0, i), (1, i))
    copy.add_circle((0, 0), 1)
    renamed = target.blocks.new("*U7")  # misma secuencia de tipos, nombre cambiado
    for i in range(3):
        renamed.add_line((0, i), (1, i))
    renamed.add_circle((0, 0), 1)

    assert copy_block_draw_order(source, target) == 2
    assert [e.dxftype() for e in drawing_sequence(copy)] == ["CIRCLE", "LINE", "LINE", "LINE"]
    assert [e.dxftype() for e in drawing_sequence(renamed)][0] == "CIRCLE"


# ----------------------------------------------------------------- exportación


def test_a_block_is_drawn_in_its_own_draw_order(tmp_path: Path) -> None:
    """En el bloque, el concreto está guardado después del acero, pero su tabla de orden lo
    manda detrás (como DRAWORDER en AutoCAD): el gris y las rayas, antes que el acero."""
    document = _document()
    block = document.blocks.new("PEDESTAL")
    steel = [_steel(block, 3, 3), _steel(block, 7, 7)]
    concrete = _concrete(block)
    set_draw_order(block, [concrete, *steel])
    document.modelspace().add_blockref("PEDESTAL", (0, 0))
    document.saveas(tmp_path / "obra.dxf")

    gray, blue, red = _order(_extract_block(tmp_path / "obra.dxf", tmp_path / "P", "PEDESTAL"))

    assert len(gray) == 1 and len(blue) == 2 and red
    assert gray[0] < min(red) and max(red) < min(blue)


def test_without_a_table_the_background_goes_under_what_crosses_it(tmp_path: Path) -> None:
    """Sin tabla, el bloque se dibuja en el orden guardado (el concreto, después del acero),
    pero el fondo del concreto va debajo de todo: el acero no queda tapado por el gris."""
    document = _document()
    block = document.blocks.new("PEDESTAL")
    _steel(block, 3, 3)
    _concrete(block)
    document.modelspace().add_blockref("PEDESTAL", (0, 0))
    document.saveas(tmp_path / "obra.dxf")

    gray, blue, red = _order(_extract_block(tmp_path / "obra.dxf", tmp_path / "P", "PEDESTAL"))

    assert len(gray) == 1 and len(blue) == 1 and red
    assert gray[0] < blue[0] < min(red)  # fondo, acero y, en su sitio, las rayas


def test_a_zone_keeps_the_draw_order_of_the_modelspace(tmp_path: Path) -> None:
    """Como la sección VF de PRGD: el concreto suelto está guardado al final del Model Space,
    pero su tabla lo dibuja antes que el acero. Un círculo azul cortado por la zona (se vuelve
    un arco nuevo, al final de la copia) conserva su sitio, encima del concreto."""
    document = _document()
    modelspace = document.modelspace()
    steel = _steel(modelspace, 3, 3)
    ring = modelspace.add_circle((10, 5), 1, dxfattribs={"color": 5})
    concrete = _concrete(modelspace)
    set_draw_order(modelspace, [concrete, steel, ring])
    document.saveas(tmp_path / "obra.dxf")
    prefix = tmp_path / "Zona"

    code = main(
        [
            "cad",
            "extract-modelspace",
            str(tmp_path / "obra.dxf"),
            "--window",
            "0",
            "0",
            "10",
            "10",
            "--scale",
            "1:100",
            "--output-prefix",
            str(prefix),
        ]
    )

    assert code == 0
    gray, blue, red = _order(prefix.with_suffix(".pdf"))
    assert len(gray) == 1 and len(blue) == 1 and red
    assert gray[0] < max(red) < blue[0]
    with pymupdf.open(prefix.with_suffix(".pdf")) as pdf:
        drawings = pdf[0].get_drawings()
    arcs = [
        i
        for i, item in enumerate(drawings)
        if item.get("color") is not None and item["color"][2] > 0.9 and item["color"][0] < 0.1
    ]
    assert arcs and min(arcs) > max(red)  # el arco recortado, encima del concreto
