from pathlib import Path

import ezdxf
import pytest

from cde_exporter.cad import export_vector_outputs
from cde_exporter.cad.exporters import _invisible_warning
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cli import main


def _drawing(path: Path, *, off: int = 0, frozen: int = 0, tiny_text: bool = False) -> Path:
    document = ezdxf.new()
    document.layers.add("Apagada").off()
    document.layers.add("Congelada").freeze()
    modelspace = document.modelspace()
    modelspace.add_line((0, 0), (10, 5))
    for index in range(off):
        modelspace.add_line((0, index), (10, index), dxfattribs={"layer": "Apagada"})
    for index in range(frozen):
        modelspace.add_line((0, index), (5, index), dxfattribs={"layer": "Congelada"})
    if tiny_text:
        modelspace.add_text("x", height=1e-12)
    document.saveas(path)
    return path


def _export(tmp_path: Path, source: Path):
    return export_vector_outputs(source, tmp_path / "out.svg", tmp_path / "out.pdf")


def test_invisible_entities_do_not_stop_export_and_are_counted_once(tmp_path: Path) -> None:
    source = _drawing(tmp_path / "ocultas.dxf", off=1, frozen=1)

    report = _export(tmp_path, source)

    assert (tmp_path / "out.svg").exists()
    assert (tmp_path / "out.pdf").exists()
    invisible = [item for item in report.warnings if "no visible" in item]
    assert invisible == [_invisible_warning(2)]
    assert invisible[0].startswith("Se ignoraron 2 entidades no visibles")
    assert "no aparecen en el PDF porque tampoco se ven en el CAD" in invisible[0]


def test_single_invisible_entity_uses_singular(tmp_path: Path) -> None:
    report = _export(tmp_path, _drawing(tmp_path / "una.dxf", off=1))

    assert _invisible_warning(1) in report.warnings
    assert _invisible_warning(1).startswith("Se ignoró 1 entidad no visible")
    assert "no aparece en el PDF porque tampoco se ve en el CAD" in _invisible_warning(1)


def test_plural_warning_for_many_entities() -> None:
    warning = _invisible_warning(17739)

    assert warning.startswith("Se ignoraron 17739 entidades no visibles")
    assert "entidades marcadas como invisibles" in warning
    assert "no aparecen en el PDF porque tampoco se ven en el CAD" in warning


def test_visible_drawing_has_no_invisible_warning(tmp_path: Path) -> None:
    report = _export(tmp_path, _drawing(tmp_path / "visible.dxf"))

    assert not any("no visible" in item for item in report.warnings)


def test_other_omissions_still_stop_export(tmp_path: Path) -> None:
    source = _drawing(tmp_path / "mixto.dxf", off=1, tiny_text=True)

    with pytest.raises(CadProcessingError, match="text height too small") as error:
        _export(tmp_path, source)

    assert "invisible" not in str(error.value)
    assert not (tmp_path / "out.svg").exists()
    assert not (tmp_path / "out.pdf").exists()


def test_extract_block_reports_invisible_entities_in_txt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)
    document = ezdxf.new()
    document.units = ezdxf.units.InsertUnits.Millimeters
    document.layers.add("Apagada").off()
    block = document.blocks.new("Detalle")
    block.add_line((0, 0), (10, 5))
    block.add_line((0, 5), (10, 0), dxfattribs={"layer": "Apagada"})
    document.modelspace().add_blockref("Detalle", (0, 0))
    source = tmp_path / "origen.dxf"
    document.saveas(source)
    out = tmp_path / "salida"

    assert (
        main(
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
            ]
        )
        == 0
    )

    assert sorted(item.name for item in out.iterdir()) == ["detalle.pdf", "detalle.txt"]
    report = (out / "detalle.txt").read_text(encoding="utf-8")
    notes = report.split("A tener en cuenta", 1)[1]
    assert "Se ignoró 1 entidad no visible" in " ".join(notes.split())
    assert "Estado final: COMPLETA CON ADVERTENCIAS" in report
