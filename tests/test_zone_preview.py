"""Vista previa de una zona del Model Space (subfase 2, ``cad preview-modelspace``)."""

import json
from pathlib import Path

import ezdxf
import pytest
from PIL import Image

from cde_exporter.cad.zone_preview import (
    drawing_groups,
    grid_step,
    paper_size,
    select_view,
)
from cde_exporter.cli import main

RED = (220, 0, 0)


def _drawing(path: Path) -> Path:
    """Dos dibujos lejanos: en (0..10, 0..10), un cuadrado en la capa «MURO» y un bloque con
    una línea en «MURO» y otra en «COTA»; en (1000..1010), un círculo en «EJE»."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    for name in ("MURO", "COTA", "EJE"):
        document.layers.add(name)
    block = document.blocks.new("DET")
    block.add_line((0, 0), (4, 0), dxfattribs={"layer": "MURO"})
    block.add_line((0, 1), (4, 1), dxfattribs={"layer": "COTA"})
    modelspace = document.modelspace()
    modelspace.add_lwpolyline(
        [(0, 0), (10, 0), (10, 10), (0, 10)], close=True, dxfattribs={"layer": "MURO"}
    )
    modelspace.add_blockref("DET", (3, 3), dxfattribs={"layer": "MURO"})
    modelspace.add_circle((1005, 1005), 5, dxfattribs={"layer": "EJE"})
    target = path / "zona.dxf"
    document.saveas(target)
    return target


def _run(tmp_path: Path, *extra: str) -> tuple[Image.Image, dict]:
    source = _drawing(tmp_path)
    output = tmp_path / "salida" / "vista.png"
    assert main(["cad", "preview-modelspace", str(source), "--output", str(output), *extra]) == 0
    data = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
    return Image.open(output).convert("RGB"), data


def _px(data: dict, x: float, y: float) -> tuple[int, int]:
    a, _b, _c, d, e, f = data["cad_to_px"]
    return int(a * x + e), int(d * y + f)


def _dark(image: Image.Image, xy: tuple[int, int], radius: int = 1) -> bool:
    """Hay trazo (oscuro) en el píxel o a su lado."""
    x0, y0 = xy
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            if sum(image.getpixel((x0 + dx, y0 + dy))) < 200:
                return True
    return False


def test_grid_step_is_a_round_number() -> None:
    assert grid_step(100.0) == 20.0
    assert grid_step(0.67) == pytest.approx(0.1)
    assert grid_step(4629.0) == 1000.0
    assert grid_step(0.0) == 1.0


def test_paper_size_at_the_scale_with_margins() -> None:
    paper = paper_size((0.0, 0.0, 0.67, 0.65), 1000.0, 10.0, 5.0)
    assert paper["content_mm"] == [67.0, 65.0]
    assert paper["page_mm"] == [77.0, 75.0]
    assert paper["fits_a1"]
    assert not paper_size((0.0, 0.0, 100.0, 1.0), 1000.0, 50.0, 5.0)["fits_a1"]


def test_drawing_groups_splits_far_apart_drawings() -> None:
    boxes = [(0, 0, 1, 1), (2, 0, 3, 1), (1000, 1000, 1001, 1001), None, (0, 500, 1, 501)]
    groups = drawing_groups(boxes, gap=10.0)
    assert [group["entities"] for group in groups] == [2, 1, 1]
    assert groups[0]["box"] == [0, 0, 3, 1]
    assert [group["number"] for group in groups] == [1, 2, 3]


def test_select_view_keeps_what_touches_the_view(tmp_path: Path) -> None:
    document = ezdxf.readfile(_drawing(tmp_path))
    everything = select_view(document, None)
    assert len(everything.entities) == 3
    assert everything.box == pytest.approx((0, 0, 1010, 1010))
    near = select_view(document, (-1, -1, 11, 11))
    assert sorted(entity.dxftype() for entity in near.entities) == ["INSERT", "LWPOLYLINE"]
    assert near.layers == {"MURO": 2}


def test_whole_modelspace_preview_lists_the_drawing_groups(tmp_path: Path, capsys) -> None:
    image, data = _run(tmp_path)
    assert data["format"] == "cde-zone-preview"
    assert data["zone"] is None and data["paper"] is None
    assert [group["entities"] for group in data["groups"]] == [2, 1]
    # La vista tiene aire alrededor: los dos grupos caben enteros.
    view = data["view"]
    assert view[0] < 0 and view[2] > 1010
    assert image.size == (data["image"]["width_px"], data["image"]["height_px"])
    assert "--view" in capsys.readouterr().out


def test_zone_preview_marks_the_zone_and_gives_the_paper_size(tmp_path: Path) -> None:
    image, data = _run(tmp_path, "--window", "2", "2", "8", "8", "--scale", "1:100")
    # Sin --view: la zona con un 25 % de contexto por lado.
    assert data["view"] == pytest.approx([0.5, 0.5, 9.5, 9.5])
    assert data["zone"] == [2.0, 2.0, 8.0, 8.0]
    assert data["paper"]["content_mm"] == [60.0, 60.0]
    assert data["paper"]["page_mm"] == [70.0, 70.0]
    assert data["image"]["footer_px"] > 0
    # Borde de la zona en rojo; la línea de «COTA» del bloque, a y = 4, se dibuja.
    left, top = _px(data, 2.0, 8.0)
    assert image.getpixel((left, (top + _px(data, 2.0, 2.0)[1]) // 2)) == RED
    assert _dark(image, _px(data, 5.0, 4.0))
    # El pie va debajo de la imagen: cad_to_px no cambia por él.
    assert image.size[1] == data["image"]["height_px"]


def test_excluded_layers_are_not_drawn_inside_blocks(tmp_path: Path) -> None:
    image, data = _run(tmp_path, "--window", "2", "2", "8", "8", "--exclude-layers", "COTA")
    assert not _dark(image, _px(data, 5.0, 4.0))  # «COTA» dentro del bloque: no se ve
    assert _dark(image, _px(data, 5.0, 3.0))  # «MURO» dentro del bloque: sí
    rows = {row["name"]: row for row in data["layers"]}
    assert rows["COTA"]["shown"] is False and rows["MURO"]["shown"] is True
    assert data["filters"]["exclude_layers"] == ["COTA"]


def test_a_whitelist_hides_everything_else(tmp_path: Path) -> None:
    image, data = _run(tmp_path, "--view", "-1", "-1", "11", "11", "--layers", "COTA")
    # El INSERT está en «MURO»: con solo «COTA» no entra nada del primer nivel.
    assert data["entities"] == {"in_view": 2, "drawn": 0, "without_bounds": 0}
    assert not _dark(image, _px(data, 5.0, 4.0))
    assert not _dark(image, _px(data, 5.0, 0.0))


def test_the_output_must_be_a_png(tmp_path: Path, capsys) -> None:
    source = _drawing(tmp_path)
    code = main(["cad", "preview-modelspace", str(source), "--output", str(tmp_path / "v.jpg")])
    assert code == 1
    assert ".png" in capsys.readouterr().err
