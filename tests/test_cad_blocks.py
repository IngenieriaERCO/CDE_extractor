from pathlib import Path

import ezdxf
import pymupdf
import pytest

from cde_exporter.cad import block_extractor
from cde_exporter.cad.block_catalog import list_referenced_blocks
from cde_exporter.cad.block_extractor import extract_block_reference, references_for_block
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.scale import output_factor, parse_nominal_scale, verify_drawing_units
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


def test_extract_one_occurrence_defaults_without_choice(tmp_path: Path) -> None:
    source = ezdxf.new()
    source.blocks.new("Detalle").add_line((0, 0), (1, 1))
    source.modelspace().add_blockref("Detalle", (0, 0))
    path = tmp_path / "one.dxf"
    source.saveas(path)
    assert main(["cad", "extract-block", str(path), "--block-name", "Detalle",
                 "--output-prefix", str(tmp_path / "one-output"), "--format", "svg"]) == 0


def test_three_occurrences_require_explicit_selection_and_list_transforms(
    tmp_path: Path, capsys
) -> None:
    document = ezdxf.new()
    document.blocks.new("Detalle").add_line((0, 0), (1, 1))
    for index in range(3):
        document.modelspace().add_blockref(
            "Detalle", (index, index), dxfattribs={"layer": f"L{index}",
                "rotation": index * 10, "xscale": index + 1, "yscale": index + 2,
                "zscale": index + 3}
        )
    path = tmp_path / "three.dxf"
    document.saveas(path)
    result = main(["cad", "extract-block", str(path), "--block-name", "Detalle",
                   "--output-prefix", str(tmp_path / "ambiguous-output")])
    assert result == 1
    error = capsys.readouterr().err
    assert "--occurrence N" in error
    assert all(f"ocurrencia {index}" in error for index in (1, 2, 3))
    assert "rotación 10" in error and "escala" in error
    assert not (tmp_path / "ambiguous-output.dxf").exists()


def test_nearby_insertion_points_are_distinguishable_and_accent_is_utf8(
    tmp_path: Path, capsys
) -> None:
    document = ezdxf.new()
    document.blocks.new("Detalle").add_line((0, 0), (1, 1))
    for point in (4921040.123, 4921040.456):
        document.modelspace().add_blockref("Detalle", (point, 0, 0))
    path = tmp_path / "nearby.dxf"
    document.saveas(path)
    assert main([
        "cad", "extract-block", str(path), "--block-name", "Detalle",
        "--output-prefix", str(tmp_path / "nearby-output"),
    ]) == 1
    output = capsys.readouterr().err
    assert "inserci\u00f3n" in output
    assert "4921040.123, 0.000, 0.000" in output
    assert "4921040.456, 0.000, 0.000" in output


def test_occurrence_two_and_out_of_range(tmp_path: Path, capsys) -> None:
    document = ezdxf.new()
    document.blocks.new("Detalle").add_line((0, 0), (1, 1))
    for index in range(3):
        document.modelspace().add_blockref("Detalle", (index, 0))
    path = tmp_path / "three.dxf"
    document.saveas(path)
    prefix = tmp_path / "selected"
    assert main(["cad", "extract-block", str(path), "--block-name", "Detalle",
                 "--occurrence", "2", "--output-prefix", str(prefix), "--format", "svg"]) == 0
    report = prefix.with_name("selected_fidelidad.txt").read_text(encoding="utf-8")
    assert "Ocurrencia usada: 2 de 3" in report
    assert "punto (1.000, 0.000, 0.000)" in report
    assert main(["cad", "extract-block", str(path), "--block-name", "Detalle",
                 "--occurrence", "4", "--output-prefix", str(tmp_path / "invalid")]) == 1
    assert "La ocurrencia 4 no existe" in capsys.readouterr().err


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


@pytest.mark.parametrize("value, expected", [("1:20", 20), ("1:50", 50), ("1:100", 100)])
def test_parse_nominal_scale(value: str, expected: int) -> None:
    assert parse_nominal_scale(value) == expected


@pytest.mark.parametrize("value", ["50", "1:0", "1:-2", "1:abc", "1:1:2", "1:nan"])
def test_parse_nominal_scale_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="Escala|denominador"):
        parse_nominal_scale(value)


def test_output_factor_for_units_and_insert_transform() -> None:
    assert output_factor(1000, 100) == pytest.approx(10)  # metros
    assert output_factor(1, 100) == pytest.approx(0.01)  # milímetros
    assert output_factor(1, 50, 0.001) == pytest.approx(0.00002)


@pytest.mark.parametrize(
    "code, name, factor", [(6, "m", 1000), (4, "mm", 1), (5, "cm", 10)]
)
def test_declared_units_are_converted_to_millimeters(code: int, name: str, factor: float) -> None:
    verification = verify_drawing_units(
        code, 0, max_block_dimension=10, insert_scale=(1, 1, 1), insert_position=(0, 0, 0)
    )
    assert verification.status == "declaradas en el DXF y coherentes"
    assert verification.unit_name == name
    assert verification.millimeters_per_unit == factor
    assert verification.verified


def test_unknown_units_are_not_verified() -> None:
    verification = verify_drawing_units(
        0, 0, max_block_dimension=10, insert_scale=(1, 1, 1), insert_position=(0, 0, 0)
    )
    assert verification.status == "desconocidas ($INSUNITS = 0 o sin valor)"
    assert not verification.verified


def test_user_units_override_unknown_declaration() -> None:
    verification = verify_drawing_units(
        0, 0, max_block_dimension=10, insert_scale=(1, 1, 1), insert_position=(0, 0, 0),
        override="m",
    )
    assert verification.status == "indicadas por el usuario (--units m)"
    assert verification.millimeters_per_unit == 1000
    assert verification.verified


def test_declared_meters_with_millimeter_sized_geometry_is_flagged() -> None:
    verification = verify_drawing_units(
        6, 0, max_block_dimension=2500, insert_scale=(1, 1, 1), insert_position=(0, 0, 0)
    )
    assert verification.status == "con indicios de inconsistencia"
    assert not verification.verified
    assert "coordenadas expresadas en milímetros" in " ".join(verification.warnings)


def test_small_insert_scale_with_large_coordinates_is_flagged() -> None:
    verification = verify_drawing_units(
        4, 0, max_block_dimension=20, insert_scale=(0.001, 0.001, 0.001),
        insert_position=(4_921_040, 0, 0),
    )
    assert verification.status == "con indicios de inconsistencia"
    assert "INSERT escala 0.001" in " ".join(verification.warnings)


def test_nominal_scale_is_logged_without_changing_pdf_scale(tmp_path: Path) -> None:
    document = ezdxf.new()
    document.units = ezdxf.units.InsertUnits.Millimeters
    document.blocks.new("Detalle").add_line((0, 0), (10, 5))
    document.modelspace().add_blockref(
        "Detalle", (0, 0), dxfattribs={"xscale": 0.001, "yscale": 0.001, "zscale": 0.001}
    )
    source = tmp_path / "scale.dxf"
    document.saveas(source)
    prefix = tmp_path / "scale-output"
    assert main([
        "cad", "extract-block", str(source), "--block-name", "Detalle", "--occurrence", "1",
        "--scale", "1:50", "--output-prefix", str(prefix), "--format", "svg",
    ]) == 0
    report = prefix.with_name("scale-output_fidelidad.txt").read_text(encoding="utf-8")
    assert "Escala nominal: 1:50 | 2e-05 mm/unidad CAD" in report
    assert "estado: declaradas en el DXF y coherentes" in report


def test_user_override_clears_unit_inconsistency() -> None:
    verification = verify_drawing_units(
        6, 0, max_block_dimension=2500, insert_scale=(1, 1, 1),
        insert_position=(0, 0, 0), override="mm",
    )
    assert verification.status == "indicadas por el usuario (--units mm)"
    assert verification.millimeters_per_unit == 1
    assert verification.verified


def test_block_unit_disagreement_is_flagged() -> None:
    verification = verify_drawing_units(
        6, 4, max_block_dimension=20, insert_scale=(1, 1, 1), insert_position=(0, 0, 0)
    )
    assert verification.status == "con indicios de inconsistencia"
    assert verification.warnings


def test_scale_format_is_validated_before_fonts_or_dxf(monkeypatch, tmp_path: Path, capsys) -> None:
    from cde_exporter import cli

    def fail_if_fonts_run(*_args, **_kwargs):
        raise AssertionError("Font setup must not run")

    monkeypatch.setattr(cli, "configure_fonts", fail_if_fonts_run)
    result = main([
        "cad", "extract-block", str(tmp_path / "missing.dxf"), "--block-name", "X",
        "--output-prefix", str(tmp_path / "out"), "--scale", "1:0",
    ])
    assert result == 1
    assert "denominador" in capsys.readouterr().err


def test_nominal_scale_conflicts_with_manual_factor(tmp_path: Path, capsys) -> None:
    document = ezdxf.new()
    document.blocks.new("Detalle").add_line((0, 0), (1, 1))
    document.modelspace().add_blockref("Detalle", (0, 0))
    source = tmp_path / "conflict.dxf"
    document.saveas(source)
    assert main([
        "cad", "extract-block", str(source), "--block-name", "Detalle", "--occurrence", "1",
        "--scale", "1:50", "--scale-factor", "0.5",
        "--output-prefix", str(tmp_path / "conflict"),
    ]) == 1
    assert "No combines --scale con --scale-factor" in capsys.readouterr().err


def test_cli_units_override_is_recorded_and_used_for_factor(tmp_path: Path) -> None:
    document = ezdxf.new()
    document.blocks.new("Detalle").add_line((0, 0), (10, 5))
    document.modelspace().add_blockref("Detalle", (0, 0))
    source = tmp_path / "unitless.dxf"
    document.saveas(source)
    prefix = tmp_path / "units-output"
    assert main([
        "cad", "extract-block", str(source), "--block-name", "Detalle", "--occurrence", "1",
        "--scale", "1:100", "--units", "m", "--output-prefix", str(prefix),
        "--format", "svg",
    ]) == 0
    report = prefix.with_name("units-output_fidelidad.txt").read_text(encoding="utf-8")
    assert "| 10 mm/unidad CAD | unidades m |" in report
    assert "estado: indicadas por el usuario (--units m)" in report


def _scaled_detail(path: Path, units_code: int, size: tuple[float, float]) -> Path:
    document = ezdxf.new()
    document.units = units_code
    document.blocks.new("Detalle").add_lwpolyline(
        [(0, 0), (size[0], 0), size, (0, size[1]), (0, 0)]
    )
    document.modelspace().add_blockref("Detalle", (0, 0))
    document.saveas(path)
    return path


def _pdf_size_mm(path: Path) -> tuple[float, float]:
    with pymupdf.open(path) as pdf:
        page = pdf[0]
        return page.rect.width * 25.4 / 72, page.rect.height * 25.4 / 72


@pytest.mark.parametrize(
    "units_code, nominal, expected",
    [(ezdxf.units.InsertUnits.Meters, "1:100", (20, 15)),
     (ezdxf.units.InsertUnits.Millimeters, "1:1", (1010, 510))],
)
def test_nominal_scale_sets_physical_pdf_size(
    tmp_path: Path, units_code: int, nominal: str, expected: tuple[float, float]
) -> None:
    size = (1, 0.5) if units_code == ezdxf.units.InsertUnits.Meters else (1000, 500)
    source = _scaled_detail(tmp_path / "scaled.dxf", units_code, size)
    prefix = tmp_path / "scaled-output"
    assert main([
        "cad", "extract-block", str(source), "--block-name", "Detalle", "--occurrence", "1",
        "--scale", nominal, "--output-prefix", str(prefix), "--format", "pdf",
    ]) == 0
    assert _pdf_size_mm(prefix.with_suffix(".pdf")) == pytest.approx(expected, abs=0.1)
    report = prefix.with_name("scaled-output_fidelidad.txt").read_text(encoding="utf-8")
    assert "Caja geométrica efectiva CAD" in report
    assert "página:" in report
    assert "Escala aproximada" not in report


def test_nominal_scale_rejects_small_page_without_vector_output(tmp_path: Path, capsys) -> None:
    source = _scaled_detail(tmp_path / "small.dxf", ezdxf.units.InsertUnits.Meters, (1, 0.5))
    prefix = tmp_path / "small-output"
    assert main([
        "cad", "extract-block", str(source), "--block-name", "Detalle", "--occurrence", "1",
        "--scale", "1:100", "--page-size-mm", "10", "10", "--output-prefix", str(prefix),
    ]) == 1
    assert "necesita una página de 20.000 × 15.000 mm" in capsys.readouterr().err
    assert not prefix.with_suffix(".pdf").exists()
    assert not prefix.with_suffix(".svg").exists()
    assert not prefix.with_suffix(".dxf").exists()
    assert not prefix.with_name("small-output_fidelidad.txt").exists()


def test_nominal_scale_keeps_geometry_size_on_larger_page(tmp_path: Path) -> None:
    source = _scaled_detail(tmp_path / "large.dxf", ezdxf.units.InsertUnits.Meters, (1, 0.5))
    prefix = tmp_path / "large-output"
    assert main([
        "cad", "extract-block", str(source), "--block-name", "Detalle", "--occurrence", "1",
        "--scale", "1:100", "--page-size-mm", "30", "25", "--output-prefix", str(prefix),
        "--format", "pdf",
    ]) == 0
    assert _pdf_size_mm(prefix.with_suffix(".pdf")) == pytest.approx((30, 25), abs=0.1)
    report = prefix.with_name("large-output_fidelidad.txt").read_text(encoding="utf-8")
    assert "contenido físico: 10.000 × 5.000 mm" in report


def test_unknown_units_nominal_scale_warns_in_report_and_terminal(
    tmp_path: Path, capsys
) -> None:
    source = _scaled_detail(tmp_path / "unknown.dxf", ezdxf.units.InsertUnits.Unitless, (1000, 500))
    prefix = tmp_path / "unknown-output"
    assert main([
        "cad", "extract-block", str(source), "--block-name", "Detalle", "--occurrence", "1",
        "--scale", "1:100", "--output-prefix", str(prefix), "--format", "pdf",
    ]) == 0
    assert "NO garantizada" in capsys.readouterr().out
    report = prefix.with_name("unknown-output_fidelidad.txt").read_text(encoding="utf-8")
    assert "ADVERTENCIA: escala física NO garantizada" in report


def test_without_nominal_scale_keeps_legacy_page_size(tmp_path: Path) -> None:
    source = _scaled_detail(
        tmp_path / "legacy.dxf", ezdxf.units.InsertUnits.Millimeters, (1000, 500)
    )
    prefix = tmp_path / "legacy-output"
    assert main([
        "cad", "extract-block", str(source), "--block-name", "Detalle", "--occurrence", "1",
        "--output-prefix", str(prefix), "--format", "pdf",
    ]) == 0
    assert _pdf_size_mm(prefix.with_suffix(".pdf")) == pytest.approx((1009.65, 509.76), abs=0.1)
    report = prefix.with_name("legacy-output_fidelidad.txt").read_text(encoding="utf-8")
    assert "Escala nominal" not in report



def _handle_catalog_fixture(path: Path, count: int) -> Path:
    document = ezdxf.new()
    document.blocks.new("Detalle").add_line((0, 0), (1, 1))
    for index in range(count):
        insert = document.modelspace().add_blockref("Detalle", (index, 0))
        insert.dxf.handle = f"{index + 1:X}"
    document.saveas(path)
    return path


def test_blocks_handles_option_lists_single_occurrence(tmp_path: Path, capsys) -> None:
    source = _handle_catalog_fixture(tmp_path / "single-handles.dxf", 1)
    assert main(["cad", "blocks", str(source), "--handles"]) == 0
    output = capsys.readouterr().out
    assert "handles por ocurrencia" in output
    assert "1: 1" in output


def test_blocks_handles_option_preserves_occurrence_order(tmp_path: Path, capsys) -> None:
    source = _handle_catalog_fixture(tmp_path / "three-handles.dxf", 3)
    assert main(["cad", "blocks", str(source), "--handles"]) == 0
    output = capsys.readouterr().out
    assert "1: 1, 2: 2, 3: 3" in output


def test_blocks_handles_option_limits_long_lists(tmp_path: Path, capsys) -> None:
    source = _handle_catalog_fixture(tmp_path / "many-handles.dxf", 12)
    assert main(["cad", "blocks", str(source), "--handles"]) == 0
    output = capsys.readouterr().out
    assert "10: A" in output
    assert "… y 2 más" in output
    assert "11: B" not in output
