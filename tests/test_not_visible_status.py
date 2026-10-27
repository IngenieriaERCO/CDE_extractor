"""Estado «no visibles» del inventario: mismas reglas de visibilidad que el exportador."""

from pathlib import Path

import ezdxf
import pytest

from cde_exporter.cad import block_commands
from cde_exporter.cad.exporters import export_vector_outputs
from cde_exporter.cad.fidelity import (
    hidden_skip_events,
    inspect_fidelity,
    iter_detail_entities_with_visibility,
)
from cde_exporter.cli import main

# Un 3DSOLID sin datos ACIS no se guarda en el DXF; R2000 admite SAT en texto.
_SAT = ["400 0 1 0", "@33 Spatial Corp.", "1 9.9999999999999995e-07 1e-10", "End-of-ACIS-data"]


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def _document(version: str = "R2018"):
    document = ezdxf.new(version)
    document.units = ezdxf.units.InsertUnits.Millimeters
    document.layers.add("Apagada").off()
    document.layers.add("Congelada").freeze()
    return document


def _save(document, path: Path) -> Path:
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


def _report(out: Path) -> str:
    return (out / "detalle.txt").read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _states(document) -> list[tuple[str, str, bool, bool]]:
    return [
        (item.entity.dxftype(), item.entity.dxf.layer, item.visible, item.hidden_by_ancestor)
        for item in iter_detail_entities_with_visibility(document)
    ]


def test_hidden_layers_and_invisible_flag_show_up_as_not_visible(tmp_path: Path) -> None:
    document = _document()
    block = document.blocks.new("Detalle")
    block.add_line((0, 0), (10, 5))
    block.add_line((0, 1), (10, 1), dxfattribs={"layer": "Apagada"})
    block.add_line((0, 2), (10, 2), dxfattribs={"layer": "Congelada"})
    block.add_line((0, 3), (10, 3), dxfattribs={"invisible": 1})
    out = tmp_path / "salida"

    assert _run(_save(document, tmp_path / "o.dxf"), out) == 0

    report = _report(out)
    assert "No visibles: 3" in report
    assert "Conservadas: 2" in report  # la línea visible y el INSERT
    assert "LINE | Apagada | no visibles | 1" in report
    assert "LINE | Congelada | no visibles | 1" in report
    assert "LINE | 0 | no visibles | 1" in report
    flat = _flat(report)
    assert (
        "LINE | Apagada | no visibles | No visible en el CAD: capa apagada o congelada, o "
        "entidad marcada como invisible. | 1"
    ) in flat
    assert "FIDELITY_RECONCILIATION" not in report
    # El aviso del exportador sigue, y cuenta lo mismo.
    assert "Se ignoraron 3 entidades no visibles" in flat


def test_not_visible_is_only_printed_when_there_are_some(tmp_path: Path) -> None:
    document = _document()
    document.blocks.new("Detalle").add_line((0, 0), (10, 5))
    out = tmp_path / "salida"

    assert _run(_save(document, tmp_path / "o.dxf"), out) == 0

    report = _report(out)
    assert "No visibles:" not in report
    assert "| no visibles |" not in report
    assert "Total de entidades: 2" in report


def test_contents_of_an_invisible_insert_are_not_visible_too(tmp_path: Path) -> None:
    document = _document()
    inner = document.blocks.new("Interior")
    inner.add_line((0, 0), (1, 1))
    inner.add_line((0, 1), (1, 0))
    block = document.blocks.new("Detalle")
    block.add_line((0, 0), (10, 5))
    block.add_blockref("Interior", (2, 2), dxfattribs={"invisible": 1})
    out = tmp_path / "salida"

    assert _run(_save(document, tmp_path / "o.dxf"), out) == 0

    report = _report(out)
    assert "INSERT | 0 | no visibles | 1" in report
    assert "LINE | 0 | no visibles | 2" in report
    assert "Total de entidades: 5" in report  # INSERT externo, línea, INSERT oculto, 2 líneas
    flat = _flat(report)
    assert (
        "LINE | 0 | no visibles | No visible en el CAD: está dentro de un INSERT marcado como "
        "invisible. | 2"
    ) in flat
    # ezdxf descarta solo el INSERT: sus líneas ni se recorren, y el aviso cuenta 1.
    assert "Se ignoró 1 entidad no visible" in flat
    assert "FIDELITY_RECONCILIATION" not in report


def test_an_insert_on_a_frozen_layer_is_visible_but_its_layer_zero_content_is_not() -> None:
    # ezdxf: un INSERT solo depende de su indicador invisible; su contenido en la capa "0"
    # hereda la capa del INSERT.
    document = _document()
    inner = document.blocks.new("Interior")
    inner.add_line((0, 0), (1, 1))  # capa "0": hereda la capa congelada del INSERT
    inner.add_line((0, 1), (1, 0), dxfattribs={"layer": "Normal"})
    document.layers.add("Normal")
    block = document.blocks.new("Detalle")
    block.add_blockref("Interior", (2, 2), dxfattribs={"layer": "Congelada"})
    document.modelspace().add_blockref("Detalle", (0, 0))

    states = _states(document)

    assert states == [
        ("INSERT", "0", True, False),  # el INSERT externo
        ("INSERT", "Congelada", True, False),  # visible aunque su capa está congelada
        ("LINE", "0", False, False),  # hereda "Congelada"
        ("LINE", "Normal", True, False),
    ]


def test_hidden_solid_neither_stops_the_extraction_nor_counts_as_unsupported(
    tmp_path: Path,
) -> None:
    document = _document("R2000")
    block = document.blocks.new("Detalle")
    block.add_line((0, 0), (10, 5))
    block.add_3dsolid(dxfattribs={"layer": "Apagada"}).sat = _SAT
    out = tmp_path / "salida"

    assert _run(_save(document, tmp_path / "o.dxf"), out) == 0  # sin --omit-unsupported

    report = _report(out)
    assert "3DSOLID | Apagada | no visibles | 1" in report
    assert "No soportadas: 0" in report
    assert "FIDELITY_RECONCILIATION" not in report


def test_hidden_text_and_hatch_are_not_counted_as_drawn(tmp_path: Path) -> None:
    document = _document()
    document.styles.new("SinFuente", dxfattribs={"font": "fuente_que_no_existe_987.ttf"})
    modelspace = document.modelspace()
    modelspace.add_mtext("oculto", dxfattribs={"style": "SinFuente", "layer": "Apagada"})
    modelspace.add_text("visible", dxfattribs={"height": 1})
    hidden = modelspace.add_hatch(dxfattribs={"layer": "Apagada"})
    hidden.set_pattern_fill("ANSI31", scale=1)
    hidden.paths.add_polyline_path([(0, 0), (1, 0), (1, 1), (0, 1)], is_closed=True)
    path = tmp_path / "ocultos.dxf"
    document.saveas(path)

    report = inspect_fidelity(path, "auto")

    assert report.hidden_total == 2
    assert report.entity_total == 3
    assert report.hatches == {}
    assert sum(report.fonts.values()) == 1  # solo el texto visible
    assert all(family != "fuente_que_no_existe_987.ttf" for (_s, family, _r, _f) in report.fonts)


def test_face_with_every_edge_invisible_is_not_visible() -> None:
    document = _document()
    visible = document.modelspace().add_3dface([(0, 0), (1, 0), (1, 1), (0, 1)])
    hidden = document.modelspace().add_3dface([(2, 0), (3, 0), (3, 1), (2, 1)])
    hidden.dxf.invisible_edges = 15  # los cuatro bordes

    states = {
        item.entity.dxf.handle: item.visible
        for item in iter_detail_entities_with_visibility(document)
    }

    assert states[visible.dxf.handle] is True
    assert states[hidden.dxf.handle] is False


def test_an_insert_array_repeats_its_hidden_content_in_the_renderer_count(tmp_path: Path) -> None:
    document = _document()
    cell = document.blocks.new("Celda")
    cell.add_line((0, 0), (1, 1))
    cell.add_line((0, 1), (1, 0), dxfattribs={"layer": "Apagada"})
    array = document.modelspace().add_blockref("Celda", (0, 0))
    array.dxf.row_count = 2
    array.dxf.column_count = 3
    array.dxf.row_spacing = 2
    array.dxf.column_spacing = 2
    path = tmp_path / "matriz.dxf"
    document.saveas(path)
    loaded = ezdxf.readfile(path)

    report = export_vector_outputs(path, tmp_path / "m.svg", tmp_path / "m.pdf")

    assert hidden_skip_events(loaded) == 6  # una línea oculta por cada una de las 2×3 celdas
    assert report.invisible == 6


def test_reconciliation_warns_if_the_renderer_and_the_model_disagree(
    tmp_path: Path, monkeypatch
) -> None:
    document = _document()
    block = document.blocks.new("Detalle")
    block.add_line((0, 0), (10, 5))
    block.add_line((0, 1), (10, 1), dxfattribs={"layer": "Apagada"})
    real = block_commands.hidden_skip_events
    monkeypatch.setattr(block_commands, "hidden_skip_events", lambda doc: real(doc) + 1)
    out = tmp_path / "salida"

    assert _run(_save(document, tmp_path / "o.dxf"), out) == 0

    assert (
        "No coincide la conciliación de entidades no visibles (modelo 2, renderizador 1)."
        in _flat(_report(out))
    )
