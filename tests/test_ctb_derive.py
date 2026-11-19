"""CTB derivada de un PDF impreso (cad/ctb_derive.py) y ``plot_style.write_ctb``."""

import json
from collections import Counter
from pathlib import Path

import ezdxf
import pytest
from ezdxf.addons import acadctb

from cde_exporter.cad.ctb_derive import derive_colors, drawn_pens, main, pdf_layer_colors
from cde_exporter.cad.plot_style import load_plot_style, write_ctb
from cde_exporter.cli import main as cli_main

TAN, BLUE, GREEN, GRAY = (184, 138, 92), (110, 138, 221), (115, 184, 92), (199, 200, 202)


# --------------------------------------------------------------------- cruce por capas


def test_a_layer_with_one_aci_gives_its_color_and_true_colors_are_left_out() -> None:
    """Como las mesas de PRGD: ACI 35 y el relleno de color verdadero gris en la misma capa."""
    pens = {"I1.M1.PV1": Counter({35: 6318, GRAY: 2106})}
    pdf = {"I1.M1.PV1": Counter({TAN: 1926})}

    result = derive_colors(pens, pdf)

    assert result.colors == {35: TAN}
    assert result.unmeasured == []
    assert result.evidence[35] == [("I1.M1.PV1", TAN, 1926, 1926, 1)]


def test_known_acis_are_removed_in_later_passes() -> None:
    pens = {
        "COTAS": Counter({7: 4}),
        "RÓTULO": Counter({7: 2, 253: 1}),  # en la segunda pasada ya se conoce el 7
    }
    pdf = {
        "COTAS": Counter({(46, 46, 46): 4}),
        "R�TULO": Counter({(46, 46, 46): 24, (186, 186, 186): 107}),
    }

    result = derive_colors(pens, pdf)

    assert result.colors == {7: (46, 46, 46), 253: (186, 186, 186)}
    assert result.evidence[253][0][-1] == 2  # pasada 2; el acento estropeado se empareja


def test_two_unknown_acis_in_the_only_layer_stay_unmeasured() -> None:
    pens = {"0-Detalles": Counter({1: 105, 3: 162})}
    pdf = {"0-Detalles": Counter({(0, 255, 0): 492, (66, 66, 66): 348})}

    result = derive_colors(pens, pdf)

    assert result.colors == {}
    assert result.unmeasured == [1, 3]


def test_white_is_not_evidence_and_more_layers_win() -> None:
    """El blanco del PDF suele ser el fondo de un texto; a igualdad de trazos, ganan las capas."""
    pens = {
        "TEXTO": Counter({5: 2, (0, 0, 0): 1}),
        "A": Counter({5: 1}),
        "B": Counter({5: 1}),
        "C": Counter({5: 1}),
    }
    pdf = {
        "TEXTO": Counter({(0, 0, 0): 4, (255, 255, 255): 2}),
        "A": Counter({(0, 0, 0): 1}),
        "B": Counter({(0, 0, 0): 1}),
        "C": Counter({(9, 9, 9): 2}),
    }

    result = derive_colors(pens, pdf)

    assert result.colors == {5: (0, 0, 0)}
    assert result.conflicts[5] == [("C", (9, 9, 9), 2)]


# --------------------------------------------------------------------- escritura de la CTB


def test_write_ctb_keeps_object_color_where_nothing_changes(tmp_path: Path) -> None:
    path = write_ctb(
        {35: TAN, 30: (255, 127, 0), 7: (46, 46, 46), 250: (0, 0, 0)},
        tmp_path / "ctb" / "derivada.ctb",
        "Derivada del PDF de prueba",
    )

    table = load_plot_style(path)
    raw = acadctb.load(str(path))  # también la lee ezdxf (descripción en ASCII)

    assert table.pen(35).color == TAN
    assert table.pen(7).color == (46, 46, 46)
    assert table.pen(30).color is None  # el medido es el de la paleta: color del objeto
    assert table.pen(1).color is None and table.pen(1).lineweight_mm is None
    assert table.description == "Derivada del PDF de prueba"
    assert raw[35].color == TAN


def test_write_ctb_rejects_an_aci_out_of_range(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_ctb({256: TAN}, tmp_path / "x.ctb", "x")


# --------------------------------------------------------------------- DXF y PDF reales


def _plan(path: Path) -> Path:
    """Capas PV1 (ACI 35), PV2 (ACI 163) y PV4 (ACI 85), cada una con un bloque de líneas en la
    capa «0» (heredan la capa del INSERT) y un relleno gris de color verdadero."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    for name, aci in (("I1.M1.PV1", 35), ("I1.M1.PV2", 163), ("I1.M2.PV4", 85)):
        document.layers.add(name, color=aci)
    panel = document.blocks.new("PANEL")
    panel.add_lwpolyline([(0, 0), (1, 0), (1, 2), (0, 2)], close=True)
    fill = panel.add_hatch()
    fill.set_solid_fill(rgb=GRAY)
    fill.paths.add_polyline_path([(0.1, 0.1), (0.9, 0.1), (0.9, 1.9)], is_closed=True)
    table = document.blocks.new("MESAS")
    for row, layer in enumerate(("I1.M1.PV1", "I1.M1.PV2", "I1.M2.PV4")):
        for column in range(3):
            table.add_blockref("PANEL", (column * 1.2, row * 2.5), dxfattribs={"layer": layer})
    document.modelspace().add_blockref("MESAS", (0, 0))
    document.saveas(path)
    return path


def test_drawn_pens_use_the_effective_layer(tmp_path: Path) -> None:
    pens = drawn_pens(ezdxf.readfile(_plan(tmp_path / "plano.dxf")))

    assert pens["I1.M1.PV1"] == Counter({35: 3, GRAY: 3})
    assert pens["I1.M2.PV4"] == Counter({85: 3, GRAY: 3})
    assert "0" not in pens


def test_round_trip_through_a_printed_pdf(tmp_path: Path) -> None:
    """Se imprime con una CTB, se deriva otra desde ese PDF y salen los mismos colores."""
    source = _plan(tmp_path / "plano.dxf")
    original = write_ctb({35: TAN, 163: BLUE, 85: GREEN}, tmp_path / "original.ctb", "original")
    prefix = tmp_path / "salida" / "mesas"
    arguments = ["cad", "extract-block", str(source), "--block-name", "MESAS", "--scale", "1:50"]
    assert (
        cli_main([*arguments, "--output-prefix", str(prefix), "--plot-style", str(original)]) == 0
    )
    pdf = prefix.with_name("mesas.pdf")
    assert set(pdf_layer_colors(pdf)) == {"i1.m1.pv1", "i1.m1.pv2", "i1.m2.pv4"}

    output = tmp_path / "derivada.ctb"
    assert main([str(source), str(pdf), str(output)]) == 0

    table = load_plot_style(output)
    assert (table.pen(35).color, table.pen(163).color, table.pen(85).color) == (TAN, BLUE, GREEN)
    assert "Derivada del PDF de AutoCAD mesas.pdf" in table.description
    report = json.loads(output.with_name("derivada.json").read_text(encoding="utf-8"))
    assert report["sha256"] == table.sha256
    assert report["unmeasured"] == {}
    assert report["measured"]["35"]["plotted"] == list(TAN)
