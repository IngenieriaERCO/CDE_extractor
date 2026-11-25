"""Trabajador, protocolo 1.1: zonas del Model Space en ``preview`` y ``export`` (subfase 3;
docs/protocolo_trabajador.md, secciones 4.1 y 5.4)."""

import json
from pathlib import Path

import ezdxf
import pytest
from PIL import Image
from test_worker import TIMEOUT, running  # noqa: F401 - fixture del trabajador en el proceso
from test_worker_export import _export, _files, _final, _load

from cde_exporter.cad import worker_protocol as protocol
from cde_exporter.cad.manifest import SCHEMA_VERSION, verify_package
from cde_exporter.cad.worker_zone import generated_names, parse_scale

ZONE = [0, 0, 10, 10]


def _source(path: Path) -> Path:
    """Dos detalles sueltos en el Model Space: en (0..10), un cuadrado en «DETALLE», un bloque
    con una línea en «DETALLE» y otra en «COTAS» y un MTEXT en «TITULOS» que sale de la zona
    por la derecha; en (1000..1010), un círculo en «EJE»."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    for name in ("DETALLE", "COTAS", "TITULOS", "EJE"):
        document.layers.add(name)
    block = document.blocks.new("DET")
    block.add_line((0, 0), (4, 0), dxfattribs={"layer": "DETALLE"})
    block.add_line((0, 1), (4, 1), dxfattribs={"layer": "COTAS"})
    modelspace = document.modelspace()
    modelspace.add_lwpolyline(
        [(1, 1), (9, 1), (9, 9), (1, 9)], close=True, dxfattribs={"layer": "DETALLE"}
    )
    modelspace.add_blockref("DET", (3, 3), dxfattribs={"layer": "DETALLE"})
    modelspace.add_mtext(
        "SECCIÓN VECINA ESC 1:20",
        dxfattribs={"layer": "TITULOS", "insert": (8, 0.6), "char_height": 0.4},
    )
    modelspace.add_circle((1005, 1005), 5, dxfattribs={"layer": "EJE"})
    document.saveas(path)
    return path


def _preview(sink, stdin, request_id: str, session: str, **data) -> dict:
    stdin.send(id=request_id, command="preview", data={"session": session, **data})
    return _final(sink, request_id)


def _px(data: dict, x: float, y: float) -> tuple[int, int]:
    a, _b, _c, d, e, f = data["cad_to_px"]
    return int(a * x + e), int(d * y + f)


def _ink(image: Image.Image, xy: tuple[int, int]) -> bool:
    """Hay trazo (alfa) en el píxel o a su lado."""
    x0, y0 = xy
    return any(image.getpixel((x0 + dx, y0 + dy))[3] > 0 for dx in (-1, 0, 1) for dy in (-1, 0, 1))


# ----------------------------------------------------------------- funciones


def test_the_scale_can_be_a_number_or_1_to_n() -> None:
    assert parse_scale(20, "x") == "1:20"
    assert parse_scale(" 1:50 ", "x") == "1:50"
    for bad in (None, 0, -5, "", "1:0", True, "veinte"):
        with pytest.raises(protocol.WorkerError):
            parse_scale(bad, "x")


def test_generated_names_skip_taken_and_existing_ones() -> None:
    existing = {"Zona_02"}
    names = generated_names(3, {"zona_01"}, lambda name: name in existing)
    assert names == ["Zona_03", "Zona_04", "Zona_05"]


# ----------------------------------------------------------------- preview


def _ready(sink) -> dict:
    """Espera el aviso de que la vista previa preparada está lista (protocolo 1.2)."""
    with sink.condition:
        assert sink.condition.wait_for(
            lambda: any(m.get("event") == "preview_ready" for m in sink.messages), TIMEOUT
        )
    return next(m for m in sink.messages if m.get("event") == "preview_ready")


def test_a_zone_preview_shows_only_the_layers_that_are_seen(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    session = _load(sink, stdin, _source(tmp_path / "obra.dxf"))["session"]
    _ready(sink)

    reply = _preview(sink, stdin, "c-p1", session, zone=ZONE, exclude_layers=["COTAS"], scale=100)

    assert reply["type"] == "result", reply
    data = reply["data"]
    assert data["kind"] == "zone" and data["ref"] is None and data["handle"] is None
    assert data["zone"] == ZONE and data["view"] == ZONE
    assert data["bounds"] == {"min": [0, 0], "max": [10, 10]}
    assert data["paper"]["content_mm"] == [100.0, 100.0]
    assert data["paper"]["page_mm"] == [110.0, 110.0]
    assert data["scale"] == "1:100"
    rows = {row["name"]: row for row in data["layer_rows"]}
    assert rows["COTAS"]["shown"] is False and rows["DETALLE"]["shown"] is True
    image = Image.open(data["png"]).convert("RGBA")
    assert list(image.size) == data["px"]
    assert _ink(image, _px(data, 5, 1))  # el cuadrado, en «DETALLE»
    assert _ink(image, _px(data, 5, 3))  # la línea del bloque en «DETALLE»
    assert not _ink(image, _px(data, 5, 4))  # la línea del bloque en «COTAS»: no

    again = _preview(
        sink, stdin, "c-p2", session, zone=ZONE, exclude_layers=["COTAS"], scale="1:50"
    )["data"]
    assert data["prepared"] is True and again["prepared"] is True
    assert again["cached"] is True and again["png"] == data["png"]
    assert again["paper"]["content_mm"] == [200.0, 200.0]  # el papel no va en la caché


def test_without_a_zone_the_preview_is_the_whole_modelspace_with_its_groups(
    running,  # noqa: F811
    tmp_path: Path,
) -> None:
    sink, stdin = running
    session = _load(sink, stdin, _source(tmp_path / "obra.dxf"))["session"]

    data = _preview(sink, stdin, "c-p", session, zone=None)["data"]

    assert data["zone"] is None and data["paper"] is None
    assert [group["entities"] for group in data["groups"]] == [3, 1]
    assert data["view"][0] == 1 and data["view"][2:] == [1010, 1010]


def test_a_zone_preview_with_a_bad_zone_or_layer_is_a_bad_request(
    running,  # noqa: F811
    tmp_path: Path,
) -> None:
    sink, stdin = running
    session = _load(sink, stdin, _source(tmp_path / "obra.dxf"))["session"]

    for number, data in enumerate(
        (
            {"zone": [0, 0, 10]},
            {"zone": [10, 0, 0, 10]},
            {"zone": ZONE, "layers": ["NO_EXISTE"]},
            {"zone": ZONE, "layers": ["A,B"]},
        )
    ):
        reply = _preview(sink, stdin, f"c-b{number}", session, **data)
        assert reply["type"] == "error", data
        assert reply["error"]["code"] == protocol.BAD_REQUEST


# ----------------------------------------------------------------- export


def test_zone_jobs_publish_packages_and_name_the_unnamed_ones(
    running,  # noqa: F811
    tmp_path: Path,
) -> None:
    sink, stdin = running
    session = _load(sink, stdin, _source(tmp_path / "obra.dxf"))["session"]
    output = tmp_path / "paquetes"
    output.mkdir()
    (output / "Zona_01.pdf").write_bytes(b"de otra exportacion")

    reply = _export(
        sink,
        stdin,
        "c-e",
        session,
        output,
        [
            {"zone": ZONE, "layers": ["DETALLE", "TITULOS"], "scale": 100},
            {"zone": [1000, 1000, 1010, 1010], "scale": "1:200", "file_stem": "Eje"},
            {"zone": ZONE, "scale": 50, "file_stem": ""},
        ],
        omit_unsupported=True,
    )

    assert reply["type"] == "result", reply
    jobs = reply["data"]["jobs"]
    assert [job["status"] for job in jobs] == ["published"] * 3
    assert [job["package_name"] for job in jobs] == ["Zona_02", "Eje", "Zona_03"]
    assert [job["name_generated"] for job in jobs] == [True, False, True]
    assert all(job["kind"] == "zone" and job["ref"] is None for job in jobs)
    assert jobs[0]["zone"] == ZONE
    assert (output / "Zona_01.pdf").read_bytes() == b"de otra exportacion"
    for name in ("Zona_02", "Eje", "Zona_03"):
        assert verify_package(output / f"{name}.pdf") == ()
    manifest = json.loads((output / "Zona_02.manifest.json").read_text("utf-8"))
    assert manifest["schema_version"] == SCHEMA_VERSION == "1.5"
    assert manifest["selection"]["window"] == ZONE
    assert manifest["selection"]["layers"] == ["DETALLE", "TITULOS"]
    assert manifest["scale"]["requested"] == "1:100"
    assert manifest["pdf"]["page_size_mm"] == pytest.approx([110, 110], abs=1e-4)
    # El MTEXT cruza el borde: máscara, y la caja del dibujo queda dentro de la página.
    drawing = manifest["pdf"]["drawing"]
    assert drawing["mask"]["zone_cad"] == ZONE
    assert drawing["page_bounds_mm"]["x1"] <= 110
    # El hash del DXF es el de «load», no el de la copia de trabajo.
    assert manifest["source"]["dxf_name"] == "obra.dxf"
    eje = json.loads((output / "Eje.manifest.json").read_text("utf-8"))
    assert eje["pdf"]["drawing"]["mask"] is None


def test_block_and_zone_jobs_go_together(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    loaded = _load(sink, stdin, _source(tmp_path / "obra.dxf"))
    output = tmp_path / "paquetes"

    reply = _export(
        sink,
        stdin,
        "c-e",
        loaded["session"],
        output,
        [
            {"ref": loaded["blocks"][0]["ref"], "scale": "1:50", "package": "Bloque"},
            {"zone": ZONE, "scale": 100},
        ],
        write_summary=True,
    )

    assert reply["type"] == "result", reply
    jobs = reply["data"]["jobs"]
    assert [job["kind"] for job in jobs] == ["block", "zone"]
    assert [job["package_name"] for job in jobs] == ["Bloque", "Zona_01"]
    assert jobs[0]["handle"] is not None and jobs[1]["handle"] is None
    summary = json.loads(Path(reply["data"]["summary"]).read_text("utf-8"))
    assert len(summary["packages"]) == 1  # las zonas no son referencias de bloque


def test_bad_zone_jobs_stop_before_writing_anything(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    session = _load(sink, stdin, _source(tmp_path / "obra.dxf"))["session"]
    output = tmp_path / "paquetes"

    cases = (
        ({"zone": [0, 0, 10, 10]}, protocol.BAD_REQUEST),  # sin escala
        ({"zone": [500, 500, 510, 510], "scale": 10}, protocol.BAD_REQUEST),  # vacía
        ({"zone": ZONE, "scale": 10, "layers": ["EJE"]}, protocol.BAD_REQUEST),  # nada de EJE
        ({"zone": ZONE, "scale": 10, "ref": "DET#1"}, protocol.BAD_REQUEST),
        ({"zone": ZONE, "scale": 10, "file_stem": "con espacio"}, protocol.OUTPUT_INVALID),
    )
    for number, (job, code) in enumerate(cases):
        reply = _export(
            sink, stdin, f"c-x{number}", session, output, [{"zone": ZONE, "scale": 10}, job]
        )
        assert reply["type"] == "error", job
        assert reply["error"]["code"] == code, (job, reply["error"])
        assert reply["error"]["details"].get("job") == "t2", reply["error"]
    assert _files(output) == []


def test_the_versions_are_protocol_1_3_and_manifest_1_5() -> None:
    """El mensaje ``ready`` las anuncia (lo comprueba test_worker con el proceso real)."""
    assert protocol.PROTOCOL_VERSION == "1.3"
    assert SCHEMA_VERSION == "1.5"
