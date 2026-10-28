"""Manifiesto JSON v1 (Fase 4, paso 3): contenido, coherencia con el PDF y validación."""

import copy
import hashlib
import json
from pathlib import Path

import ezdxf
import pymupdf
import pytest

from cde_exporter.cad import block_commands
from cde_exporter.cad.manifest import DOCUMENT_TYPE, SCHEMA_VERSION, file_name, validate_manifest
from cde_exporter.cli import main

_MM_PER_POINT = 25.4 / 72


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


@pytest.fixture
def captured(monkeypatch) -> dict:
    """Guarda el manifiesto que construye el flujo (todavía no se publica como archivo)."""
    store: dict = {}
    original = block_commands.build_manifest

    def capture(*args, **kwargs):
        store["manifest"] = original(*args, **kwargs)
        return store["manifest"]

    monkeypatch.setattr(block_commands, "build_manifest", capture)
    return store


def _extract(tmp_path: Path, *extra: str) -> tuple[Path, Path]:
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    block = document.blocks.new("DET")
    block.add_line((0, 0), (10, 0))
    block.add_line((0, 0), (0, 4))
    # Referencia desplazada: las coordenadas CAD del manifiesto son las del Model Space.
    document.modelspace().add_blockref("DET", (100, 50))
    source = tmp_path / "origen.dxf"
    document.saveas(source)
    out = tmp_path / "salida"
    code = main(
        [
            "cad",
            "extract-block",
            str(source),
            "--block-name",
            "DET",
            "--output-prefix",
            str(out / "detalle"),
            *extra,
        ]
    )
    assert code == 0
    return source, out / "detalle.pdf"


def test_manifest_identifies_source_pdf_and_scale(tmp_path: Path, captured: dict) -> None:
    source, pdf = _extract(tmp_path, "--scale", "1:50")
    manifest = captured["manifest"]

    assert validate_manifest(manifest) == ()
    assert manifest["document_type"] == DOCUMENT_TYPE == "cde-detail-manifest"
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["model_version"] == "2"
    assert manifest["source"]["dxf_name"] == "origen.dxf"
    assert manifest["source"]["dxf_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest["source"]["dxf_bytes"] == source.stat().st_size
    assert manifest["source"]["block_name"] == "DET"
    assert manifest["source"]["reference"]["occurrence"] == 1
    # El PDF publicado es exactamente el que describe el manifiesto.
    assert manifest["pdf"]["file_name"] == "detalle.pdf"
    assert manifest["pdf"]["sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert manifest["pdf"]["bytes"] == pdf.stat().st_size
    assert manifest["pdf"]["page_count"] == 1
    assert manifest["scale"] == {
        "requested": "1:50",
        "denominator": 50.0,
        "factor_mm_per_cad_unit": 20.0,
        "physical_scale_guaranteed": True,
        "physical_scale_reason": None,
    }
    assert manifest["exporter"]["ezdxf"] and manifest["exporter"]["pymupdf"]
    assert manifest["options"]["text_layer"] is True
    assert manifest["options"]["font_fallback"] == "arial.ttf"
    checks = {check["name"]: check["blocking"] for check in manifest["validation"]["checks"]}
    assert checks["PDF legible"] is True
    assert checks["Escala del dibujo"] is False
    by_type = {row["entity_type"]: row["total"] for row in manifest["entities"]["by_type"]}
    assert by_type == {"INSERT": 1, "LINE": 2}
    assert "\\" not in json.dumps(manifest, ensure_ascii=False)


def test_cad_to_page_matrix_maps_a_cad_point_onto_the_pdf(tmp_path: Path, captured: dict) -> None:
    _source, pdf = _extract(tmp_path, "--scale", "1:50")
    drawing = captured["manifest"]["pdf"]["drawing"]
    a, b, c, d, e, f = drawing["cad_to_page"]["matrix"]

    def to_page(x: float, y: float) -> tuple[float, float]:
        return a * x + c * y + e, b * x + d * y + f

    with pymupdf.open(pdf) as document:
        lines = [
            item
            for drawn in document[0].get_drawings()
            for item in drawn["items"]
            if item[0] == "l"
        ]
    points = {
        (round(p.x * _MM_PER_POINT, 3), round(p.y * _MM_PER_POINT, 3))
        for _, *ps in lines
        for p in ps
    }
    # Esquina (100, 50) y extremos (110, 50) y (100, 54) en coordenadas del Model Space.
    for cad_point in ((100, 50), (110, 50), (100, 54)):
        x, y = to_page(*cad_point)
        assert (round(x, 3), round(y, 3)) in points, cad_point
    # 10 m a 1:50 = 200 mm; Y hacia abajo en la página.
    assert to_page(110, 50)[0] - to_page(100, 50)[0] == pytest.approx(200.0, abs=1e-6)
    assert to_page(100, 54)[1] < to_page(100, 50)[1]
    page = drawing["page_bounds_mm"]
    assert (page["x0"], page["y0"]) == pytest.approx((5.0, 5.0), abs=1e-6)  # centrado exacto


def test_without_nominal_scale_the_physical_scale_is_explicitly_not_guaranteed(
    tmp_path: Path, captured: dict
) -> None:
    _extract(tmp_path)  # sin --scale (aviso y escala 1:1 aproximada)
    scale = captured["manifest"]["scale"]

    assert scale["requested"] is None
    assert scale["physical_scale_guaranteed"] is False
    assert scale["physical_scale_reason"].startswith("Sin escala nominal")


def test_notices_are_grouped_by_category_with_consistent_counts(
    tmp_path: Path, captured: dict
) -> None:
    _extract(tmp_path, "--scale", "1:50")
    notices = captured["manifest"]["notices"]

    assert set(notices["by_category"]) == {
        "fonts",
        "hatch",
        "dependencies",
        "units",
        "text",
        "renderer",
        "scale",
        "entities",
    }
    listed = [item for items in notices["by_category"].values() for item in items]
    assert sum(notices["counts"].values()) == len(listed)
    renderer = notices["by_category"]["renderer"]
    assert [item["code"] for item in renderer] == ["RENDERER_MESSAGE"]  # $PDSIZE por defecto


@pytest.fixture
def valid_manifest(tmp_path: Path, captured: dict) -> dict:
    _extract(tmp_path, "--scale", "1:50")
    return copy.deepcopy(captured["manifest"])


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda m: m.pop("pdf"), "manifiesto.pdf: falta la clave"),
        (lambda m: m.update(document_type="otro"), "manifiesto.document_type"),
        (lambda m: m.update(schema_version="2.0"), "versión 2.0 no compatible"),
        (lambda m: m.update(extra=1), "manifiesto.extra: clave no prevista"),
        (lambda m: m["source"].update(dxf_name="C:\\datos\\origen.dxf"), "source.dxf_name"),
        (lambda m: m["source"].update(dxf_name="datos/origen.dxf"), "source.dxf_name"),
        (lambda m: m["pdf"].update(sha256="abc"), "pdf.sha256: se esperaba un SHA-256"),
        (lambda m: m["pdf"].update(page_count=0), "al menos una página"),
        (lambda m: m["pdf"].update(page_size_mm=[1.0]), "lista de 2 números"),
        (lambda m: m["scale"].update(physical_scale_guaranteed=None), "falta el valor"),
        (lambda m: m["notices"]["counts"].update(info=99), "no cuadra con los avisos"),
        (lambda m: m["validation"].update(status="complete"), "'complete' con avisos"),
        (lambda m: m["validation"]["checks"][0].update(blocking="sí"), "se esperaba bool"),
    ],
)
def test_validate_manifest_reports_each_problem(valid_manifest: dict, change, expected) -> None:
    assert validate_manifest(valid_manifest) == ()

    change(valid_manifest)

    assert any(expected in problem for problem in validate_manifest(valid_manifest))


def test_file_name_keeps_only_the_name_with_forward_slashes() -> None:
    assert file_name("C:\\Proyectos\\ERCO\\plano.dxf") == "plano.dxf"
    assert file_name("data/input/plano.dxf") == "plano.dxf"
    assert file_name(None) is None
