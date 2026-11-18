"""Entidades de tipos que ezdxf no sabe leer (de complementos de AutoCAD, p. ej.
VehicleTrackingPath, PLGRIDSYSTEM o GEOMAPIMAGE): ezdxf las guarda como ``DXFTagStorage``, sin
capa, color ni visibilidad. Nunca tumban un ``load``, una vista previa ni una exportación: se
omiten y se avisa con el tipo y la cantidad."""

import json
from pathlib import Path

import ezdxf
import pytest
from ezdxf.entities import DXFTagStorage
from ezdxf.lldxf.extendedtags import ExtendedTags
from test_worker import TIMEOUT, running  # noqa: F401 - fixture del trabajador en el proceso

from cde_exporter.cad.inventory import build_inventory
from cde_exporter.cad.reader import (
    dxf_attr,
    is_unknown_entity,
    read_dxf,
    unknown_entity_counts,
)
from cde_exporter.cli import main

KIND = "VehicleTrackingPath"


def _odd(document, layout, kind: str = KIND):
    handle = document.entitydb.next_handle()
    tags = ExtendedTags.from_text(
        f"  0\n{kind}\n  5\n{handle}\n100\nAcDbEntity\n  8\nCAPA_RARA\n100\nAcDb{kind}\n 90\n1\n"
    )
    entity = DXFTagStorage.load(tags, document)
    document.entitydb.add(entity)
    layout.add_entity(entity)
    return entity


def _source(path: Path) -> Path:
    """«Via» (dos líneas, una entidad rara y un INSERT de «Carril», que tiene otra), una rara
    suelta en el Model Space y una línea suelta."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    lane = document.blocks.new("Carril")
    lane.add_line((0, 1), (10, 1))
    _odd(document, lane, "PLGRIDSYSTEM")
    road = document.blocks.new("Via")
    road.add_line((0, 0), (10, 0))
    road.add_line((10, 0), (10, 4))
    _odd(document, road)
    road.add_blockref("Carril", (0, 0))
    modelspace = document.modelspace()
    modelspace.add_blockref("Via", (0, 0))
    _odd(document, modelspace)
    modelspace.add_line((0, 0), (5, 5))
    document.saveas(path)
    return path


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def test_the_test_entity_is_read_as_an_unknown_type(tmp_path: Path) -> None:
    document = read_dxf(_source(tmp_path / "plano.dxf"))
    odd = next(e for e in document.blocks["Via"] if e.dxftype() == KIND)

    assert is_unknown_entity(odd)
    assert not odd.dxf.is_supported("invisible") and not odd.dxf.is_supported("layer")
    with pytest.raises(ezdxf.DXFAttributeError):
        odd.dxf.get("invisible", 0)  # la causa del error del DXF de 830 MB
    assert dxf_attr(odd, "invisible", 0) == 0
    assert dxf_attr(odd, "layer", "0") == "0"
    assert unknown_entity_counts(document, "Via") == {"PLGRIDSYSTEM": 1, KIND: 1}


def test_inventory_skips_and_counts_unknown_entities(tmp_path: Path) -> None:
    inventory = build_inventory(read_dxf(_source(tmp_path / "plano.dxf")))

    block = inventory["blocks"][0]
    assert block["ref"] == "Via#1"
    assert block["unknown_entities"] == [
        {"type": "PLGRIDSYSTEM", "count": 1},
        {"type": KIND, "count": 1},
    ]
    assert block["entities"] == 4  # dos líneas, el INSERT de «Carril» y su línea
    assert block["bounds"]["min"] == [0, 0] and block["bounds"]["max"] == [10, 4]
    assert inventory["unknown_entities"] == [
        {"type": "PLGRIDSYSTEM", "modelspace": 0, "in_blocks": 1},
        {"type": KIND, "modelspace": 1, "in_blocks": 1},
    ]


def test_list_json_loads_and_warns(tmp_path: Path, capsys) -> None:
    source = _source(tmp_path / "plano.dxf")
    argv = ["cad", "extract-all-blocks", str(source), "--output-dir", str(tmp_path / "p")]

    assert main([*argv, "--list", "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    notice = next(n for n in data["notices"] if n["code"] == "UNKNOWN_ENTITIES_OMITTED")
    assert notice["severity"] == "warning"
    assert "PLGRIDSYSTEM (1)" in notice["message"] and f"{KIND} (2)" in notice["message"]


def test_extract_block_omits_them_with_a_notice(tmp_path: Path, capsys) -> None:
    source = _source(tmp_path / "plano.dxf")
    prefix = tmp_path / "salida" / "via"

    code = main(
        [
            "cad",
            "extract-block",
            str(source),
            "--block-name",
            "Via",
            "--output-prefix",
            str(prefix),
            "--scale",
            "1:50",
        ]
    )

    assert code == 0
    manifest = json.loads(prefix.with_name("via.manifest.json").read_text(encoding="utf-8"))
    notices = [n for group in manifest["notices"]["by_category"].values() for n in group]
    notice = next(n for n in notices if n["code"] == "UNKNOWN_ENTITIES_OMITTED")
    assert notice["severity"] == "warning" and "Se omitieron 2 entidades" in notice["message"]
    assert notice["advice"]
    assert manifest["validation"]["status"] == "complete_with_warnings"
    # No se confunden con entidades sin caja: no hay aviso ENTITIES_WITHOUT_BOUNDS.
    assert "ENTITIES_WITHOUT_BOUNDS" not in {n["code"] for n in notices}
    report = prefix.with_suffix(".txt").read_text(encoding="utf-8")
    assert f"{KIND} (1)" in report
    assert "Se omitieron 2 entidades" in capsys.readouterr().err


def test_extract_all_blocks_and_modelspace_do_not_fail(tmp_path: Path) -> None:
    source = _source(tmp_path / "plano.dxf")

    assert (
        main(
            [
                "cad",
                "extract-all-blocks",
                str(source),
                "--output-dir",
                str(tmp_path / "p"),
                "--scale",
                "1:50",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "cad",
                "extract-modelspace",
                str(source),
                "--output-prefix",
                str(tmp_path / "ms" / "ms"),
                "--scale",
                "1:50",
                "--window",
                "-1",
                "-1",
                "20",
                "20",
            ]
        )
        == 0
    )
    assert (tmp_path / "ms" / "ms.manifest.json").is_file()


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


def test_worker_load_preview_and_export(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    source = _source(tmp_path / "plano.dxf")

    stdin.send(id="c-1", command="load", data={"dxf": str(source)})
    loaded = _final(sink, "c-1")
    assert loaded["type"] == "result", loaded
    session = loaded["data"]["session"]
    stdin.send(id="c-2", command="preview", data={"session": session, "ref": "Via#1"})
    preview = _final(sink, "c-2")
    stdin.send(
        id="c-3",
        command="export",
        data={
            "session": session,
            "output_dir": str(tmp_path / "paquetes"),
            "jobs": [{"ref": "Via#1", "scale": "1:50"}],
        },
    )
    export = _final(sink, "c-3")

    assert preview["type"] == "result", preview
    assert preview["data"]["omitted"][KIND] == 1
    assert preview["data"]["omitted"]["PLGRIDSYSTEM"] == 1
    assert export["type"] == "result", export
    job = export["data"]["jobs"][0]
    assert job["status"] == "published"
    assert "UNKNOWN_ENTITIES_OMITTED" in {n["code"] for n in job["notices"]}
