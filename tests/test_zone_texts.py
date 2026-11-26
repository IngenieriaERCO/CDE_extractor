"""Textos de una zona y escala propuesta (orden ``texts``, protocolo 1.3;
docs/protocolo_trabajador.md, sección 4.3)."""

from pathlib import Path

import ezdxf
import pytest
from ezdxf.math import Vec2
from test_worker import running  # noqa: F401 - fixture del trabajador en el proceso
from test_worker_export import _final, _load

from cde_exporter.cad import worker_protocol as protocol
from cde_exporter.cad.scale_text import find_scales, propose
from cde_exporter.cad.zone_texts import TextBlocks, zone_texts

# ----------------------------------------------------------------- patrones de escala


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ESC 1:25", [("1:25", None)]),
        ("ESCALA: 1:25", [("1:25", None)]),
        ("ESC-1:10", [("1:10", None)]),
        ("esc. 1/50", [("1:50", None)]),
        ("Esc: 1:10", [("1:10", None)]),
        ("Escala 1 : 75.", [("1:75", None)]),
        ("SECCIÓN-V-F 0.30 X 0.15\nESC-1:10", [("1:10", None)]),
        ("ESCALA 1:1.000", [("1:1000", None)]),
        ("ESCALA 1:12.5", [("1:12.5", None)]),
        ("ESC 1:2,5", [("1:2.5", None)]),
        ("ESC 1:25 Y 1:50", [("1:25", None), ("1:50", None)]),
        ("ESC. H 1:1000 V 1:100", [("1:1000", "H"), ("1:100", "V")]),
        ("ESCALA HORIZONTAL 1:500", [("1:500", "H")]),
        ("ESC 1:25 / ESC 1:25", [("1:25", None)]),
    ],
)
def test_scale_patterns_are_found(text: str, expected: list) -> None:
    assert [(match.scale, match.axis) for match in find_scales(text)] == expected


@pytest.mark.parametrize(
    "text",
    ["PENDIENTE 1:2", "ESCALERA 1:20", "DESC 1:5", "1:25", "ESC 0:25", "ESCALA GRÁFICA", "S/E"],
)
def test_texts_without_a_written_scale_give_nothing(text: str) -> None:
    assert find_scales(text) == []


def _text(text: str, height: float = 1.0) -> dict:
    return {"text": text, "x": 0.0, "y": 0.0, "height": height, "layer": "T", "handle": "1"}


def test_a_single_scale_is_proposed_with_its_sources() -> None:
    candidates, proposed = propose([_text("PLANTA\nESC 1:25"), _text("CORTE ESCALA 1:25")])
    assert proposed == "1:25"
    assert [(c["scale"], c["count"]) for c in candidates] == [("1:25", 2)]
    assert candidates[0]["sources"][0]["text"] == "PLANTA\nESC 1:25"


def test_several_scales_are_all_offered_and_none_is_proposed() -> None:
    texts = [_text("A ESC 1:10"), _text("B ESC 1:10"), _text("C ESC 1:25", height=5)]
    candidates, proposed = propose(texts)
    assert proposed is None
    assert [(c["scale"], c["count"]) for c in candidates] == [("1:10", 2), ("1:25", 1)]
    # A igualdad de veces, primero la del texto más grande.
    candidates, _ = propose([_text("ESC 1:10"), _text("ESC 1:25", height=5)])
    assert [c["scale"] for c in candidates] == ["1:25", "1:10"]


def test_scales_with_an_axis_are_offered_but_not_proposed() -> None:
    candidates, proposed = propose([_text("PERFIL ESC. H 1:500")])
    assert proposed is None and candidates[0]["axis"] == "H"
    assert propose([_text("SIN ESCALA")]) == ([], None)


# ----------------------------------------------------------------- textos de la zona


def _source(path: Path) -> Path:
    """En (0..10): un TEXT en «TITULOS», un MTEXT en «NOTAS», un bloque con un TEXT en la capa
    «0» (toma la del INSERT) y otro en «COTAS», un ATTRIB, un MULTILEADER y un TEXT en una
    capa apagada; en (100..110), otro TEXT con otra escala."""
    document = ezdxf.new("R2018")
    for name in ("TITULOS", "NOTAS", "COTAS", "DETALLE", "APAGADA"):
        document.layers.add(name)
    document.layers.get("APAGADA").off()
    block = document.blocks.new("DET")
    block.add_text("DENTRO ESC 1:25", height=0.5, dxfattribs={"layer": "0", "insert": (1, 1)})
    block.add_text("COTA 1:25", height=0.5, dxfattribs={"layer": "COTAS", "insert": (1, 2)})
    block.add_attdef("TAG", (0, 0), dxfattribs={"height": 0.3})
    modelspace = document.modelspace()
    modelspace.add_text(
        "PLANTA ESC 1:25",
        height=0.4,
        rotation=90,
        dxfattribs={"layer": "TITULOS", "insert": (2, 9)},
    )
    modelspace.add_mtext(
        "SECCIÓN-V-F 0.30 X 0.15\\PESC-1:25",
        dxfattribs={"layer": "NOTAS", "insert": (5, 8), "char_height": 0.3},
    )
    insert = modelspace.add_blockref(
        "DET", (3, 3), dxfattribs={"layer": "DETALLE", "xscale": 2, "yscale": 2}
    )
    insert.add_attrib("TAG", "ATRIBUTO", (3, 2.5), dxfattribs={"height": 0.25, "layer": "0"})
    builder = modelspace.add_multileader_mtext("Standard", dxfattribs={"layer": "NOTAS"})
    builder.set_content("NOTA DE DIRECTRIZ", char_height=0.2)
    builder.add_leader_line(ezdxf.render.mleader.ConnectionSide.left, [(6, 2)])
    builder.build(insert=Vec2(7, 3))
    modelspace.add_text(
        "OCULTO ESC 1:5", height=0.3, dxfattribs={"layer": "APAGADA", "insert": (8, 8)}
    )
    modelspace.add_text(
        "OTRA ESC 1:50", height=0.3, dxfattribs={"layer": "TITULOS", "insert": (105, 5)}
    )
    document.saveas(path)
    return path


def _texts(path: Path, zone, **options) -> list[dict]:
    document = ezdxf.readfile(path)
    return zone_texts(document, document.modelspace(), zone, blocks=TextBlocks(document), **options)


def test_the_texts_of_a_zone_include_blocks_attributes_and_leaders(tmp_path: Path) -> None:
    texts = _texts(_source(tmp_path / "obra.dxf"), (0, 0, 10, 10))
    by_text = {item["text"]: item for item in texts}

    assert set(by_text) == {
        "PLANTA ESC 1:25",
        "SECCIÓN-V-F 0.30 X 0.15\nESC-1:25",
        "DENTRO ESC 1:25",
        "COTA 1:25",
        "ATRIBUTO",
        "NOTA DE DIRECTRIZ",
    }
    planta = by_text["PLANTA ESC 1:25"]
    assert (planta["x"], planta["y"], planta["rotation"]) == (2, 9, 90)
    assert planta["kind"] == "TEXT" and planta["layer"] == "TITULOS" and planta["blocks"] == []
    # Dentro del bloque: en coordenadas del Model Space, con la escala del INSERT, y la capa
    # «0» toma la del INSERT.
    inner = by_text["DENTRO ESC 1:25"]
    assert (inner["x"], inner["y"], inner["height"]) == (5, 5, 1)
    assert inner["layer"] == "DETALLE" and inner["blocks"] == ["DET"]
    assert inner["modelspace_handle"] != inner["handle"]
    assert by_text["ATRIBUTO"]["kind"] == "ATTRIB" and by_text["ATRIBUTO"]["layer"] == "DETALLE"
    assert by_text["NOTA DE DIRECTRIZ"]["kind"] == "MULTILEADER"
    assert by_text["SECCIÓN-V-F 0.30 X 0.15\nESC-1:25"]["kind"] == "MTEXT"
    # De arriba abajo.
    assert [item["y"] for item in texts] == sorted((item["y"] for item in texts), reverse=True)


def test_the_layer_filter_and_hidden_layers_apply_inside_blocks(tmp_path: Path) -> None:
    path = _source(tmp_path / "obra.dxf")
    names = {item["text"] for item in _texts(path, (0, 0, 10, 10), exclude_layers=("COTAS",))}
    assert "COTA 1:25" not in names and "DENTRO ESC 1:25" in names
    # Excluir la capa del INSERT quita el bloque entero (y sus atributos).
    names = {item["text"] for item in _texts(path, (0, 0, 10, 10), exclude_layers=("DETALLE",))}
    assert not names & {"DENTRO ESC 1:25", "COTA 1:25", "ATRIBUTO"}
    names = {item["text"] for item in _texts(path, (0, 0, 10, 10), layers=("TITULOS",))}
    assert names == {"PLANTA ESC 1:25"}
    # La capa apagada solo sale si se pide.
    names = {item["text"] for item in _texts(path, (0, 0, 10, 10), reveal=frozenset({"apagada"}))}
    assert "OCULTO ESC 1:5" in names


# ----------------------------------------------------------------- orden «texts»


def _ask(sink, stdin, request_id: str, session: str, **data) -> dict:
    stdin.send(id=request_id, command="texts", data={"session": session, **data})
    return _final(sink, request_id)


def test_the_texts_command_returns_texts_and_the_proposed_scale(
    running,  # noqa: F811
    tmp_path: Path,
) -> None:
    sink, stdin = running
    session = _load(sink, stdin, _source(tmp_path / "obra.dxf"), prepare_preview=False)["session"]

    data = _ask(sink, stdin, "c-t1", session, zone=[0, 0, 10, 10], exclude_layers=["COTAS"])
    assert data["type"] == "result", data
    data = data["data"]
    assert data["total"] == 5 and data["truncated"] is False and len(data["texts"]) == 5
    assert data["proposed_scale"] == "1:25"
    assert data["scale_candidates"][0]["count"] == 3
    assert {source["text"] for source in data["scale_candidates"][0]["sources"]} == {
        "PLANTA ESC 1:25",
        "SECCIÓN-V-F 0.30 X 0.15\nESC-1:25",
        "DENTRO ESC 1:25",
    }
    assert next(t for t in data["texts"] if t["text"] == "ATRIBUTO")["scales"] == []

    # Dos escalas en la zona: se ofrecen las dos y no se propone ninguna.
    both = _ask(sink, stdin, "c-t2", session, zone=[0, 0, 110, 10])["data"]
    assert both["proposed_scale"] is None
    assert {c["scale"] for c in both["scale_candidates"]} == {"1:25", "1:50"}

    # «max_texts» corta la lista, pero la escala se busca en todos.
    short = _ask(sink, stdin, "c-t3", session, zone=[0, 0, 110, 10], max_texts=1)["data"]
    assert short["truncated"] is True and len(short["texts"]) == 1 and short["total"] == 7
    assert len(short["scale_candidates"]) == 2

    nothing = _ask(sink, stdin, "c-t4", session, zone=[50, 50, 60, 60])["data"]
    assert nothing["texts"] == [] and nothing["scale_candidates"] == []
    assert nothing["proposed_scale"] is None


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"zone": [0, 0]},
        {"zone": [0, 0, 10, 10], "layers": ["NO_EXISTE"]},
        {"zone": [0, 0, 10, 10], "max_texts": 0},
        {"zone": [0, 0, 10, 10], "include_hidden_layers": ["NO_EXISTE"]},
    ],
)
def test_bad_texts_requests_are_bad_request(running, tmp_path: Path, data: dict) -> None:  # noqa: F811
    sink, stdin = running
    session = _load(sink, stdin, _source(tmp_path / "obra.dxf"), prepare_preview=False)["session"]
    reply = _ask(sink, stdin, "c-bad", session, **data)
    assert reply["type"] == "error" and reply["error"]["code"] == protocol.BAD_REQUEST, reply


def test_texts_is_a_command_since_protocol_1_3() -> None:
    assert protocol.PROTOCOL_VERSION == "1.4"
    assert "texts" in protocol.COMMANDS and "texts" not in protocol.IMMEDIATE_COMMANDS
