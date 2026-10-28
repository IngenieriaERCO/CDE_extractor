import json
from pathlib import Path

import ezdxf
import pytest

from cde_exporter.cad.block_extractor import BlockExtraction, BlockReference
from cde_exporter.cad.detail_builder import build_extracted_detail
from cde_exporter.cad.detail_model import Bounds3D
from cde_exporter.cad.scale import UnitVerification
from cde_exporter.cli import main


def _inputs(*, unbounded: bool = False):
    reference = BlockReference(
        order=1,
        name="Detalle",
        layer="A-ARQ",
        handle="2A",
        insert=(10.0, 20.0, 0.0),
        rotation=90.0,
        xscale=1.0,
        yscale=2.0,
        zscale=1.0,
    )
    extraction = BlockExtraction(reference, 4, 4, unbounded)
    verified = UnitVerification("declaradas en el DXF y coherentes", "mm", 1.0, True)
    return reference, extraction, verified


@pytest.mark.parametrize("with_scale", [False, True])
@pytest.mark.parametrize("with_bounds", [False, True])
def test_builder_populates_model_with_optional_scale_and_bounds(
    tmp_path: Path, with_scale: bool, with_bounds: bool
) -> None:
    reference, extraction, verification = _inputs()
    detail = build_extracted_detail(
        source_path=tmp_path / "source.dxf",
        block_name="Detalle",
        reference=reference,
        extraction=extraction,
        unit_verification=verification,
        nominal_denominator=50.0 if with_scale else None,
        factor_mm_per_drawing_unit=0.02 if with_scale else None,
        effective_bounds_xyz=((0.0, 0.0, 0.0), (100.0, 50.0, 0.0)) if with_bounds else None,
    )
    assert detail.source.path.endswith("source.dxf")
    assert detail.reference.occurrence == 1
    assert detail.units.status == verification.status
    assert detail.scale is not None if with_scale else detail.scale is None
    assert detail.bounds == (
        Bounds3D((0.0, 0.0, 0.0), (100.0, 50.0, 0.0)) if with_bounds else None
    )
    assert detail.inventory == detail.dispositions == detail.dependencies == ()


def test_builder_maps_unit_warnings_and_unbounded_entities() -> None:
    reference, extraction, _ = _inputs(unbounded=True)
    verification = UnitVerification(
        "con indicios de inconsistencia", "m", 1000.0, False,
        ("bloque grande", "INSERT con escala sospechosa"),
    )
    detail = build_extracted_detail(
        source_path="source.dxf",
        block_name="Detalle",
        reference=reference,
        extraction=extraction,
        unit_verification=verification,
        nominal_denominator=50.0,
        factor_mm_per_drawing_unit=20.0,
        effective_bounds_xyz=None,
    )
    assert [warning.code for warning in detail.warnings] == [
        "UNITS_SUSPECT", "UNITS_SUSPECT", "ENTITIES_WITHOUT_BOUNDS"
    ]
    assert all(warning.severity.value == "warning" for warning in detail.warnings)


def test_builder_adds_warning_for_unknown_units_without_detail_messages() -> None:
    reference, extraction, _ = _inputs()
    verification = UnitVerification(
        "desconocidas ($INSUNITS = 0 o sin valor)", "desconocida", 1.0, False
    )
    detail = build_extracted_detail(
        source_path="source.dxf", block_name="Detalle", reference=reference,
        extraction=extraction, unit_verification=verification,
        nominal_denominator=None, factor_mm_per_drawing_unit=None,
        effective_bounds_xyz=None,
    )
    assert detail.warnings[0].code == "UNITS_UNVERIFIED"


def _source(path: Path, count: int = 1) -> Path:
    document = ezdxf.new()
    document.units = ezdxf.units.InsertUnits.Millimeters
    document.blocks.new("Detalle").add_line((0, 0), (10, 5))
    for index in range(count):
        document.modelspace().add_blockref("Detalle", (index * 20, 0))
    document.saveas(path)
    return path


def test_detail_model_has_model_version_and_manifest_is_utf8_json(
    tmp_path: Path, model_details
) -> None:
    source = _source(tmp_path / "source.dxf")
    prefix = tmp_path / "out"
    assert main([
        "cad", "extract-block", str(source), "--block-name", "Detalle", "--occurrence", "1",
        "--scale", "1:50", "--output-prefix", str(prefix), "--format", "svg",
    ]) == 0
    manifest = json.loads((tmp_path / "out.manifest.json").read_text(encoding="utf-8"))
    assert manifest["model_version"] == "2"
    data = model_details[0]
    assert data["model_version"] == "2"
    assert data["scale"]["nominal_denominator"] == 50.0
    assert data["source"]["block_name"] == "Detalle"


def test_detail_json_option_no_longer_exists(tmp_path: Path, capsys) -> None:
    source = _source(tmp_path / "source.dxf")
    with pytest.raises(SystemExit):
        main([
            "cad", "extract-block", str(source), "--block-name", "Detalle",
            "--output-prefix", str(tmp_path / "out"), "--detail-json", str(tmp_path / "d.json"),
        ])
    assert "--detail-json" in capsys.readouterr().err
    assert not list(tmp_path.glob("out*"))


def test_all_publishes_one_manifest_per_occurrence(tmp_path: Path, model_details) -> None:
    source = _source(tmp_path / "source.dxf", count=2)
    prefix = tmp_path / "all-out"
    assert main([
        "cad", "extract-block", str(source), "--block-name", "Detalle", "--all",
        "--output-prefix", str(prefix), "--format", "svg",
    ]) == 0
    first = json.loads((tmp_path / "all-out_001.manifest.json").read_text(encoding="utf-8"))
    second = json.loads((tmp_path / "all-out_002.manifest.json").read_text(encoding="utf-8"))
    assert first["source"]["reference"]["occurrence"] == 1
    assert second["source"]["reference"]["occurrence"] == 2
    assert first["source"]["reference"]["total_occurrences"] == 2
    assert second["source"]["reference"]["total_occurrences"] == 2
    assert first["pdf"]["file_name"] == "all-out_001.pdf"
    assert [item["reference"]["occurrence"] for item in model_details] == [1, 2]
