"""Tablas de AutoCAD (ACAD_TABLE) dibujadas desde su bloque interno «*T» (``acad_tables.py``).

ezdxf carga una ACAD_TABLE como ``AcadTableBlockContent`` (una ``DXFTagStorage``, sin capa ni
color leídos) y el exportador la omitía: en PRGD-25 no salían la «TABLA DE GANCHO ESTÁNDAR», la
de estribos ni la fila «Total refuerzo» del «CUADRO DESPIECE», ni en el PDF ni en la vista
previa. Al leer el DXF, cada tabla que se puede dibujar pasa a ser un INSERT de su bloque «*T»
(mismo handle, mismo puesto del orden de dibujo, capa y color de los códigos del DXF); la que no
(sin bloque o girada) se omite con aviso. En las zonas, lo que no se puede situar ni dibujar se
avisa siempre (``MODELSPACE_UNKNOWN_SKIPPED``).
"""

import json
import os
from pathlib import Path

import ezdxf
import numpy as np
import pymupdf
import pytest
from ezdxf.entities.acad_table import AcadTableBlockContent
from ezdxf.lldxf.extendedtags import ExtendedTags
from PIL import Image
from test_preview_glyphs import _covered, _ink
from test_unknown_entities import _odd
from test_worker import running  # noqa: F401 - fixture del trabajador en el proceso
from test_worker_export import _export, _final, _load
from test_worker_zone import _ready

from cde_exporter.cad.acad_tables import NO_BLOCK, ROTATED, is_table
from cde_exporter.cad.preview_index import TEXT_SUBSTITUTIONS
from cde_exporter.cad.reader import read_dxf
from cde_exporter.cli import main

STANDARD = ["TABLA DE GANCHO ESTÁNDAR", "BARRA", "90°", "180°", "1/4\"", "11.0", "13.0"]
STIRRUPS = ["TABLA DE GANCHO ESTRIBOS", "BARRA", "135°", "3/8\"", "10.0", "9.0"]
TOTAL = ["Total refuerzo fy=420 Mpa (kg)", "125,40"]
X, Y = 4695620.0, 2230509.0
ZONE = [X - 0.5, Y - 0.5, X + 13.5, Y + 5.5]


def _table_block(document, texts: list[str], width: float):
    """Bloque «*T» como el que AutoCAD guarda con cada tabla: el marco con LINE y una fila de
    MTEXT por texto, de arriba abajo, desde (0, 0) hacia abajo (como las tablas de AutoCAD)."""
    block = document.blocks.new_anonymous_block(type_char="T")
    rows = len(texts)
    for row in range(rows + 1):
        block.add_line((0, -0.4 * row), (width, -0.4 * row), dxfattribs={"color": 0})
    block.add_line((0, 0), (0, -0.4 * rows), dxfattribs={"color": 0})
    block.add_line((width, 0), (width, -0.4 * rows), dxfattribs={"color": 0})
    for row, text in enumerate(texts):
        block.add_mtext(
            text,
            dxfattribs={"insert": (0.1, -0.4 * row - 0.1), "char_height": 0.15, "color": 0},
        )
    return block


def _table(document, layout, block_name: str, insert, layer: str, direction=(1, 0)):
    """Una ACAD_TABLE con los grupos que guarda AutoCAD: capa y color en AcDbEntity, bloque e
    inserción en AcDbBlockReference y la dirección de las filas en AcDbTable."""
    handle = document.entitydb.next_handle()
    tags = ExtendedTags.from_text(
        f"  0\nACAD_TABLE\n  5\n{handle}\n100\nAcDbEntity\n  8\n{layer}\n 62\n1\n"
        f"100\nAcDbBlockReference\n  2\n{block_name}\n 10\n{insert[0]}\n 20\n{insert[1]}\n"
        f" 30\n0.0\n100\nAcDbTable\n280\n0\n 11\n{direction[0]}\n 21\n{direction[1]}\n 31\n0.0\n"
    )
    table = AcadTableBlockContent.load(tags, document)
    document.entitydb.add(table)
    layout.add_entity(table)
    return table


def _source(path: Path, *, broken: bool = False, unknown: bool = False) -> Path:
    """Las dos tablas de gancho (capa FI-TEXTOS) y la del total (TABLA_MEDICION), en rojo (color
    de la tabla; el contenido del bloque es PorBloque), con una línea suelta entre medias. Con
    ``broken``, además, una tabla girada y otra sin su bloque; con ``unknown``, una entidad de
    un complemento suelta en el Model Space."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    for layer in ("FI-TEXTOS", "TABLA_MEDICION"):
        document.layers.add(layer)
    modelspace = document.modelspace()
    standard = _table_block(document, STANDARD, 3.0)
    stirrups = _table_block(document, STIRRUPS, 4.0)
    total = _table_block(document, TOTAL, 4.0)
    _table(document, modelspace, standard.name, (X, Y + 5), "FI-TEXTOS")
    modelspace.add_line((X, Y), (X + 13, Y))
    _table(document, modelspace, stirrups.name, (X + 3.5, Y + 5), "FI-TEXTOS")
    _table(document, modelspace, total.name, (X + 8, Y + 5), "TABLA_MEDICION")
    if broken:
        rotated = _table_block(document, ["GIRADA"], 2.0)
        _table(document, modelspace, rotated.name, (X, Y + 1), "FI-TEXTOS", direction=(0, 1))
        _table(document, modelspace, "*T99", (X + 5, Y + 1), "FI-TEXTOS")
    if unknown:
        _odd(document, modelspace)
    document.saveas(path)
    return path


def _zone(source: Path, out: Path, *extra: str) -> tuple[Path, dict, str]:
    prefix = out / "tablas"
    arguments = ["cad", "extract-modelspace", str(source), "--window", *map(str, ZONE)]
    assert main([*arguments, "--scale", "1:50", "--output-prefix", str(prefix), *extra]) == 0
    manifest = json.loads(prefix.with_name("tablas.manifest.json").read_text("utf-8"))
    report = prefix.with_name("tablas.txt").read_text(encoding="utf-8")
    return prefix.with_name("tablas.pdf"), manifest, report


def _codes(manifest: dict) -> set[str]:
    return {
        notice["code"]
        for notices in manifest["notices"]["by_category"].values()
        for notice in notices
    }


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def test_each_table_becomes_an_insert_of_its_block_in_its_place(tmp_path: Path) -> None:
    source = _source(tmp_path / "tablas.dxf")
    raw = ezdxf.readfile(source)
    before = [(e.dxf.handle, e.dxftype()) for e in raw.modelspace()]
    assert [kind for _h, kind in before] == ["ACAD_TABLE", "LINE", "ACAD_TABLE", "ACAD_TABLE"]

    document = read_dxf(source)
    after = list(document.modelspace())
    # Mismo handle y mismo puesto del orden de dibujo; capa y color de los códigos del DXF.
    assert [e.dxf.handle for e in after] == [handle for handle, _kind in before]
    tables = [e for e in after if e.dxftype() == "INSERT"]
    assert [e.dxf.name for e in tables] == [
        e.get_block_name() for e in raw.modelspace() if is_table(e)
    ]
    assert [e.dxf.layer for e in tables] == ["FI-TEXTOS", "FI-TEXTOS", "TABLA_MEDICION"]
    assert {e.dxf.color for e in tables} == {1}
    assert source.read_bytes() == source.read_bytes()  # el archivo no se toca (solo se leyó)
    assert not any(is_table(e) for e in document.entitydb.values())


def test_the_zone_pdf_has_the_three_tables_with_all_their_texts(tmp_path: Path) -> None:
    pdf, manifest, report = _zone(_source(tmp_path / "tablas.dxf"), tmp_path / "salida")
    with pymupdf.open(pdf) as document:
        page = document[0]
        text = page.get_text()
        red = [d for d in page.get_drawings() if d.get("color") and d["color"][0] > 0.9]
    for line in STANDARD + STIRRUPS + TOTAL:
        assert line in text, line
    assert red  # el marco PorBloque, con el color de la tabla
    assert "ACAD_TABLE_FROM_BLOCK" in _codes(manifest)
    assert "3 tablas de AutoCAD (ACAD_TABLE) dibujadas desde el bloque interno" in report
    assert "MODELSPACE_UNKNOWN_SKIPPED" not in _codes(manifest)
    # La capa es la de la tabla: excluirla la quita del PDF.
    pdf, manifest, _report = _zone(
        _source(tmp_path / "tablas.dxf"), tmp_path / "sin_medicion", "--exclude-layers",
        "TABLA_MEDICION",
    )
    with pymupdf.open(pdf) as document:
        text = document[0].get_text()
    assert TOTAL[0] not in text and STANDARD[0] in text


def test_tables_that_cannot_be_drawn_and_unknown_entities_are_never_lost_silently(
    tmp_path: Path,
) -> None:
    pdf, manifest, report = _zone(
        _source(tmp_path / "tablas.dxf", broken=True, unknown=True), tmp_path / "salida"
    )
    assert "MODELSPACE_UNKNOWN_SKIPPED" in _codes(manifest)
    assert f"ACAD_TABLE (1, {NO_BLOCK})" in report
    assert f"ACAD_TABLE (1, {ROTATED})" in report
    assert "VehicleTrackingPath (1, tipo que ezdxf no sabe leer)" in report
    with pymupdf.open(pdf) as document:
        assert STANDARD[0] in document[0].get_text()  # las demás salen igual


def test_a_table_inside_an_exported_block(tmp_path: Path) -> None:
    """En un bloque: la tabla que se puede dibujar sale (info) y la girada se avisa."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    detail = document.blocks.new("DETALLE")
    detail.add_line((0, 0), (5, 0))
    _table(document, detail, _table_block(document, TOTAL, 4.0).name, (0, 3), "TABLA_MEDICION")
    rotated = _table_block(document, ["GIRADA"], 2.0).name
    _table(document, detail, rotated, (0, 6), "TABLA_MEDICION", direction=(0, 1))
    document.modelspace().add_blockref("DETALLE", (0, 0))
    document.saveas(tmp_path / "bloque.dxf")
    prefix = tmp_path / "salida" / "detalle"
    arguments = ["cad", "extract-block", str(tmp_path / "bloque.dxf"), "--block-name", "DETALLE"]
    assert main([*arguments, "--scale", "1:50", "--output-prefix", str(prefix)]) == 0
    manifest = json.loads(prefix.with_name("detalle.manifest.json").read_text("utf-8"))
    assert {"ACAD_TABLE_FROM_BLOCK", "ACAD_TABLE_NOT_DRAWN"} <= _codes(manifest)
    assert f"1 tabla de AutoCAD (ACAD_TABLE) no sale en el PDF: 1 {ROTATED}." in (
        prefix.with_name("detalle.txt").read_text(encoding="utf-8")
    )
    with pymupdf.open(prefix.with_suffix(".pdf")) as pdf:
        text = pdf[0].get_text()
    assert TOTAL[0] in text and "GIRADA" not in text


def test_the_prepared_preview_and_the_texts_command_show_the_tables(
    running,  # noqa: F811
    tmp_path: Path,
) -> None:
    """La vista previa preparada dibuja las tablas donde el PDF, con sus letras, y la orden
    ``texts`` devuelve sus textos."""
    sink, stdin = running
    session = _load(sink, stdin, _source(tmp_path / "tablas.dxf"))["session"]
    _ready(sink)
    stdin.send(id="c-t", command="texts", data={"session": session, "zone": ZONE})
    found = {item["text"] for item in _final(sink, "c-t")["data"]["texts"]}
    assert set(STANDARD + STIRRUPS + TOTAL) <= found
    stdin.send(
        id="c-p", command="preview", data={"session": session, "zone": ZONE, "long_side_px": 1600}
    )
    preview = _final(sink, "c-p")["data"]
    assert preview["prepared"] is True
    assert preview["texts"]["drawn"] == len(STANDARD + STIRRUPS + TOTAL)
    a, _b, _c, d, e, f = preview["cad_to_px"]
    mine = np.asarray(Image.open(preview["png"]).convert("RGBA"))

    answer = _export(
        sink, stdin, "c-x", session, tmp_path / "paquetes",
        [{"job": "t1", "zone": ZONE, "scale": "1:50", "file_stem": "Tablas"}],
        font_substitutions=str(TEXT_SUBSTITUTIONS),
    )
    job = answer["data"]["jobs"][0]
    assert job["status"] == "published", job
    manifest = json.loads(Path(job["package"]["manifest"]).read_text(encoding="utf-8"))
    pa, _pb, _pc, pd, pe, pf = manifest["pdf"]["drawing"]["cad_to_page"]["matrix"]
    mm_per_pt = 25.4 / 72
    height, width = mine.shape[:2]
    x0, y1, x1, y0 = (0 - e) / a, (0 - f) / d, (width - e) / a, (height - f) / d
    clip = pymupdf.Rect(
        (pa * x0 + pe) / mm_per_pt, (pd * y1 + pf) / mm_per_pt,
        (pa * x1 + pe) / mm_per_pt, (pd * y0 + pf) / mm_per_pt,
    )
    with pymupdf.open(job["package"]["pdf"]) as pdf:
        zoom = width / clip.width
        pixmap = pdf[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
    theirs = np.frombuffer(pixmap.samples, np.uint8).reshape(pixmap.height, pixmap.width, 3)
    theirs = np.dstack([theirs, np.full(theirs.shape[:2], 255, np.uint8)])
    rows, columns = min(height, theirs.shape[0]), min(width, theirs.shape[1])
    mine_ink, their_ink = _ink(mine[:rows, :columns]), _ink(theirs[:rows, :columns])
    assert their_ink.sum() > 2000 and mine_ink.sum() > 2000
    assert _covered(their_ink, mine_ink) > 0.95
    assert _covered(mine_ink, their_ink) > 0.95


# ----------------------------------------------------------------- PRGD-25 (datos de ERCO)

_PRGD = Path("C:/Users/LuisAngelNunezPadill/Downloads/PRGD-25-1365-CIV-PL-106_1_VA.dxf")


@pytest.mark.skipif(
    os.environ.get("CDE_REFERENCE_TESTS") != "1" or not _PRGD.is_file(),
    reason="PRGD-25 de Descargas: solo con CDE_REFERENCE_TESTS=1",
)
def test_prgd_tables_come_out_with_all_their_texts(tmp_path: Path) -> None:
    """Las dos tablas de gancho y la del total del «CUADRO DESPIECE»: todos los textos de su
    bloque «*T» se buscan en el PDF de su zona."""
    raw = ezdxf.readfile(_PRGD)
    cases = {
        "7C78E": [4695620.5, 2230509.0, 4695628.5, 2230514.0],  # gancho estándar y estribos
        "81EB6": [4696015.0, 2229980.0, 4696018.0, 2229981.5],  # total del cuadro despiece
    }
    expected = {}
    for handle in ("7C78E", "7C78F", "81EB6"):
        block = raw.blocks[raw.entitydb[handle].get_block_name()]
        expected[handle] = [e.plain_text().strip() for e in block if e.dxftype() == "MTEXT"]
    assert "TABLA DE GANCHO ESTÁNDAR" in expected["7C78E"]
    assert "TABLA DE GANCHO ESTRIBOS" in expected["7C78F"]
    assert any(text.startswith("Total refuerzo") for text in expected["81EB6"])
    for handle, zone in cases.items():
        prefix = tmp_path / handle / "zona"
        arguments = ["cad", "extract-modelspace", str(_PRGD), "--window", *map(str, zone)]
        assert main([*arguments, "--scale", "1:50", "--output-prefix", str(prefix)]) == 0
        with pymupdf.open(prefix.with_suffix(".pdf")) as pdf:
            page = pdf[0]
            texts = [text for key in ([handle, "7C78F"] if handle == "7C78E" else [handle])
                     for text in expected[key] if text]
            missing = [text for text in texts if not page.search_for(text.split("\n")[0])]
        assert missing == [], missing
