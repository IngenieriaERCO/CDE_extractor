import json
from pathlib import Path

import ezdxf
import pytest
from PIL import Image

from cde_exporter.cad.block_extractor import BlockExtraction, references_for_block
from cde_exporter.cad.detail_builder import build_extracted_detail
from cde_exporter.cad.detail_model import Severity
from cde_exporter.cad.scale import UnitVerification
from cde_exporter.cli import main

_UNC_REFERENCE = chr(92) * 2 + "servidor" + chr(92) + "recurso" + chr(92) + "imagen.png"


def _document_with_detail():
    document = ezdxf.new()
    return document, document.blocks.new("Detalle")


def _build(document, source_path: Path):
    document.modelspace().add_blockref("Detalle", (0, 0))
    reference = references_for_block(document, "Detalle")[0]
    return build_extracted_detail(
        source_path=source_path,
        block_name="Detalle",
        reference=reference,
        extraction=BlockExtraction(reference, 0, 0, False),
        unit_verification=UnitVerification("desconocidas", "desconocida", 1.0, False),
        nominal_denominator=None,
        factor_mm_per_drawing_unit=None,
        effective_bounds_xyz=None,
        document=document,
        include_dependencies=True,
    )


def _dependency(result, kind: str, reference: str):
    return next(
        item
        for item in result.dependencies
        if item.kind == kind and item.reference == reference
    )


def _warning(result, code: str):
    return next(item for item in result.warnings if item.code == code)


@pytest.mark.parametrize(
    ("block_flag", "block_name"),
    [
        pytest.param(ezdxf.entities.Block.XREF, "Xref", id="xref"),
        pytest.param(ezdxf.entities.Block.XREF_OVERLAY, "Overlay", id="xref-overlay"),
    ],
)
def test_xref_file_exists_is_resolved(
    tmp_path: Path, block_flag: int, block_name: str
) -> None:
    document, detail = _document_with_detail()
    (tmp_path / "base.dxf").touch()
    document.blocks.new(
        block_name, dxfattribs={"flags": block_flag, "xref_path": "base.dxf"}
    )
    detail.add_blockref(block_name, (0, 0))

    result = _build(document, tmp_path / "origen.dxf")

    dependency = _dependency(result, "xref", "base.dxf")
    assert dependency.resolved is True
    assert dependency.required is True


def test_xref_file_missing_is_critical(tmp_path: Path) -> None:
    document, detail = _document_with_detail()
    document.blocks.new(
        "Xref", dxfattribs={"flags": ezdxf.entities.Block.XREF, "xref_path": "falta.dxf"}
    )
    detail.add_blockref("Xref", (0, 0))

    result = _build(document, tmp_path / "origen.dxf")

    assert _dependency(result, "xref", "falta.dxf").resolved is False
    assert _warning(result, "DEPENDENCY_UNRESOLVED_XREF").severity is Severity.CRITICAL


def test_image_file_exists_is_resolved(tmp_path: Path) -> None:
    document, detail = _document_with_detail()
    image_path = tmp_path / "imagen.png"
    image_path.touch()
    detail.add_image(document.add_image_def(str(image_path), (2, 2)), (0, 0), (1, 1))

    result = _build(document, tmp_path / "origen.dxf")

    assert _dependency(result, "image", str(image_path)).resolved is True


def test_image_file_missing_has_warning_severity(tmp_path: Path) -> None:
    document, detail = _document_with_detail()
    detail.add_image(document.add_image_def("falta.png", (2, 2)), (0, 0), (1, 1))

    result = _build(document, tmp_path / "origen.dxf")

    assert _dependency(result, "image", "falta.png").resolved is False
    assert _warning(result, "DEPENDENCY_UNRESOLVED_IMAGE").severity is Severity.WARNING


def test_pdf_underlay_is_reported_and_unresolved_underlay_warns(tmp_path: Path) -> None:
    document, detail = _document_with_detail()
    underlay_path = tmp_path / "base.pdf"
    detail.add_underlay(document.add_underlay_def(str(underlay_path), fmt="pdf"), (0, 0))

    result = _build(document, tmp_path / "origen.dxf")

    assert _dependency(result, "underlay", str(underlay_path)).resolved is False
    assert _warning(result, "DEPENDENCY_UNRESOLVED_UNDERLAY").severity is Severity.WARNING


def test_missing_insert_definition_is_critical(tmp_path: Path) -> None:
    document, detail = _document_with_detail()
    detail.add_blockref("BloqueAusente", (0, 0))

    result = _build(document, tmp_path / "origen.dxf")

    assert _dependency(result, "missing_block", "BloqueAusente").resolved is False
    assert _warning(result, "DEPENDENCY_UNRESOLVED_BLOCK").severity is Severity.CRITICAL


def test_same_image_used_twice_produces_one_dependency(tmp_path: Path) -> None:
    document, detail = _document_with_detail()
    image_def = document.add_image_def("compartida.png", (2, 2))
    detail.add_image(image_def, (0, 0), (1, 1))
    detail.add_image(image_def, (2, 0), (1, 1))

    result = _build(document, tmp_path / "origen.dxf")

    assert result.dependencies == (
        _dependency(result, "image", "compartida.png"),
    )


def test_dependency_in_nested_block_is_included(tmp_path: Path) -> None:
    document, detail = _document_with_detail()
    nested = document.blocks.new("Anidado")
    nested.add_image(document.add_image_def("anidada.png", (2, 2)), (0, 0), (1, 1))
    detail.add_blockref("Anidado", (0, 0))

    result = _build(document, tmp_path / "origen.dxf")

    assert _dependency(result, "image", "anidada.png").required is True


@pytest.mark.parametrize(
    ("reference", "detail_marker"),
    [
        pytest.param("https://example.invalid/imagen.png", "URL", id="url"),
        pytest.param(_UNC_REFERENCE, "UNC", id="unc"),
    ],
)
def test_url_and_unc_are_rejected_without_path_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reference: str, detail_marker: str
) -> None:
    document, detail = _document_with_detail()
    detail.add_image(document.add_image_def(reference, (2, 2)), (0, 0), (1, 1))

    def reject_path_access(_path):
        raise AssertionError("No se debe consultar una ruta URL o UNC")

    monkeypatch.setattr(Path, "exists", reject_path_access)
    result = _build(document, tmp_path / "origen.dxf")

    dependency = _dependency(result, "image", reference)
    assert dependency.resolved is False
    assert detail_marker in dependency.detail


def test_dependency_order_is_stable_by_kind_and_reference(tmp_path: Path) -> None:
    document, detail = _document_with_detail()
    detail.add_image(document.add_image_def("z.png", (2, 2)), (0, 0), (1, 1))
    detail.add_image(document.add_image_def("a.png", (2, 2)), (2, 0), (1, 1))
    detail.add_underlay(document.add_underlay_def("base.pdf", fmt="pdf"), (0, 0))
    document.blocks.new(
        "Xref", dxfattribs={"flags": ezdxf.entities.Block.XREF, "xref_path": "base.dxf"}
    )
    detail.add_blockref("Xref", (0, 0))

    result = _build(document, tmp_path / "origen.dxf")

    assert list(result.dependencies) == sorted(
        result.dependencies, key=lambda item: (item.kind, item.reference)
    )


def test_modelspace_image_outside_selected_block_is_excluded(tmp_path: Path) -> None:
    document, detail = _document_with_detail()
    image_inside = tmp_path / "imagen-dentro-del-bloque.png"
    image_outside = tmp_path / "imagen-suelta-en-modelspace.png"
    Image.new("RGB", (2, 2), "white").save(image_inside)
    Image.new("RGB", (2, 2), "black").save(image_outside)
    detail.add_image(
        document.add_image_def(str(image_inside), (2, 2)), (0, 0), (1, 1)
    )
    document.modelspace().add_image(
        document.add_image_def(str(image_outside), (2, 2)), (100, 100), (1, 1)
    )
    document.modelspace().add_blockref("Detalle", (0, 0))
    source = tmp_path / "origen-con-imagen-suelta.dxf"
    document.saveas(source)
    prefix = tmp_path / "detalle-extraido"
    manifest = tmp_path / "detalle.json"

    assert main([
        "cad", "extract-block", str(source), "--block-name", "Detalle",
        "--occurrence", "1", "--output-prefix", str(prefix), "--format", "svg",
        "--detail-json", str(manifest),
    ]) == 0

    dependencies = json.loads(manifest.read_text(encoding="utf-8"))["dependencies"]
    references = {dependency["reference"] for dependency in dependencies}
    assert str(image_inside) in references
    assert str(image_outside) not in references

def test_detail_without_external_dependencies_has_empty_tuple(tmp_path: Path) -> None:
    document, detail = _document_with_detail()
    detail.add_line((0, 0), (1, 1))

    result = _build(document, tmp_path / "origen.dxf")

    assert result.dependencies == ()
    assert not any(warning.code.startswith("DEPENDENCY_") for warning in result.warnings)
