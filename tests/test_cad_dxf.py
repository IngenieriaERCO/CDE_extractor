from pathlib import Path
from types import SimpleNamespace

import ezdxf
import pymupdf
import pytest

from cde_exporter.cad import (
    CadProcessingError,
    CadWindow,
    diagnose_unbounded_entities,
    export_vector_outputs,
    extract_dxf_region,
    inspect_dxf,
    reader,
    select_by_window,
    selection,
)
from cde_exporter.cli import main


def create_sample_dxf(path: Path) -> Path:
    document = ezdxf.new()
    document.units = ezdxf.units.InsertUnits.Millimeters
    for layer_name in ("SLAB", "BEAM", "OTHER", "PILES"):
        document.layers.new(layer_name)
    modelspace = document.modelspace()
    modelspace.add_line((0, 0), (10, 10), dxfattribs={"layer": "SLAB"})
    modelspace.add_line((-5, 5), (5, 5), dxfattribs={"layer": "BEAM"})
    modelspace.add_line((100, 100), (110, 110), dxfattribs={"layer": "OTHER"})
    block = document.blocks.new("PILE")
    block.add_circle((0, 0), radius=1, dxfattribs={"layer": "PILES"})
    modelspace.add_blockref(
        "PILE",
        insert=(3, 3),
        dxfattribs={"layer": "PILES", "xscale": 1.5, "rotation": 30},
    )
    document.saveas(path)
    return path


def create_entity_without_layer(document, monkeypatch):
    entity = document.modelspace().add_line((0, 0), (1, 1))
    monkeypatch.setattr(entity, "is_supported_dxf_attrib", lambda key: key != "layer")
    monkeypatch.setattr(entity, "dxftype", lambda: "GEOMAPIMAGE")
    return entity


def use_unbounded_entity_fixture(monkeypatch):
    document = ezdxf.new()
    modelspace = document.modelspace()
    mtext = modelspace.add_mtext(
        "nota",
        dxfattribs={"insert": (2, 3, 0), "layer": "TEXTOS"},
    )
    polyline = modelspace.add_polyline3d(
        [(15, 15, 0), (20, 20, 0)],
        dxfattribs={"layer": "POLILINEAS"},
    )
    solid = modelspace.add_3dsolid(dxfattribs={"layer": "SOLIDOS"})
    region = modelspace.add_region(dxfattribs={"layer": "REGIONES"})
    polyline.dxf.handle = mtext.dxf.handle
    monkeypatch.setattr(reader, "read_dxf", lambda _path: document)
    monkeypatch.setattr(reader, "entity_bounds", lambda _entity: None)
    return document, mtext, polyline, solid, region


def test_inspect_reports_units_layers_types_and_extents(tmp_path: Path) -> None:
    source = create_sample_dxf(tmp_path / "source.dxf")

    result = inspect_dxf(source)

    assert result.entity_count == 4
    assert result.units_name == "Millimeters"
    assert result.entity_types == {"INSERT": 1, "LINE": 3}
    assert result.layers["SLAB"] == 1
    assert "PILE" in result.block_names
    assert result.extents == (-5.0, 0.0, 110.0, 110.0)
    assert result.entities_without_bounds == {}


def test_inspect_reports_entity_type_without_layer_attribute(monkeypatch) -> None:
    document = ezdxf.new()
    entity = create_entity_without_layer(document, monkeypatch)
    assert not entity.is_supported_dxf_attrib("layer")
    monkeypatch.setattr(reader, "read_dxf", lambda _path: document)

    result = inspect_dxf("synthetic.dxf")

    assert result.entity_count == 1
    assert result.entity_types == {"GEOMAPIMAGE": 1}
    assert result.layers == {"<sin atributo layer: GEOMAPIMAGE>": 1}


def test_inspect_defaults_supported_but_absent_layer_to_zero(monkeypatch) -> None:
    document = ezdxf.new()
    entity = document.modelspace().add_line((0, 0), (1, 1))
    entity.dxf.discard("layer")
    assert entity.is_supported_dxf_attrib("layer")
    monkeypatch.setattr(reader, "read_dxf", lambda _path: document)

    result = inspect_dxf("synthetic.dxf")

    assert result.layers == {"0": 1}


def test_ensure_layer_table_entries_preserves_entities_without_layer_attribute(monkeypatch) -> None:
    document = ezdxf.new()
    entity = create_entity_without_layer(document, monkeypatch)
    assert not entity.is_supported_dxf_attrib("layer")
    before = list(document.modelspace())

    reader.ensure_layer_table_entries(document)

    assert list(document.modelspace()) == before


def test_diagnose_unbounded_entities_reports_positions_order_layers_and_window(monkeypatch) -> None:
    _, mtext, polyline, solid, region = use_unbounded_entity_fixture(monkeypatch)

    report = diagnose_unbounded_entities("synthetic.dxf", CadWindow(0, 0, 10, 10))

    assert [entity.entity_type for entity in report.entities] == [
        "MTEXT",
        "POLYLINE",
        "3DSOLID",
        "REGION",
    ]
    assert [entity.modelspace_order for entity in report.entities] == [1, 2, 3, 4]
    assert report.entities[0].handle == mtext.dxf.handle
    assert report.entities[0].position == (2.0, 3.0, 0.0)
    assert report.entities[1].handle == polyline.dxf.handle == mtext.dxf.handle
    assert report.entities[1].position == (15.0, 15.0, 0.0)
    assert report.entities[2].handle == solid.dxf.handle
    assert report.entities[2].position is None
    assert report.entities[2].position_note == "sin posición fiable"
    assert report.entities[3].handle == region.dxf.handle
    assert report.entities[3].position is None
    assert report.counts_by_type == {"3DSOLID": 1, "MTEXT": 1, "POLYLINE": 1, "REGION": 1}
    assert report.counts_by_layer == {
        "POLILINEAS": 1,
        "REGIONES": 1,
        "SOLIDOS": 1,
        "TEXTOS": 1,
    }
    assert (report.inside_window, report.outside_window, report.without_position) == (1, 1, 2)


def test_cli_diagnose_prints_window_counts(monkeypatch, capsys) -> None:
    use_unbounded_entity_fixture(monkeypatch)

    result = main(
        [
            "cad",
            "diagnose",
            "synthetic.dxf",
            "--window",
            "0",
            "0",
            "10",
            "10",
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "1 | MTEXT | TEXTOS |" in output
    assert "2 | POLYLINE | POLILINEAS |" in output
    assert "sin posición fiable" in output
    assert "Posiciones dentro de --window: 1" in output
    assert "Posiciones fuera de --window: 1" in output
    assert "Entidades sin posición: 2" in output


def test_diagnose_reports_nested_unbounded_entities_and_writes_utf8(
    monkeypatch, tmp_path: Path
) -> None:
    document = ezdxf.new()
    inner = document.blocks.new("INTERNO")
    inner.add_3dsolid(dxfattribs={"layer": "SOLIDOS"})
    inner.add_line((0, 0), (1, 1), dxfattribs={"layer": "GEOMETRIA"})
    outer = document.blocks.new("EXTERNO")
    outer.add_mtext("nota", dxfattribs={"layer": "TEXTOS"})
    outer.add_blockref("INTERNO", insert=(1, 1))
    outer.add_line((2, 2), (3, 3), dxfattribs={"layer": "GEOMETRIA"})
    first = document.modelspace().add_blockref("EXTERNO", insert=(12, 12))
    second = document.modelspace().add_blockref("EXTERNO", insert=(5, 5))
    monkeypatch.setattr(reader, "read_dxf", lambda _path: document)
    real_bounds = reader.entity_bounds
    bad_entities = {
        entity
        for block in (inner, outer)
        for entity in block
        if entity.dxftype() in {"3DSOLID", "MTEXT"}
    }
    root_bounds = {first: (2.0, 2.0, 4.0, 4.0), second: (20.0, 20.0, 30.0, 30.0)}

    def synthetic_bounds(entity):
        if entity in bad_entities:
            return None
        if entity in root_bounds:
            return root_bounds[entity]
        return real_bounds(entity)

    monkeypatch.setattr(reader, "entity_bounds", synthetic_bounds)
    report = diagnose_unbounded_entities("synthetic.dxf", CadWindow(0, 0, 10, 10))

    assert [(row.modelspace_order, row.block_name) for row in report.block_inserts] == [
        (1, "EXTERNO"),
        (2, "EXTERNO"),
    ]
    assert report.block_inserts[0].unbounded_by_type == {"3DSOLID": 1, "MTEXT": 1}
    assert report.block_inserts[0].insert_inside_window is False
    assert report.block_inserts[0].partial_bounds_touch_window is True
    assert report.block_inserts[1].insert_inside_window is True
    assert report.block_inserts[1].partial_bounds_touch_window is False
    assert report.nested_counts_by_type == {"3DSOLID": 1, "MTEXT": 1}
    assert report.inspect_total == 2
    assert report.inspect_counts_by_type == report.nested_counts_by_type

    output = tmp_path / "diagnostico.txt"
    assert (
        main(
            [
                "cad",
                "diagnose",
                "synthetic.dxf",
                "--window",
                "0",
                "0",
                "10",
                "10",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    content = output.read_bytes().decode("utf-8")
    assert "Punto de inserción" in content
    assert "Total como aviso de cad inspect: 2" in content


def test_window_selection_uses_xy_box_intersection_and_keeps_whole_entities(
    tmp_path: Path,
) -> None:
    source = create_sample_dxf(tmp_path / "source.dxf")
    window = CadWindow(0, 0, 8, 8)

    result = select_by_window(source, window)

    assert result.entity_types == {"INSERT": 1, "LINE": 2}
    assert result.bounds == (-5.0, 0.0, 10.0, 10.0)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ((0, 0, 0, 1), "ancho y alto"),
        ((0, 1, 2, 1), "ancho y alto"),
        ((0, 0, float("inf"), 1), "finito"),
    ],
)
def test_reject_invalid_window(values, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        CadWindow(*values)


def test_selection_refuses_unbounded_entities(tmp_path: Path) -> None:
    source = ezdxf.new()
    source.modelspace().add_ray((0, 0), (1, 0))
    path = tmp_path / "ray.dxf"
    source.saveas(path)

    with pytest.raises(CadProcessingError, match="calcular cajas"):
        select_by_window(path, CadWindow(-1, -1, 1, 1))


def test_allow_unbounded_keeps_insert_when_partial_bounds_touch_window(monkeypatch) -> None:
    document = ezdxf.new()
    block = document.blocks.new("PARCIAL")
    solid = block.add_3dsolid(dxfattribs={"layer": "SOLIDOS"})
    block.add_line((0, 0), (2, 2), dxfattribs={"layer": "LINEAS"})
    insert = document.modelspace().add_blockref("PARCIAL", insert=(0, 0))
    real_bounds = reader.entity_bounds

    def bounds(entity):
        return None if entity is solid else real_bounds(entity)

    monkeypatch.setattr(reader, "entity_bounds", bounds)
    monkeypatch.setattr(selection, "entity_bounds", bounds)

    selected = select_by_window(document, CadWindow(-1, -1, 3, 3), allow_unbounded=True)

    assert selected.entities == (insert,)
    actual_rows = [
        (row.entity_type, row.block_name, row.status) for row in selected.unbounded_entities
    ]
    assert actual_rows == [("3DSOLID", "PARCIAL", "incluido en la selección")]


def test_allow_unbounded_stops_at_direct_known_position_inside_and_logs_outside(
    monkeypatch,
) -> None:
    document = ezdxf.new()
    document.modelspace().add_line((0, 0), (2, 2))
    inside = document.modelspace().add_mtext("dentro", dxfattribs={"insert": (1, 1)})
    monkeypatch.setattr(
        reader,
        "entity_bounds",
        lambda entity: None if entity is inside else (0, 0, 2, 2),
    )
    monkeypatch.setattr(selection, "entity_bounds", reader.entity_bounds)

    with pytest.raises(CadProcessingError, match="posición conocida cae dentro"):
        select_by_window(document, CadWindow(0, 0, 3, 3), allow_unbounded=True)

    outside_document = ezdxf.new()
    outside_document.modelspace().add_line((0, 0), (2, 2))
    outside = outside_document.modelspace().add_mtext("fuera", dxfattribs={"insert": (20, 20)})
    monkeypatch.setattr(
        reader,
        "entity_bounds",
        lambda entity: None if entity is outside else (0, 0, 2, 2),
    )
    monkeypatch.setattr(selection, "entity_bounds", reader.entity_bounds)

    selected = select_by_window(outside_document, CadWindow(0, 0, 3, 3), allow_unbounded=True)
    assert len(selected.entities) == 1
    assert selected.unbounded_entities[0].status == "no verificado"


def test_cli_allow_unbounded_writes_utf8_omitted_report(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    source = create_sample_dxf(tmp_path / "source.dxf")
    item = SimpleNamespace(
        modelspace_order=7,
        entity_type="MTEXT",
        layer="TEXTOS",
        handle="AB12",
        block_name=None,
        status="no verificado",
    )

    def extract(_source, _window, output, **_kwargs):
        ezdxf.new().saveas(output)
        return SimpleNamespace(
            unbounded_entities=(item,),
            units_code=0,
            extracted_entity_count=1,
            source_entity_count=2,
        )

    def export(_dxf, svg, pdf, **_kwargs):
        Path(svg).write_text("<svg/>", encoding="utf-8")
        Path(pdf).write_bytes(b"%PDF-test")
        return SimpleNamespace(warnings=())

    monkeypatch.setattr("cde_exporter.cli.extract_dxf_region", extract)
    monkeypatch.setattr("cde_exporter.cli.export_vector_outputs", export)
    prefix = tmp_path / "salida" / "detalle"
    assert (
        main(
            [
                "cad",
                "extract",
                str(source),
                "--window",
                "0",
                "0",
                "8",
                "8",
                "--output-prefix",
                str(prefix),
                "--format",
                "svg",
                "--allow-unbounded",
            ]
        )
        == 0
    )
    report = prefix.with_name("detalle_omitidas.txt")
    content = report.read_bytes().decode("utf-8")
    assert "7 | MTEXT | TEXTOS | AB12 | - | no verificado" in content
    assert "no pudieron validarse por coordenadas" in content
    assert "PDF/SVG puede no dibujar" in content
    assert "1 no verificadas" in capsys.readouterr().out


def test_extract_writes_only_selected_entities_and_preserves_input(tmp_path: Path) -> None:
    source_path = create_sample_dxf(tmp_path / "source.dxf")
    original_bytes = source_path.read_bytes()
    output = tmp_path / "nested" / "detail.dxf"

    result = extract_dxf_region(source_path, CadWindow(0, 0, 8, 8), output)
    extracted = ezdxf.readfile(output)

    assert result.extracted_entity_count == 3
    assert len(extracted.modelspace()) == 3
    assert extracted.units == ezdxf.units.InsertUnits.Millimeters
    assert extracted.blocks.get("PILE") is not None
    assert extracted.layers.get("SLAB") is not None
    insert = extracted.modelspace().query("INSERT").first
    assert insert.dxf.xscale == 1.5
    assert insert.dxf.rotation == 30
    assert source_path.read_bytes() == original_bytes


def test_extraction_supplies_default_layer_resources_when_missing(tmp_path: Path) -> None:
    document = ezdxf.new()
    document.modelspace().add_line(
        (0, 0),
        (4, 4),
        dxfattribs={"layer": "UNDECLARED"},
    )
    source = tmp_path / "source.dxf"
    document.saveas(source)
    output = tmp_path / "detail.dxf"

    extract_dxf_region(source, CadWindow(-1, -1, 5, 5), output)
    extracted = ezdxf.readfile(output)

    assert extracted.layers.get("UNDECLARED") is not None


def test_extraction_rejects_existing_output_and_source_overwrite(tmp_path: Path) -> None:
    source = create_sample_dxf(tmp_path / "source.dxf")
    output = tmp_path / "detail.dxf"
    output.write_text("keep", encoding="utf-8")

    with pytest.raises(CadProcessingError, match="ya existe"):
        extract_dxf_region(source, CadWindow(0, 0, 8, 8), output)
    assert output.read_text(encoding="utf-8") == "keep"

    with pytest.raises(CadProcessingError, match="original"):
        extract_dxf_region(source, CadWindow(0, 0, 8, 8), source, overwrite=True)


def test_reject_non_dxf_input(tmp_path: Path) -> None:
    dwg = tmp_path / "drawing.dwg"
    dwg.write_bytes(b"not a DWG")

    with pytest.raises(CadProcessingError, match="lee DXF"):
        inspect_dxf(dwg)


def test_missing_and_corrupt_dxf_are_reported(tmp_path: Path) -> None:
    with pytest.raises(CadProcessingError, match="No existe"):
        inspect_dxf(tmp_path / "missing.dxf")
    corrupt = tmp_path / "corrupt.dxf"
    corrupt.write_bytes(b"not a DXF")

    with pytest.raises(CadProcessingError, match="No se pudo leer"):
        inspect_dxf(corrupt)


def test_unitless_drawing_is_reported_without_assuming_physical_units(
    tmp_path: Path,
) -> None:
    document = ezdxf.new()
    document.header["$INSUNITS"] = 0
    document.modelspace().add_line((0, 0), (1, 1))
    source = tmp_path / "unitless.dxf"
    document.saveas(source)

    result = inspect_dxf(source)

    assert result.units_code == 0
    assert result.units_name == "Unitless"


def test_export_vector_outputs_creates_valid_svg_and_pdf(tmp_path: Path) -> None:
    source = create_sample_dxf(tmp_path / "source.dxf")
    extracted = tmp_path / "detail.dxf"
    extract_dxf_region(source, CadWindow(0, 0, 8, 8), extracted)

    report = export_vector_outputs(
        extracted,
        tmp_path / "detail.svg",
        tmp_path / "detail.pdf",
    )

    assert report.svg_element_count > 1
    assert report.pdf_page_count == 1
    assert report.pdf_vector_drawing_count > 0
    svg_text = (tmp_path / "detail.svg").read_text(encoding="utf-8")
    assert "<svg" in svg_text
    assert "stroke: #000000" in svg_text
    assert (tmp_path / "detail.pdf").read_bytes().startswith(b"%PDF")


def test_export_accepts_page_dimensions_scale_and_margin(tmp_path: Path) -> None:
    source = create_sample_dxf(tmp_path / "source.dxf")
    extracted = tmp_path / "detail.dxf"
    extract_dxf_region(source, CadWindow(0, 0, 8, 8), extracted)

    export_vector_outputs(
        extracted,
        tmp_path / "scaled.svg",
        tmp_path / "scaled.pdf",
        page_size_mm=(100, 80),
        margin_mm=4,
        scale_factor=1,
    )
    pdf = pymupdf.open(tmp_path / "scaled.pdf")
    try:
        assert pdf.page_count == 1
        assert pdf[0].rect.width == pytest.approx(100 * 72 / 25.4, abs=1)
        assert pdf[0].rect.height == pytest.approx(80 * 72 / 25.4, abs=1)
    finally:
        pdf.close()


def test_export_rejects_scale_that_would_overflow_page(tmp_path: Path) -> None:
    source = create_sample_dxf(tmp_path / "source.dxf")
    extracted = tmp_path / "detail.dxf"
    extract_dxf_region(source, CadWindow(0, 0, 8, 8), extracted)

    with pytest.raises(CadProcessingError, match="no cabe"):
        export_vector_outputs(
            extracted,
            tmp_path / "scaled.svg",
            tmp_path / "scaled.pdf",
            page_size_mm=(100, 80),
            margin_mm=5,
            scale_factor=20,
        )


def test_cli_extract_creates_three_outputs_and_refuses_overwrite(tmp_path: Path, capsys) -> None:
    source = create_sample_dxf(tmp_path / "source.dxf")
    prefix = tmp_path / "output" / "detail"
    arguments = [
        "cad",
        "extract",
        str(source),
        "--window",
        "0",
        "0",
        "8",
        "8",
        "--output-prefix",
        str(prefix),
    ]

    assert main(arguments) == 0
    assert all(prefix.with_suffix(suffix).is_file() for suffix in (".dxf", ".svg", ".pdf"))
    assert prefix.with_name("detail_fidelidad.txt").is_file()
    assert "Entidades extraídas: 3 de 4." in capsys.readouterr().out

    assert main(arguments) == 1
    assert "ya existe" in capsys.readouterr().err


def test_cli_extract_warns_that_it_is_deprecated(tmp_path: Path, capsys) -> None:
    """«cad extract» hace lo mismo que antes, pero avisa de que está en desuso."""
    source = create_sample_dxf(tmp_path / "source.dxf")
    prefix = tmp_path / "output" / "detail"

    assert main(
        ["cad", "extract", str(source), "--window", "0", "0", "8", "8", "--output-prefix",
         str(prefix)]
    ) == 0

    error = capsys.readouterr().err
    assert "«cad extract» está en desuso" in error
    assert "cad extract-modelspace --window" in error
    assert prefix.with_suffix(".dxf").is_file() and prefix.with_suffix(".pdf").is_file()


def test_cli_can_publish_only_requested_vector_format(tmp_path: Path) -> None:
    source = create_sample_dxf(tmp_path / "source.dxf")
    prefix = tmp_path / "output" / "detail"

    result = main(
        [
            "cad",
            "extract",
            str(source),
            "--window",
            "0",
            "0",
            "8",
            "8",
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


@pytest.mark.parametrize("mode", ("normal", "outline", "solid", "ignore"))
def test_export_hatch_modes_create_vector_outputs(tmp_path: Path, mode: str) -> None:
    source = create_sample_dxf(tmp_path / "source.dxf")
    export_vector_outputs(source, tmp_path / "out.svg", tmp_path / "out.pdf", hatch_mode=mode)


def test_cli_keeps_dxf_and_report_if_vector_export_fails(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    source = create_sample_dxf(tmp_path / "source.dxf")
    original = source.read_bytes()
    monkeypatch.setattr(
        "cde_exporter.cli.export_vector_outputs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(CadProcessingError("fallo sintético")),
    )
    prefix = tmp_path / "out" / "detail"
    result = main(
        [
            "cad",
            "extract",
            str(source),
            "--window",
            "0",
            "0",
            "8",
            "8",
            "--output-prefix",
            str(prefix),
            "--allow-unbounded",
            "--hatch-mode",
            "outline",
        ]
    )
    assert result == 1
    assert prefix.with_suffix(".dxf").exists()
    report = prefix.with_name("detail_omitidas.txt").read_text(encoding="utf-8")
    assert "Rayados simplificados: modo outline" in report
    assert prefix.with_name("detail_fidelidad.txt").is_file()
    assert source.read_bytes() == original
    assert "fallo sintético" in capsys.readouterr().err
