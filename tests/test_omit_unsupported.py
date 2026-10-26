import json
import os
import subprocess
import sys
from pathlib import Path

import ezdxf
import pytest

from cde_exporter.cad import exporters
from cde_exporter.cad.block_commands import _omitted_terminal_line
from cde_exporter.cad.detail_model import (
    OMITTED_REASON,
    DetailSource,
    EntityCount,
    EntityDisposition,
    EntityStatus,
    ExtractedDetail,
    SelectedReference,
    UnitInfo,
    mark_omitted_entities,
    omitted_entities_message,
)
from cde_exporter.cad.exporters import export_vector_outputs
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cli import main

# Un 3DSOLID sin datos ACIS no se guarda en el DXF; R2000 admite SAT en texto.
_SAT = ["400 0 1 0", "@33 Spatial Corp.", "1 9.9999999999999995e-07 1e-10", "End-of-ACIS-data"]


def _add_solid(layout, layer: str) -> None:
    layout.add_3dsolid(dxfattribs={"layer": layer}).sat = _SAT


def _source(path: Path, *, solids: int = 2, nested: bool = False, tiny_text: bool = False) -> Path:
    document = ezdxf.new("R2000")
    document.units = ezdxf.units.InsertUnits.Millimeters
    block = document.blocks.new("Detalle")
    block.add_line((0, 0), (10, 5))
    for _ in range(solids):
        _add_solid(block, "SOLIDOS")
    if nested:
        inner = document.blocks.new("Interno")
        inner.add_line((0, 0), (2, 2))
        _add_solid(inner, "INTERNA")
        block.add_blockref("Interno", (1, 1))
    if tiny_text:
        block.add_text("x", height=1e-12)
    document.modelspace().add_blockref("Detalle", (0, 0))
    document.saveas(path)
    return path


def _run(source: Path, out: Path, *extra: str) -> int:
    return main(
        [
            "cad",
            "extract-block",
            str(source),
            "--block-name",
            "Detalle",
            "--scale",
            "1:50",
            "--output-prefix",
            str(out / "detalle"),
            *extra,
        ]
    )


def _names(folder: Path) -> list[str]:
    return sorted(item.name for item in folder.iterdir()) if folder.exists() else []


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def test_default_is_strict_and_suggests_the_option(tmp_path: Path, capsys) -> None:
    out = tmp_path / "salida"

    assert _run(_source(tmp_path / "origen.dxf"), out) == 1

    captured = capsys.readouterr()
    message = captured.out + captured.err
    assert "3DSOLID: unsupported" in message
    assert (
        "Puede usar en cad extract-block --solids footprint (dibujar los 3DSOLID como "
        "huella 2D) o --omit-unsupported (omitirlos)."
    ) in " ".join(message.split())
    assert _names(out) == []


def test_option_omits_counts_and_reports_in_txt_and_terminal(tmp_path: Path, capsys) -> None:
    out = tmp_path / "salida"

    assert _run(_source(tmp_path / "origen.dxf"), out, "--omit-unsupported") == 0

    assert _names(out) == ["detalle.pdf", "detalle.txt"]
    report = (out / "detalle.txt").read_text(encoding="utf-8")
    assert "3DSOLID | SOLIDOS | omitidas | 2" in report
    assert "3DSOLID | SOLIDOS | no soportadas" not in report
    assert "Omitidas: 2; omitidas por --omit-unsupported" in _flat(report)
    notes = _flat(report.split("A tener en cuenta", 1)[1])
    assert (
        "Se omitieron 2 entidades no soportadas (--omit-unsupported): "
        "3DSOLID en capa SOLIDOS (2); no aparecen en el PDF."
    ) in notes
    assert "Revise en el CAD si esa geometría es necesaria en el PDF." in notes
    assert "Estado final: COMPLETA CON ADVERTENCIAS" in report
    last_line = capsys.readouterr().err.strip().splitlines()[-1]
    assert last_line == (
        "Aviso: se omitieron 2 entidades no soportadas (3DSOLID en SOLIDOS); no aparecen en el PDF."
    )


def test_summary_is_last_line_when_both_streams_share_a_pipe(tmp_path: Path) -> None:
    # Con stdout y stderr en la misma tubería, stdout llega con búfer; sin vaciarlo,
    # el aviso de stderr saldría antes que el resto del resumen.
    source = _source(tmp_path / "origen.dxf")
    out = tmp_path / "salida"
    env = {key: value for key, value in os.environ.items() if key != "CDE_KEEP_INTERMEDIATES"}
    env["PYTHONIOENCODING"] = "utf-8"
    code = "import sys; from cde_exporter.cli import main; sys.exit(main(sys.argv[1:]))"
    arguments = [
        "cad",
        "extract-block",
        str(source),
        "--block-name",
        "Detalle",
        "--scale",
        "1:50",
        "--output-prefix",
        str(out / "detalle"),
        "--omit-unsupported",
    ]

    completed = subprocess.run(
        [sys.executable, "-c", code, *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        timeout=300,
        check=False,
    )

    assert completed.returncode == 0
    lines = completed.stdout.decode("utf-8").strip().splitlines()
    assert lines[-1] == (
        "Aviso: se omitieron 2 entidades no soportadas (3DSOLID en SOLIDOS); no aparecen en el PDF."
    )
    assert any(line.startswith("Creado: ") for line in lines[:-1])


def test_single_omission_uses_singular(tmp_path: Path, capsys) -> None:
    out = tmp_path / "salida"

    assert _run(_source(tmp_path / "origen.dxf", solids=1), out, "--omit-unsupported") == 0

    report = _flat((out / "detalle.txt").read_text(encoding="utf-8"))
    assert "Se omitió 1 entidad no soportada (--omit-unsupported)" in report
    assert "no aparece en el PDF." in report
    assert capsys.readouterr().err.strip().splitlines()[-1] == (
        "Aviso: se omitió 1 entidad no soportada (3DSOLID en SOLIDOS); no aparece en el PDF."
    )


def test_nested_solid_is_counted_on_its_own_layer(tmp_path: Path) -> None:
    out = tmp_path / "salida"
    source = _source(tmp_path / "origen.dxf", solids=1, nested=True)

    assert _run(source, out, "--omit-unsupported") == 0

    report = (out / "detalle.txt").read_text(encoding="utf-8")
    assert "3DSOLID | INTERNA | omitidas | 1" in report
    assert "3DSOLID | SOLIDOS | omitidas | 1" in report
    assert "FIDELITY_RECONCILIATION" not in report


def test_other_omissions_still_stop_with_the_option(tmp_path: Path, capsys) -> None:
    out = tmp_path / "salida"
    source = _source(tmp_path / "origen.dxf", tiny_text=True)

    assert _run(source, out, "--omit-unsupported") == 1

    captured = capsys.readouterr()
    message = captured.out + captured.err
    assert "text height too small" in message
    assert "3DSOLID: unsupported" not in message
    assert "Puede usar --omit-unsupported" not in message
    assert _names(out) == []


def test_detail_json_marks_dispositions_as_omitted(tmp_path: Path) -> None:
    out = tmp_path / "salida"
    manifest = tmp_path / "detalle.json"

    assert (
        _run(
            _source(tmp_path / "origen.dxf"),
            out,
            "--omit-unsupported",
            "--detail-json",
            str(manifest),
        )
        == 0
    )

    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["model_version"] == "2"
    solids = [item for item in data["dispositions"] if item["entity_type"] == "3DSOLID"]
    assert [item["status"] for item in solids] == ["omitted", "omitted"]
    assert all(item["reason"] == OMITTED_REASON for item in solids)
    assert "UNSUPPORTED_OMITTED" in [item["code"] for item in data["warnings"]]


def _flat_source(path: Path, solids: int) -> Path:
    document = ezdxf.new("R2000")
    document.modelspace().add_line((0, 0), (1, 1))
    for _ in range(solids):
        _add_solid(document.modelspace(), "SOLIDOS")
    document.saveas(path)
    return path


def test_export_returns_omitted_rows_and_stays_strict_by_default(tmp_path: Path) -> None:
    flat = _flat_source(tmp_path / "plano.dxf", 3)

    with pytest.raises(CadProcessingError, match="--omit-unsupported"):
        export_vector_outputs(flat, tmp_path / "a.svg", tmp_path / "a.pdf")
    report = export_vector_outputs(
        flat, tmp_path / "b.svg", tmp_path / "b.pdf", omit_unsupported=True
    )

    assert report.omitted == (("3DSOLID", "SOLIDOS", 3),)


def test_svg_and_pdf_disagreement_stops_export(tmp_path: Path, monkeypatch) -> None:
    flat = _flat_source(tmp_path / "plano.dxf", 1)
    original = exporters._draw_to_pdf

    def different(*args, **kwargs):
        content, warnings, _omitted = original(*args, **kwargs)
        return content, warnings, (("3DSOLID", "SOLIDOS", 2),)

    monkeypatch.setattr(exporters, "_draw_to_pdf", different)

    with pytest.raises(CadProcessingError, match="no coinciden"):
        export_vector_outputs(flat, tmp_path / "c.svg", tmp_path / "c.pdf", omit_unsupported=True)
    assert not (tmp_path / "c.svg").exists()
    assert not (tmp_path / "c.pdf").exists()


def _model(unsupported: int) -> ExtractedDetail:
    return ExtractedDetail(
        source=DetailSource("entrada.dxf", "Detalle"),
        reference=SelectedReference(1, "A1", "0", (0, 0, 0), 0, (1, 1, 1)),
        units=UnitInfo(4, "mm", "declaradas en el DXF y coherentes", 1.0, ()),
        scale=None,
        bounds=None,
        inventory=(
            EntityCount("3DSOLID", "S", EntityStatus.UNSUPPORTED, unsupported),
            EntityCount("LINE", "0", EntityStatus.CONSERVED, 1),
        ),
        dispositions=tuple(
            EntityDisposition(index, None, "3DSOLID", "S", EntityStatus.UNSUPPORTED, "x")
            for index in range(unsupported)
        ),
        dependencies=(),
        warnings=(),
    )


def test_mark_converts_only_the_reported_quantity() -> None:
    marked = mark_omitted_entities(_model(3), (("3DSOLID", "S", 2),))

    assert [
        (row.status, row.count) for row in marked.inventory if row.entity_type == "3DSOLID"
    ] == [
        (EntityStatus.OMITTED, 2),
        (EntityStatus.UNSUPPORTED, 1),
    ]
    assert [item.status for item in marked.dispositions] == [
        EntityStatus.OMITTED,
        EntityStatus.OMITTED,
        EntityStatus.UNSUPPORTED,
    ]
    assert [warning.code for warning in marked.warnings] == ["UNSUPPORTED_OMITTED"]


def test_mark_reports_reconciliation_when_renderer_omits_more() -> None:
    marked = mark_omitted_entities(_model(1), (("3DSOLID", "S", 3),))

    codes = [warning.code for warning in marked.warnings]
    assert codes == ["UNSUPPORTED_OMITTED", "FIDELITY_RECONCILIATION"]
    assert "renderizador 3, modelo 1" in marked.warnings[1].message


def test_mark_without_omissions_returns_same_detail() -> None:
    detail = _model(1)

    assert mark_omitted_entities(detail, ()) is detail


def test_message_agreement_and_group_limit() -> None:
    many = omitted_entities_message((("3DSOLID", "PVcase PV Modules (detailed)", 17739),))
    groups = tuple((f"T{index:02}", "0", 1) for index in range(12))

    assert many.startswith("Se omitieron 17739 entidades no soportadas")
    assert many.endswith("no aparecen en el PDF.")
    assert "… y 2 grupos más" in omitted_entities_message(groups)
    assert "… y 1 grupo más" in omitted_entities_message(groups[:11])
    assert "T09" in omitted_entities_message(groups)
    assert "T10" not in omitted_entities_message(groups)


def test_terminal_line_formats() -> None:
    assert _omitted_terminal_line((("3DSOLID", "PVcase PV Modules (detailed)", 17739),)) == (
        "Aviso: se omitieron 17739 entidades no soportadas "
        "(3DSOLID en PVcase PV Modules (detailed)); no aparecen en el PDF."
    )
    line = _omitted_terminal_line((("A", "1", 1), ("B", "2", 2), ("C", "3", 3), ("D", "4", 4)))
    assert line == (
        "Aviso: se omitieron 10 entidades no soportadas "
        "(A en 1: 1; B en 2: 2; C en 3: 3; y 1 grupo más); no aparecen en el PDF."
    )
