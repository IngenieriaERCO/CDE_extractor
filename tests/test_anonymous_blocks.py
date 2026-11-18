"""Bloques anónimos «*U» del Model Space que se ofrecen para exportar: los que dibujan 100
entidades visibles o más (``CDE_ANON_MIN_ENTITIES``), con un nombre legible (capa + handle del
INSERT) y ``anonymous: true`` en ``load``."""

import json
from pathlib import Path

import ezdxf
import pytest
from test_worker import TIMEOUT, running  # noqa: F401 - fixture del trabajador en el proceso

from cde_exporter.cad.inventory import ANON_MIN_ENV, build_inventory
from cde_exporter.cad.manifest import verify_package


def _document(panels: int = 30):
    """Una «mesa» anónima (*U) de ``panels`` paneles de 4 entidades en la capa I1.M1.PV1, una
    etiqueta anónima de 2 entidades, una cota (*D) y un bloque con nombre."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    panel = document.blocks.new("PANEL_615")
    panel.add_lwpolyline([(0, 0), (1.3, 0), (1.3, 2.4), (0, 2.4)], close=True)
    panel.add_line((0, 0), (1.3, 2.4))
    panel.add_line((0, 2.4), (1.3, 0))
    panel.add_circle((0.65, 1.2), 0.1)
    table = document.blocks.new_anonymous_block("U")
    for index in range(panels):
        table.add_blockref("PANEL_615", (index * 1.35, 0))
    label = document.blocks.new_anonymous_block("U")
    label.add_line((0, 0), (1, 0))
    label.add_text("1250")
    dimension = document.blocks.new_anonymous_block("D")
    for index in range(150):
        dimension.add_line((0, index), (1, index))
    named = document.blocks.new("VIA")
    named.add_line((0, 0), (100, 0))
    modelspace = document.modelspace()
    mesa = modelspace.add_blockref(
        table.name, (5000, 2000), dxfattribs={"layer": "I1.M1.PV1", "rotation": 180}
    )
    modelspace.add_blockref(label.name, (10, 10), dxfattribs={"layer": "ETIQUETAS"})
    modelspace.add_blockref(dimension.name, (0, 0), dxfattribs={"layer": "COTAS"})
    modelspace.add_blockref("VIA", (0, 0))
    return document, mesa


def test_big_anonymous_blocks_are_offered_with_a_readable_name() -> None:
    document, mesa = _document()

    inventory = build_inventory(document)

    blocks = {block["ref"]: block for block in inventory["blocks"]}
    offered = blocks[f"{mesa.dxf.name}#1"]
    assert offered["anonymous"] is True
    assert offered["label"] == f"I1.M1.PV1_{mesa.dxf.handle}"
    assert offered["file_stem"] == f"I1.M1.PV1_{mesa.dxf.handle}"
    assert offered["visible_entities"] == 120  # 30 paneles × 4
    assert offered["bounds"]["min"][0] < 5000 <= offered["bounds"]["max"][0]  # girada 180°
    assert blocks["VIA#1"]["anonymous"] is False and blocks["VIA#1"]["label"] == "VIA"
    omitted = {item["name"]: item for item in inventory["omitted"]}
    small = next(item for name, item in omitted.items() if name.upper().startswith("*U"))
    assert small["anonymous"] is True and small["visible_entities"] == 2
    assert "menos de 100 entidades visibles" in small["reason"]
    dimension = next(item for name, item in omitted.items() if name.upper().startswith("*D"))
    assert "anonymous" not in dimension  # las cotas (*D) nunca se ofrecen


@pytest.mark.parametrize(("threshold", "offered"), [("121", 0), ("120", 1), ("2", 2), ("x", 1)])
def test_the_threshold_can_be_changed(monkeypatch, threshold: str, offered: int) -> None:
    monkeypatch.setenv(ANON_MIN_ENV, threshold)
    document, _mesa = _document()

    blocks = build_inventory(document, include_layers=False)["blocks"]

    assert sum(block["anonymous"] for block in blocks) == offered


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


def test_an_offered_anonymous_block_is_exported(running, tmp_path: Path, monkeypatch) -> None:  # noqa: F811
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)
    sink, stdin = running
    document, mesa = _document()
    source = tmp_path / "plano.dxf"
    document.saveas(source)
    stdin.send(id="c-1", command="load", data={"dxf": str(source)})
    loaded = _final(sink, "c-1")["data"]
    offered = next(block for block in loaded["blocks"] if block["anonymous"])

    stdin.send(
        id="c-2",
        command="export",
        data={
            "session": loaded["session"],
            "output_dir": str(tmp_path / "paquetes"),
            "jobs": [{"ref": offered["ref"], "scale": "1:100"}],
        },
    )
    answer = _final(sink, "c-2")

    job = answer["data"]["jobs"][0]
    assert job["status"] == "published", answer
    assert Path(job["package"]["pdf"]).name == f"I1.M1.PV1_{mesa.dxf.handle}.pdf"
    manifest = json.loads(Path(job["package"]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["source"]["block_name"] == mesa.dxf.name
    assert manifest["source"]["reference"]["handle"] == mesa.dxf.handle
    assert verify_package(job["package"]["pdf"]) == ()


def test_a_dotted_output_prefix_is_not_an_extension(tmp_path: Path, monkeypatch) -> None:
    """«I1.M1.PV1_12900» lleva puntos: se publican «I1.M1.PV1_12900.pdf»…, y una extensión de
    salida en el prefijo se sigue rechazando."""
    from cde_exporter.cli import main

    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)
    document, _mesa = _document()
    source = tmp_path / "plano.dxf"
    document.saveas(source)
    base = ["cad", "extract-block", str(source), "--block-name", "VIA", "--scale", "1:100"]

    assert main([*base, "--output-prefix", str(tmp_path / "out" / "detalle.v2")]) == 0
    assert main([*base, "--output-prefix", str(tmp_path / "otro" / "detalle.pdf")]) == 1

    names = sorted(item.name for item in (tmp_path / "out").iterdir())
    assert names == ["detalle.v2.manifest.json", "detalle.v2.pdf", "detalle.v2.txt"]
    assert verify_package(tmp_path / "out" / "detalle.v2.pdf") == ()


def test_load_threshold_option_wins_over_the_environment(
    running,  # noqa: F811
    tmp_path: Path,
    monkeypatch,
) -> None:
    """«anonymous_min_entities» de «load» manda sobre CDE_ANON_MIN_ENTITIES y se devuelve."""
    monkeypatch.setenv(ANON_MIN_ENV, "500")
    sink, stdin = running
    document, _mesa = _document()
    source = tmp_path / "plano.dxf"
    document.saveas(source)

    stdin.send(id="c-1", command="load", data={"dxf": str(source)})
    by_environment = _final(sink, "c-1")["data"]
    stdin.send(id="c-2", command="load", data={"dxf": str(source), "anonymous_min_entities": 2})
    by_option = _final(sink, "c-2")["data"]
    stdin.send(id="c-3", command="load", data={"dxf": str(source), "anonymous_min_entities": -1})
    wrong = _final(sink, "c-3")

    assert by_environment["anonymous_min_entities"] == 500
    assert sum(block["anonymous"] for block in by_environment["blocks"]) == 0
    assert by_option["anonymous_min_entities"] == 2
    assert sum(block["anonymous"] for block in by_option["blocks"]) == 2
    assert wrong["type"] == "error" and wrong["error"]["code"] == "BAD_REQUEST"


def test_an_offered_anonymous_block_has_a_preview_by_ref(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    document, mesa = _document()
    source = tmp_path / "plano.dxf"
    document.saveas(source)
    stdin.send(id="c-1", command="load", data={"dxf": str(source)})
    loaded = _final(sink, "c-1")["data"]

    stdin.send(
        id="c-2",
        command="preview",
        data={"session": loaded["session"], "ref": f"{mesa.dxf.name}#1"},
    )
    preview = _final(sink, "c-2")

    assert preview["type"] == "result", preview
    assert preview["data"]["handle"] == mesa.dxf.handle
    assert preview["data"]["bounds"] is not None and Path(preview["data"]["png"]).is_file()


def test_the_insert_handle_finds_a_renumbered_anonymous_block(
    running,  # noqa: F811
    tmp_path: Path,
    monkeypatch,
) -> None:
    """AutoCAD renumera los «*U» al guardar (*U76 → *U999) pero no el handle del INSERT: con
    solo «handle», preview y export encuentran el mismo bloque y el paquete se llama igual."""
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)
    sink, stdin = running
    document, mesa = _document()
    first = tmp_path / "antes.dxf"
    document.saveas(first)
    old_name, handle = mesa.dxf.name, mesa.dxf.handle
    document.blocks.rename_block(old_name, "*U999")
    mesa.dxf.name = "*U999"
    second = tmp_path / "despues.dxf"
    document.saveas(second)

    stdin.send(id="c-1", command="load", data={"dxf": str(first)})
    before = next(b for b in _final(sink, "c-1")["data"]["blocks"] if b["handle"] == handle)
    stdin.send(id="c-2", command="load", data={"dxf": str(second)})
    loaded = _final(sink, "c-2")["data"]
    after = next(b for b in loaded["blocks"] if b["handle"] == handle)
    stdin.send(id="c-3", command="preview", data={"session": loaded["session"], "handle": handle})
    preview = _final(sink, "c-3")
    stdin.send(
        id="c-4",
        command="export",
        data={
            "session": loaded["session"],
            "output_dir": str(tmp_path / "paquetes"),
            "jobs": [{"handle": handle.lower(), "scale": "1:100"}],
        },
    )
    job = _final(sink, "c-4")["data"]["jobs"][0]

    assert before["ref"] == f"{old_name}#1" and after["ref"] == "*U999#1"
    assert before["label"] == after["label"] == f"I1.M1.PV1_{handle}"
    assert before["file_stem"] == after["file_stem"]
    assert preview["type"] == "result" and preview["data"]["ref"] == "*U999#1"
    assert job["status"] == "published", job
    assert Path(job["package"]["pdf"]).name == f"I1.M1.PV1_{handle}.pdf"
    manifest = json.loads(Path(job["package"]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["source"]["block_name"] == "*U999"
    assert manifest["source"]["reference"]["handle"] == handle


def test_handle_alone_reports_omitted_and_unknown_references() -> None:
    from cde_exporter.cad.worker import find_reference
    from cde_exporter.cad.worker_protocol import WorkerError

    document, _mesa = _document()
    inventory = build_inventory(document, include_layers=False)
    small = next(item for item in inventory["omitted"] if item.get("anonymous"))

    with pytest.raises(WorkerError) as omitted:
        find_reference(inventory, {"handle": small["handle"]})
    with pytest.raises(WorkerError) as unknown:
        find_reference(inventory, {"handle": "FFFFFF"})

    assert omitted.value.code == "NOT_EXPORTABLE"
    assert unknown.value.code == "REFERENCE_NOT_FOUND"
