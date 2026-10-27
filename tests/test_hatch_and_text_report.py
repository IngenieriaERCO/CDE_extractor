"""Registro de HATCH (patrón, operación y motivo) y nota de texto dibujado como trazados."""

import json
from pathlib import Path

import ezdxf
import pymupdf
import pytest

from cde_exporter.cad.detail_extras import hatch_summary
from cde_exporter.cad.detail_model import HatchDetail, HatchGroup, HatchSummary, _to_plain_data
from cde_exporter.cad.fidelity import inspect_fidelity
from cde_exporter.cli import main

_ARIAL = Path("C:/Windows/Fonts/arial.ttf")
_SQUARE = [(0, 0), (1, 0), (1, 1), (0, 1)]


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def _source(path: Path, *, hatches: bool = True, text: bool = True) -> Path:
    """DET con un HATCH sólido, uno de patrón, uno de degradado y uno de patrón muy denso."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Millimeters
    block = document.blocks.new("DET")
    block.add_line((0, 0), (20, 10))
    if hatches:
        solid = block.add_hatch(dxfattribs={"layer": "RELLENO"})
        solid.set_solid_fill()
        solid.paths.add_polyline_path(_SQUARE, is_closed=True)
        pattern = block.add_hatch(dxfattribs={"layer": "PATRON"})
        pattern.set_pattern_fill("ANSI31", scale=2.5)
        pattern.paths.add_polyline_path([(2 + x, y) for x, y in _SQUARE], is_closed=True)
        gradient = block.add_hatch(dxfattribs={"layer": "DEGRADADO"})
        gradient.set_gradient(name="LINEAR")
        gradient.paths.add_polyline_path([(4 + x, y) for x, y in _SQUARE], is_closed=True)
        dense = block.add_hatch(dxfattribs={"layer": "DENSO"})
        dense.set_pattern_fill("ANSI35", scale=0.005)
        dense.paths.add_polyline_path([(0, 3), (15, 3), (15, 3.2), (0, 3.2)], is_closed=True)
    if text:
        block.add_text("Hola", dxfattribs={"height": 1, "insert": (8, 1)})
    document.modelspace().add_blockref("DET", (0, 0))
    document.saveas(path)
    return path


def _extract(source: Path, out: Path, *extra: str) -> tuple[str, Path]:
    code = main(
        [
            "cad",
            "extract-block",
            str(source),
            "--block-name",
            "DET",
            "--scale",
            "1:50",
            "--hatch-threshold",
            "50",
            "--output-prefix",
            str(out / "detalle"),
            *extra,
        ]
    )
    assert code == 0
    return (out / "detalle.txt").read_text(encoding="utf-8"), out / "detalle.pdf"


def _flat(text: str) -> str:
    return " ".join(text.split())


# (simplificados esperados, texto de la operación, texto del motivo)
_MODES = {
    "normal": (0, None, None),
    "auto": (1, "relleno sólido en lugar del patrón", "--hatch-threshold 50"),
    "solid": (3, "relleno sólido en lugar del", "--hatch-mode solid"),
    "outline": (4, "solo contorno, sin relleno ni patrón", "--hatch-mode outline"),
    "ignore": (4, "no se dibuja", "--hatch-mode ignore"),
}


@pytest.mark.parametrize("mode", list(_MODES))
def test_hatch_groups_reconcile_with_the_fidelity_report(tmp_path: Path, mode: str) -> None:
    source = _source(tmp_path / "origen.dxf")
    report = inspect_fidelity(source, mode, 50)

    summary = hatch_summary(report)

    expected, operation, reason = _MODES[mode]
    assert summary is not None and summary.mode == mode
    assert sum(group.count for group in summary.groups) == summary.total == 4
    simplified = [group for group in summary.groups if group.action == "simplified"]
    kept = [group for group in summary.groups if group.action == "conserved"]
    assert sum(group.count for group in simplified) == summary.simplified == expected
    assert sum(group.count for group in kept) == 4 - expected
    assert all(group.operation == "conservado" and group.reason is None for group in kept)
    for group in simplified:
        assert operation in group.operation
        assert reason in group.reason


def test_auto_mode_keeps_the_original_pattern_and_simplifies_only_the_dense_one(
    tmp_path: Path,
) -> None:
    summary = hatch_summary(inspect_fidelity(_source(tmp_path / "origen.dxf"), "auto", 50))

    assert [
        (group.kind, group.pattern, group.scale, group.count, group.action)
        for group in summary.groups
    ] == [
        ("degradado", "SOLID", 1.0, 1, "conserved"),
        ("patrón", "ANSI31", 2.5, 1, "conserved"),
        ("patrón", "ANSI35", 0.005, 1, "simplified"),
        ("sólido", "SOLID", 1.0, 1, "conserved"),
    ]
    assert summary.details[0].operation == "relleno sólido en lugar del patrón"


def test_old_hatch_data_without_the_new_fields_still_builds_and_serializes() -> None:
    detail = HatchDetail("A", "ANSI31", 0.5, "simplified", "motivo")
    summary = HatchSummary(2, 1, (detail,))

    assert detail.operation is None
    assert summary.groups == () and summary.mode is None
    json.dumps(_to_plain_data(summary))
    group = HatchGroup("patrón", "ANSI31", 0.5, 2, "conserved", "conservado")
    assert _to_plain_data(group)["reason"] is None


def test_report_lists_pattern_operation_and_reason_in_auto_mode(tmp_path: Path) -> None:
    report, _pdf = _extract(_source(tmp_path / "origen.dxf"), tmp_path / "salida")
    flat = _flat(report)

    assert "Modo de rayado: auto" in flat
    assert "patrón | ANSI31 | 2.5 | conservado | 1" in flat
    assert "sólido | SOLID | 1 | conservado | 1" in flat
    assert (
        "patrón | ANSI35 | 0.005 | relleno sólido en lugar del patrón; motivo: estimación de "
        "tramos superior a --hatch-threshold 50 | 1"
    ) in flat
    assert (
        "Simplificación HATCH: capa DENSO, patrón ANSI35, escala 0.005; operación: relleno "
        "sólido en lugar del patrón; motivo:"
    ) in flat


@pytest.mark.parametrize("mode", ["outline", "ignore"])
def test_report_explains_every_hatch_when_the_mode_simplifies_all(
    tmp_path: Path, mode: str
) -> None:
    report, _pdf = _extract(
        _source(tmp_path / "origen.dxf"), tmp_path / "salida", "--hatch-mode", mode
    )
    flat = _flat(report)

    assert "HATCH simplificados: 4" in flat
    assert "Motivo de simplificación HATCH: no disponible" not in flat
    assert f"motivo: --hatch-mode {mode} | 1" in flat
    assert "patrón | ANSI31 | 2.5 |" in flat


@pytest.mark.skipif(not _ARIAL.is_file(), reason="arial.ttf no está instalado")
def test_report_notes_that_text_is_drawn_as_paths_and_the_pdf_agrees(tmp_path: Path) -> None:
    report, pdf = _extract(_source(tmp_path / "origen.dxf"), tmp_path / "salida")

    notes = _flat(report.split("A tener en cuenta", 1)[1])
    assert (
        "El texto del PDF (1 entidad de texto: TEXT, MTEXT, ATTRIB y DIMENSION) está dibujado "
        "como trazados vectoriales" in notes
    )
    assert "no se puede seleccionar, copiar ni buscar" in notes
    # La nota describe el PDF real: si el texto pasa a ser buscable, hay que cambiar la nota.
    with pymupdf.open(pdf) as document:
        assert document[0].get_text().strip() == ""


def test_report_has_no_text_note_without_text(tmp_path: Path) -> None:
    report, _pdf = _extract(_source(tmp_path / "origen.dxf", text=False), tmp_path / "salida")

    assert "trazados vectoriales" not in report


def test_report_without_hatches_has_no_hatch_treatment(tmp_path: Path) -> None:
    report, _pdf = _extract(
        _source(tmp_path / "origen.dxf", hatches=False, text=False), tmp_path / "salida"
    )

    assert "Tratamiento de los HATCH" not in report
