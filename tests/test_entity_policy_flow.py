"""La tabla de política en el flujo real: MULTILEADER y REGION en el inventario y al dibujar."""

from pathlib import Path

import ezdxf
import pymupdf
import pytest
from ezdxf.addons.drawing import RenderContext
from ezdxf.addons.drawing import svg as svg_backend
from ezdxf.math import Vec2
from ezdxf.render.mleader import ConnectionSide

from cde_exporter.cad.entity_policy import EntityFacts, decide
from cde_exporter.cad.exporters import _StrictFrontend
from cde_exporter.cad.fidelity import entity_facts, fidelity_dispatch
from cde_exporter.cli import main

# Un REGION sin datos ACIS no se guarda en el DXF; R2000 admite SAT en texto.
_SAT = ["400 0 1 0", "@33 Spatial Corp.", "1 9.9999999999999995e-07 1e-10", "End-of-ACIS-data"]


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def _add_multileader(layout) -> None:
    builder = layout.add_multileader_mtext("Standard")
    builder.set_content("Nota")
    builder.add_leader_line(ConnectionSide.left, [(0, 0), (3, 3)])
    builder.build(insert=Vec2(10, 10))


def _source(path: Path, *, multileader: bool = False, region: bool = False) -> Path:
    document = ezdxf.new("R2000" if region else "R2018")
    document.units = ezdxf.units.InsertUnits.Millimeters
    block = document.blocks.new("DET")
    block.add_line((0, 0), (20, 10))
    if multileader:
        _add_multileader(block)
    if region:
        block.new_entity("REGION", {"layer": "REGIONES"}).sat = _SAT
    document.modelspace().add_blockref("DET", (0, 0))
    document.saveas(path)
    return path


def _run(source: Path, out: Path, *extra: str) -> int:
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


def _flat(text: str) -> str:
    return " ".join(text.split())


def _drawings(pdf: Path) -> int:
    with pymupdf.open(pdf) as document:
        return len(document[0].get_drawings())


def test_multileader_with_data_is_conserved_and_really_drawn(tmp_path: Path) -> None:
    plain = tmp_path / "sin"
    assert _run(_source(tmp_path / "a.dxf"), plain) == 0
    out = tmp_path / "con"

    assert _run(_source(tmp_path / "b.dxf", multileader=True), out) == 0

    report = (out / "detalle.txt").read_text(encoding="utf-8")
    assert "MULTILEADER | 0 | conservadas | 1" in report
    assert "No soportadas: 0" in report
    assert "FIDELITY_RECONCILIATION" not in report
    # De verdad se dibuja: el PDF tiene más trazos que el del bloque sin MULTILEADER.
    assert _drawings(out / "detalle.pdf") > _drawings(plain / "detalle.pdf")


def _document_with_multileaders():
    """Un MULTILEADER con datos y otro sin ninguno (no se puede guardar en un DXF)."""
    document = ezdxf.new("R2018")
    modelspace = document.modelspace()
    _add_multileader(modelspace)
    empty = modelspace.new_entity("MULTILEADER", {"layer": "VACIA"})
    good = next(entity for entity in modelspace if entity is not empty)
    return document, good, empty


def test_multileader_facts_distinguish_data_from_no_geometry() -> None:
    document, good, empty = _document_with_multileaders()
    dispatch = fidelity_dispatch(document)

    drawn = decide(entity_facts(good, dispatch, document))
    undrawn = decide(entity_facts(empty, dispatch, document))

    assert drawn.status.value == "conserved"
    assert undrawn.status.value == "unsupported" and undrawn.cause == "sin geometría dibujable"


def test_multileader_without_geometry_is_not_silently_dropped_by_the_renderer() -> None:
    document, _good, _empty = _document_with_multileaders()
    modelspace = document.modelspace()

    strict = _StrictFrontend(RenderContext(document), svg_backend.SVGBackend())
    strict.draw_layout(modelspace)
    omitting = _StrictFrontend(
        RenderContext(document), svg_backend.SVGBackend(), omit_unsupported=True
    )
    omitting.draw_layout(modelspace)

    assert strict.skipped == ["MULTILEADER: unsupported"]
    assert not omitting.skipped
    assert omitting.omitted == {("MULTILEADER", "VACIA"): 1}


def test_a_multileader_that_draws_is_not_reported_as_skipped() -> None:
    document = ezdxf.new("R2018")
    _add_multileader(document.modelspace())

    frontend = _StrictFrontend(RenderContext(document), svg_backend.SVGBackend())
    frontend.draw_layout(document.modelspace())

    assert frontend.skipped == [] and not frontend.omitted


def test_region_is_unsupported_with_the_omit_hint(tmp_path: Path, capsys) -> None:
    out = tmp_path / "salida"

    assert _run(_source(tmp_path / "o.dxf", region=True), out) == 1

    message = _flat(capsys.readouterr().err)
    assert "REGION: unsupported" in message
    assert "Puede usar --omit-unsupported en cad extract-block para continuar sin ellas." in message
    assert not out.exists() or not list(out.iterdir())


def test_region_is_omitted_with_the_option(tmp_path: Path) -> None:
    out = tmp_path / "salida"

    assert _run(_source(tmp_path / "o.dxf", region=True), out, "--omit-unsupported") == 0

    report = (out / "detalle.txt").read_text(encoding="utf-8")
    assert "REGION | REGIONES | omitidas | 1" in report
    assert "Omitida por --omit-unsupported: ezdxf no puede dibujar este tipo." in _flat(report)
    assert "FIDELITY_RECONCILIATION" not in report


def test_hidden_facts_never_reach_ezdxf() -> None:
    # Una entidad no visible no se examina: no hace falta ni su geometría.
    document = ezdxf.new("R2018")
    line = document.modelspace().add_line((0, 0), (1, 1))

    facts = entity_facts(line, fidelity_dispatch(document), document, visible=False)

    assert facts == EntityFacts("LINE", visible=False)
