import ezdxf
import pytest

from cde_exporter.cad import block_commands
from cde_exporter.cad.detail_model import ValidationCheck
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cli import main


def _source(path):
    document = ezdxf.new()
    document.units = ezdxf.units.InsertUnits.Millimeters
    document.blocks.new("Detalle").add_line((0, 0), (10, 5))
    document.modelspace().add_blockref("Detalle", (0, 0))
    document.saveas(path)
    return path


def _arguments(source, prefix, *extra):
    return [
        "cad", "extract-block", str(source), "--block-name", "Detalle",
        "--scale", "1:50", "--output-prefix", str(prefix), *extra,
    ]


def _names(folder):
    return sorted(item.name for item in folder.iterdir())


def _fail_export(*args, **kwargs):
    raise CadProcessingError("fallo simulado de la exportación")


def test_successful_extraction_leaves_only_pdf_and_txt(tmp_path, monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)
    source = _source(tmp_path / "origen.dxf")
    out = tmp_path / "salida"

    assert main(_arguments(source, out / "detalle")) == 0

    assert _names(out) == ["detalle.pdf", "detalle.txt"]
    report = (out / "detalle.txt").read_text(encoding="utf-8")
    assert "INFORME DE EXTRACCIÓN Y FIDELIDAD" in report
    assert "Escala nominal: 1:50" in report
    assert "Estado final: COMPLETA" in report
    assert "[OK] PDF legible" in report


def test_development_option_keeps_dxf_and_svg(tmp_path, monkeypatch):
    monkeypatch.setenv("CDE_KEEP_INTERMEDIATES", "1")
    source = _source(tmp_path / "origen.dxf")
    out = tmp_path / "salida"

    assert main(_arguments(source, out / "detalle")) == 0

    assert _names(out) == ["detalle.dxf", "detalle.pdf", "detalle.svg", "detalle.txt"]


def test_failed_export_leaves_no_files(tmp_path, monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)
    monkeypatch.setattr(block_commands, "export_vector_outputs", _fail_export)
    source = _source(tmp_path / "origen.dxf")
    out = tmp_path / "salida"

    assert main(_arguments(source, out / "detalle")) == 1

    assert _names(out) == []


def test_failed_pdf_validation_publishes_nothing(tmp_path, monkeypatch):
    failed = ValidationCheck("Una sola página", False, "Se encontraron dos páginas.")
    monkeypatch.setattr(
        block_commands, "validate_output_pdf", lambda path, expected: (failed,)
    )
    source = _source(tmp_path / "origen.dxf")
    out = tmp_path / "salida"

    assert main(_arguments(source, out / "detalle")) == 1

    assert _names(out) == []


def test_failed_extraction_with_overwrite_keeps_previous_files(tmp_path, monkeypatch):
    monkeypatch.setattr(block_commands, "export_vector_outputs", _fail_export)
    source = _source(tmp_path / "origen.dxf")
    out = tmp_path / "salida"
    out.mkdir()
    (out / "detalle.pdf").write_text("ANTERIOR", encoding="utf-8")
    (out / "detalle.txt").write_text("ANTERIOR", encoding="utf-8")

    assert main(_arguments(source, out / "detalle", "--overwrite")) == 1

    assert _names(out) == ["detalle.pdf", "detalle.txt"]
    assert (out / "detalle.pdf").read_text(encoding="utf-8") == "ANTERIOR"
    assert (out / "detalle.txt").read_text(encoding="utf-8") == "ANTERIOR"


def test_existing_output_without_overwrite_is_not_touched(tmp_path):
    source = _source(tmp_path / "origen.dxf")
    out = tmp_path / "salida"
    out.mkdir()
    (out / "detalle.pdf").write_text("ANTERIOR", encoding="utf-8")

    assert main(_arguments(source, out / "detalle")) == 1

    assert _names(out) == ["detalle.pdf"]
    assert (out / "detalle.pdf").read_text(encoding="utf-8") == "ANTERIOR"


def test_overwrite_replaces_both_files(tmp_path):
    source = _source(tmp_path / "origen.dxf")
    out = tmp_path / "salida"
    out.mkdir()
    (out / "detalle.pdf").write_text("ANTERIOR", encoding="utf-8")
    (out / "detalle.txt").write_text("ANTERIOR", encoding="utf-8")

    assert main(_arguments(source, out / "detalle", "--overwrite")) == 0

    assert _names(out) == ["detalle.pdf", "detalle.txt"]
    assert (out / "detalle.txt").read_text(encoding="utf-8") != "ANTERIOR"


def test_publish_files_rolls_back_when_second_destination_exists(tmp_path):
    first_staged = tmp_path / "a.tmp"
    second_staged = tmp_path / "b.tmp"
    first_staged.write_text("A", encoding="utf-8")
    second_staged.write_text("B", encoding="utf-8")
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    second.write_text("ANTERIOR", encoding="utf-8")

    with pytest.raises(CadProcessingError):
        block_commands._publish_files(
            [(first_staged, first), (second_staged, second)], overwrite=False
        )

    assert not first.exists()
    assert second.read_text(encoding="utf-8") == "ANTERIOR"