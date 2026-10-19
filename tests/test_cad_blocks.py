from pathlib import Path

import ezdxf
import pytest

from cde_exporter.cad import block_extractor
from cde_exporter.cad.block_catalog import list_referenced_blocks
from cde_exporter.cad.block_extractor import extract_block_reference, references_for_block
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cli import main


def create_blocks(path: Path) -> Path:
    document = ezdxf.new()
    document.units = ezdxf.units.InsertUnits.Millimeters
    nested = document.blocks.new("Anidado")
    nested.add_circle((0, 0), 2)
    nested.add_3dsolid()
    block = document.blocks.new("Técnico Ágil")
    block.add_line((0, 0), (4, 2))
    block.add_blockref("Anidado", (1, 1))
    modelspace = document.modelspace()
    modelspace.add_blockref(
        "Técnico Ágil",
        (10, 20),
        dxfattribs={"layer": "A", "rotation": 35, "xscale": 2, "yscale": 3},
    )
    modelspace.add_blockref("Técnico Ágil", (30, 40), dxfattribs={"layer": "B"})
    anon = document.blocks.new("*U42")
    anon.add_line((0, 0), (1, 1))
    modelspace.add_blockref("*U42", (0, 0))
    document.saveas(path)
    return path


def test_blocks_filter_anonymous_and_utf8_report(tmp_path: Path) -> None:
    source = create_blocks(tmp_path / "source.dxf")
    rows = list_referenced_blocks(source, filter_text="tecnico agil")
    assert len(rows) == 1
    assert rows[0].name == "Técnico Ágil"
    assert rows[0].references == 2
    assert rows[0].layers == ("A", "B")
    assert not rows[0].anonymous

    assert main(["cad", "blocks", str(source), "--include-anonymous"]) == 0


def test_extract_block_copies_nested_definition_and_insert_transform(
    monkeypatch, tmp_path: Path
) -> None:
    source = create_blocks(tmp_path / "source.dxf")
    original = source.read_bytes()
    output = tmp_path / "extract.dxf"
    real_bounds = block_extractor.entity_bounds
    monkeypatch.setattr(
        block_extractor,
        "entity_bounds",
        lambda entity: None if entity.dxftype() == "CIRCLE" else real_bounds(entity),
    )
    report = extract_block_reference(source, "Técnico Ágil", 1, output)
    extracted = ezdxf.readfile(output)
    insert = extracted.modelspace().query("INSERT").first
    assert insert.dxf.name == "Técnico Ágil"
    assert tuple(insert.dxf.insert) == pytest.approx((10, 20, 0))
    assert insert.dxf.rotation == pytest.approx(35)
    assert insert.dxf.xscale == pytest.approx(2)
    assert insert.dxf.yscale == pytest.approx(3)
    assert "Anidado" in extracted.blocks
    assert report.definition_entity_count == 2
    assert report.unbounded
    assert source.read_bytes() == original


def test_reference_selection_requires_occurrence_and_handles_duplicate_handles(
    tmp_path: Path,
) -> None:
    source = create_blocks(tmp_path / "source.dxf")
    document = ezdxf.readfile(source)
    refs = references_for_block(document, "Técnico Ágil")
    assert refs[0].order < refs[1].order
    refs_entities = [
        entity
        for entity in document.modelspace().query("INSERT")
        if entity.dxf.name == "Técnico Ágil"
    ]
    refs_entities[1].dxf.handle = refs_entities[0].dxf.handle
    duplicate_path = tmp_path / "duplicate.dxf"
    document.saveas(duplicate_path)

    assert (
        main(
            [
                "cad",
                "extract-block",
                str(source),
                "--block-name",
                "Técnico Ágil",
                "--output-prefix",
                str(tmp_path / "ambiguous"),
            ]
        )
        == 1
    )
    report = extract_block_reference(duplicate_path, "Técnico Ágil", 2, tmp_path / "second.dxf")
    assert report.reference.order > 1


def test_extract_block_unknown_name_suggests_similar_name(tmp_path: Path) -> None:
    source = create_blocks(tmp_path / "source.dxf")
    with pytest.raises(CadProcessingError, match="Nombres parecidos"):
        extract_block_reference(source, "Tecnico Agil", 1, tmp_path / "out.dxf")


def test_cli_extract_block_honors_format_and_writes_dxf_before_vectors(
    tmp_path: Path,
) -> None:
    source = create_blocks(tmp_path / "source.dxf")
    prefix = tmp_path / "result" / "detail"
    result = main(
        [
            "cad",
            "extract-block",
            str(source),
            "--block-name",
            "Técnico Ágil",
            "--occurrence",
            "1",
            "--format",
            "svg",
            "--output-prefix",
            str(prefix),
        ]
    )
    assert result == 0
    assert prefix.with_suffix(".dxf").is_file()
    assert prefix.with_suffix(".svg").is_file()
    assert not prefix.with_suffix(".pdf").exists()
    assert prefix.with_name("detail_fidelidad.txt").is_file()
