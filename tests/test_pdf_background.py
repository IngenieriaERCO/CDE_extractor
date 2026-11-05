"""El PDF del detalle no lleva fondo de página: el componedor superpone detalles en la plancha.

Solo desaparece el rectángulo blanco del tamaño de la página. Los rellenos blancos del
dibujo (HATCH sólido blanco, WIPEOUT) se conservan y el color 7 sigue saliendo negro.
"""

from pathlib import Path

import ezdxf
import pymupdf
import pytest

from cde_exporter.cli import main

_WHITE = (1.0, 1.0, 1.0)
_BLACK = (0.0, 0.0, 0.0)


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def _square(x: float) -> list[tuple[float, float]]:
    return [(x, 0), (x + 10, 0), (x + 10, 10), (x, 10)]


@pytest.fixture
def drawings(tmp_path: Path) -> tuple[pymupdf.Rect, list[dict]]:
    """Detalle con líneas de color 7 y negras, un HATCH sólido blanco y un WIPEOUT."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    block = document.blocks.new("DET")
    block.add_line((0, 0), (10, 10), dxfattribs={"color": 7})
    block.add_line((0, 10), (10, 0), dxfattribs={"true_color": 0x000000})
    hatch = block.add_hatch(dxfattribs={"true_color": 0xFFFFFF})
    hatch.paths.add_polyline_path(_square(20), is_closed=True)
    block.add_wipeout(_square(40))
    document.modelspace().add_blockref("DET", (0, 0))
    source = tmp_path / "origen.dxf"
    document.saveas(source)
    out = tmp_path / "salida"
    code = main(
        [
            "cad",
            "extract-block",
            str(source),
            "--block-name",
            "DET",
            "--scale",
            "1:100",
            "--output-prefix",
            str(out / "detalle"),
        ]
    )
    assert code == 0
    with pymupdf.open(out / "detalle.pdf") as pdf:
        page = pdf[0]
        return pymupdf.Rect(page.rect), page.get_drawings()


def _is_page_sized(rect: pymupdf.Rect, page: pymupdf.Rect) -> bool:
    return abs(rect.width - page.width) < 0.5 and abs(rect.height - page.height) < 0.5


def _rgb(color) -> tuple[float, ...] | None:
    return None if color is None else tuple(round(value, 3) for value in color)


def test_pdf_has_no_page_background(drawings) -> None:
    page, items = drawings
    page_sized_fills = [
        item
        for item in items
        if item.get("fill") is not None and _is_page_sized(item["rect"], page)
    ]
    assert page_sized_fills == []


def test_white_fills_of_the_drawing_are_kept(drawings) -> None:
    page, items = drawings
    white_fills = [item for item in items if _rgb(item.get("fill")) == _WHITE]
    # El HATCH sólido blanco y el WIPEOUT: 10 m a 1:100 = 100 mm = 283,46 pt de lado.
    assert len(white_fills) == 2
    for item in white_fills:
        assert item["rect"].width == pytest.approx(100 * 72 / 25.4, abs=0.5)
        assert not _is_page_sized(item["rect"], page)


def test_black_lines_stay_black(drawings) -> None:
    _page, items = drawings
    line_colors = [
        _rgb(item.get("color"))
        for item in items
        if item.get("color") is not None
        and any(entry[0] == "l" for entry in item["items"])
        and item.get("fill") is None
    ]
    # La de color 7 (blanco o negro según el fondo) y la negra de color verdadero.
    assert len(line_colors) >= 2
    assert set(line_colors) == {_BLACK}
