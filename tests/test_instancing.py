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


def test_copying_an_instance_marker_shares_the_block_recording() -> None:
    """La copia de una marca lleva su propia matriz, pero la misma grabación del bloque: así el
    PDF la reconoce y su ``id`` no puede pasar a otro bloque."""
    import copy

    from ezdxf.math import Matrix44

    instance = exporters._Instance([], 0)
    marker = exporters.InstanceRecord(instance, Matrix44.translate(1, 2, 0))
    marker.property_hash, marker.handle = 7, "1AD0C"

    clone = copy.deepcopy([marker, marker])

    assert clone[0] is clone[1]
    assert clone[0] is not marker and clone[0].instance is instance
    assert (clone[0].property_hash, clone[0].handle) == (7, "1AD0C")
    clone[0].transform_inplace(Matrix44.translate(10, 0, 0))
    assert marker.matrix.get_row(3)[:2] == (1, 2)


def _place_fv_tables(document) -> None:
    """Como las mesas FV de «via tunez»: cada mesa (capa propia, color PorCapa) es un bloque
    distinto con 10 módulos de un bloque también distinto, y cada módulo lleva el panel común
    en capa «0» con color propio; los contornos del panel, en capa «0» y PorCapa, toman la capa
    de la mesa."""
    colors = {"MESA_NAR": 30, "MESA_AZU": 163, "MESA_VER": 85}
    for layer, color in colors.items():
        document.layers.add(layer, color=color)
    panel = document.blocks.new("PANEL")
    for offset in (0.0, 0.1, 0.2):
        low, high_x, high_y = offset, 2 - offset, 1 - offset
        panel.add_lwpolyline(
            [(low, low), (high_x, low), (high_x, high_y), (low, high_y)],
            close=True,
            dxfattribs={"layer": "0"},
        )
    detail = document.blocks.new("PLANTA")
    layers = list(colors)
    for table in range(12):
        layer = layers[(table * 5 // 3) % 3]
        module = document.blocks.new(f"MODULO_{table}")
        module.add_blockref("PANEL", (0, 0), dxfattribs={"layer": "0", "color": 30})
        mesa = document.blocks.new(f"MESA_{table}")
        for index in range(10):
            mesa.add_blockref(module.name, (index * 2.2, 0), dxfattribs={"layer": layer})
        position = ((table % 3) * 25.0, (table // 3) * 3.0)
        detail.add_blockref(mesa.name, position, dxfattribs={"layer": layer})
    document.modelspace().add_blockref("PLANTA", (0, 0))


def test_nested_panel_on_layer_zero_keeps_the_color_of_each_table_every_time(
    tmp_path: Path, monkeypatch
) -> None:
    """Un XObject anidado se reconocía por el ``id`` de una copia temporal de su grabación: al
    liberarse, otro panel podía recibir su XObject y la mesa salía con el color de otra capa (y
    distinto en cada exportación). Se exporta varias veces y cada vez debe coincidir, color por
    color y posición por posición, con la exportación sin reutilización."""
    source = _source(tmp_path / "planta.dxf", _place_fv_tables)
    reference = _drawings(
        _export(source, tmp_path / "sin", monkeypatch, instancing=False).with_suffix(".pdf")
    )
    by_color = {key[1]: len(rects) for key, rects in reference["groups"].items()}
    # 12 mesas × 10 módulos × 3 contornos, repartidas en 3 colores.
    assert sum(by_color.values()) == 360 and len(by_color) == 3

    for run in range(4):
        after = _drawings(
            _export(source, tmp_path / f"con_{run}", monkeypatch, instancing=True).with_suffix(
                ".pdf"
            )
        )
        assert after["xobjects"] > 0
        _assert_same(reference, after)


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


def _rotated_diagonals(document) -> None:
    """Como una isometría: un bloque con una diagonal, insertado tres veces girado 30°. La caja
    de la diagonal tiene esquinas que no son del dibujo; giradas, salían fuera de él."""
    block = document.blocks.new("DIAGONAL")
    block.add_line((0, 0), (4, 1))
    detail = document.blocks.new("ISO")
    for x in (0.0, 10.0, 20.0):
        detail.add_blockref("DIAGONAL", (x, 0), dxfattribs={"rotation": 30.0})
    document.modelspace().add_blockref("ISO", (0, 0))


def test_a_rotated_reused_block_measures_its_real_strokes(
    tmp_path: Path, monkeypatch, reused
) -> None:
    """El tamaño dibujado (la caja que graba ezdxf, que compara «Escala del dibujo» y que fija
    la página) sale de los trazos reales del bloque girado, no de las esquinas giradas de su
    caja: con reutilización, el mismo PDF, la misma página y la escala exacta."""
    source = _source(tmp_path / "iso.dxf", _rotated_diagonals)
    results = {}
    for instancing in (False, True):
        monkeypatch.setenv(exporters.INSTANCING_ENV, "1" if instancing else "0")
        prefix = tmp_path / f"salida_{instancing}" / "iso"
        arguments = ["cad", "extract-block", str(source), "--block-name", "ISO"]
        assert main([*arguments, "--scale", "1:50", "--output-prefix", str(prefix)]) == 0
        manifest = json.loads(prefix.with_name("iso.manifest.json").read_text("utf-8"))
        checks = {item["name"]: item for item in manifest["validation"]["checks"]}
        results[instancing] = (manifest, checks, _drawings(prefix.with_suffix(".pdf")))
    assert reused  # el segundo y el tercero reutilizan el dibujo del primero
    plain, instanced = results[False], results[True]
    assert instanced[1]["Escala del dibujo"]["passed"], instanced[1]["Escala del dibujo"]["detail"]
    assert instanced[0]["pdf"]["page_size_mm"] == plain[0]["pdf"]["page_size_mm"]
    assert instanced[0]["pdf"]["drawing"]["cad_bounds"] == plain[0]["pdf"]["drawing"]["cad_bounds"]
    _assert_same(plain[2], instanced[2])
