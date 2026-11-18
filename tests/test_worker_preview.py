"""Trabajador, etapa 2: ``preview`` con caché (docs/protocolo_trabajador.md, sección 4)."""

import json
import os
import threading
import time
from pathlib import Path

import ezdxf
import pytest
from ezdxf.acis import api as acis
from ezdxf.render import forms
from PIL import Image
from test_worker import TIMEOUT, running  # noqa: F401 - fixture del trabajador en el proceso

from cde_exporter.cad import worker as worker_module
from cde_exporter.cad import worker_protocol as protocol
from cde_exporter.cad.exporters import DrawingCancelled
from cde_exporter.cad.preview import PreviewCache, draw_reference, layers_tag, paint
from cde_exporter.cad.reader import read_dxf

RED = (255, 0, 0, 255)


def _source(path: Path) -> Path:
    """«Detalle» insertado en (1000, 2000), girado 30° y a escala 2: una línea roja de (0, 0) a
    (10, 0), una negra (color 7) de (0, 0) a (0, 5), un rayado sólido con hueco, un 3DSOLID y
    una línea en una capa apagada. También un bloque anónimo y uno vacío."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    document.layers.add("OCULTA", color=3).off()
    block = document.blocks.new("Detalle")
    block.add_line((0, 0), (10, 0), dxfattribs={"color": 1})
    block.add_line((0, 0), (0, 5), dxfattribs={"color": 7})
    hatch = block.add_hatch(color=5)
    hatch.paths.add_polyline_path([(20, 0), (40, 0), (40, 20), (20, 20)], is_closed=True)
    hatch.paths.add_polyline_path([(25, 5), (35, 5), (35, 15), (25, 15)], is_closed=True)
    solid = block.add_3dsolid()
    acis.export_dxf(solid, [acis.body_from_mesh(forms.cube().scale(4, 2, 1))])
    block.add_line((0, 0), (-30, -30), dxfattribs={"layer": "OCULTA"})
    modelspace = document.modelspace()
    modelspace.add_blockref(
        "Detalle", (1000, 2000), dxfattribs={"rotation": 30, "xscale": 2, "yscale": 2}
    )
    document.blocks.new("VACIO")
    modelspace.add_blockref("VACIO", (0, 0))
    document.saveas(path)
    return path


def _insert_matrix_point(x: float, y: float) -> tuple[float, float]:
    """Punto del bloque en el marco único (INSERT en (1000, 2000), 30°, escala 2)."""
    import math

    angle = math.radians(30)
    return (
        1000 + 2 * (x * math.cos(angle) - y * math.sin(angle)),
        2000 + 2 * (x * math.sin(angle) + y * math.cos(angle)),
    )


def _pixel(cad_to_px: list[float], point: tuple[float, float]) -> tuple[int, int]:
    a, b, c, d, e, f = cad_to_px
    x, y = point
    return int(a * x + c * y + e), int(b * x + d * y + f)


def _near(image: Image.Image, pixel: tuple[int, int], color, radius: int = 1) -> bool:
    x0, y0 = pixel
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            x, y = x0 + dx, y0 + dy
            if 0 <= x < image.width and 0 <= y < image.height and image.getpixel((x, y)) == color:
                return True
    return False


def _load(sink, stdin, source: Path) -> dict:
    stdin.send(id="c-load", command="load", data={"dxf": str(source)})
    return sink.wait("c-load", "result")["data"]


def _preview(sink, stdin, request_id: str, **data) -> dict:
    stdin.send(id=request_id, command="preview", data=data)
    with sink.condition:
        sink.condition.wait_for(
            lambda: any(
                m["id"] == request_id and m["type"] in ("result", "error") for m in sink.messages
            ),
            TIMEOUT,
        )
    return next(
        m for m in sink.messages if m["id"] == request_id and m["type"] in ("result", "error")
    )


# ----------------------------------------------------------------- en el trabajador


def test_preview_places_known_points_with_cad_to_px(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    loaded = _load(sink, stdin, _source(tmp_path / "plano.dxf"))

    answer = _preview(sink, stdin, "c-1", session=loaded["session"], ref="Detalle#1")

    data = answer["data"]
    a, b, c, d, _e, _f = data["cad_to_px"]
    assert b == 0 and c == 0 and a == pytest.approx(-d) and a > 0
    assert max(data["px"]) == 1500
    image = Image.open(data["png"])
    assert image.mode == "RGBA" and list(image.size) == data["px"]
    assert image.getpixel((0, 0))[3] == 0  # fondo transparente
    # El extremo (10, 0) de la línea roja y el (0, 5) de la negra (color 7 → negro).
    assert _near(image, _pixel(data["cad_to_px"], _insert_matrix_point(10, 0)), RED)
    assert _near(image, _pixel(data["cad_to_px"], _insert_matrix_point(0, 5)), (0, 0, 0, 255))
    # El rayado tiene un hueco: su centro queda transparente; su borde, relleno.
    blue = image.getpixel(_pixel(data["cad_to_px"], _insert_matrix_point(22, 10)))
    assert blue[3] == 255 and blue[2] > 200
    assert image.getpixel(_pixel(data["cad_to_px"], _insert_matrix_point(30, 10)))[3] == 0
    # Los 3DSOLID no se dibujan; la capa apagada tampoco.
    assert data["omitted"] == {"3DSOLID": 1, "REGION": 0}
    hidden_end = _pixel(data["cad_to_px"], _insert_matrix_point(-30, -30))
    assert not (0 <= hidden_end[0] < image.width and 0 <= hidden_end[1] < image.height)
    # La caja de la vista previa está dentro de la de «load».
    box = loaded["blocks"][0]["bounds"]
    assert data["bounds"]["min"][0] >= box["min"][0] - 1e-6
    assert data["bounds"]["max"][1] <= box["max"][1] + 1e-6
    assert data["cached"] is False and data["ref"] == "Detalle#1"
    progress = [m["data"] for m in sink.messages if m["id"] == "c-1" and m["type"] == "progress"]
    assert progress and {item["stage"] for item in progress} <= {"draw", "paint"}
    percents = [item["percent"] for item in progress]
    assert percents == sorted(percents) and percents[-1] == 100.0


def test_preview_is_cached_by_size_and_hidden_layers(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    loaded = _load(sink, stdin, _source(tmp_path / "plano.dxf"))
    session = loaded["session"]
    handle = loaded["blocks"][0]["handle"]

    first = _preview(sink, stdin, "c-1", session=session, ref="Detalle#1")["data"]
    again = _preview(sink, stdin, "c-2", session=session, block="Detalle", handle=handle)["data"]
    small = _preview(sink, stdin, "c-3", session=session, ref="Detalle#1", long_side_px=300)
    hidden = _preview(
        sink, stdin, "c-4", session=session, ref="Detalle#1", include_hidden_layers=["oculta"]
    )["data"]

    assert again["cached"] is True and again["png"] == first["png"]
    assert {k: v for k, v in again.items() if k not in ("cached", "seconds")} == {
        k: v for k, v in first.items() if k not in ("cached", "seconds")
    }
    assert small["data"]["cached"] is False and max(small["data"]["px"]) == 300
    assert Path(first["png"]).name == f"{handle}_1500_visible.png"
    assert (
        Path(hidden["png"]).name == f"{handle}_1500_{layers_tag(frozenset({'oculta'}), False)}.png"
    )
    assert hidden["include_hidden_layers"] == ["oculta"]
    image = Image.open(hidden["png"])
    green = (0, 255, 0, 255)
    assert _near(image, _pixel(hidden["cad_to_px"], _insert_matrix_point(-30, -30)), green)
    assert Path(first["png"]).parent.name == loaded["dxf"]["fingerprint"]


@pytest.mark.parametrize(
    ("data", "code"),
    [
        ({"session": "s-9", "ref": "Detalle#1"}, protocol.NO_SESSION),
        ({"ref": "NO_EXISTE#1"}, protocol.BLOCK_NOT_FOUND),
        ({"ref": "Detalle#2"}, protocol.REFERENCE_NOT_FOUND),
        ({"block": "Detalle", "handle": "FFFFF"}, protocol.REFERENCE_NOT_FOUND),
        ({"ref": "VACIO#1"}, protocol.NOT_EXPORTABLE),
        ({"ref": "Detalle"}, protocol.BAD_REQUEST),
        ({"block": "Detalle"}, protocol.BAD_REQUEST),
        ({"ref": "Detalle#1", "long_side_px": 10}, protocol.BAD_REQUEST),
        ({"ref": "Detalle#1", "long_side_px": 1500.0}, protocol.BAD_REQUEST),
        ({"ref": "Detalle#1", "include_hidden_layers": ["NO_EXISTE"]}, protocol.BAD_REQUEST),
        ({"ref": "Detalle#1", "include_hidden_layers": "OCULTA"}, protocol.BAD_REQUEST),
    ],
)
def test_bad_previews_answer_with_their_code(
    running,  # noqa: F811
    tmp_path: Path,
    data: dict,
    code: str,
) -> None:
    sink, stdin = running
    loaded = _load(sink, stdin, _source(tmp_path / "plano.dxf"))
    request = {"session": loaded["session"], **data}

    answer = _preview(sink, stdin, "c-1", **request)

    assert answer["type"] == "error" and answer["error"]["code"] == code
    assert answer["command"] == "preview"


def test_preview_without_load_is_no_session(running) -> None:  # noqa: F811
    sink, stdin = running
    answer = _preview(sink, stdin, "c-1", session="s-1", ref="Detalle#1")
    assert answer["error"]["code"] == protocol.NO_SESSION


def test_preview_after_the_dxf_changed_is_dxf_changed(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    source = _source(tmp_path / "plano.dxf")
    loaded = _load(sink, stdin, source)
    later = time.time() + 10
    os.utime(source, (later, later))

    answer = _preview(sink, stdin, "c-1", session=loaded["session"], ref="Detalle#1")

    assert answer["error"]["code"] == protocol.DXF_CHANGED


def test_preview_can_be_cancelled_while_drawing(
    running,  # noqa: F811
    tmp_path: Path,
    monkeypatch,
) -> None:
    sink, stdin = running
    loaded = _load(sink, stdin, _source(tmp_path / "plano.dxf"))
    started = threading.Event()
    real = worker_module.draw_reference

    def slow(document, insert, **kwargs):
        check = kwargs["check"]

        def waiting_check() -> None:
            started.set()
            deadline = time.monotonic() + TIMEOUT
            while time.monotonic() < deadline:
                check()  # lanza en cuanto llega «cancel»
                time.sleep(0.01)

        return real(document, insert, **{**kwargs, "check": waiting_check})

    monkeypatch.setattr(worker_module, "draw_reference", slow)
    stdin.send(id="c-1", command="preview", data={"session": loaded["session"], "ref": "Detalle#1"})
    assert started.wait(TIMEOUT)
    stdin.send(id="c-2", command="cancel", data={"target": "c-1"})

    assert sink.wait("c-1", "error")["error"]["code"] == protocol.CANCELLED
    assert not list((tmp_path / "trabajo" / "previews").rglob("*.png"))  # nada en la caché


# ----------------------------------------------------------------- piezas


def test_cancelling_inside_a_nested_insert_is_not_swallowed(tmp_path: Path) -> None:
    document = read_dxf(_source(tmp_path / "plano.dxf"))
    insert = next(e for e in document.modelspace() if e.dxf.name == "Detalle")
    calls = 0

    def check() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:  # ya dentro del INSERT
            raise DrawingCancelled()

    with pytest.raises(DrawingCancelled):
        draw_reference(document, insert, check=check)


def test_empty_drawing_gives_an_empty_image(tmp_path: Path) -> None:
    document = ezdxf.new("R2018")
    block = document.blocks.new("SOLO_SOLIDO")
    solid = block.add_3dsolid()
    acis.export_dxf(solid, [acis.body_from_mesh(forms.cube())])
    insert = document.modelspace().add_blockref("SOLO_SOLIDO", (0, 0))

    player, omitted = draw_reference(document, insert)
    picture = paint(player, 1500)

    assert omitted["3DSOLID"] == 1
    assert picture.bounds is None and picture.cad_to_px is None
    assert picture.image.size == (1, 1)


def test_cache_trims_the_least_recently_used(tmp_path: Path) -> None:
    cache = PreviewCache(tmp_path / "previews", limit_mb=500)
    image = Image.effect_noise((100, 100), 64).convert("RGBA")  # ~40 KB en PNG
    entries = []
    for index in range(3):
        png, meta = cache.paths("huella", f"H{index}", 100, "visible")
        cache.put(png, meta, image, {"index": index})
        stamp = time.time() - 100 + index
        os.utime(png, (stamp, stamp))
        os.utime(meta, (stamp, stamp))
        entries.append((png, meta))
    assert cache.get(*entries[0]) == {"index": 0}  # la más antigua pasa a ser la más reciente
    one = sum(path.stat().st_size for path in entries[0])
    cache.limit_bytes = 2.5 * one  # caben dos de las tres

    cache.trim(keep=entries[2])

    assert entries[0][0].exists() and entries[2][0].exists()
    assert not entries[1][0].exists()
    assert json.loads(entries[2][1].read_text(encoding="utf-8")) == {"index": 2}


def test_layers_tag() -> None:
    assert layers_tag(None, False) == "visible"
    assert layers_tag(frozenset({"a"}), True) == "hidden-all"
    tag = layers_tag(frozenset({"a", "b"}), False)
    assert tag.startswith("hidden-") and len(tag) == 13
    assert tag == layers_tag(frozenset({"b", "a"}), False)
