"""Capas apagadas o congeladas: avisos (dentro del bloque y sueltas en el Model Space) y la
opción --include-hidden-layers."""

import hashlib
import json
from pathlib import Path

import ezdxf
import pymupdf
import pytest

from cde_exporter.cad.hidden_layers import (
    STATE_FROZEN,
    STATE_OFF,
    STATE_OFF_AND_FROZEN,
    STATE_VISIBLE,
    hidden_layer_counts,
    hidden_overlap,
    loose_summary,
)
from cde_exporter.cad.project_commands import SUMMARY_NAME
from cde_exporter.cli import main


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def _source(path: Path) -> Path:
    """Bloque DET en (0, 0) de 10 × 4 m con:
    - 2 líneas visibles (capa VISIBLE);
    - 2 líneas en la capa APAGADA y 1 círculo en CONGELADA;
    - el bloque SUB (1 línea en la capa «0») insertado en la capa congelada HEREDA.
    En el Model Space, sueltas: 2 polilíneas en la capa congelada CURVAS sobre el bloque, 1
    lejos (en 500, 500) y 1 línea visible."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    document.layers.add("VISIBLE")
    document.layers.add("APAGADA").off()
    document.layers.add("CONGELADA").freeze()
    document.layers.add("HEREDA").freeze()
    document.layers.add("CURVAS").freeze()
    sub = document.blocks.new("SUB")
    sub.add_line((1, 1), (2, 2))  # capa «0»: toma la del INSERT
    det = document.blocks.new("DET")
    det.add_line((0, 0), (10, 0), dxfattribs={"layer": "VISIBLE"})
    det.add_line((0, 0), (0, 4), dxfattribs={"layer": "VISIBLE"})
    det.add_line((0, 4), (10, 4), dxfattribs={"layer": "APAGADA"})
    det.add_line((10, 0), (10, 4), dxfattribs={"layer": "APAGADA"})
    det.add_circle((5, 2), 1, dxfattribs={"layer": "CONGELADA"})
    det.add_blockref("SUB", (0, 0), dxfattribs={"layer": "HEREDA"})
    modelspace = document.modelspace()
    modelspace.add_blockref("DET", (0, 0))
    for y in (1.0, 3.0):
        modelspace.add_polyline3d([(-5, y, 30), (15, y, 30)], dxfattribs={"layer": "CURVAS"})
    modelspace.add_polyline3d([(500, 500, 30), (510, 500, 30)], dxfattribs={"layer": "CURVAS"})
    modelspace.add_line((-20, -20), (-19, -19), dxfattribs={"layer": "VISIBLE"})
    document.saveas(path)
    return path


def _extract(source: Path, prefix: Path, *extra: str) -> int:
    return main(
        [
            "cad",
            "extract-block",
            str(source),
            "--block-name",
            "DET",
            "--scale",
            "1:100",
            "--output-prefix",
            str(prefix),
            *extra,
        ]
    )


def _manifest(prefix: Path) -> dict:
    return json.loads(prefix.with_name(f"{prefix.name}.manifest.json").read_text("utf-8"))


def _notices(manifest: dict, code: str) -> list[dict]:
    return [
        notice
        for notices in manifest["notices"]["by_category"].values()
        for notice in notices
        if notice["code"] == code
    ]


def _drawings(pdf: Path) -> int:
    with pymupdf.open(pdf) as document:
        return len(document[0].get_drawings())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_counts_hidden_entities_by_the_layer_that_hides_them(tmp_path: Path) -> None:
    document = ezdxf.readfile(_source(tmp_path / "obra.dxf"))
    document.layers.get("APAGADA").freeze()  # apagada y congelada a la vez
    counts = {item.layer: (item.state, item.count) for item in hidden_layer_counts(document)}
    assert counts == {
        "APAGADA": (STATE_OFF_AND_FROZEN, 2),
        "CONGELADA": (STATE_FROZEN, 1),
        "CURVAS": (STATE_FROZEN, 3),
        "HEREDA": (STATE_FROZEN, 1),  # la línea de SUB está en la capa «0»
    }


def test_hidden_overlap_only_counts_loose_entities_over_the_box(tmp_path: Path) -> None:
    document = ezdxf.readfile(_source(tmp_path / "obra.dxf"))
    overlap = hidden_overlap(document, (0.0, 0.0, 10.0, 4.0))
    assert [(item.layer, item.state, item.count) for item in overlap] == [
        ("CURVAS", STATE_FROZEN, 2)
    ]
    assert hidden_overlap(document, None) == ()
    assert loose_summary(document) == [
        {"layer": "CURVAS", "state": STATE_FROZEN, "entities": 3},
        {"layer": "VISIBLE", "state": STATE_VISIBLE, "entities": 1},
    ]


def test_extract_block_warns_about_hidden_layers(tmp_path: Path, capsys) -> None:
    source = _source(tmp_path / "obra.dxf")
    prefix = tmp_path / "salida" / "det"

    assert _extract(source, prefix) == 0

    manifest = _manifest(prefix)
    hidden = _notices(manifest, "HIDDEN_LAYERS_CONTENT")
    assert [notice["severity"] for notice in hidden] == ["info"] * 3
    messages = " ".join(notice["message"] for notice in hidden)
    assert f"Capa {STATE_OFF} «APAGADA»: 2 entidades del bloque «DET» no se dibujan." in messages
    assert f"Capa {STATE_FROZEN} «CONGELADA»: 1 entidad del bloque «DET» no se dibuja." in messages
    assert "«HEREDA»" in messages
    assert "--include-hidden-layers" in hidden[0]["advice"]
    overlap = _notices(manifest, "MODELSPACE_HIDDEN_OVERLAP")
    assert len(overlap) == 1 and overlap[0]["severity"] == "warning"
    assert "«CURVAS»: 2 entidades sueltas en el Model Space" in overlap[0]["message"]
    assert manifest["validation"]["status"] == "complete_with_warnings"
    assert manifest["options"]["include_hidden_layers"] is None
    report = prefix.with_suffix(".txt").read_text("utf-8")
    assert "«APAGADA»: 2 entidades del bloque «DET» no se dibujan" in report
    assert "«CURVAS»: 2 entidades sueltas" in report
    captured = capsys.readouterr()
    assert "Capa apagada «APAGADA»: 2 entidades" in captured.out
    assert "Aviso: Capa congelada «CURVAS»: 2 entidades sueltas" in captured.err


def test_include_all_hidden_layers_draws_them_without_touching_the_source(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path / "obra.dxf")
    before = _sha256(source)
    plain, revealed = tmp_path / "sin" / "det", tmp_path / "con" / "det"

    assert _extract(source, plain) == 0
    assert _extract(source, revealed, "--include-hidden-layers") == 0

    assert _sha256(source) == before
    manifest = _manifest(revealed)
    assert not _notices(manifest, "HIDDEN_LAYERS_CONTENT")
    included = _notices(manifest, "HIDDEN_LAYERS_INCLUDED")
    assert len(included) == 3
    assert any("«APAGADA»: se dibujan 2 entidades" in item["message"] for item in included)
    assert manifest["options"]["include_hidden_layers"] == ["*"]
    # Las 2 líneas, el círculo y la línea de SUB salen ahora en el PDF.
    assert _drawings(revealed.with_suffix(".pdf")) > _drawings(plain.with_suffix(".pdf"))
    # Lo suelto del Model Space no está en el bloque: sigue sin salir y se sigue avisando.
    assert len(_notices(manifest, "MODELSPACE_HIDDEN_OVERLAP")) == 1


def test_include_only_the_listed_hidden_layers(tmp_path: Path) -> None:
    source = _source(tmp_path / "obra.dxf")
    prefix = tmp_path / "salida" / "det"

    assert _extract(source, prefix, "--include-hidden-layers", "apagada") == 0

    manifest = _manifest(prefix)
    included = _notices(manifest, "HIDDEN_LAYERS_INCLUDED")
    assert len(included) == 1 and "«APAGADA»" in included[0]["message"]
    still_hidden = " ".join(n["message"] for n in _notices(manifest, "HIDDEN_LAYERS_CONTENT"))
    assert "«CONGELADA»" in still_hidden and "«HEREDA»" in still_hidden
    assert "«APAGADA»" not in still_hidden
    assert manifest["options"]["include_hidden_layers"] == ["apagada"]


def test_unknown_layer_stops_before_creating_files(tmp_path: Path, capsys) -> None:
    source = _source(tmp_path / "obra.dxf")
    out = tmp_path / "salida"

    assert _extract(source, out / "det", "--include-hidden-layers", "APAGADA,NO_EXISTE") == 1

    assert not out.exists() or not any(out.iterdir())
    assert "«NO_EXISTE»" in capsys.readouterr().err


def test_project_summary_lists_hidden_layers_and_loose_modelspace(tmp_path: Path, capsys) -> None:
    source = _source(tmp_path / "obra.dxf")
    out = tmp_path / "proyecto"

    code = main(
        ["cad", "extract-all-blocks", str(source), "--output-dir", str(out), "--scale", "1:100"]
    )

    assert code == 0
    summary = json.loads((out / SUMMARY_NAME).read_text("utf-8"))
    (package,) = summary["packages"]
    assert package["hidden_layers"] == [
        {"layer": "APAGADA", "state": STATE_OFF, "entities": 2},
        {"layer": "CONGELADA", "state": STATE_FROZEN, "entities": 1},
        {"layer": "HEREDA", "state": STATE_FROZEN, "entities": 1},
    ]
    assert package["included_hidden_layers"] == []
    assert package["modelspace_hidden_overlap"] == [
        {"layer": "CURVAS", "state": STATE_FROZEN, "entities": 2}
    ]
    assert summary["modelspace_loose"] == [
        {"layer": "CURVAS", "state": STATE_FROZEN, "entities": 3},
        {"layer": "VISIBLE", "state": STATE_VISIBLE, "entities": 1},
    ]
    assert summary["totals"]["hidden_layer_entities"] == 4
    assert summary["totals"]["modelspace_loose_entities"] == 4
    assert summary["totals"]["modelspace_loose_hidden_entities"] == 3
    text = capsys.readouterr().out
    assert "capa congelada «CURVAS»: 3" in text
    assert "capa apagada «APAGADA»: 2 entidades en «DET»" in text
    assert "AVISO: 2 sueltas ocultas en el Model Space encima" in text


def test_project_includes_hidden_layers_when_asked(tmp_path: Path) -> None:
    source = _source(tmp_path / "obra.dxf")
    out = tmp_path / "proyecto"

    code = main(
        [
            "cad",
            "extract-all-blocks",
            str(source),
            "--output-dir",
            str(out),
            "--scale",
            "1:100",
            "--include-hidden-layers",
            "CONGELADA",
        ]
    )

    assert code == 0
    (package,) = json.loads((out / SUMMARY_NAME).read_text("utf-8"))["packages"]
    assert package["included_hidden_layers"] == [
        {"layer": "CONGELADA", "state": STATE_FROZEN, "entities": 1}
    ]
    assert [row["layer"] for row in package["hidden_layers"]] == ["APAGADA", "HEREDA"]


def test_loose_entities_without_layer_are_skipped(tmp_path: Path) -> None:
    """Una entidad que ezdxf no interpreta (GEOMAPIMAGE, como en Perfil) no tiene capa."""
    source = _source(tmp_path / "obra.dxf")
    text = source.read_text("utf-8")
    end = text.index("  0\nENDSEC", text.index("ENTITIES"))
    unknown = "  0\nGEOMAPIMAGE\n  5\nABCD\n330\n1F\n100\nAcDbEntity\n  8\n0\n"
    source.write_text(text[:end] + unknown + text[end:], "utf-8")
    prefix = tmp_path / "salida" / "det"

    assert _extract(source, prefix) == 0

    assert len(_notices(_manifest(prefix), "MODELSPACE_HIDDEN_OVERLAP")) == 1
