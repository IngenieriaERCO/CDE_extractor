"""Orden «cad extract-modelspace»: exportar el Model Space (todo, por capas o por zona)."""

import hashlib
import json
from pathlib import Path

import ezdxf
import pytest

from cde_exporter.cad.manifest import validate_manifest, verify_package
from cde_exporter.cad.modelspace_commands import WORKING_BLOCK, working_document
from cde_exporter.cli import main


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def _source(path: Path) -> Path:
    """Model Space georreferenciado (en metros, cerca de X = 4 920 000):
    - 3 curvas de nivel (polilíneas 3D) en la capa congelada CURVAS, en y = 0, 10 y 20;
    - 1 polilínea de longitud nula en CURVAS;
    - 2 líneas en VIAS (una cruza x = 50) y 1 en EJES;
    - 2 referencias del bloque DET (10 × 10)."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    document.layers.add("CURVAS").freeze()
    document.layers.add("VIAS")
    document.layers.add("EJES")
    x0 = 4_920_000.0
    modelspace = document.modelspace()
    for index, y in enumerate((0.0, 10.0, 20.0)):
        modelspace.add_polyline3d(
            [(x0, y, 30 + index), (x0 + 100, y, 30 + index)], dxfattribs={"layer": "CURVAS"}
        )
    modelspace.add_polyline3d([(x0 + 5, 5, 30), (x0 + 5, 5, 30)], dxfattribs={"layer": "CURVAS"})
    modelspace.add_line((x0, -5), (x0 + 40, -5), dxfattribs={"layer": "VIAS"})
    modelspace.add_line((x0 + 30, 30), (x0 + 80, 30), dxfattribs={"layer": "VIAS"})
    modelspace.add_line((x0, 40), (x0 + 10, 40), dxfattribs={"layer": "EJES"})
    block = document.blocks.new("DET")
    block.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)
    modelspace.add_blockref("DET", (x0 + 20, 5))
    modelspace.add_blockref("DET", (x0 + 200, 5))
    document.saveas(path)
    return path


def _run(source: Path, prefix: Path, *extra: str) -> int:
    return main(
        [
            "cad",
            "extract-modelspace",
            str(source),
            "--output-prefix",
            str(prefix),
            "--scale",
            "1:500",
            *extra,
        ]
    )


def _manifest(prefix: Path) -> dict:
    return json.loads(prefix.with_name(f"{prefix.name}.manifest.json").read_text("utf-8"))


def _codes(manifest: dict) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for notices in manifest["notices"]["by_category"].values():
        for notice in notices:
            found.setdefault(notice["code"], []).append(notice["message"])
    return found


def test_exports_the_whole_modelspace_as_a_package(tmp_path: Path) -> None:
    source = _source(tmp_path / "obra.dxf")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    prefix = tmp_path / "salida" / "todo"

    assert _run(source, prefix) == 0

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert sorted(item.name for item in prefix.parent.iterdir()) == [
        "todo.manifest.json",
        "todo.pdf",
        "todo.txt",
    ]
    assert verify_package(prefix.with_suffix(".pdf")) == ()
    manifest = _manifest(prefix)
    assert validate_manifest(manifest) == ()
    assert manifest["source"]["dxf_name"] == "obra.dxf"
    assert manifest["source"]["dxf_sha256"] == before
    assert manifest["source"]["block_name"] == WORKING_BLOCK
    assert manifest["selection"] == {
        "kind": "modelspace",
        "layers": [],
        "exclude_layers": [],
        "window": None,
        "window_from_block": None,
        "entities": 8,  # 3 curvas + 3 líneas + 2 INSERT (la de longitud nula no)
        "not_copied": 0,
        "zero_length_skipped": 1,
        "without_bounds_skipped": 0,
        "clip": None,  # sin zona no hay recorte
    }
    # Coordenadas del Model Space de origen: las curvas congeladas no se dibujan, así que la
    # caja va de la vía de abajo (y = -5) a la segunda referencia de DET (x0 + 210).
    cad = manifest["pdf"]["drawing"]["cad_bounds"]
    assert cad["min"] == pytest.approx([4_920_000.0, -5.0])
    assert cad["max"] == pytest.approx([4_920_210.0, 40.0])
    codes = _codes(manifest)
    assert codes["MODELSPACE_ZERO_LENGTH"] == [
        "1 entidad de longitud nula (líneas o polilíneas que en el PDF serían un punto suelto) "
        "no se dibuja."
    ]
    assert codes["HIDDEN_LAYERS_CONTENT"] == [
        "Capa congelada «CURVAS»: 3 entidades de la selección del Model Space no se dibujan."
    ]
    report = prefix.with_suffix(".txt").read_text("utf-8")
    assert "Model Space (cad extract-modelspace)" in report
    assert "Capas pedidas: todas" in report
    assert "De longitud nula (no se dibujan): 1" in report


def test_hidden_layers_requested_alone_need_the_option(tmp_path: Path, capsys) -> None:
    source = _source(tmp_path / "obra.dxf")
    out = tmp_path / "salida"

    assert _run(source, out / "curvas", "--layers", "curvas") == 1

    assert not out.exists() or not any(out.iterdir())
    error = capsys.readouterr().err
    assert "«CURVAS» (congelada)" in error and "--include-hidden-layers" in error

    assert _run(source, out / "curvas", "--layers", "curvas", "--include-hidden-layers") == 0

    manifest = _manifest(out / "curvas")
    assert manifest["selection"]["layers"] == ["curvas"]
    assert manifest["selection"]["entities"] == 3
    assert manifest["options"]["include_hidden_layers"] == ["*"]
    assert _codes(manifest)["HIDDEN_LAYERS_INCLUDED"] == [
        "Capa congelada en el CAD «CURVAS»: se dibujan 3 entidades de la selección del Model "
        "Space por --include-hidden-layers (el DXF de origen no cambia)."
    ]
    cad = manifest["pdf"]["drawing"]["cad_bounds"]
    assert cad["min"] == pytest.approx([4_920_000.0, 0.0])
    assert cad["max"] == pytest.approx([4_920_100.0, 20.0])


def test_layer_patterns_and_exclusions(tmp_path: Path) -> None:
    source = _source(tmp_path / "obra.dxf")
    prefix = tmp_path / "salida" / "vias"

    assert _run(source, prefix, "--layers", "v*,EJ?S", "--exclude-layers", "ejes") == 0

    selection = _manifest(prefix)["selection"]
    assert selection["entities"] == 2
    assert selection["layers"] == ["v*", "EJ?S"]
    assert selection["exclude_layers"] == ["ejes"]


def test_unknown_layer_pattern_stops(tmp_path: Path, capsys) -> None:
    source = _source(tmp_path / "obra.dxf")

    assert _run(source, tmp_path / "salida" / "x", "--layers", "NADA*") == 1

    assert "Ninguna capa del DXF coincide con --layers" in capsys.readouterr().err


def test_window_with_no_clip_keeps_whole_entities_that_touch_it(tmp_path: Path) -> None:
    source = _source(tmp_path / "obra.dxf")
    prefix = tmp_path / "salida" / "zona"
    x0 = 4_920_000.0
    window = (str(x0 + 50), "0", str(x0 + 60), "35")

    assert _run(source, prefix, "--layers", "VIAS", "--window", *window, "--no-clip") == 0

    manifest = _manifest(prefix)
    assert manifest["selection"]["entities"] == 1
    assert manifest["selection"]["window"] == pytest.approx([x0 + 50, 0, x0 + 60, 35])
    # La línea de x0 + 30 a x0 + 80 sale entera: sin recorte.
    cad = manifest["pdf"]["drawing"]["cad_bounds"]
    assert cad["min"][0] == pytest.approx(x0 + 30)
    assert cad["max"][0] == pytest.approx(x0 + 80)


def test_window_clips_lines_and_the_page_is_the_window(tmp_path: Path) -> None:
    source = _source(tmp_path / "obra.dxf")
    prefix = tmp_path / "salida" / "zona"
    x0 = 4_920_000.0
    window = (str(x0 + 50), "0", str(x0 + 60), "35")

    assert _run(source, prefix, "--layers", "VIAS", "--window", *window) == 0

    manifest = _manifest(prefix)
    assert manifest["selection"]["clip"] == {
        "clipped_entities": 1,
        "pieces": 1,
        "outside_removed": 0,
        "not_clipped": [],
    }
    # Lo dibujado es el trozo de la línea dentro de la zona…
    cad = manifest["pdf"]["drawing"]["cad_bounds"]
    assert cad["min"] == pytest.approx([x0 + 50, 30])
    assert cad["max"] == pytest.approx([x0 + 60, 30])
    # …y la página mide la zona (10 × 35 m a 1:500 = 20 × 70 mm, más 5 mm de margen).
    assert manifest["pdf"]["page_size_mm"] == pytest.approx([30.0, 80.0], abs=1e-6)
    assert "MODELSPACE_CLIPPED" in _codes(manifest)
    checks = {check["name"]: check["passed"] for check in manifest["validation"]["checks"]}
    assert all(checks.values()), checks


def test_window_from_block_needs_the_occurrence_when_there_are_several(
    tmp_path: Path, capsys
) -> None:
    source = _source(tmp_path / "obra.dxf")
    prefix = tmp_path / "salida" / "det"

    assert _run(source, prefix, "--window-from-block", "DET") == 1
    assert "--occurrence N" in capsys.readouterr().err

    assert (
        _run(
            source,
            prefix,
            "--window-from-block",
            "det",
            "--occurrence",
            "1",
            "--include-hidden-layers",
        )
        == 0
    )

    selection = _manifest(prefix)["selection"]
    assert selection["window_from_block"] == "DET"
    assert selection["window"] == pytest.approx([4_920_020.0, 5.0, 4_920_030.0, 15.0])
    # La curva de y = 10 y la referencia de DET; la de longitud nula está fuera de la zona.
    assert selection["entities"] == 2
    assert selection["zero_length_skipped"] == 0


def test_list_shows_the_size_and_creates_nothing(tmp_path: Path, capsys) -> None:
    source = _source(tmp_path / "obra.dxf")
    out = tmp_path / "salida"

    assert _run(source, out / "todo", "--list") == 0

    assert not out.exists()
    text = capsys.readouterr().out
    assert "Entidades seleccionadas: 8" in text
    assert "capa «CURVAS»: 3 (congelada: no se dibuja)" in text
    assert "De longitud nula (no se dibujarían): 1" in text
    assert "cabe en una plancha A1" in text

    assert _run(source, out / "todo", "--list", "--include-hidden-layers") == 0
    text = capsys.readouterr().out
    assert "capa «CURVAS»: 3 (congelada en el CAD: se dibuja por --include-hidden-layers)" in text


def test_empty_selection_stops(tmp_path: Path, capsys) -> None:
    source = _source(tmp_path / "obra.dxf")

    assert _run(source, tmp_path / "salida" / "x", "--window", "0", "0", "1", "1") == 1

    assert "La selección del Model Space está vacía" in capsys.readouterr().err


def test_working_block_name_does_not_clash_with_an_existing_block() -> None:
    document = ezdxf.new("R2018")
    document.blocks.new(WORKING_BLOCK).add_line((0, 0), (1, 1))
    line = document.modelspace().add_line((0, 0), (5, 0))
    insert = document.modelspace().add_blockref(WORKING_BLOCK, (10, 0))

    working, name, not_copied, clip = working_document(document, (line, insert))

    assert name == f"{WORKING_BLOCK}_2"
    assert not_copied == 0 and clip is None
    (reference,) = working.modelspace()
    assert reference.dxf.name == name
    assert [entity.dxftype() for entity in working.blocks[name]] == ["LINE", "INSERT"]


def test_window_uses_the_vertices_when_ezdxf_cannot_measure_a_polyline(monkeypatch) -> None:
    """En el DXF real, polilíneas de 2 o 3 mm con coordenadas de millones de metros: ezdxf no
    les calcula caja. La selección por zona usa entonces la de sus vértices."""
    from cde_exporter.cad import modelspace_commands

    document = ezdxf.new("R2018")
    polyline = document.modelspace().add_polyline3d([(5, 5, 0), (5.002, 5.001, 0)])
    monkeypatch.setattr(modelspace_commands, "entity_bounds", lambda entity: None)

    selection = modelspace_commands.select_modelspace(document, (), (), (0, 0, 10, 10))

    assert selection.entities == (polyline,)
    assert selection.without_bounds == 0
