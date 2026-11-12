"""DXF muy grandes: copia temporal (leída por partes) sin el contenido de los bloques sin usar."""

import hashlib
import json
from pathlib import Path

import ezdxf
import pymupdf
import pytest
from ezdxf.math import Vec2
from ezdxf.render.mleader import ConnectionSide

from cde_exporter.cad.large_dxf import (
    LARGE_DXF_ENV,
    SOURCE_PRUNED_CODE,
    is_large,
    prune_unused_blocks,
    unused_blocks,
)
from cde_exporter.cad.project_commands import SUMMARY_NAME
from cde_exporter.cli import main


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)
    monkeypatch.delenv(LARGE_DXF_ENV, raising=False)


def _source(path: Path) -> Path:
    """Bloques usados de varias formas y dos sin usar (uno solo está dentro del otro)."""
    document = ezdxf.new("R2018", setup=True)
    document.units = ezdxf.units.InsertUnits.Meters
    modelspace = document.modelspace()
    document.blocks.new("HIJO").add_line((0, 0), (1, 1))
    document.blocks.new("PADRE").add_blockref("HIJO", (0, 0))  # anidado
    document.blocks["PADRE"].add_lwpolyline([(0, 0), (8, 0), (8, 6), (0, 6)], close=True)
    modelspace.add_blockref("PADRE", (0, 0))
    document.blocks.new("FLECHA").add_line((0, 0), (1, 0))  # flecha de un DIMSTYLE
    document.dimstyles.new("CON_FLECHA").dxf.dimblk = "FLECHA"
    modelspace.add_linear_dim(base=(0, 8), p1=(0, 0), p2=(8, 0), dimstyle="CON_FLECHA").render()
    document.blocks.new("ETIQUETA").add_circle((0, 0), 1)  # contenido de un MULTILEADER
    builder = modelspace.add_multileader_block(style="Standard")
    builder.set_content(name="ETIQUETA")
    builder.add_leader_line(ConnectionSide.left, [Vec2(-3, -3)])
    builder.build(insert=Vec2(12, 3))
    unused = document.blocks.new("SIN_USO")
    for index in range(50):
        unused.add_circle((index, 0), 5)
    unused.add_blockref("SOLO_EN_SIN_USO", (0, 0))
    document.blocks.new("SOLO_EN_SIN_USO").add_line((0, 0), (2, 2))
    document.saveas(path)
    return path


def _lines(path: Path) -> list[bytes]:
    return path.read_bytes().splitlines(keepends=True)


def test_finds_only_the_blocks_nothing_uses(tmp_path: Path) -> None:
    unused = unused_blocks(_source(tmp_path / "obra.dxf"))

    names = {name for name, _entities, _bytes in unused.values()}
    assert {"SIN_USO", "SOLO_EN_SIN_USO"} <= names
    assert not names & {"HIJO", "PADRE", "FLECHA", "ETIQUETA"}
    assert not any(name.startswith(("*Model_Space", "*Paper_Space")) for name in names)
    assert unused["sin_uso"][1] == 51  # 50 círculos y la referencia anidada


def test_copy_only_empties_the_unused_blocks(tmp_path: Path) -> None:
    source = _source(tmp_path / "obra.dxf")
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    report = prune_unused_blocks(source, tmp_path / "copia.dxf")

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert report.source_bytes == source.stat().st_size
    assert report.copy_bytes == (tmp_path / "copia.dxf").stat().st_size < report.source_bytes
    assert report.emptied[0].name == "SIN_USO"  # el mayor primero
    copy = ezdxf.readfile(tmp_path / "copia.dxf")
    original = ezdxf.readfile(source)
    assert not copy.audit().has_errors
    emptied = {item.name for item in report.emptied}
    for block in original.blocks:
        expected = 0 if block.name in emptied else len(block)
        assert len(copy.blocks[block.name]) == expected, block.name
    assert len(copy.modelspace()) == len(original.modelspace())
    # Lo demás es byte a byte igual: la copia es el original sin unas líneas.
    copied = iter(_lines(tmp_path / "copia.dxf"))
    pending = next(copied, None)
    for line in _lines(source):
        if line == pending:
            pending = next(copied, None)
    assert pending is None


def test_threshold_comes_from_the_environment(tmp_path: Path, monkeypatch) -> None:
    source = _source(tmp_path / "obra.dxf")
    assert not is_large(source)  # 200 MB por defecto
    monkeypatch.setenv(LARGE_DXF_ENV, "0")
    assert is_large(source)
    binary = tmp_path / "binario.dxf"
    binary.write_bytes(b"AutoCAD Binary DXF\r\n\x1a\x00resto")
    assert not is_large(binary)  # un DXF binario no se lee por partes


def _extract(source: Path, prefix: Path) -> int:
    return main(
        [
            "cad",
            "extract-block",
            str(source),
            "--block-name",
            "PADRE",
            "--scale",
            "1:50",
            "--output-prefix",
            str(prefix),
        ]
    )


def _drawings(pdf: Path) -> list:
    with pymupdf.open(pdf) as document:
        return [
            (item["type"], tuple(round(value, 3) for value in item["rect"]))
            for item in document[0].get_drawings()
        ]


def test_extract_block_reads_a_large_dxf_from_the_copy(tmp_path: Path, monkeypatch, capsys) -> None:
    source = _source(tmp_path / "obra.dxf")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    assert _extract(source, tmp_path / "normal" / "padre") == 0

    monkeypatch.setenv(LARGE_DXF_ENV, "0")
    out = tmp_path / "grande"
    assert _extract(source, out / "padre") == 0

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert sorted(item.name for item in out.iterdir()) == [
        "padre.manifest.json",
        "padre.pdf",
        "padre.txt",
    ]  # la copia temporal no queda
    manifest = json.loads((out / "padre.manifest.json").read_text("utf-8"))
    assert manifest["source"]["dxf_sha256"] == before  # el hash es el del original
    (notice,) = [
        item
        for items in manifest["notices"]["by_category"].values()
        for item in items
        if item["code"] == SOURCE_PRUNED_CODE
    ]
    assert notice["severity"] == "info" and "«SIN_USO»" in notice["message"]
    assert "copia temporal" in (out / "padre.txt").read_text("utf-8")
    assert "se leyó desde una copia temporal" in capsys.readouterr().out
    # El dibujo es el mismo.
    assert _drawings(out / "padre.pdf") == _drawings(tmp_path / "normal" / "padre.pdf")


def test_project_and_modelspace_use_the_copy(tmp_path: Path, monkeypatch) -> None:
    source = _source(tmp_path / "obra.dxf")
    monkeypatch.setenv(LARGE_DXF_ENV, "0")

    assert (
        main(
            [
                "cad",
                "extract-all-blocks",
                str(source),
                "--output-dir",
                str(tmp_path / "proyecto"),
                "--scale",
                "1:50",
            ]
        )
        == 0
    )
    summary = json.loads((tmp_path / "proyecto" / SUMMARY_NAME).read_text("utf-8"))
    assert summary["source_copy"]["source_bytes"] == source.stat().st_size
    assert "SIN_USO" in {item["name"] for item in summary["source_copy"]["emptied_blocks"]}
    assert not [item for item in (tmp_path / "proyecto").iterdir() if item.is_dir()]

    prefix = tmp_path / "ms" / "todo"
    assert (
        main(
            [
                "cad",
                "extract-modelspace",
                str(source),
                "--output-prefix",
                str(prefix),
                "--scale",
                "1:50",
            ]
        )
        == 0
    )
    manifest = json.loads(prefix.with_name("todo.manifest.json").read_text("utf-8"))
    codes = {
        item["code"] for items in manifest["notices"]["by_category"].values() for item in items
    }
    assert SOURCE_PRUNED_CODE in codes

    # Con --list no queda ninguna carpeta creada.
    assert (
        main(
            [
                "cad",
                "extract-modelspace",
                str(source),
                "--output-prefix",
                str(tmp_path / "nada" / "x"),
                "--list",
            ]
        )
        == 0
    )
    assert not (tmp_path / "nada").exists()
