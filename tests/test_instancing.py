"""Reutilización del dibujo de un bloque con muchas referencias idénticas (``_StrictFrontend``).

Cada caso se exporta con y sin reutilización (CDE_INSTANCING=0) y se exige el mismo PDF:
mismos trazados, colores, anchos y posiciones.
"""

import json
from pathlib import Path

import ezdxf
import pymupdf
import pytest

from cde_exporter.cad import exporters
from cde_exporter.cli import main


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)
    monkeypatch.delenv(exporters.INSTANCING_ENV, raising=False)


@pytest.fixture
def reused(monkeypatch) -> list[int]:
    """Cuántas veces se reutilizó un dibujo grabado en la última exportación."""
    calls: list[int] = []
    original = exporters._StrictFrontend._replay_instance

    def spy(self, *args, **kwargs):
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(exporters._StrictFrontend, "_replay_instance", spy)
    return calls


def _module(document, name="MODULO"):
    """Módulo: contorno PorBloque, relleno sólido PorCapa y una línea discontinua en capa 0."""
    document.layers.add("PANELES", color=5)
    document.layers.add("RELLENO", color=8)
    if "DASHED" not in document.linetypes:
        document.linetypes.add("DASHED", pattern=[0.6, 0.5, -0.1])
    block = document.blocks.new(name)
    block.add_lwpolyline(
        [(0, 0), (2, 0), (2, 1), (0, 1)], close=True, dxfattribs={"color": 0, "layer": "PANELES"}
    )
    hatch = block.add_hatch(color=256, dxfattribs={"layer": "RELLENO"})
    hatch.paths.add_polyline_path([(0.1, 0.1), (1.9, 0.1), (1.9, 0.9), (0.1, 0.9)])
    block.add_line((0, 0.5), (2, 0.5), dxfattribs={"layer": "0", "linetype": "DASHED"})
    return block


def _source(path: Path, fill) -> Path:
    document = ezdxf.new("R2018", setup=True)
    document.units = ezdxf.units.InsertUnits.Meters
    fill(document)
    document.saveas(path)
    return path


def _place_modules(document) -> None:
    _module(document)
    document.layers.add("FILA_A", color=1)
    document.layers.add("FILA_B", color=3)
    detail = document.blocks.new("PLANTA")
    for index in range(60):
        x, y = (index % 10) * 3.0, (index // 10) * 2.0
        attributes = {"layer": "FILA_A" if index % 2 else "FILA_B"}
        if index % 7 == 0:
            attributes["color"] = 6  # color propio: lo heredan los PorBloque
        if index % 5 == 0:
            attributes["rotation"] = 30.0
        if index % 11 == 0:
            attributes["xscale"] = -1.0  # simetría
        if index % 13 == 0:
            attributes.update(xscale=2.0, yscale=2.0)
        detail.add_blockref("MODULO", (x, y), dxfattribs=attributes)
    document.modelspace().add_blockref("PLANTA", (100, 200))


def _export(source: Path, out: Path, monkeypatch, *, instancing: bool) -> Path:
    monkeypatch.setenv(exporters.INSTANCING_ENV, "1" if instancing else "0")
    prefix = out / "planta"
    assert (
        main(
            [
                "cad",
                "extract-block",
                str(source),
                "--block-name",
                "PLANTA",
                "--scale",
                "1:100",
                "--output-prefix",
                str(prefix),
            ]
        )
        == 0
    )
    return prefix


def _drawings(pdf: Path) -> list:
    with pymupdf.open(pdf) as document:
        page = document[0]
        rows = [
            (
                item["type"],
                tuple(round(value, 3) for value in item.get("color") or ()),
                tuple(round(value, 3) for value in item.get("fill") or ()),
                round(item.get("width") or 0, 3),
                tuple(round(value, 2) for value in item["rect"]),
                len(item["items"]),
            )
            for item in page.get_drawings()
        ]
        return [tuple(round(value, 3) for value in page.rect), sorted(rows)]


def _same_pdf(source: Path, tmp_path: Path, monkeypatch) -> tuple[list, list]:
    before = _drawings(
        _export(source, tmp_path / "antes", monkeypatch, instancing=False).with_suffix(".pdf")
    )
    after = _drawings(
        _export(source, tmp_path / "despues", monkeypatch, instancing=True).with_suffix(".pdf")
    )
    return before, after


def test_repeated_modules_are_reused_and_the_pdf_is_the_same(
    tmp_path: Path, monkeypatch, reused
) -> None:
    source = _source(tmp_path / "planta.dxf", _place_modules)

    before, after = _same_pdf(source, tmp_path, monkeypatch)

    assert before == after
    # 60 módulos con 16 combinaciones distintas (capa, color propio, giro no cuenta,
    # simetría y escala): el resto se reutiliza.
    assert len(reused) >= 40
    manifest = json.loads((tmp_path / "despues" / "planta.manifest.json").read_text("utf-8"))
    checks = {item["name"]: item["passed"] for item in manifest["validation"]["checks"]}
    assert all(checks.values()), checks
    codes = {
        item["code"] for items in manifest["notices"]["by_category"].values() for item in items
    }
    assert "FIDELITY_RECONCILIATION" not in codes


def test_nested_blocks_and_hidden_layers_inside_are_reused_consistently(
    tmp_path: Path, monkeypatch, reused
) -> None:
    def fill(document) -> None:
        _module(document)
        document.layers.add("OCULTA", color=2).off()
        mesa = document.blocks.new("MESA")
        for index in range(4):
            mesa.add_blockref("MODULO", (index * 2.2, 0))
        mesa.add_line((0, -0.2), (8.8, -0.2), dxfattribs={"layer": "OCULTA"})
        detail = document.blocks.new("PLANTA")
        for index in range(30):
            detail.add_blockref("MESA", (0, index * 1.5), dxfattribs={"layer": "FILA"})
        document.modelspace().add_blockref("PLANTA", (0, 0))

    source = _source(tmp_path / "planta.dxf", fill)

    before, after = _same_pdf(source, tmp_path, monkeypatch)

    assert before == after
    assert len(reused) >= 29
    manifest = json.loads((tmp_path / "despues" / "planta.manifest.json").read_text("utf-8"))
    codes = {
        item["code"] for items in manifest["notices"]["by_category"].values() for item in items
    }
    # Las 30 líneas de la capa apagada se cuentan igual (la conciliación cuadra).
    assert "FIDELITY_RECONCILIATION" not in codes
    assert "HIDDEN_LAYERS_CONTENT" in codes


@pytest.mark.parametrize("variant", ["texto", "atributo", "patron"])
def test_blocks_that_change_per_reference_are_not_reused(
    tmp_path: Path, monkeypatch, reused, variant
) -> None:
    def fill(document) -> None:
        block = _module(document)
        if variant == "texto":
            block.add_text("M", dxfattribs={"height": 0.3, "insert": (0.2, 0.2)})
        elif variant == "patron":
            hatch = block.add_hatch(color=1)
            hatch.set_pattern_fill("ANSI31", scale=0.1)
            hatch.paths.add_polyline_path([(0, 0), (1, 0), (1, 1), (0, 1)])
        else:
            block.add_attdef("NUM", (0.2, 0.2), dxfattribs={"height": 0.3})
        detail = document.blocks.new("PLANTA")
        for index in range(10):
            reference = detail.add_blockref("MODULO", (index * 3.0, 0))
            if variant == "atributo":
                reference.add_attrib("NUM", str(index), (index * 3.0 + 0.2, 0.2))
        document.modelspace().add_blockref("PLANTA", (0, 0))

    source = _source(tmp_path / "planta.dxf", fill)

    before, after = _same_pdf(source, tmp_path, monkeypatch)

    assert before == after
    assert reused == []
