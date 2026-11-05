"""Escala real del dibujo en el PDF (Fase 4, paso 2): página exacta y comprobación medida."""

from pathlib import Path

import ezdxf
import pymupdf
import pytest

from cde_exporter.cad import pdf_checks
from cde_exporter.cad.pdf_checks import check_drawing_scale
from cde_exporter.cli import main

_MM_PER_POINT = 25.4 / 72


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def _extract_small_detail(tmp_path: Path) -> tuple[str, Path]:
    """10 m × 2,5 m a 1:1000: página de 20 × 12,5 mm, donde ezdxf truncaba un 1,22 %."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    block = document.blocks.new("DET")
    block.add_line((0, 0), (10, 0))
    block.add_line((0, 0), (0, 2.5))
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
            "1:1000",
            "--output-prefix",
            str(out / "detalle"),
        ]
    )
    assert code == 0
    return (out / "detalle.txt").read_text(encoding="utf-8"), out / "detalle.pdf"


def _line_lengths_mm(page: pymupdf.Page) -> tuple[float, float]:
    """Ancho y alto (mm) de las líneas dibujadas."""
    points = [
        point
        for drawing in page.get_drawings()
        for item in drawing["items"]
        if item[0] == "l"
        for point in item[1:]
    ]
    width = max(point.x for point in points) - min(point.x for point in points)
    height = max(point.y for point in points) - min(point.y for point in points)
    return width * _MM_PER_POINT, height * _MM_PER_POINT


def test_small_detail_is_drawn_at_its_exact_scale(tmp_path: Path) -> None:
    report, pdf = _extract_small_detail(tmp_path)

    with pymupdf.open(pdf) as document:
        page = document[0]
        width, height = _line_lengths_mm(page)
        # Antes: 9,878 × 2,469 mm (−1,22 %).
        assert width == pytest.approx(10.0, abs=1e-4)
        assert height == pytest.approx(2.5, abs=1e-4)
        # La página no lleva fondo: ningún relleno (el detalle solo tiene líneas).
        assert all(item.get("fill") is None for item in page.get_drawings())
    flat = " ".join(report.split())
    assert "[OK] Escala del dibujo: Escala medida en el PDF: 1:1000.0" in flat
    assert "Escala física: garantizada" in flat


def test_scale_mismatch_is_a_warning_and_the_scale_is_not_guaranteed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # Una tolerancia negativa hace fallar la comprobación aunque la escala sea exacta.
    monkeypatch.setattr(pdf_checks, "DRAWING_SCALE_TOLERANCE_MM", -1.0)

    report, pdf = _extract_small_detail(tmp_path)

    assert pdf.is_file()
    flat = " ".join(report.split())
    assert "[AVISO] Escala del dibujo: Escala medida en el PDF" in flat
    assert (
        "Escala física: NO GARANTIZADA; motivo: la escala medida en el PDF no coincide con la "
        "nominal" in flat
    )
    assert "Estado final: COMPLETA CON ADVERTENCIAS" in flat
    assert "Aviso: Escala medida en el PDF" in " ".join(capsys.readouterr().err.split())


def _pdf_with_line(path: Path, length_pt: float) -> Path:
    with pymupdf.open() as document:
        page = document.new_page(width=200, height=100)
        shape = page.new_shape()
        shape.draw_rect(page.rect)  # fondo del tamaño de la página: no cuenta
        shape.finish(fill=(1, 1, 1), color=None)
        shape.draw_line((20, 50), (20 + length_pt, 50))
        shape.draw_line((20, 50), (20, 50 - length_pt / 4))
        shape.finish(color=(0, 0, 0), width=2)
        shape.commit()
        document.save(path)
    return path


def test_check_measures_the_drawing_and_ignores_the_background(tmp_path: Path) -> None:
    length_mm = 100 * _MM_PER_POINT
    pdf = _pdf_with_line(tmp_path / "linea.pdf", 100)
    # 1 unidad CAD = 1 mm de papel: el dibujo mide length_mm × length_mm/4 unidades.
    drawn = (length_mm, length_mm / 4)

    exact = check_drawing_scale(pdf, 1.0, drawn, 1000)
    # 0,02 mm más de lo esperado en 35,3 mm: supera la tolerancia de 0,01 mm.
    wrong = check_drawing_scale(pdf, 1.0, (length_mm - 0.02, length_mm / 4), 1000)

    assert exact.passed
    assert "1:1000.0000 (X) y 1:1000.0000 (Y)" in exact.detail
    assert not wrong.passed
    assert "Mayor diferencia de longitud: +0.0200 mm" in wrong.detail
