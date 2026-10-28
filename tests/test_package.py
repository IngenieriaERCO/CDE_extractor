"""Paquete publicado (Fase 4, pasos 5 y 6): avisos críticos, orden, todo o nada y coherencia."""

import json
from pathlib import Path

import ezdxf
import pytest

from cde_exporter.cad import block_commands
from cde_exporter.cad.detail_model import (
    ADVISORY_CHECKS,
    BLOCKING_CHECKS,
    MANIFEST_CHECK,
    check_blocks,
)
from cde_exporter.cad.manifest import verify_package
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.pdf_checks import validate_output_pdf
from cde_exporter.cli import main


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def _source(path: Path, *, count: int = 1, xref: bool = False, missing_block: bool = False):
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    block = document.blocks.new("DET")
    block.add_line((0, 0), (10, 0))
    block.add_line((0, 0), (0, 4))
    if xref:
        document.blocks.new(
            "Topografia",
            dxfattribs={"flags": ezdxf.entities.Block.XREF, "xref_path": "topografia_base.dxf"},
        )
        block.add_blockref("Topografia", (0, 0))
    if missing_block:
        block.add_blockref("BloqueAusente", (0, 0))
    for index in range(count):
        document.modelspace().add_blockref("DET", (index * 20, 0))
    document.saveas(path)
    return path


def _extract(source: Path, out: Path, *extra: str) -> int:
    return main(
        [
            "cad",
            "extract-block",
            str(source),
            "--block-name",
            "DET",
            "--scale",
            "1:50",
            "--output-prefix",
            str(out / "detalle"),
            *extra,
        ]
    )


def _names(folder: Path) -> list[str]:
    return sorted(item.name for item in folder.iterdir()) if folder.exists() else []


def _never_draw(*args, **kwargs):
    raise AssertionError("no debería dibujarse: el aviso crítico se conoce antes")


# --------------------------------------------------------------------------- paso 5


def test_unresolved_xref_blocks_publication_with_a_clear_message(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(block_commands, "export_vector_outputs", _never_draw)
    out = tmp_path / "salida"

    assert _extract(_source(tmp_path / "origen.dxf", xref=True), out) == 1

    assert _names(out) == []
    error = " ".join(capsys.readouterr().err.split())
    assert "La extracción se detuvo por 1 aviso crítico" in error
    assert "Dependencia xref no resuelta (topografia_base.dxf)" in error
    assert "BIND en AutoCAD" in error
    assert error.endswith("No se generaron archivos.")


def test_missing_block_definition_blocks_publication(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(block_commands, "export_vector_outputs", _never_draw)
    out = tmp_path / "salida"

    assert _extract(_source(tmp_path / "origen.dxf", missing_block=True), out) == 1

    assert _names(out) == []
    error = " ".join(capsys.readouterr().err.split())
    assert "BloqueAusente" in error
    assert "AUDIT" in error


def test_every_check_is_explicitly_blocking_or_advisory(tmp_path: Path) -> None:
    assert not BLOCKING_CHECKS & ADVISORY_CHECKS
    assert MANIFEST_CHECK in BLOCKING_CHECKS
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"no es un PDF")
    # Todas las comprobaciones que puede devolver la validación del PDF están clasificadas.
    checks = validate_output_pdf(pdf, (10.0, 10.0), ("texto",), (1.0, (1.0, 1.0), 50))
    for check in checks:
        check_blocks(check.name)
    with pytest.raises(ValueError, match="sin clasificar"):
        check_blocks("Comprobación inventada")


# --------------------------------------------------------------------------- paso 6


def test_manifests_are_published_last(tmp_path: Path, monkeypatch) -> None:
    published: list[str] = []
    original = block_commands._publish_files

    def record(pairs, overwrite):
        published.extend(destination.name for _staged, destination in pairs)
        return original(pairs, overwrite)

    monkeypatch.setattr(block_commands, "_publish_files", record)
    out = tmp_path / "salida"

    assert _extract(_source(tmp_path / "origen.dxf", count=2), out, "--all") == 0

    assert published == [
        "detalle_001.pdf",
        "detalle_001.txt",
        "detalle_002.pdf",
        "detalle_002.txt",
        "detalle_001.manifest.json",
        "detalle_002.manifest.json",
    ]
    for name in ("detalle_001.pdf", "detalle_002.pdf"):
        assert verify_package(out / name) == ()


def test_all_is_all_or_nothing(tmp_path: Path, monkeypatch) -> None:
    calls = []
    original = block_commands.export_vector_outputs

    def fail_on_second(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise CadProcessingError("fallo simulado en la segunda referencia")
        return original(*args, **kwargs)

    monkeypatch.setattr(block_commands, "export_vector_outputs", fail_on_second)
    out = tmp_path / "salida"

    assert _extract(_source(tmp_path / "origen.dxf", count=2), out, "--all") == 1

    # Ni la primera referencia ni carpetas de trabajo: nada.
    assert len(calls) == 2
    assert _names(out) == []


def test_incoherent_manifest_blocks_publication(tmp_path: Path, monkeypatch, capsys) -> None:
    original = block_commands.build_manifest

    def wrong_hash(*args, **kwargs):
        manifest = original(*args, **kwargs)
        manifest["pdf"]["sha256"] = "0" * 64
        return manifest

    monkeypatch.setattr(block_commands, "build_manifest", wrong_hash)
    out = tmp_path / "salida"

    assert _extract(_source(tmp_path / "origen.dxf"), out) == 1

    assert _names(out) == []
    error = " ".join(capsys.readouterr().err.split())
    assert "Manifiesto coherente con el PDF" in error
    assert "SHA-256" in error


def test_package_without_manifest_or_with_another_pdf_is_recognized(tmp_path: Path, capsys) -> None:
    out = tmp_path / "salida"
    assert _extract(_source(tmp_path / "origen.dxf"), out) == 0
    pdf, manifest = out / "detalle.pdf", out / "detalle.manifest.json"
    assert verify_package(pdf) == ()
    assert main(["cad", "verify-package", str(pdf)]) == 0

    # Otro PDF con el mismo nombre (p. ej. reemplazado sin su manifiesto): no coincide.
    pdf.write_bytes(pdf.read_bytes() + b"\n%otro")
    assert any("SHA-256" in problem for problem in verify_package(manifest))
    # Un corte antes de publicar el manifiesto: el PDF solo es un paquete incompleto.
    manifest.unlink()
    assert verify_package(pdf) == (
        "Paquete incompleto: falta el manifiesto detalle.manifest.json.",
    )
    capsys.readouterr()
    assert main(["cad", "verify-package", str(pdf)]) == 1
    assert "Paquete incompleto" in capsys.readouterr().err


def test_published_manifest_is_the_one_validated(tmp_path: Path) -> None:
    out = tmp_path / "salida"

    assert _extract(_source(tmp_path / "origen.dxf"), out) == 0

    text = (out / "detalle.manifest.json").read_text(encoding="utf-8")
    data = json.loads(text)
    assert data["pdf"]["file_name"] == "detalle.pdf"
    assert data["source"]["dxf_name"] == "origen.dxf"
    assert "\\" not in text
    assert "\r" not in text
    report = (out / "detalle.txt").read_text(encoding="utf-8")
    assert "Manifiesto: detalle.manifest.json" in report
