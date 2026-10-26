"""Entidades con datos inválidos: sin traceback, medición saltándolas y trato como no soportadas.

El caso real (VIA_2MA.dxf) es un LEADER cuyo override de DIMSTYLE da como estilo de texto
el handle de un XRECORD; ezdxf falla al leer su nombre.
"""

import hashlib
import subprocess
import sys
from pathlib import Path

import ezdxf
import pytest
from ezdxf import bbox
from ezdxf.addons.drawing import RenderContext
from ezdxf.addons.drawing import svg as svg_backend

from cde_exporter.cad import block_commands
from cde_exporter.cad.block_extractor import extract_block_reference
from cde_exporter.cad.exporters import _StrictFrontend
from cde_exporter.cad.reader import safe_extents
from cde_exporter.cli import main


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)
    monkeypatch.delenv("CDE_DEBUG", raising=False)


def _source(path: Path, *, repairable: bool) -> Path:
    """VIA_MA > PRUEBA_2_1 con un LEADER cuyo DIMTXSTY apunta a un XRECORD.

    Con ``repairable`` el XRECORD tiene un handle que no existe en el DXF extraído y, al
    copiar, ezdxf lo deja en '0' (como en VIA_2MA.dxf). Si no, el handle acaba apuntando a
    otro objeto que tampoco es un estilo de texto y el LEADER sigue fallando al extraer.
    """
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Millimeters
    if repairable:
        document.entitydb.handles.reset("ABCDE")
    xrecord = document.objects.add_xrecord(document.rootdict.dxf.handle)
    inner = document.blocks.new("PRUEBA_2_1")
    inner.add_line((0, 0), (10, 5))
    leader = inner.add_leader([(0, 0), (5, 5), (8, 5)], dxfattribs={"layer": "C-ROAD-INTS-TEXT"})
    leader.set_xdata(
        "ACAD",
        [(1000, "DSTYLE"), (1002, "{"), (1070, 340), (1005, xrecord.dxf.handle), (1002, "}")],
    )
    outer = document.blocks.new("VIA_MA")
    outer.add_line((0, 0), (1, 1))
    outer.add_blockref("PRUEBA_2_1", (20, 0))
    document.modelspace().add_blockref("VIA_MA", (0, 0))
    document.saveas(path)
    return path


def _leader_handle(path: Path) -> str:
    document = ezdxf.readfile(path)
    return next(e.dxf.handle for e in document.blocks.get("PRUEBA_2_1") if e.dxftype() == "LEADER")


def _arguments(source: Path, out: Path, *extra: str) -> list[str]:
    return [
        "cad",
        "extract-block",
        str(source),
        "--block-name",
        "VIA_MA",
        "--scale",
        "1:50",
        "--output-prefix",
        str(out / "detalle"),
        *extra,
    ]


def _names(folder: Path) -> list[str]:
    return sorted(item.name for item in folder.iterdir()) if folder.exists() else []


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_fixture_reproduces_the_source_failure(tmp_path: Path) -> None:
    source = _source(tmp_path / "origen.dxf", repairable=True)

    with pytest.raises(ezdxf.DXFAttributeError, match="XRECORD"):
        bbox.extents(ezdxf.readfile(source).blocks.get("VIA_MA"))


def test_repaired_entity_is_excluded_from_measurement_and_drawn(tmp_path: Path, capsys) -> None:
    source = _source(tmp_path / "origen.dxf", repairable=True)
    out = tmp_path / "salida"
    handle = _leader_handle(source)

    assert main(_arguments(source, out)) == 0

    assert _names(out) == ["detalle.pdf", "detalle.txt"]
    expected = (
        f"Se excluyó de la medición 1 entidad con datos inválidos: LEADER {handle} "
        "(capa C-ROAD-INTS-TEXT, bloque PRUEBA_2_1). Se dibuja con el estilo de texto "
        "por defecto."
    )
    captured = capsys.readouterr()
    assert f"Aviso: {expected}" in captured.err
    assert "Traceback" not in captured.err
    report = (out / "detalle.txt").read_text(encoding="utf-8")
    assert expected in _flat(report.split("A tener en cuenta", 1)[1])
    assert "LEADER | C-ROAD-INTS-TEXT | conservadas | 1" in report
    assert "no aparece en el PDF" not in report
    # El LEADER solo falla en el original: en lo que se dibuja sí tiene caja.
    assert "entidades sin caja" not in _flat(report)
    assert "entidades sin caja" not in captured.err


def _assert_still_invalid_after_extraction(tmp_path: Path, source: Path) -> None:
    target = tmp_path / "comprobacion.dxf"
    extract_block_reference(source, "VIA_MA", 1, target, overwrite=True)
    with pytest.raises(Exception):  # noqa: B017 - precondición: el extraído sigue roto
        bbox.extents(ezdxf.readfile(target).modelspace())


def test_invalid_entity_stops_with_clear_message_without_option(tmp_path: Path, capsys) -> None:
    source = _source(tmp_path / "origen.dxf", repairable=False)
    _assert_still_invalid_after_extraction(tmp_path, source)
    out = tmp_path / "salida"

    assert main(_arguments(source, out)) == 1

    message = _flat(capsys.readouterr().err)
    assert "LEADER (handle " in message
    assert "capa C-ROAD-INTS-TEXT): datos inválidos: DXFAttributeError" in message
    assert "Puede usar --omit-unsupported" in message
    assert "Traceback" not in message
    assert _names(out) == []


def test_invalid_entity_is_omitted_with_option_and_inventory_agrees(tmp_path: Path, capsys) -> None:
    source = _source(tmp_path / "origen.dxf", repairable=False)
    _assert_still_invalid_after_extraction(tmp_path, source)
    out = tmp_path / "salida"

    assert main(_arguments(source, out, "--omit-unsupported")) == 0

    assert _names(out) == ["detalle.pdf", "detalle.txt"]
    report = (out / "detalle.txt").read_text(encoding="utf-8")
    assert "LEADER | C-ROAD-INTS-TEXT | omitidas | 1" in report
    assert "Omitida por --omit-unsupported: datos inválidos: DXFAttributeError" in _flat(report)
    assert "FIDELITY_RECONCILIATION" not in report
    notes = _flat(report.split("A tener en cuenta", 1)[1])
    assert "Se omitió 1 entidad no soportada (--omit-unsupported): LEADER" in notes
    assert "No se pudo dibujar y se omitió (--omit-unsupported)." in notes
    assert "Se dibuja con" not in notes
    assert "Traceback" not in capsys.readouterr().err


def test_unexpected_error_shows_message_without_traceback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    def broken(*_args, **_kwargs):
        raise KeyError("dato interno")

    monkeypatch.setattr(block_commands, "write_fidelity_report", broken)
    source = _source(tmp_path / "origen.dxf", repairable=True)
    out = tmp_path / "salida"

    assert main(_arguments(source, out)) == 1

    message = capsys.readouterr().err
    assert "Error CAD inesperado (KeyError): 'dato interno'. No se generaron archivos." in message
    assert "CDE_DEBUG=1" in message
    assert "Traceback" not in message
    assert _names(out) == []


def test_cde_debug_shows_the_traceback(tmp_path: Path, monkeypatch, capsys) -> None:
    def broken(*_args, **_kwargs):
        raise KeyError("dato interno")

    monkeypatch.setattr(block_commands, "write_fidelity_report", broken)
    monkeypatch.setenv("CDE_DEBUG", "1")

    assert main(_arguments(_source(tmp_path / "origen.dxf", repairable=True), tmp_path / "s")) == 1

    assert "Traceback" in capsys.readouterr().err


def test_real_process_prints_no_traceback(tmp_path: Path) -> None:
    # Fuera de pytest (el proceso real): un error inesperado tampoco muestra traceback.
    source = _source(tmp_path / "origen.dxf", repairable=True)
    code = (
        "import sys\n"
        "from cde_exporter.cad import block_commands\n"
        "def broken(*a, **k):\n"
        "    raise KeyError('dato interno')\n"
        "block_commands.write_fidelity_report = broken\n"
        "from cde_exporter.cli import main\n"
        "sys.exit(main(sys.argv[1:]))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, *_arguments(source, tmp_path / "salida")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )

    assert completed.returncode == 1
    assert "Traceback" not in completed.stderr
    assert "Error CAD inesperado (KeyError)" in completed.stderr


def test_source_dxf_is_not_modified(tmp_path: Path) -> None:
    source = _source(tmp_path / "origen.dxf", repairable=True)
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    assert main(_arguments(source, tmp_path / "salida")) == 0

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_safe_extents_matches_bbox_when_nothing_fails(tmp_path: Path) -> None:
    document = ezdxf.new()
    block = document.blocks.new("B")
    block.add_line((0, 0), (10, 5))
    block.add_circle((3, 3), 2)
    reference = document.modelspace().add_blockref("B", (7, 1), dxfattribs={"rotation": 30})

    box, invalid = safe_extents(document.modelspace())
    expected = bbox.extents([reference])

    assert invalid == ()
    assert box.extmin.isclose(expected.extmin) and box.extmax.isclose(expected.extmax)


def test_safe_extents_skips_only_the_invalid_nested_entity(tmp_path: Path) -> None:
    source = _source(tmp_path / "origen.dxf", repairable=True)
    document = ezdxf.readfile(source)
    handle = _leader_handle(source)

    box, invalid = safe_extents(document.blocks.get("VIA_MA"), block="VIA_MA")

    assert [(item.entity_type, item.handle, item.layer, item.block) for item in invalid] == [
        ("LEADER", handle, "C-ROAD-INTS-TEXT", "PRUEBA_2_1")
    ]
    assert "XRECORD" in invalid[0].reason
    # La línea de 0,0 a 1,1 y la de PRUEBA_2_1 desplazada a x=20 siguen midiéndose.
    assert box.extmin.isclose((0, 0, 0))
    assert box.extmax.isclose((30, 5, 0))


def test_failed_entity_leaves_no_partial_strokes(monkeypatch) -> None:
    from ezdxf.addons.drawing.frontend import Frontend

    document = ezdxf.new()
    document.modelspace().add_line((0, 0), (1, 1))
    document.modelspace().add_line((5, 5), (6, 6), dxfattribs={"layer": "ROTA"})
    original = Frontend.draw_line_entity

    def draw_then_fail(self, entity, properties):
        original(self, entity, properties)
        if entity.dxf.layer == "ROTA":
            raise ValueError("datos rotos a mitad del dibujo")

    monkeypatch.setattr(Frontend, "draw_line_entity", draw_then_fail)
    backend = svg_backend.SVGBackend()
    frontend = _StrictFrontend(RenderContext(document), backend, omit_unsupported=True)

    frontend.draw_layout(document.modelspace())

    assert len(backend.records) == 1
    assert frontend.omitted == {("LINE", "ROTA"): 1}
