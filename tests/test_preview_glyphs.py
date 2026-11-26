"""Partes de las letras y MULTILEADER con gráfico proxy en la vista previa preparada.

Con coordenadas de millones (las de PRGD-25), la vista previa perdía letras enteras
(«REFUERZO DE MUROS EN PLANTA» salía «R FU DE / R S EN P NT») y la «Ø» de «Ø=½"»:
``Path.flattening`` unía las partes de cada letra en una sola línea, y el hueco se decidía por el
sentido de giro con un área calculada sin precisión. Ahora cada parte va por separado y es hueco
la que queda dentro de otra de la misma letra (``preview_index._glyph_rings``).

Y ezdxf dibujaba los MULTILEADER desde su gráfico proxy, que AutoCAD puede guardar de relleno (el
texto «AcDbMLeader (ACDB_MLEADER_CLASS)» en un recuadro): ahora se dibujan desde sus datos, en el
PDF y en la vista previa (``exporters._StrictFrontend._draw_composite``).

Cada caso se comprueba contra el PDF publicado de la misma zona: la tinta de la vista previa y la
del PDF coinciden.
"""

import json
import struct
from pathlib import Path
from types import SimpleNamespace

import ezdxf
import numpy as np
import pymupdf
import pytest
from ezdxf.math import Vec2
from ezdxf.proxygraphic import ProxyGraphic
from ezdxf.render import mleader
from PIL import Image, ImageFilter
from test_worker import running  # noqa: F401 - fixture del trabajador en el proceso
from test_worker_export import _export, _final, _load
from test_worker_zone import _ready

from cde_exporter.cad.preview_index import TEXT_SUBSTITUTIONS, Preparation, paint_index

_ARIAL = Path("C:/Windows/Fonts/arial.ttf")
_CALIBRI = Path("C:/Windows/Fonts/calibrib.ttf")
pytestmark = pytest.mark.skipif(
    not (_ARIAL.is_file() and _CALIBRI.is_file()), reason="faltan arial.ttf o calibrib.ttf"
)
MM_PER_PT = 25.4 / 72
# Las coordenadas de PRGD-25 (zona de las cajas de paso).
X, Y = 4696010.0, 2229980.0
ZONE = [X, Y, X + 4.0, Y + 2.5]
STUB = "AcDbMLeader (ACDB_MLEADER_CLASS)"


def _stub_proxy(x: float, y: float) -> bytes:
    """Gráfico proxy como el que AutoCAD guardó en los 386 MULTILEADER de PRGD-25: el texto
    «AcDbMLeader (ACDB_MLEADER_CLASS)» (unicode, tipo 36) y un recuadro (polilínea, tipo 6)."""
    text = STUB.encode("utf-16-le") + b"\0\0"
    text += b"\0" * (-len(text) % 4)
    text_data = struct.pack("<9d3d", x, y, 0, 0, 0, 1, 1, 0, 0, 0.02, 1, 0) + text
    box = [(x, y - 0.01), (x + 0.6, y - 0.01), (x + 0.6, y + 0.03), (x, y + 0.03), (x, y - 0.01)]
    box_data = struct.pack("<L", len(box)) + b"".join(struct.pack("<3d", *p, 0) for p in box)
    entries = [(36, text_data), (6, box_data)]
    body = b"".join(struct.pack("<2L", 8 + len(data), kind) + data for kind, data in entries)
    return struct.pack("<2L", 8 + len(body), len(entries)) + body


def _source(path: Path | None, only_81ced: bool = False):
    """Los dos textos de PRGD-25 tal como están en el DXF (81CED y 81E7A) y un MULTILEADER con
    su texto real «NOTA 1» y el gráfico proxy de relleno. Sin ``path``, el documento."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    style = document.styles.add("ARIAL", font="")
    style.set_extended_font_data("Sora ExtraLight", italic=False, bold=False)
    modelspace = document.modelspace()
    modelspace.add_mtext(  # 81CED
        "\\pxqc;REFUERZO DE MUROS EN PLANTA",
        dxfattribs={
            "style": "ARIAL",
            "insert": (X + 1.1156, Y + 1.4438),
            "char_height": 0.05,
            "width": 0.7783,
            "attachment_point": 4,
        },
    )
    if only_81ced:
        return document
    modelspace.add_mtext(  # 81E7A
        '{\\fCalibri|b1|i0|c0|p34;\u00d8=\u00bd"}',
        dxfattribs={"insert": (X + 2.2837, Y + 1.3610), "char_height": 0.1, "width": 202.7},
    )
    builder = modelspace.add_multileader_mtext("Standard")
    builder.set_content("NOTA 1", char_height=0.08, alignment=mleader.TextAlignment.left)
    builder.add_leader_line(mleader.ConnectionSide.left, [Vec2(X + 1.0, Y + 0.5)])
    builder.build(insert=Vec2(X + 2.0, Y + 0.8))
    builder.multileader.proxy_graphic = _stub_proxy(X + 2.0, Y + 0.8)
    if path is None:
        return document
    document.saveas(path)
    return path


def _ink(pixels: np.ndarray) -> np.ndarray:
    """Tinta: lo que no es fondo (transparente o blanco)."""
    rgb = pixels[:, :, :3].astype(int)
    return (pixels[:, :, 3] > 0) & (rgb.sum(axis=2) < 600)


def _covered(mine: np.ndarray, theirs: np.ndarray) -> float:
    """Fracción de la tinta de ``mine`` que cae sobre la de ``theirs`` (con 2 píxeles de
    holgura por el redondeo de cada dibujo)."""
    wide = Image.fromarray(theirs.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(5))
    wide = np.asarray(wide) > 0
    return float((mine & wide).sum()) / max(1, int(mine.sum()))


def test_the_saved_proxy_is_the_stub_and_ezdxf_would_prefer_it(tmp_path: Path) -> None:
    """La prueba de abajo parte del caso real: ezdxf, por sí solo, dibujaría el relleno."""
    document = ezdxf.readfile(_source(tmp_path / "a.dxf"))
    multileader = next(iter(document.modelspace().query("MULTILEADER")))
    stub = [e.dxf.text for e in ProxyGraphic(multileader.proxy_graphic, document).virtual_entities()
            if e.dxftype() == "TEXT"]
    assert stub == [STUB]
    assert [e.dxf.text for e in multileader.virtual_entities() if e.dxftype() == "TEXT"] == [STUB]


def test_each_part_of_a_letter_is_a_ring_and_holes_are_inside_the_same_letter() -> None:
    document = _source(None, only_81ced=True)
    index = Preparation(document, boxes_target=SimpleNamespace(modelspace_boxes=None)).build()
    # «REFUERZO DE MUROS EN PLANTA»: 23 letras y 9 huecos (R R O D R O P A A).
    assert index.texts == 5  # una por palabra
    assert index.span_start.size - 1 == 23 + 9
    assert int(index.span_hole.sum()) == 9


def test_the_preview_letters_and_multileader_match_the_pdf(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    session = _load(sink, stdin, _source(tmp_path / "prgd.dxf"))["session"]
    _ready(sink)
    stdin.send(
        id="c-p", command="preview",
        data={"session": session, "zone": ZONE, "long_side_px": 1600},
    )
    preview = _final(sink, "c-p")["data"]
    assert preview["prepared"] is True
    assert preview["png"].endswith("_it2.png")  # la marca nueva: las «_it» no se reutilizan
    a, _b, _c, d, e, f = preview["cad_to_px"]
    mine = np.asarray(Image.open(preview["png"]).convert("RGBA"))

    answer = _export(
        sink, stdin, "c-x", session, tmp_path / "paquetes",
        [{"job": "t1", "zone": ZONE, "scale": "1:50", "file_stem": "Letras"}],
        font_substitutions=str(TEXT_SUBSTITUTIONS),
    )
    job = answer["data"]["jobs"][0]
    assert job["status"] == "published", job
    report = Path(job["package"]["pdf"]).with_suffix(".txt").read_text(encoding="utf-8")
    assert STUB not in report
    with pymupdf.open(job["package"]["pdf"]) as pdf:
        page = pdf[0]
        assert STUB not in page.get_text()
        assert "NOTA 1" in page.get_text()  # la capa de texto: el texto real del MULTILEADER
        # La misma vista que la vista previa: de píxel a CAD con su matriz, y de CAD a la
        # página con la del manifiesto (en este caso, 20 mm por metro a 1:50).
        manifest = json.loads(Path(job["package"]["manifest"]).read_text(encoding="utf-8"))
        pa, _pb, _pc, pd, pe, pf = manifest["pdf"]["drawing"]["cad_to_page"]["matrix"]
        height, width = mine.shape[:2]
        x0, y1 = (0 - e) / a, (0 - f) / d
        x1, y0 = (width - e) / a, (height - f) / d
        clip = pymupdf.Rect(
            (pa * x0 + pe) / MM_PER_PT, (pd * y1 + pf) / MM_PER_PT,
            (pa * x1 + pe) / MM_PER_PT, (pd * y0 + pf) / MM_PER_PT,
        )
        zoom = width / clip.width
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
    theirs = np.frombuffer(pixmap.samples, np.uint8).reshape(pixmap.height, pixmap.width, 3)
    theirs = np.dstack([theirs, np.full(theirs.shape[:2], 255, np.uint8)])
    rows, columns = min(height, theirs.shape[0]), min(width, theirs.shape[1])
    mine_ink, their_ink = _ink(mine[:rows, :columns]), _ink(theirs[:rows, :columns])
    assert their_ink.sum() > 2000 and mine_ink.sum() > 2000
    # Antes del arreglo faltaban letras enteras, la «Ø», y el MULTILEADER era el recuadro
    # (muy por debajo de esto).
    assert _covered(their_ink, mine_ink) > 0.95, "falta tinta del PDF en la vista previa"
    assert _covered(mine_ink, their_ink) > 0.95, "la vista previa pinta lo que el PDF no"


def test_paint_index_of_far_coordinates_keeps_the_holes() -> None:
    """Una «O» a 4,7 millones de metros: la tinta en el trazo y el hueco vacío."""
    document = ezdxf.new("R2018")
    document.styles.add("ARIAL", font="arial.ttf")  # la «O» de «txt» es rectangular
    document.modelspace().add_text("O", height=4, dxfattribs={"insert": (X, Y), "style": "ARIAL"})
    index = Preparation(document, boxes_target=SimpleNamespace(modelspace_boxes=None)).build()
    assert int(index.span_hole.sum()) == 1
    image, _stats = paint_index(index, (X - 1, Y - 1, X + 5, Y + 5), 600)
    a, _b, _c, d, e, f = image.cad_to_px
    line = index.text_line[0]
    middle_x, middle_y = (line[0] + line[2]) / 2, line[1]
    pixel = (int(a * middle_x + e), int(d * middle_y + f))
    assert image.image.getpixel(pixel)[3] == 0  # el hueco de la «O»
    left = (int(a * (line[0] + 0.1) + e), int(d * middle_y + f))
    assert image.image.getpixel(left)[3] > 0  # su trazo izquierdo
