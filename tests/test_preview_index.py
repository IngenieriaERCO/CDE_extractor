"""Vista previa preparada (protocolo 1.2): índice del Model Space, turno entre la preparación y
los pedidos, y el trabajador con sus avisos (preview_index.py)."""

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import ezdxf
import pytest
from test_worker import TIMEOUT, running  # noqa: F401 - fixture del trabajador en el proceso
from test_worker_export import _final, _load

from cde_exporter.cad.preview import paint
from cde_exporter.cad.preview_index import (
    Gate,
    Preparation,
    PreparationStopped,
    paint_index,
)
from cde_exporter.cad.zone_preview import boxes_for, draw_entities, modelspace_boxes, select_view

ZONE = [0, 0, 10, 10]


def _document():
    """Un cuadrado en «MURO»; un bloque (en «MURO») con una línea en «MURO», otra en «COTAS» y
    otra en «0»; y, lejos, un círculo en «EJE»."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    for name in ("MURO", "COTAS", "EJE"):
        document.layers.add(name)
    block = document.blocks.new("DET")
    block.add_line((0, 0), (4, 0), dxfattribs={"layer": "MURO"})
    block.add_line((0, 1), (4, 1), dxfattribs={"layer": "COTAS"})
    block.add_line((0, 2), (4, 2))  # capa «0»: toma la del INSERT
    modelspace = document.modelspace()
    modelspace.add_lwpolyline(
        [(1, 1), (9, 1), (9, 9), (1, 9)], close=True, dxfattribs={"layer": "MURO"}
    )
    modelspace.add_blockref("DET", (3, 3), dxfattribs={"layer": "MURO"})
    modelspace.add_circle((1005, 1005), 5, dxfattribs={"layer": "EJE"})
    return document


def _px(picture, x: float, y: float) -> tuple[int, int]:
    a, _b, _c, d, e, f = picture.cad_to_px
    return int(a * x + e), int(d * y + f)


def _ink(image, xy) -> bool:
    x0, y0 = xy
    return any(image.getpixel((x0 + dx, y0 + dy))[3] > 0 for dx in (-1, 0, 1) for dy in (-1, 0, 1))


def _index(document=None):
    document = document or _document()
    target = SimpleNamespace(modelspace_boxes=None)
    index = Preparation(document, boxes_target=target).build()
    return document, index, target


# ----------------------------------------------------------------- índice


def test_the_index_keeps_the_layers_each_stroke_depends_on() -> None:
    _document_, index, target = _index()
    assert target.modelspace_boxes is not None  # las cajas, de paso, para las zonas
    assert index.items >= 5
    assert ("MURO", "COTAS") in index.chains  # la línea en «COTAS» dentro del INSERT en «MURO»
    assert ("MURO",) in index.chains  # la de «0» toma la del INSERT
    assert index.extent == pytest.approx((1, 1, 1010, 1010))

    def without(*hidden):
        return lambda layer: layer not in hidden

    picture, _stats = paint_index(index, tuple(ZONE), 500, without("COTAS"))
    image = picture.image
    assert _ink(image, _px(picture, 5, 1))  # el cuadrado
    assert _ink(image, _px(picture, 5, 3))  # «MURO» en el bloque
    assert not _ink(image, _px(picture, 5, 4))  # «COTAS» en el bloque: no se ve
    assert _ink(image, _px(picture, 5, 5))  # «0» en el bloque: se ve con el INSERT
    hidden, _stats = paint_index(index, tuple(ZONE), 500, without("MURO"))
    # Sin «MURO» desaparece el INSERT entero (también lo de «0» y «COTAS»).
    assert not any(_ink(hidden.image, _px(hidden, 5, y)) for y in (1, 3, 4, 5))


def test_the_index_gives_the_same_image_frame_as_the_direct_preview() -> None:
    document, index, target = _index()
    picture, _stats = paint_index(index, tuple(ZONE), 800)
    everything = select_view(document, tuple(ZONE), target.modelspace_boxes)
    player, _omitted = draw_entities(document, everything.entities)
    direct = paint(player, 800, frame=tuple(ZONE))
    assert picture.cad_to_px == direct.cad_to_px
    assert picture.image.size == direct.image.size
    assert picture.bounds == direct.bounds


def test_far_away_tiny_strokes_become_dots() -> None:
    _document_, index, _target = _index()
    near, near_stats = paint_index(index, tuple(ZONE), 500)
    far, far_stats = paint_index(index, (-50000, -50000, 50000, 50000), 500)
    assert near_stats["dots"] == 0
    assert far_stats["dots"] == far_stats["items"] > 0  # todo mide menos de un píxel
    assert any(far.image.getpixel((x, y))[3] for x in range(500) for y in range(500))


# ----------------------------------------------------------------- turno


def test_a_request_takes_the_turn_at_the_next_checkpoint() -> None:
    gate = Gate()
    stop = threading.Event()
    log: list[str] = []

    def preparation() -> None:
        gate.hold()
        try:
            while True:
                gate.checkpoint(stop)
                log.append("prep")
                time.sleep(0.002)
        except PreparationStopped:
            log.append("stopped")
        finally:
            gate.release()

    thread = threading.Thread(target=preparation, daemon=True)
    thread.start()
    time.sleep(0.05)
    began = time.perf_counter()
    with gate.turn():
        waited = time.perf_counter() - began
        count = len(log)
        time.sleep(0.05)
        assert len(log) == count  # mientras se atiende el pedido, la preparación espera
    assert waited < 0.5
    time.sleep(0.05)
    assert len(log) > count  # y luego sigue
    stop.set()
    thread.join(TIMEOUT)
    assert log[-1] == "stopped"


# ----------------------------------------------------------------- trabajador


def _source(path: Path) -> Path:
    _document().saveas(path)
    return path


def _events(sink, name: str) -> list[dict]:
    return [m for m in sink.messages if m.get("type") == "event" and m.get("event") == name]


def _wait_event(sink, name: str, count: int = 1) -> list[dict]:
    with sink.condition:
        assert sink.condition.wait_for(lambda: len(_events(sink, name)) >= count, TIMEOUT)
    return _events(sink, name)


def test_load_prepares_the_preview_in_the_background(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    loaded = _load(sink, stdin, _source(tmp_path / "obra.dxf"))
    assert loaded["preview_preparation"]["state"] in ("running", "ready")

    ready = _wait_event(sink, "preview_ready")[0]
    assert ready["id"] is None and ready["data"]["session"] == loaded["session"]
    assert ready["data"]["items"] >= 5 and ready["data"]["memory_mb"] >= 0
    progress = _events(sink, "preview_progress")
    assert progress and [m["data"]["percent"] for m in progress] == sorted(
        m["data"]["percent"] for m in progress
    )
    stdin.send(id="c-s", command="status")
    status = _final(sink, "c-s")["data"]
    assert status["preview_preparation"]["state"] == "ready"

    stdin.send(id="c-p", command="preview", data={"session": loaded["session"], "zone": ZONE})
    data = _final(sink, "c-p")["data"]
    assert data["prepared"] is True and data["lod"]["strokes"] >= 4


def test_without_preparation_the_preview_is_drawn_as_before(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    loaded = _load(sink, stdin, _source(tmp_path / "obra.dxf"), prepare_preview=False)
    assert loaded["preview_preparation"] is None

    stdin.send(id="c-p", command="preview", data={"session": loaded["session"], "zone": ZONE})
    data = _final(sink, "c-p")["data"]
    assert data["prepared"] is False and data["lod"] is None
    assert _events(sink, "preview_ready") == []


def test_another_configuration_is_prepared_when_it_is_asked_for(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    loaded = _load(sink, stdin, _source(tmp_path / "obra.dxf"))
    _wait_event(sink, "preview_ready")
    request = {"session": loaded["session"], "zone": ZONE, "print_transparency": False}

    stdin.send(id="c-p1", command="preview", data=request)
    assert _final(sink, "c-p1")["data"]["prepared"] is False  # mientras se prepara

    second = _wait_event(sink, "preview_ready", 2)[1]
    assert second["data"]["print_transparency"] is False
    stdin.send(id="c-p2", command="preview", data=request)
    assert _final(sink, "c-p2")["data"]["prepared"] is True


def test_the_boxes_resume_where_they_were_left() -> None:
    """Las cajas del Model Space, interrumpidas a medias (un pedido que toma el turno), siguen
    donde se quedaron y dan lo mismo que de una vez (zone_preview.boxes_for)."""
    document = _document()
    for x in range(200):
        document.modelspace().add_line((x, 0), (x, 1))
    calls = {"n": 0}

    def interrupt() -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise PreparationStopped()

    target = SimpleNamespace(modelspace_boxes=None)
    with pytest.raises(PreparationStopped):
        boxes_for(target, document, interrupt)
    assert target.modelspace_boxes is None and target.boxes_builder.position > 0
    rows = boxes_for(target, document)
    assert target.boxes_builder is None
    expected = modelspace_boxes(document)
    assert [(id(e), box) for e, box, _p in rows] == [(id(e), box) for e, box, _p in expected]


def test_the_boxes_survive_someone_else_finishing_them_meanwhile() -> None:
    """Como en el trabajador: la preparación cede el turno a mitad de las cajas y el pedido de
    una zona las termina; al retomar, la preparación no repite filas ni se sale de la lista."""
    document = _document()
    for x in range(200):
        document.modelspace().add_line((x, 0), (x, 1))
    target = SimpleNamespace(modelspace_boxes=None)
    calls = {"n": 0}

    def yield_turn() -> None:  # el pedido toma el turno y termina las cajas
        calls["n"] += 1
        if calls["n"] == 3:
            boxes_for(target, document)

    rows = boxes_for(target, document, yield_turn)
    expected = modelspace_boxes(document)
    assert calls["n"] >= 3
    assert len(rows) == len(expected)
    assert [(id(e), box) for e, box, _p in rows] == [(id(e), box) for e, box, _p in expected]
    assert target.modelspace_boxes is rows
