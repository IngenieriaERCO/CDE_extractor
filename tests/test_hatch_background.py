"""Color de fondo de los HATCH de patrón (``HATCHBACKGROUNDCOLOR``): AutoCAD lo pinta debajo del
patrón y ezdxf no; el exportador lo rellena antes del patrón (como el concreto AR-CONC con fondo
gris de PRGD), en bloques y en zonas recortadas."""

from pathlib import Path

import ezdxf
import pymupdf
import pytest

from cde_exporter.cad.exporters import hatch_background_color
from cde_exporter.cli import main

GRAY = (147, 149, 152)


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def _raw(value: int) -> int:
    """Valor de 32 bits como lo guarda AutoCAD (código 1071, con signo)."""
    return value - 2**32 if value >= 2**31 else value


def _hatch(layout, background=None, raw=None, solid=False):
    hatch = layout.add_hatch(color=1)
    if not solid:
        hatch.set_pattern_fill("ANSI31", color=1, scale=0.5)
    hatch.paths.add_polyline_path([(0, 0), (10, 0), (10, 10), (0, 10)])
    if background is not None:
        hatch.bgcolor = background
    if raw is not None:
        hatch.set_xdata("HATCHBACKGROUNDCOLOR", [(1071, _raw(raw))])
    return hatch


def test_the_background_color_reads_true_color_and_aci() -> None:
    document = ezdxf.new("R2018")
    document.appids.new("HATCHBACKGROUNDCOLOR")
    modelspace = document.modelspace()
    assert hatch_background_color(_hatch(modelspace, GRAY)) == "#939598"
    assert hatch_background_color(_hatch(modelspace, raw=0xC30000FE)) == "#cccccc"  # ACI 254
    assert hatch_background_color(_hatch(modelspace, raw=0xC3000007)) == "#000000"  # ACI 7
    assert hatch_background_color(_hatch(modelspace, raw=0xC8000000)) is None  # «ninguno»
    assert hatch_background_color(_hatch(modelspace)) is None
    assert hatch_background_color(_hatch(modelspace, GRAY, solid=True)) is None
    assert hatch_background_color(modelspace.add_line((0, 0), (1, 1))) is None


def _source(path: Path) -> Path:
    """Bloque «CONCRETO» con un HATCH ANSI31 rojo de fondo gris, insertado en (0, 0); y el mismo
    HATCH suelto en el Model Space en (100, 0)."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    document.appids.new("HATCHBACKGROUNDCOLOR")
    block = document.blocks.new("CONCRETO")
    _hatch(block, GRAY)
    block.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)
    modelspace = document.modelspace()
    modelspace.add_blockref("CONCRETO", (0, 0))
    loose = _hatch(modelspace, GRAY)
    loose.translate(100, 0, 0)
    document.saveas(path)
    return path


def _fills(pdf: Path) -> tuple[list, list]:
    """(rellenos grises, índices de los trazos rojos del patrón) en el orden del PDF."""
    with pymupdf.open(pdf) as document:
        drawings = document[0].get_drawings()
    gray = [
        (index, item)
        for index, item in enumerate(drawings)
        if item.get("fill") is not None
        and all(abs(a - b / 255) < 0.01 for a, b in zip(item["fill"], GRAY, strict=True))
    ]
    red = [
        index
        for index, item in enumerate(drawings)
        if item.get("color") is not None and item["color"][0] > 0.9 and item["color"][1] < 0.1
    ]
    return gray, red


def test_a_block_hatch_gets_its_background_under_the_pattern(tmp_path: Path) -> None:
    prefix = tmp_path / "Concreto"
    code = main(
        [
            "cad",
            "extract-block",
            str(_source(tmp_path / "obra.dxf")),
            "--block-name",
            "CONCRETO",
            "--scale",
            "1:100",
            "--output-prefix",
            str(prefix),
        ]
    )
    assert code == 0
    gray, red = _fills(prefix.with_suffix(".pdf"))
    assert len(gray) == 1 and red
    assert gray[0][0] < min(red)  # el fondo, antes (debajo) del patrón
    rect = gray[0][1]["rect"]
    assert rect.width * 25.4 / 72 == pytest.approx(100, abs=0.05)  # 10 m a 1:100


def test_a_clipped_zone_hatch_keeps_its_background(tmp_path: Path) -> None:
    prefix = tmp_path / "Zona"
    code = main(
        [
            "cad",
            "extract-modelspace",
            str(_source(tmp_path / "obra.dxf")),
            "--window",
            "105",
            "0",
            "120",
            "10",
            "--scale",
            "1:100",
            "--output-prefix",
            str(prefix),
        ]
    )
    assert code == 0
    gray, red = _fills(prefix.with_suffix(".pdf"))
    assert len(gray) == 1 and red
    assert gray[0][0] < min(red)
    # El HATCH suelto (100..110) recortado a la zona (105..120): el fondo mide 5 m = 50 mm.
    assert gray[0][1]["rect"].width * 25.4 / 72 == pytest.approx(50, abs=0.05)


def test_outline_mode_draws_no_background(tmp_path: Path) -> None:
    prefix = tmp_path / "Contorno"
    code = main(
        [
            "cad",
            "extract-block",
            str(_source(tmp_path / "obra.dxf")),
            "--block-name",
            "CONCRETO",
            "--scale",
            "1:100",
            "--hatch-mode",
            "outline",
            "--output-prefix",
            str(prefix),
        ]
    )
    assert code == 0
    gray, _red = _fills(prefix.with_suffix(".pdf"))
    assert gray == []
