"""Trabajador (``cde-exporter worker``), etapa 1: protocolo, arranque, ``load``, ``status``,
``cancel``, ``close`` y stdout solo con JSON en UTF-8 (docs/protocolo_trabajador.md)."""

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

import ezdxf
import pytest

from cde_exporter.cad import worker as worker_module
from cde_exporter.cad import worker_protocol as protocol
from cde_exporter.cad.dxf_session import TEMP_PREFIX
from cde_exporter.cad.worker import Worker, _Output
from cde_exporter.cad.worker_protocol import WorkerError, parse_request

TIMEOUT = 60


def _line(**fields) -> bytes:
    return json.dumps({"protocol": "1.0", **fields}, ensure_ascii=False).encode("utf-8") + b"\n"


def _source(path: Path, name: str = "Detalle Ñ") -> Path:
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    block = document.blocks.new(name)
    block.add_line((0, 0), (10, 0))
    block.add_line((10, 0), (10, 4))
    document.modelspace().add_blockref(name, (0, 0))
    document.saveas(path)
    return path


# ----------------------------------------------------------------- protocolo


@pytest.mark.parametrize(
    ("line", "code"),
    [
        (b"\xff\xfe", protocol.BAD_REQUEST),
        (b"{no es json", protocol.BAD_REQUEST),
        (b"[1, 2]", protocol.BAD_REQUEST),
        (b'{"protocol": "1.0", "command": "status"}', protocol.BAD_REQUEST),
        (b'{"id": "c-1", "command": "status"}', protocol.BAD_REQUEST),
        (b'{"protocol": "2.0", "id": "c-1", "command": "status"}', protocol.PROTOCOL_VERSION_ERROR),
        (b'{"protocol": "1.0", "id": "c-1", "command": "borrar"}', protocol.UNKNOWN_COMMAND),
        (b'{"protocol": "1.0", "id": "c-1", "command": "load", "data": 3}', protocol.BAD_REQUEST),
    ],
)
def test_bad_requests_are_rejected_with_their_code(line: bytes, code: str) -> None:
    with pytest.raises(WorkerError) as failure:
        parse_request(line)
    assert failure.value.code == code


def test_a_good_request_and_a_minor_version_are_accepted() -> None:
    request = parse_request(_line(id="c-1", command="load", data={"dxf": "C:/plano.dxf"}))
    assert (request.id, request.command, request.data) == ("c-1", "load", {"dxf": "C:/plano.dxf"})
    assert parse_request(b'{"protocol": "1.7", "id": "c-2", "command": "status"}').data == {}


def test_messages_are_one_utf8_line_without_escaped_accents() -> None:
    data = protocol.encode(protocol.message("ready", None, data={"texto": "Ñandú"}))
    assert data.endswith(b"\n") and data.count(b"\n") == 1
    assert "Ñandú".encode() in data
    assert json.loads(data)["protocol"] == protocol.PROTOCOL_VERSION


def test_unknown_error_codes_are_a_programming_error() -> None:
    with pytest.raises(ValueError):
        WorkerError("INVENTADO", "x")


# ----------------------------------------------------------------- trabajador en el proceso


class _Sink:
    """stdout del trabajador: guarda cada mensaje y deja esperar a uno."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.condition = threading.Condition()

    def write(self, data: bytes) -> None:
        assert data.endswith(b"\n") and data.count(b"\n") == 1
        with self.condition:
            self.messages.append(json.loads(data.decode("utf-8")))
            self.condition.notify_all()

    def flush(self) -> None:
        return None

    def wait(self, request_id: str, kind: str) -> dict:
        def found():
            return next(
                (m for m in self.messages if m["id"] == request_id and m["type"] == kind), None
            )

        with self.condition:
            assert self.condition.wait_for(lambda: found() is not None, TIMEOUT), (
                request_id,
                kind,
                self.messages,
            )
            return found()


class _Stdin:
    def __init__(self) -> None:
        self.lines: queue.Queue = queue.Queue()

    def send(self, **fields) -> None:
        self.lines.put(_line(**fields))

    def end(self) -> None:
        self.lines.put(None)

    def __iter__(self):
        while (line := self.lines.get()) is not None:
            yield line


@pytest.fixture
def running(tmp_path: Path):
    """Trabajador con sus dos hilos; al final se cierra stdin y se espera a que termine."""
    sink, stdin = _Sink(), _Stdin()
    worker = Worker(_Output(sink), tmp_path / "trabajo", 500)
    reader = threading.Thread(target=worker.read_requests, args=(stdin,), daemon=True)
    main = threading.Thread(target=worker.run, daemon=True)
    reader.start()
    main.start()
    yield sink, stdin
    stdin.end()
    main.join(TIMEOUT)
    assert not main.is_alive()


def test_load_returns_the_inventory_with_monotonic_progress(running, tmp_path: Path) -> None:
    sink, stdin = running
    source = _source(tmp_path / "plano.dxf")

    stdin.send(id="c-1", command="load", data={"dxf": str(source)})
    result = sink.wait("c-1", "result")

    data = result["data"]
    assert result["command"] == "load" and data["session"] == "s-1"
    assert data["dxf"]["name"] == "plano.dxf"
    assert [block["ref"] for block in data["blocks"]] == ["Detalle Ñ#1"]
    assert data["margin_mm"] == 5.0
    percents = [m["data"]["percent"] for m in sink.messages if m["type"] == "progress"]
    assert percents and percents == sorted(percents) and percents[-1] == 100.0

    stdin.send(id="c-2", command="status")
    status = sink.wait("c-2", "result")["data"]
    assert status["busy"] is False and status["session"] == "s-1"
    assert status["dxf"] == "plano.dxf"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        ({}, protocol.BAD_REQUEST),
        ({"dxf": "C:/no/existe.dxf"}, protocol.DXF_NOT_FOUND),
        ({"dxf": "x.dxf", "units": "leguas"}, protocol.BAD_REQUEST),
        ({"dxf": "x.dxf", "margin_mm": -1}, protocol.BAD_REQUEST),
        ({"dxf": "x.dxf", "margin_mm": True}, protocol.BAD_REQUEST),
        ({"dxf": "x.dxf", "include_layers": "no"}, protocol.BAD_REQUEST),
    ],
)
def test_bad_loads_answer_with_an_error(running, data: dict, code: str) -> None:
    sink, stdin = running
    stdin.send(id="c-1", command="load", data=data)
    assert sink.wait("c-1", "error")["error"]["code"] == code


def test_an_invalid_dxf_is_dxf_invalid(running, tmp_path: Path) -> None:
    sink, stdin = running
    broken = tmp_path / "roto.dxf"
    broken.write_text("esto no es un DXF", encoding="utf-8")

    stdin.send(id="c-1", command="load", data={"dxf": str(broken)})

    assert sink.wait("c-1", "error")["error"]["code"] == protocol.DXF_INVALID


def test_status_busy_and_cancel_while_loading(running, tmp_path: Path, monkeypatch) -> None:
    sink, stdin = running
    source = _source(tmp_path / "plano.dxf")
    started, release = threading.Event(), threading.Event()
    real_load = worker_module.load_dxf

    def slow_load(*args, **kwargs):
        started.set()
        assert release.wait(TIMEOUT)
        return real_load(*args, **kwargs)

    monkeypatch.setattr(worker_module, "load_dxf", slow_load)

    stdin.send(id="c-1", command="load", data={"dxf": str(source)})
    assert started.wait(TIMEOUT)
    stdin.send(id="c-2", command="status")
    status = sink.wait("c-2", "result")["data"]
    assert status["busy"] is True and status["current"] == "c-1"

    stdin.send(id="c-3", command="load", data={"dxf": str(source)})
    busy = sink.wait("c-3", "error")
    assert busy["error"]["code"] == protocol.BUSY
    assert busy["error"]["details"] == {"current": "c-1"}

    stdin.send(id="c-4", command="cancel", data={"target": "otro"})
    assert sink.wait("c-4", "result")["data"] == {"accepted": False}
    stdin.send(id="c-5", command="cancel", data={"target": "c-1"})
    assert sink.wait("c-5", "result")["data"] == {"accepted": True}
    release.set()

    assert sink.wait("c-1", "error")["error"]["code"] == protocol.CANCELLED
    stdin.send(id="c-6", command="status")
    status = sink.wait("c-6", "result")["data"]
    assert status["busy"] is False and status["session"] is None


def test_cancel_with_nothing_running_is_not_accepted(running) -> None:
    sink, stdin = running
    stdin.send(id="c-1", command="cancel")
    assert sink.wait("c-1", "result")["data"] == {"accepted": False}


def test_an_unexpected_failure_does_not_stop_the_worker(
    running, tmp_path: Path, monkeypatch
) -> None:
    sink, stdin = running
    source = _source(tmp_path / "plano.dxf")

    def broken(*args, **kwargs):
        raise RuntimeError("fallo de prueba")

    monkeypatch.setattr(worker_module, "build_inventory", broken)
    stdin.send(id="c-1", command="load", data={"dxf": str(source)})

    failure = sink.wait("c-1", "error")["error"]
    assert failure["code"] == protocol.INTERNAL
    assert "fallo de prueba" in failure["message"]
    assert "traceback" not in failure["details"]
    stdin.send(id="c-2", command="status")
    assert sink.wait("c-2", "result")["data"]["busy"] is False


def test_preview_and_export_are_not_yet_available(running) -> None:
    sink, stdin = running
    stdin.send(id="c-1", command="preview")
    assert sink.wait("c-1", "error")["error"]["code"] == protocol.UNKNOWN_COMMAND


def test_bad_lines_are_answered_and_the_worker_goes_on(running) -> None:
    sink, stdin = running
    stdin.lines.put(b"{roto\n")
    stdin.send(id="c-1", command="status")
    assert sink.wait("c-1", "result")["data"]["busy"] is False
    assert sink.messages[0]["type"] == "error" and sink.messages[0]["id"] is None


def test_close_answers_and_ends(running) -> None:
    sink, stdin = running
    stdin.send(id="c-1", command="close")
    assert sink.wait("c-1", "result")["data"] == {}


# ----------------------------------------------------------------- proceso real


def test_worker_process_speaks_only_json_on_stdout(tmp_path: Path) -> None:
    source = _source(tmp_path / "plano Ñ.dxf")
    work = tmp_path / "trabajo"
    stale = work / f"{TEMP_PREFIX}viejo"
    stale.mkdir(parents=True)
    (stale / "pid").write_text("999999999", encoding="ascii")
    keep = work / "no_tocar"
    keep.mkdir()
    code = "import sys; from cde_exporter.cli import main; sys.exit(main(sys.argv[1:]))"
    process = subprocess.Popen(
        [sys.executable, "-c", code, "worker", "--work-dir", str(work)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready = json.loads(process.stdout.readline().decode("utf-8"))
        assert ready["type"] == "ready" and ready["id"] is None
        assert ready["data"]["protocol_version"] == protocol.PROTOCOL_VERSION
        assert ready["data"]["work_dir"] == str(work.resolve())
        assert not stale.exists() and keep.is_dir()

        process.stdin.write(_line(id="c-1", command="load", data={"dxf": str(source)}))
        process.stdin.flush()
        lines = []
        while True:
            message = json.loads(process.stdout.readline().decode("utf-8"))
            lines.append(message)
            if message["type"] in ("result", "error"):
                break
        assert lines[-1]["type"] == "result"
        assert lines[-1]["data"]["dxf"]["name"] == "plano Ñ.dxf"
        assert lines[-1]["data"]["blocks"][0]["name"] == "Detalle Ñ"

        process.stdin.write(_line(id="c-2", command="close"))
        process.stdin.flush()
        rest, _errors = process.communicate(timeout=TIMEOUT)
    finally:
        if process.poll() is None:
            process.kill()
    final = [json.loads(line) for line in rest.decode("utf-8").splitlines()]
    assert final == [
        {"protocol": "1.0", "id": "c-2", "type": "result", "command": "close", "data": {}}
    ]
    assert process.returncode == 0
