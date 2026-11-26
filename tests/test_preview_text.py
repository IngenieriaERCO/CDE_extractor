"""Texto real en la vista previa preparada (protocolo 1.4; preview_index.py y
docs/protocolo_trabajador.md, sección 4.2): letras desde 6 píxeles de altura, raya de 1 a 6 y
punto por debajo; misma fuente, altura y giro que en el PDF."""

import json
import math
from pathlib import Path
from types import SimpleNamespace

import ezdxf
import numpy as np
import pymupdf
from PIL import Image
from test_preview_index import _ink, _px
from test_worker import running  # noqa: F401 - fixture del trabajador en el proceso
from test_worker_export import _export, _final, _load
from test_worker_zone import _ready

from cde_exporter.cad.preview_index import (
    TEXT_FILL,
    TEXT_LINE,
    TEXT_SUBSTITUTIONS,
    Preparation,
    paint_index,
)

MM_PER_PT = 25.4 / 72


def _document():
    """«HOLA» (altura 1) en (2, 2); «O» (altura 4) en (2, 5); «VERTICAL» girado 90° en (9, 1);
    y, dentro de un bloque escalado ×2, «BLOQUE» (altura 0,5, capa «0») en (1, 0) del bloque."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    document.layers.add("TEXTOS")
    document.layers.add("DETALLE")
    block = document.blocks.new("DET")
    block.add_text("BLOQUE", height=0.5, dxfattribs={"insert": (1, 0)})
    modelspace = document.modelspace()
    modelspace.add_text("HOLA", height=1, dxfattribs={"layer": "TEXTOS", "insert": (2, 2)})
    modelspace.add_text("O", height=4, dxfattribs={"layer": "TEXTOS", "insert": (2, 5)})
    modelspace.add_text(
        "VERTICAL", height=0.5, rotation=90, dxfattribs={"layer": "TEXTOS", "insert": (9, 1)}
    )
    modelspace.add_blockref(
        "DET", (20, 0), dxfattribs={"layer": "DETALLE", "xscale": 2, "yscale": 2}
    )
    return document


def _index(document=None):
    document = document or _document()
    return Preparation(document, boxes_target=SimpleNamespace(modelspace_boxes=None)).build()


def _texts(index) -> list[tuple[int, float, np.ndarray]]:
    """(tipo, altura, línea media) de cada texto del índice, en el orden de dibujo."""
    found = []
    for item in np.nonzero(index.text_slot >= 0)[0]:
        slot = index.text_slot[item]
        found.append((int(index.kind[item]), float(index.text_height[slot]), index.text_line[slot]))
    return found


def test_each_text_is_one_item_with_its_height_and_middle_line() -> None:
    index = _index()
    texts = _texts(index)
    assert index.texts == 4 and len(texts) == 4
    assert all(kind == TEXT_FILL for kind, _h, _l in texts)  # TrueType: letras rellenas
    heights = sorted(height for _kind, height, _line in texts)
    # La altura con la escala del INSERT: «BLOQUE» mide 0,5 × 2.
    assert heights == [0.5, 1.0, 1.0, 4.0]
    hola = next(line for _k, height, line in texts if height == 1.0 and line[0] < 5)
    x0, y0, x1, y1 = hola
    assert 2 <= x0 < 2.2 and 3 < x1 < 6  # de izquierda a derecha, desde el punto de inserción
    assert y0 == y1 and 2.2 < y0 < 2.8  # a media altura de las letras
    vertical = next(line for _k, height, line in texts if height == 0.5)
    assert abs(vertical[0] - vertical[2]) < 1e-9 and vertical[3] > vertical[1]  # girado 90°


def test_letters_are_painted_from_6_pixels_lines_from_1_and_dots_below() -> None:
    index = _index()
    # 100 px por unidad: «HOLA» mide 100 px de alto.
    near, stats = paint_index(index, (0, 0, 10, 10), 1020)
    assert stats["texts"] == {"drawn": 3, "as_lines": 0, "as_dots": 0}
    image = near.image
    # La «O» grande: tinta en el trazo y hueco en el centro (no es su caja).
    x0, y, x1, _y = next(line for _k, height, line in _texts(index) if height == 4.0)
    assert _ink(image, _px(near, x0 + 0.05, y))  # el trazo izquierdo de la «O»
    assert image.getpixel(_px(near, (x0 + x1) / 2, y))[3] == 0  # el hueco
    # 3 px por unidad: «HOLA» (1) es una raya; la «O» (4 → 12 px), letras.
    middle, stats = paint_index(index, (0, 0, 330, 330), 1010)
    assert stats["texts"]["as_lines"] >= 3 and stats["texts"]["drawn"] == 1
    # 0,5 px por unidad: todo punto salvo la «O» (2 px): raya.
    far, stats = paint_index(index, (0, 0, 2000, 2000), 1010)
    assert stats["texts"] == {"drawn": 0, "as_lines": 1, "as_dots": 3}


def test_the_fonts_are_the_ones_of_the_pdf() -> None:
    """Con la sustitución de la vista previa (la del PDF de referencia), «romans.shx» sale como
    Arial rellena; sin ella sería una fuente de trazo (TEXT_LINE)."""
    assert TEXT_SUBSTITUTIONS.is_file()
    document = ezdxf.new("R2018")
    document.styles.add("ROMANS", font="romans.shx")
    document.modelspace().add_text("ROMANS", height=1, dxfattribs={"style": "ROMANS"})
    kinds = {kind for kind, _height, _line in _texts(_index(document))}
    assert kinds == {TEXT_FILL}
    assert TEXT_LINE not in kinds


def _zone_source(path: Path) -> Path:
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    modelspace = document.modelspace()
    modelspace.add_text(
        "ESC 1:25", height=0.5, rotation=30, dxfattribs={"color": 1, "insert": (1, 1)}
    )
    modelspace.add_lwpolyline([(0, 0), (6, 0), (6, 4), (0, 4)], close=True)
    document.saveas(path)
    return path


def test_the_preview_letters_land_where_the_pdf_draws_them(running, tmp_path: Path) -> None:  # noqa: F811
    """La caja de la tinta roja del texto en la vista previa preparada y la de sus trazos en el
    PDF publicado coinciden a menos de 1 píxel de la vista previa."""
    sink, stdin = running
    session = _load(sink, stdin, _zone_source(tmp_path / "texto.dxf"))["session"]
    _ready(sink)
    stdin.send(
        id="c-p",
        command="preview",
        data={"session": session, "zone": [0, 0, 6, 4], "long_side_px": 1200},
    )
    preview = _final(sink, "c-p")["data"]
    assert preview["prepared"] is True
    assert preview["texts"] == {"drawn": 1, "as_lines": 0, "as_dots": 0}
    a, _b, _c, d, e, f = preview["cad_to_px"]
    pixels = np.asarray(Image.open(preview["png"]).convert("RGBA"))
    red = np.argwhere(
        (pixels[:, :, 0] > 200) & (pixels[:, :, 1] < 80) & (pixels[:, :, 3] > 0)
    )  # (fila, columna)
    columns, rows = red[:, 1], red[:, 0]
    preview_box = (
        (columns.min() - e) / a,
        (rows.max() + 1 - f) / d,
        (columns.max() + 1 - e) / a,
        (rows.min() - f) / d,
    )

    answer = _export(
        sink,
        stdin,
        "c-x",
        session,
        tmp_path / "paquetes",
        [{"job": "t1", "zone": [0, 0, 6, 4], "scale": "1:50", "file_stem": "Texto"}],
        font_substitutions=str(TEXT_SUBSTITUTIONS),
    )
    assert answer["type"] == "result", answer
    job = answer["data"]["jobs"][0]
    assert job["status"] == "published", job
    manifest = json.loads(Path(job["package"]["manifest"]).read_text(encoding="utf-8"))
    pa, _pb, _pc, pd, pe, pf = manifest["pdf"]["drawing"]["cad_to_page"]["matrix"]
    with pymupdf.open(job["package"]["pdf"]) as pdf:
        points = [
            point
            for drawing in pdf[0].get_drawings()
            if drawing.get("fill") and tuple(round(v, 2) for v in drawing["fill"]) == (1, 0, 0)
            for item in drawing["items"]
            for point in item[1:]
            if hasattr(point, "x")
        ]
    xs = [((p.x * MM_PER_PT) - pe) / pa for p in points]
    ys = [((p.y * MM_PER_PT) - pf) / pd for p in points]
    pdf_box = (min(xs), min(ys), max(xs), max(ys))
    pixel = 1 / a
    assert all(
        math.isclose(mine, theirs, abs_tol=1.5 * pixel)
        for mine, theirs in zip(preview_box, pdf_box, strict=True)
    ), (preview_box, pdf_box, pixel)
