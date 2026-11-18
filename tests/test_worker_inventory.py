"""Inventario de ``load`` y ``--list --json``: cajas en el marco único, capas ocultas, 3DSOLID,
referencias omitidas, capas con colores efectivos y nombres de paquete."""

import hashlib
import json
import os
from pathlib import Path

import ezdxf
import pytest
from ezdxf import bbox
from ezdxf.acis import api as acis
from ezdxf.render import forms

from cde_exporter.cad.dxf_session import (
    TEMP_PREFIX,
    clean_temporaries,
    fingerprint,
    load_dxf,
    make_temporary,
)
from cde_exporter.cad.file_names import PACKAGE_NAME_MAX, is_package_name, package_name
from cde_exporter.cad.inventory import _anonymous_reason, build_inventory, page_size_mm
from cde_exporter.cad.large_dxf import LARGE_DXF_ENV
from cde_exporter.cad.project_commands import (
    REASON_ANONYMOUS,
    REASON_NO_DEFINITION,
    REASON_NOTHING_VISIBLE,
)
from cde_exporter.cli import main


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)
    monkeypatch.delenv(LARGE_DXF_ENV, raising=False)


def _document():
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    return document


def _rectangle_block(document, name: str = "B", layer: str = "0"):
    """Bloque con dos líneas que llenan la caja (0, 0)–(10, 4)."""
    block = document.blocks.new(name)
    block.add_line((0, 0), (10, 0), dxfattribs={"layer": layer})
    block.add_line((10, 0), (10, 4), dxfattribs={"layer": layer})
    return block


def _only(inventory: dict) -> dict:
    assert len(inventory["blocks"]) == 1
    return inventory["blocks"][0]


def _approx(values, expected) -> None:
    assert values == pytest.approx(expected, abs=1e-6)


# ----------------------------------------------------------------- nombres de paquete


def test_package_name_is_ascii_and_safe() -> None:
    assert package_name("Detalle 1.5 (Ñ)") == "Detalle_1.5_N"
    assert package_name("  ...  ") == "bloque"
    assert package_name("CON") == "CON_"
    assert is_package_name(package_name("Muro: tipo «A»/B"))


def test_long_package_names_are_cut_with_a_stable_hash() -> None:
    long_a = "Detalle " + "x" * 100 + " A"
    long_b = "Detalle " + "x" * 100 + " B"
    name_a, name_b = package_name(long_a), package_name(long_b)
    assert len(name_a) <= PACKAGE_NAME_MAX and len(name_b) <= PACKAGE_NAME_MAX
    assert name_a != name_b
    assert name_a == package_name(long_a)
    assert is_package_name(name_a) and not is_package_name("con espacio")


# ----------------------------------------------------------------- cajas


def test_bounds_of_a_moved_rotated_and_scaled_insert_are_exact() -> None:
    document = _document()
    _rectangle_block(document)
    insert = document.modelspace().add_blockref(
        "B", (100, 50), dxfattribs={"rotation": 90, "xscale": 2, "yscale": 2}
    )

    block = _only(build_inventory(document))

    # (x, y) del bloque → (100 − 2y, 50 + 2x): x de 92 a 100, y de 50 a 70.
    assert block["bounds"]["exact"] is True
    _approx(block["bounds"]["min"], [92, 50])
    _approx(block["bounds"]["max"], [100, 70])
    exact = bbox.extents([insert])
    _approx(block["bounds"]["min"], [exact.extmin.x, exact.extmin.y])
    _approx(block["bounds"]["max"], [exact.extmax.x, exact.extmax.y])
    _approx(block["size_cad"], [8, 20])
    _approx(block["size_m"], [8, 20])
    assert block["insert"]["point"] == [100, 50, 0]
    assert block["insert"]["rotation_deg"] == 90
    assert block["insert"]["scale"] == [2, 2, 1]


def test_bounds_of_an_oblique_insert_contain_the_exact_box() -> None:
    document = _document()
    _rectangle_block(document)
    insert = document.modelspace().add_blockref("B", (5, 5), dxfattribs={"rotation": 30})

    block = _only(build_inventory(document))

    exact = bbox.extents([insert])
    assert block["bounds"]["exact"] is False
    assert block["bounds"]["min"][0] <= exact.extmin.x + 1e-6
    assert block["bounds"]["min"][1] <= exact.extmin.y + 1e-6
    assert block["bounds"]["max"][0] >= exact.extmax.x - 1e-6
    assert block["bounds"]["max"][1] >= exact.extmax.y - 1e-6


def test_hidden_layers_stay_out_of_bounds_unless_asked() -> None:
    document = _document()
    document.layers.add("OCULTA").off()
    block = _rectangle_block(document)
    block.add_line((0, 0), (50, 30), dxfattribs={"layer": "OCULTA"})
    document.modelspace().add_blockref("B", (0, 0))

    reference = _only(build_inventory(document))

    _approx(reference["bounds"]["max"], [10, 4])
    _approx(reference["bounds_with_hidden"]["max"], [50, 30])
    assert reference["hidden_layer_entities"] == 1


def test_without_hidden_layers_there_is_no_box_with_hidden() -> None:
    document = _document()
    _rectangle_block(document)
    document.modelspace().add_blockref("B", (0, 0))

    reference = _only(build_inventory(document))

    assert reference["bounds_with_hidden"] is None
    assert reference["hidden_layer_entities"] == 0


def test_bounds_include_the_footprint_of_3d_solids() -> None:
    document = _document()
    module = document.blocks.new("S")
    module.add_line((0, 0), (1, 1))
    solid = module.add_3dsolid(dxfattribs={"layer": "SOLIDOS"})
    acis.export_dxf(solid, [acis.body_from_mesh(forms.cube().scale(4, 2, 1))])
    document.modelspace().add_blockref("S", (10, 0))

    reference = _only(build_inventory(document))

    # La huella de la caja es (−2, −1)–(2, 1) en el bloque.
    _approx(reference["bounds"]["min"], [8, -1])
    _approx(reference["bounds"]["max"], [12, 1])
    assert reference["contains"]["solids_3d"] == 1


def test_nested_inserts_and_attributes_count_in_the_box() -> None:
    document = _document()
    _rectangle_block(document, "HIJO")
    parent = document.blocks.new("PADRE")
    parent.add_blockref("HIJO", (20, 0), dxfattribs={"rotation": 180})
    insert = document.modelspace().add_blockref("PADRE", (0, 0))
    insert.add_attrib("ROTULO", "texto", (30, 30))

    reference = _only(build_inventory(document))

    assert reference["bounds"]["exact"] is True
    _approx(reference["bounds"]["min"], [10, -4])
    assert reference["bounds"]["max"][0] > 30 and reference["bounds"]["max"][1] > 30
    assert reference["contains"]["texts"] == 1


# ----------------------------------------------------------------- referencias


def test_references_have_handles_occurrences_and_unique_stems() -> None:
    document = _document()
    _rectangle_block(document, "Muro tipo Ñ")
    modelspace = document.modelspace()
    first = modelspace.add_blockref("Muro tipo Ñ", (0, 0))
    second = modelspace.add_blockref("Muro tipo Ñ", (40, 0))

    blocks = build_inventory(document)["blocks"]

    assert [block["ref"] for block in blocks] == ["Muro tipo Ñ#1", "Muro tipo Ñ#2"]
    assert [block["handle"] for block in blocks] == [first.dxf.handle, second.dxf.handle]
    assert all(block["handle"] for block in blocks)
    assert [block["file_stem"] for block in blocks] == ["Muro_tipo_N_001", "Muro_tipo_N_002"]
    assert all(block["total_occurrences"] == 2 for block in blocks)


def test_references_that_are_not_exported_are_listed_with_their_reason() -> None:
    document = _document()
    modelspace = document.modelspace()
    document.blocks.new("VACIO")
    modelspace.add_blockref("VACIO", (0, 0))
    anonymous = document.blocks.new_anonymous_block("U")
    anonymous.add_line((0, 0), (1, 1))
    modelspace.add_blockref(anonymous.name, (0, 0))
    dimension = document.blocks.new_anonymous_block("D")
    dimension.add_line((0, 0), (1, 1))
    modelspace.add_blockref(dimension.name, (0, 0))
    modelspace.add_blockref("NO_EXISTE", (0, 0))

    inventory = build_inventory(document)

    assert inventory["blocks"] == []
    reasons = {item["name"]: item["reason"] for item in inventory["omitted"]}
    assert reasons == {
        "VACIO": REASON_NOTHING_VISIBLE,
        anonymous.name: _anonymous_reason(100),  # un «*U» de 1 entidad: menos de 100
        dimension.name: REASON_ANONYMOUS,  # los «*D», «*X»… nunca se ofrecen
        "NO_EXISTE": REASON_NO_DEFINITION,
    }
    small = next(item for item in inventory["omitted"] if item["name"] == anonymous.name)
    assert small["anonymous"] is True and small["visible_entities"] == 1
    assert all(item["handle"] for item in inventory["omitted"])


# ----------------------------------------------------------------- capas y unidades


def test_layers_report_effective_colors_and_usage() -> None:
    document = _document()
    document.layers.add("ROJO", color=1)
    document.layers.add("NEGRO", color=7)
    block = document.blocks.new("B")
    block.add_line((0, 0), (1, 0), dxfattribs={"layer": "ROJO"})
    block.add_line((0, 0), (0, 1), dxfattribs={"layer": "NEGRO"})
    block.add_line((0, 0), (1, 1), dxfattribs={"layer": "NEGRO", "color": 3})
    document.modelspace().add_blockref("B", (0, 0))

    layers = {layer["name"]: layer for layer in build_inventory(document)["layers"]}

    assert layers["ROJO"]["color"] == {"aci": 1, "rgb": "#ff0000"}
    assert layers["ROJO"]["effective_colors"] == [{"rgb": "#ff0000", "entities": 1}]
    assert layers["NEGRO"]["color"] == {"aci": 7, "rgb": "#000000"}
    assert {item["rgb"] for item in layers["NEGRO"]["effective_colors"]} == {
        "#000000",
        "#00ff00",
    }
    assert layers["ROJO"]["usage"] == [{"ref": "B#1", "types": {"LINE": 1}}]
    assert layers["ROJO"]["state"] == {"on": True, "frozen": False, "plot": True}


def test_without_layers_the_table_is_empty() -> None:
    document = _document()
    _rectangle_block(document)
    document.modelspace().add_blockref("B", (0, 0))

    assert build_inventory(document, include_layers=False)["layers"] == []


def test_units_can_be_overridden_and_set_the_size_in_meters() -> None:
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Unitless
    _rectangle_block(document)
    document.modelspace().add_blockref("B", (0, 0))

    assert _only(build_inventory(document))["size_m"] is None
    inventory = build_inventory(document, units="mm", margin_mm=7.5)
    reference = _only(inventory)
    _approx(reference["size_m"], [0.01, 0.004])
    assert inventory["units"]["verified"] is True
    assert inventory["margin_mm"] == 7.5
    assert page_size_mm(reference, 1, 7.5) == [25.0, 19.0]


def test_inventory_rejects_unknown_units() -> None:
    with pytest.raises(ValueError):
        build_inventory(_document(), units="pulgadas romanas")


# ----------------------------------------------------------------- lectura del DXF


def _saved(path: Path) -> Path:
    document = _document()
    _rectangle_block(document)
    unused = document.blocks.new("SIN_USAR")
    for index in range(20):
        unused.add_line((0, index), (5, index))
    document.modelspace().add_blockref("B", (0, 0))
    document.saveas(path)
    return path


def test_load_dxf_hashes_and_fingerprints_the_file(tmp_path: Path) -> None:
    source = _saved(tmp_path / "plano.dxf")

    loaded = load_dxf(source, tmp_path / "trabajo")

    assert loaded.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert loaded.fingerprint == fingerprint(source)
    assert loaded.source_copy is None and loaded.notices == []
    assert not loaded.changed()
    data = loaded.dxf_json()
    assert data["name"] == "plano.dxf" and data["bytes"] == source.stat().st_size


def test_load_dxf_notices_size_or_date_changes(tmp_path: Path) -> None:
    source = _saved(tmp_path / "plano.dxf")
    loaded = load_dxf(source, tmp_path)
    before = fingerprint(source)

    with source.open("ab") as stream:
        stream.write(b"\n")

    assert loaded.changed()
    assert fingerprint(source) != before


def test_large_dxf_is_read_from_the_copy_with_the_same_hash(tmp_path: Path, monkeypatch) -> None:
    source = _saved(tmp_path / "plano.dxf")
    monkeypatch.setenv(LARGE_DXF_ENV, "0")
    work = tmp_path / "trabajo"

    loaded = load_dxf(source, work)

    assert loaded.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert [item["name"] for item in loaded.source_copy["emptied_blocks"]] == ["SIN_USAR"]
    assert [notice["code"] for notice in loaded.notices] == ["SOURCE_UNUSED_BLOCKS_SKIPPED"]
    assert list(work.glob(f"{TEMP_PREFIX}*")) == []  # la copia ya se borró


def test_clean_temporaries_only_removes_dead_temporary_folders(tmp_path: Path) -> None:
    alive = make_temporary(tmp_path)
    dead = make_temporary(tmp_path)
    (dead / "pid").write_text("999999999", encoding="ascii")
    without_pid = tmp_path / f"{TEMP_PREFIX}sin_pid"
    without_pid.mkdir()
    other = tmp_path / "datos_del_usuario"
    other.mkdir()

    assert clean_temporaries(tmp_path) == 2

    assert alive.is_dir() and other.is_dir()
    assert not dead.exists() and not without_pid.exists()
    assert (alive / "pid").read_text(encoding="ascii") == str(os.getpid())


# ----------------------------------------------------------------- --list --json


def _list_json(source: Path, out: Path, *extra: str) -> list[str]:
    return [
        "cad",
        "extract-all-blocks",
        str(source),
        "--output-dir",
        str(out),
        "--list",
        "--json",
        *extra,
    ]


def test_list_json_writes_only_json_and_creates_nothing(tmp_path: Path, capsys) -> None:
    source = _saved(tmp_path / "plano.dxf")
    out = tmp_path / "proyecto"

    assert main(_list_json(source, out, "--scale", "1:100")) == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "session" not in data
    assert data["dxf"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    block = data["blocks"][0]
    assert block["scale"] == "1:100"
    # 10 m × 4 m a 1:100 son 100 mm × 40 mm, más 5 mm de margen por lado.
    assert block["page_size_mm"] == [110.0, 50.0]
    assert data["layers"]
    assert not out.exists()


def test_list_json_without_scale_or_layers(tmp_path: Path, capsys) -> None:
    source = _saved(tmp_path / "plano.dxf")

    assert main(_list_json(source, tmp_path / "p", "--no-layers")) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["layers"] == []
    assert "scale" not in data["blocks"][0]


def test_list_json_reports_errors_as_json(tmp_path: Path, capsys) -> None:
    assert main(_list_json(tmp_path / "no_existe.dxf", tmp_path / "p")) == 1

    data = json.loads(capsys.readouterr().out)
    assert data["error"]["code"] == "DXF_NOT_FOUND"


def test_json_needs_list(tmp_path: Path, capsys) -> None:
    source = _saved(tmp_path / "plano.dxf")
    code = main(["cad", "extract-all-blocks", str(source), "--output-dir", str(tmp_path), "--json"])

    assert code == 1
    assert "--json solo se usa con --list" in capsys.readouterr().err
