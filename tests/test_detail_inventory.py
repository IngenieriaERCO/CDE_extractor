from collections import Counter
from pathlib import Path

import ezdxf

from cde_exporter.cad.block_extractor import (
    BlockExtraction,
    references_for_block,
)
from cde_exporter.cad.detail_builder import build_extracted_detail
from cde_exporter.cad.detail_model import EntityStatus, detail_to_dict
from cde_exporter.cad.fidelity import FidelityReport, inspect_fidelity
from cde_exporter.cad.reader import read_dxf
from cde_exporter.cad.scale import UnitVerification


def _build_detail(path: Path, *, hatch_mode: str = "auto", threshold: int = 1):
    report = inspect_fidelity(path, hatch_mode=hatch_mode, hatch_threshold=threshold)
    document = read_dxf(path)
    reference = references_for_block(document, "Detalle")[0]
    extraction = BlockExtraction(
        reference=reference,
        definition_entity_count=len(document.blocks["Detalle"]),
        units_code=int(document.header.get("$INSUNITS", 0) or 0),
        unbounded=False,
    )
    verification = UnitVerification(
        "declaradas en el DXF y coherentes", "mm", 1.0, True
    )
    detail = build_extracted_detail(
        source_path=path,
        block_name="Detalle",
        reference=reference,
        extraction=extraction,
        unit_verification=verification,
        nominal_denominator=None,
        factor_mm_per_drawing_unit=None,
        effective_bounds_xyz=None,
        document=document,
        fidelity_report=report,
        hatch_mode=hatch_mode,
        hatch_threshold=threshold,
    )
    return detail, report, document


def _rich_detail(path: Path) -> Path:
    document = ezdxf.new()
    document.units = ezdxf.units.InsertUnits.Millimeters
    document.styles.new("SinFuente", dxfattribs={"font": "fuente_inventada_2d.ttf"})

    child = document.blocks.new("Anidado")
    child.add_line((0, 0), (2, 2))
    child.add_hatch().set_solid_fill()
    patterned = child.add_hatch(dxfattribs={"layer": "PATRON"})
    patterned.set_pattern_fill("ANSI35", scale=0.005)
    patterned.paths.add_polyline_path(
        [(0, 0), (15, 0), (15, 0.2), (0, 0.2)], is_closed=True
    )
    child.add_text("Texto", dxfattribs={"height": 1, "style": "SinFuente"})
    image_def = document.add_image_def("externa.png", (4, 3))
    child.add_image(image_def, (0, 0), (4, 3))

    detail = document.blocks.new("Detalle")
    detail.add_blockref("Anidado", (1, 1))
    detail.add_attdef("ETIQUETA", insert=(0, 0), dxfattribs={"height": 1})
    document.modelspace().add_blockref("Detalle", (10, 20)).add_auto_attribs(
        {"ETIQUETA": "Asociado"}
    )
    document.saveas(path)
    return path


def test_nested_inventory_classifies_hatches_fonts_and_unsupported(tmp_path: Path) -> None:
    source = _rich_detail(tmp_path / "rich.dxf")
    detail, report, _document = _build_detail(source)
    by_type_status = Counter(
        {(row.entity_type, row.status): row.count for row in detail.inventory}
    )

    assert by_type_status[("INSERT", EntityStatus.CONSERVED)] == 2
    assert by_type_status[("ATTRIB", EntityStatus.TRANSFORMED)] == 1
    assert by_type_status[("LINE", EntityStatus.CONSERVED)] == 1
    assert by_type_status[("HATCH", EntityStatus.SIMPLIFIED)] == 1
    assert by_type_status[("HATCH", EntityStatus.CONSERVED)] == 1
    assert by_type_status[("TEXT", EntityStatus.TRANSFORMED)] == 1
    assert by_type_status[("IMAGE", EntityStatus.UNSUPPORTED)] == 1

    transformed = [item for item in detail.dispositions if item.status is EntityStatus.TRANSFORMED]
    simplified = [item for item in detail.dispositions if item.status is EntityStatus.SIMPLIFIED]
    unsupported = [item for item in detail.dispositions if item.status is EntityStatus.UNSUPPORTED]
    assert all("sustituida" in item.reason for item in transformed)
    assert "--hatch-threshold" in simplified[0].reason
    assert "archivo raster externo" in unsupported[0].reason
    assert all(item.reason for item in detail.dispositions)

    serialized = detail_to_dict(detail)
    assert serialized["inventory"]
    assert serialized["dispositions"]
    assert detail.dependencies == ()
    assert isinstance(report, FidelityReport)


def test_inventory_reconciles_with_fidelity_report(tmp_path: Path) -> None:
    source = _rich_detail(tmp_path / "reconcile.dxf")
    detail, report, _document = _build_detail(source)
    assert sum(row.count for row in detail.inventory) == report.entity_total

    model_unsupported = Counter()
    for row in detail.inventory:
        if row.status is EntityStatus.UNSUPPORTED:
            model_unsupported[(row.entity_type, row.layer)] += row.count
    report_unsupported = Counter()
    for (entity_type, layer, _reason), count in report.unsupported_counts.items():
        report_unsupported[(entity_type, layer)] += count
    assert model_unsupported == report_unsupported

    model_simplified_hatches = sum(
        row.count
        for row in detail.inventory
        if row.entity_type == "HATCH" and row.status is EntityStatus.SIMPLIFIED
    )
    assert model_simplified_hatches == len(report.hatch_simplifications)
    model_transformed = sum(
        row.count
        for row in detail.inventory
        if row.status is EntityStatus.TRANSFORMED
    )
    report_substitutions = sum(
        count for (_style, _source, _resolved, found), count in report.fonts.items() if not found
    )
    assert model_transformed == report_substitutions


def test_duplicated_handles_keep_distinct_traversal_indices(tmp_path: Path) -> None:
    document = ezdxf.new()
    image_def = document.add_image_def("external.png", (2, 2))
    block = document.blocks.new("Detalle")
    block.add_image(image_def, (0, 0), (2, 2))
    block.add_image(image_def, (3, 0), (2, 2))
    document.modelspace().add_blockref("Detalle", (0, 0))
    source = tmp_path / "duplicate-handles.dxf"
    document.saveas(source)

    fidelity = inspect_fidelity(source)
    document = read_dxf(source)
    images = list(document.blocks["Detalle"].query("IMAGE"))
    images[1].dxf.handle = images[0].dxf.handle
    reference = references_for_block(document, "Detalle")[0]
    detail = build_extracted_detail(
        source_path=source,
        block_name="Detalle",
        reference=reference,
        extraction=BlockExtraction(reference, 2, 0, False),
        unit_verification=UnitVerification("desconocidas", "desconocida", 1.0, False),
        nominal_denominator=None,
        factor_mm_per_drawing_unit=None,
        effective_bounds_xyz=None,
        document=document,
        fidelity_report=fidelity,
        hatch_mode="normal",
        hatch_threshold=50000,
    )
    image_dispositions = [
        item for item in detail.dispositions if item.entity_type == "IMAGE"
    ]
    assert len(image_dispositions) == 2
    assert image_dispositions[0].handle == image_dispositions[1].handle
    assert image_dispositions[0].index != image_dispositions[1].index


def test_insert_cycle_is_bounded_and_counted_once_per_traversal_path(tmp_path: Path) -> None:
    document = ezdxf.new()
    first = document.blocks.new("Detalle")
    second = document.blocks.new("Ciclo")
    first.add_blockref("Ciclo", (0, 0))
    second.add_blockref("Detalle", (0, 0))
    document.modelspace().add_blockref("Detalle", (0, 0))
    source = tmp_path / "cycle.dxf"
    document.saveas(source)

    detail, report, _document = _build_detail(source)
    inserts = sum(
        row.count for row in detail.inventory if row.entity_type == "INSERT"
    )
    assert inserts == 3
    assert inserts == report.entity_total
