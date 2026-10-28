import re
from pathlib import Path

import ezdxf
import pytest

from cde_exporter.cli import main

# Modelos capturados en la prueba en curso (fixture ``model_details`` de conftest.py).
_details: list[dict] = []


@pytest.fixture(autouse=True)
def _use_model_details(model_details):
    global _details
    _details = model_details


def _extract(path: Path, output: Path) -> dict:
    """Extrae y devuelve el modelo del detalle (antes, el JSON de --detail-json)."""
    assert main([
        "cad", "extract-block", str(path), "--block-name", "Detalle",
        "--output-prefix", str(output.parent / ("salida-" + output.stem)), "--format", "svg",
        "--hatch-mode", "solid",
    ]) == 0
    return _details[-1]


def test_detail_json_fills_extra_fields_from_existing_flow(tmp_path: Path, capsys) -> None:
    source = tmp_path / "con-extras.dxf"
    drawing = ezdxf.new()
    drawing.styles.new("FuenteAusente", dxfattribs={"font": "fuente_ausente_987.ttf"})
    inner = drawing.blocks.new("Interior")
    inner.add_text("texto", dxfattribs={"height": 1, "style": "FuenteAusente"})
    inner.add_hatch().set_pattern_fill("ANSI31", scale=1)
    outer = drawing.blocks.new("Detalle")
    outer.add_line((0, 0), (2, 1))
    outer.add_blockref("Interior", (1, 1))
    drawing.modelspace().add_blockref(
        "Detalle", (10, 20), dxfattribs={"rotation": 30, "xscale": 2, "yscale": 1}
    )
    drawing.saveas(source)

    detail = _extract(source, tmp_path / "con-extras.json")

    assert [step["kind"] for step in detail["transformations"]] == [
        "insertion", "rotation", "scale",
    ]
    assert detail["transformations"] == [
        {"kind": "insertion", "description": "Punto de inserción (X, Y, Z)", "values": [10, 20, 0]},
        {"kind": "rotation", "description": "Rotación aplicada (grados)", "values": [30]},
        {"kind": "scale", "description": "Escala no uniforme aplicada", "values": [2, 1, 1]},
    ]
    assert detail["nested_blocks"] == [
        {"name": "Interior", "max_depth": 1, "reference_count": 1},
    ]
    assert detail["fonts"] == [
        {
            "requested": "fuente_ausente_987.ttf",
            "resolved": detail["fonts"][0]["resolved"],
            "found": False,
            "uses": 1,
            "origin": "respaldo automático",
        },
    ]
    assert detail["hatches"]["total"] == 1
    assert detail["hatches"]["simplified"] == 1

    summary = capsys.readouterr().out
    missing_count = sum(font["uses"] for font in detail["fonts"] if not font["found"])
    reported_count = int(re.search(r"(\d+) usos de fuentes sustitutas", summary).group(1))
    assert missing_count == reported_count == 1


def test_detail_json_keeps_extra_fields_empty_when_absent(tmp_path: Path) -> None:
    source = tmp_path / "sin-extras.dxf"
    drawing = ezdxf.new()
    drawing.blocks.new("Detalle").add_line((0, 0), (2, 1))
    drawing.modelspace().add_blockref("Detalle", (0, 0))
    drawing.saveas(source)

    detail = _extract(source, tmp_path / "sin-extras.json")

    assert len(detail["transformations"]) == 3
    assert detail["nested_blocks"] == []
    assert detail["fonts"] == []
    assert detail["hatches"] is None
