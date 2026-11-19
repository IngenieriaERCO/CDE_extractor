"""--plot-style (tabla de estilos de impresión .ctb) y --no-print-transparency: solo cambian el
dibujo cuando se piden (cad/plot_style.py)."""

import json
import struct
import zlib
from io import StringIO
from pathlib import Path

import ezdxf
import pymupdf
import pytest
from ezdxf.addons import acadctb
from ezdxf.addons.drawing.properties import Properties
from test_worker import TIMEOUT, running  # noqa: F401 - fixture del trabajador en el proceso

from cde_exporter.cad.manifest import verify_package
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.plot_style import apply_plot_style, load_plot_style, plotted_rgb
from cde_exporter.cli import main

PT_PER_MM = 72 / 25.4


def _ctb(path: Path, description: str = "CTB de diseño ERCO", encoding: str = "cp1252") -> Path:
    """CTB sintética: ACI 35 → (184, 138, 92) y 0,35 mm; ACI 7 → (46, 46, 46); ACI 3 en
    grises; ACI 5 tramado al 50 %. Se comprime a mano para poder guardar la descripción en
    cp1252, como las CTB de AutoCAD en español."""
    table = acadctb.new_ctb()
    table.description = description
    table[35].color = (184, 138, 92)
    table[35].set_lineweight(0.35)
    table[7].color = (46, 46, 46)
    table[3].grayscale = True
    table[5].screen = 50
    memfile = StringIO()
    table.write_content(memfile)
    raw = (memfile.getvalue() + "\0").encode(encoding)
    body = zlib.compress(raw)
    header = b"PIAFILEVERSION_2.0,CTBVER1,compress\r\npmzlibcodec"
    path.write_bytes(header + struct.pack("<LLL", zlib.adler32(body), len(raw), len(body)) + body)
    return path


def _document(path: Path) -> Path:
    """Bloque DETALLE con una línea de cada caso y un bloque VELO (dos HATCH, al 90 % y al 50 %
    de transparencia) insertado tres veces."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    document.layers.add("MESA", color=35)
    velo = document.blocks.new("VELO")
    high = velo.add_hatch(color=9)
    high.set_solid_fill(color=9, rgb=(199, 200, 202))
    high.paths.add_polyline_path([(0, 0), (1, 0), (1, 1), (0, 1)], is_closed=True)
    high.transparency = 0.9
    medium = velo.add_hatch(color=9)
    medium.set_solid_fill(color=9, rgb=(10, 120, 10))
    medium.paths.add_polyline_path([(2, 0), (3, 0), (3, 1), (2, 1)], is_closed=True)
    medium.transparency = 0.5
    detail = document.blocks.new("DETALLE")
    detail.add_line((0, 0), (10, 0), dxfattribs={"layer": "MESA"})  # PorCapa: ACI 35
    detail.add_line((0, 1), (10, 1), dxfattribs={"color": 7})
    detail.add_line((0, 2), (10, 2), dxfattribs={"color": 35, "true_color": 0x0A141E})
    detail.add_line((0, 3), (10, 3), dxfattribs={"color": 3})
    detail.add_line((0, 4), (10, 4), dxfattribs={"color": 5})
    detail.add_line((0, 5), (10, 5), dxfattribs={"color": 0})  # PorBloque: el del INSERT
    for index in range(3):
        detail.add_blockref("VELO", (index * 4, 6))
    document.modelspace().add_blockref("DETALLE", (0, 0), dxfattribs={"color": 35})
    document.saveas(path)
    return path


def _export(tmp_path: Path, name: str, *extra: str) -> Path:
    source = tmp_path / "plano.dxf"
    if not source.exists():
        _document(source)
    prefix = tmp_path / "salida" / name
    arguments = ["cad", "extract-block", str(source), "--block-name", "DETALLE"]
    arguments += ["--scale", "1:100", "--output-prefix", str(prefix), *extra]
    assert main(arguments) == 0
    return prefix


def _strokes(pdf: Path) -> dict[tuple[int, int, int], float]:
    with pymupdf.open(pdf) as document:
        return {
            tuple(round(value * 255) for value in item["color"]): round(item["width"], 3)
            for item in document[0].get_drawings()
            if item["type"] == "s"
        }


def _fills(pdf: Path) -> list[float]:
    with pymupdf.open(pdf) as document:
        return sorted(
            round(item.get("fill_opacity") or 1.0, 2)
            for item in document[0].get_drawings()
            if item["type"] == "f"
        )


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


# --------------------------------------------------------------------- lectura de la CTB


def test_a_ctb_with_a_cp1252_description_is_read(tmp_path: Path) -> None:
    path = _ctb(tmp_path / "erco.ctb")
    with pytest.raises(UnicodeDecodeError):
        acadctb.load(str(path))  # ezdxf 1.4.4 asume UTF-8

    table = load_plot_style(path)

    assert table.describe()["name"] == "erco.ctb"
    assert len(table.describe()["sha256"]) == 64
    assert table.pen(35).color == (184, 138, 92) and table.pen(35).lineweight_mm == 0.35
    assert table.pen(36).color is None and not table.pen(36).changes_anything


@pytest.mark.parametrize(
    ("name", "content"),
    [("tabla.stb", b"PIAFILEVERSION_2.0,STBVER1"), ("tabla.ctb", b"no es una ctb")],
)
def test_files_that_are_not_a_ctb_are_rejected(tmp_path: Path, name: str, content: bytes) -> None:
    path = tmp_path / name
    path.write_bytes(content)

    with pytest.raises(CadProcessingError):
        load_plot_style(path)
    with pytest.raises(CadProcessingError):
        load_plot_style(tmp_path / "no_existe.ctb")


# --------------------------------------------------------------------- color y grosor


def _properties(pen: int, color: str, lineweight: float = 0.25) -> Properties:
    properties = Properties()
    properties.pen, properties.color, properties.lineweight = pen, color, lineweight
    return properties


def test_the_ctb_applies_to_colors_that_come_from_an_aci(tmp_path: Path) -> None:
    table = load_plot_style(_ctb(tmp_path / "erco.ctb"))
    from_aci = _properties(35, "#7f5f3f19")  # ACI 35 con transparencia (alfa 0x19)
    true_color = _properties(35, "#0a141e")
    white = _properties(7, "#000000")  # ACI 7 se dibuja negro sobre papel blanco
    other = _properties(36, "#7f3f00")

    for properties in (from_aci, true_color, white, other):
        apply_plot_style(table, properties, "#000000")

    assert (from_aci.color, from_aci.lineweight) == ("#b88a5c19", 0.35)  # el alfa se conserva
    assert (true_color.color, true_color.lineweight) == ("#0a141e", 0.25)
    assert white.color == "#2e2e2e"
    assert other.color == "#7f3f00"


def test_grayscale_and_screening(tmp_path: Path) -> None:
    table = load_plot_style(_ctb(tmp_path / "erco.ctb"))

    assert plotted_rgb(table.pen(3), (0, 255, 0)) == (150, 150, 150)
    assert plotted_rgb(table.pen(5), (0, 0, 255)) == (128, 128, 255)


def test_without_plot_style_the_drawing_does_not_change(tmp_path: Path) -> None:
    plain = _export(tmp_path, "sin")
    same = _export(tmp_path, "transparencia", "--no-print-transparency")

    strokes = _strokes(plain.with_name("sin.pdf"))
    assert strokes[(127, 95, 63)] == pytest.approx(0.25 * PT_PER_MM, abs=0.002)  # ACI 35
    assert (0, 0, 0) in strokes and (0, 255, 0) in strokes and (0, 0, 255) in strokes
    assert _strokes(same.with_name("transparencia.pdf")) == strokes
    manifest = json.loads(plain.with_name("sin.manifest.json").read_text(encoding="utf-8"))
    assert manifest["options"]["plot_style"] is None
    assert manifest["options"]["print_transparency"] is True


def test_plot_style_colors_and_lineweights_in_the_pdf(tmp_path: Path) -> None:
    ctb = _ctb(tmp_path / "erco.ctb")
    prefix = _export(tmp_path, "con_ctb", "--plot-style", str(ctb))

    pdf = prefix.with_name("con_ctb.pdf")
    strokes = _strokes(pdf)
    assert strokes[(184, 138, 92)] == pytest.approx(0.35 * PT_PER_MM, abs=0.002)  # PorCapa
    assert (127, 95, 63) not in strokes  # tampoco la PorBloque: el INSERT es ACI 35
    assert (46, 46, 46) in strokes and (0, 0, 0) not in strokes
    assert (10, 20, 30) in strokes  # color verdadero: sin cambios
    assert (150, 150, 150) in strokes and (128, 128, 255) in strokes
    assert verify_package(pdf) == ()
    manifest = json.loads(prefix.with_name("con_ctb.manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "1.4"
    assert manifest["options"]["plot_style"] == load_plot_style(ctb).describe()
    report = prefix.with_name("con_ctb.txt").read_text(encoding="utf-8")
    assert "PLOT_STYLE_APPLIED" in report and "erco.ctb" in report


@pytest.mark.parametrize("instancing", ["1", "0"])
def test_no_print_transparency_skips_only_high_transparency_hatches(
    tmp_path: Path, monkeypatch, instancing: str
) -> None:
    """Los tres HATCH al 90 % no se dibujan (también cuando el bloque se reutiliza); los del
    50 % sí. Con los intermedios, el SVG y el PDF tienen que coincidir."""
    monkeypatch.setenv("CDE_INSTANCING", instancing)
    monkeypatch.setenv("CDE_KEEP_INTERMEDIATES", "1")
    before = _export(tmp_path, "con_velo")
    after = _export(tmp_path, "sin_velo", "--no-print-transparency")

    assert _fills(before.with_name("con_velo.pdf")) == [0.1, 0.1, 0.1, 0.5, 0.5, 0.5]
    assert _fills(after.with_name("sin_velo.pdf")) == [0.5, 0.5, 0.5]
    assert verify_package(after.with_name("sin_velo.pdf")) == ()
    manifest = json.loads(after.with_name("sin_velo.manifest.json").read_text(encoding="utf-8"))
    assert manifest["options"]["print_transparency"] is False
    report = after.with_name("sin_velo.txt").read_text(encoding="utf-8")
    assert "HATCH_TRANSPARENT_OMITTED" in report
    assert "No se dibujaron 3 HATCH con transparencia alta" in report


def test_a_missing_ctb_stops_before_writing_anything(tmp_path: Path) -> None:
    source = _document(tmp_path / "plano.dxf")
    out = tmp_path / "salida"

    code = main(
        [
            "cad",
            "extract-block",
            str(source),
            "--block-name",
            "DETALLE",
            "--scale",
            "1:100",
            "--output-prefix",
            str(out / "x"),
            "--plot-style",
            str(tmp_path / "no.ctb"),
        ]
    )

    assert code == 1
    assert not out.exists() or not any(out.iterdir())


# --------------------------------------------------------------------- trabajador


def _final(sink, request_id: str) -> dict:
    with sink.condition:
        assert sink.condition.wait_for(
            lambda: any(
                m["id"] == request_id and m["type"] in ("result", "error") for m in sink.messages
            ),
            TIMEOUT,
        )
    return next(
        m for m in sink.messages if m["id"] == request_id and m["type"] in ("result", "error")
    )


def test_worker_export_and_preview_accept_the_options(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    source = _document(tmp_path / "plano.dxf")
    ctb = _ctb(tmp_path / "erco.ctb")
    stdin.send(id="c-1", command="load", data={"dxf": str(source)})
    session = _final(sink, "c-1")["data"]["session"]
    options = {"plot_style": str(ctb), "print_transparency": False}

    stdin.send(id="c-2", command="preview", data={"session": session, "ref": "DETALLE#1"})
    plain = _final(sink, "c-2")["data"]
    stdin.send(
        id="c-3", command="preview", data={"session": session, "ref": "DETALLE#1", **options}
    )
    styled = _final(sink, "c-3")
    assert styled["type"] == "result", styled["error"]["message"]
    styled = styled["data"]
    stdin.send(
        id="c-4",
        command="export",
        data={
            "session": session,
            "output_dir": str(tmp_path / "paquetes"),
            "jobs": [{"ref": "DETALLE#1", "scale": "1:100"}],
            "options": options,
        },
    )
    job = _final(sink, "c-4")["data"]["jobs"][0]

    assert plain["plot_style"] is None and plain["print_transparency"] is True
    assert styled["plot_style"] == load_plot_style(ctb).describe()
    assert styled["print_transparency"] is False and styled["png"] != plain["png"]
    assert job["status"] == "published", job
    manifest = json.loads(Path(job["package"]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["options"]["plot_style"]["name"] == "erco.ctb"
    assert manifest["options"]["print_transparency"] is False


@pytest.mark.parametrize(
    "options",
    [{"plot_style": "no_existe.ctb"}, {"plot_style": 3}, {"print_transparency": "no"}],
)
def test_worker_rejects_wrong_options(running, tmp_path: Path, options: dict) -> None:  # noqa: F811
    sink, stdin = running
    source = _document(tmp_path / "plano.dxf")
    stdin.send(id="c-1", command="load", data={"dxf": str(source)})
    session = _final(sink, "c-1")["data"]["session"]

    stdin.send(
        id="c-2", command="preview", data={"session": session, "ref": "DETALLE#1", **options}
    )
    _final(sink, "c-2")
    stdin.send(
        id="c-3",
        command="export",
        data={
            "session": session,
            "output_dir": str(tmp_path / "paquetes"),
            "jobs": [{"ref": "DETALLE#1", "scale": "1:100"}],
            "options": options,
        },
    )

    for request_id in ("c-2", "c-3"):
        answer = _final(sink, request_id)
        assert answer["type"] == "error" and answer["error"]["code"] == "BAD_REQUEST", answer
    assert not (tmp_path / "paquetes").exists()
