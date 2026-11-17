"""Reutilización del dibujo de un bloque con muchas referencias idénticas (``_StrictFrontend``)
y, en el PDF, su escritura como Form XObject colocado con su matriz.

Cada caso se exporta con y sin reutilización (CDE_INSTANCING=0) y se exige el mismo PDF:
mismos trazados, colores, rellenos, anchos y tramos, y cada trazado en el mismo sitio a menos de
0,01 pt (PyMuPDF escribe 6 cifras significativas: el redondeo de una coordenada de cientos de
puntos cambia según se escriba cada trazo en su sitio o una vez dentro del XObject).
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
    monkeypatch.delenv(exporters.XOBJECT_ENV, raising=False)


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


def _drawings(pdf: Path) -> dict:
    """Página, trazados agrupados por (tipo, color, relleno, ancho, tramos) con su caja, y
    cuántos Form XObject tiene la página."""
    with pymupdf.open(pdf) as document:
        page = document[0]
        groups: dict = {}
        for item in page.get_drawings():
            key = (
                item["type"],
                tuple(round(value, 3) for value in item.get("color") or ()),
                tuple(round(value, 3) for value in item.get("fill") or ()),
                round(item.get("width") or 0, 3),
                len(item["items"]),
            )
            groups.setdefault(key, []).append(tuple(item["rect"]))
        return {
            "page": tuple(round(value, 3) for value in page.rect),
            "groups": groups,
            "xobjects": len(page.get_xobjects()),
        }


def _assert_same(before: dict, after: dict, tolerance: float = 0.01) -> None:
    assert after["page"] == before["page"]
    assert {key: len(rects) for key, rects in after["groups"].items()} == {
        key: len(rects) for key, rects in before["groups"].items()
    }
    for key, rects in before["groups"].items():
        pending = list(after["groups"][key])
        for rect in rects:
            distances = [
                max(abs(a - b) for a, b in zip(rect, other, strict=True)) for other in pending
            ]
            best = min(range(len(pending)), key=distances.__getitem__)
            assert distances[best] < tolerance, (key, rect, pending[best])
            pending.pop(best)


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

    _assert_same(before, after)
    # 60 módulos con 16 combinaciones distintas (capa, color propio, giro no cuenta,
    # simetría y escala): el resto se reutiliza, y en el PDF como Form XObject.
    assert len(reused) >= 40
    assert before["xobjects"] == 0 and after["xobjects"] > 0
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

    _assert_same(before, after)
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

    _assert_same(before, after)
    assert reused == []
    assert after["xobjects"] == 0


def test_copying_the_strokes_instead_of_xobjects_gives_the_same_pdf(
    tmp_path: Path, monkeypatch
) -> None:
    """CDE_XOBJECT=0: se reutiliza el dibujo pero se copian los trazos (como en el SVG)."""
    source = _source(tmp_path / "planta.dxf", _place_modules)
    monkeypatch.setenv(exporters.XOBJECT_ENV, "0")

    before, after = _same_pdf(source, tmp_path, monkeypatch)

    _assert_same(before, after)
    assert after["xobjects"] == 0


def test_xobject_package_passes_the_checks_and_verify_package(tmp_path: Path, monkeypatch) -> None:
    from cde_exporter.cad.manifest import verify_package

    source = _source(tmp_path / "planta.dxf", _place_modules)
    prefix = _export(source, tmp_path / "salida", monkeypatch, instancing=True)

    assert verify_package(prefix.with_suffix(".pdf")) == ()
    manifest = json.loads(prefix.with_name("planta.manifest.json").read_text("utf-8"))
    checks = {item["name"]: item for item in manifest["validation"]["checks"]}
    # «Escala del dibujo» mide los trazos con get_drawings, que entra en los XObject.
    assert checks["Escala del dibujo"]["passed"], checks["Escala del dibujo"]["detail"]
    assert all(item["passed"] for item in checks.values())
