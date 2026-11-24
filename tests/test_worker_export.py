"""Trabajador, etapa 3: ``export`` con avance, cancelación y manifiesto 1.3
(docs/protocolo_trabajador.md, secciones 5, 6, 7, 9 y 13)."""

import json
import math
import os
import threading
import time
from pathlib import Path

import ezdxf
import pymupdf
import pytest
from ezdxf.acis import api as acis
from ezdxf.render import forms
from PIL import Image
from test_worker import TIMEOUT, running  # noqa: F401 - fixture del trabajador en el proceso

from cde_exporter.cad import block_commands
from cde_exporter.cad import worker_protocol as protocol
from cde_exporter.cad.dxf_session import TEMP_PREFIX
from cde_exporter.cad.manifest import SCHEMA_VERSION, validate_manifest, verify_package
from cde_exporter.cad.project_commands import SUMMARY_NAME

MM_PER_PT = 25.4 / 72


def _source(path: Path) -> Path:
    """«Detalle» en (1000, 2000), girado 30° y a escala 2, con una línea roja de (0, 0) a
    (10, 0); «Simple» con una línea en una capa apagada; «Solido» con un 3DSOLID."""
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    document.layers.add("OCULTA").off()
    detail = document.blocks.new("Detalle")
    detail.add_line((0, 0), (10, 0), dxfattribs={"color": 1})
    detail.add_line((0, 0), (0, 5))
    simple = document.blocks.new("Simple")
    simple.add_line((0, 0), (8, 0))
    simple.add_line((8, 0), (8, 4))
    simple.add_line((0, 0), (20, 20), dxfattribs={"layer": "OCULTA"})
    solid_block = document.blocks.new("Solido")
    solid_block.add_line((0, 0), (1, 1))
    solid = solid_block.add_3dsolid()
    acis.export_dxf(solid, [acis.body_from_mesh(forms.cube().scale(4, 2, 1))])
    modelspace = document.modelspace()
    modelspace.add_blockref(
        "Detalle", (1000, 2000), dxfattribs={"rotation": 30, "xscale": 2, "yscale": 2}
    )
    modelspace.add_blockref("Simple", (0, 0))
    modelspace.add_blockref("Simple", (50, 0))
    modelspace.add_blockref("Solido", (100, 0))
    document.saveas(path)
    return path


def _detail_point(x: float, y: float) -> tuple[float, float]:
    angle = math.radians(30)
    return (
        1000 + 2 * (x * math.cos(angle) - y * math.sin(angle)),
        2000 + 2 * (x * math.sin(angle) + y * math.cos(angle)),
    )


def _final(sink, request_id: str) -> dict:
    with sink.condition:
        assert sink.condition.wait_for(
            lambda: any(
                m["id"] == request_id and m["type"] in ("result", "error") for m in sink.messages
            ),
            TIMEOUT,
        )
    return next(
        m for m in sink.messages if m["id"] == request_id and m["type"] in ("result", "error")
    )


def _load(sink, stdin, source: Path, **data) -> dict:
    stdin.send(id="c-load", command="load", data={"dxf": str(source), **data})
    return _final(sink, "c-load")["data"]


def _export(sink, stdin, request_id: str, session: str, output: Path, jobs, **options) -> dict:
    stdin.send(
        id=request_id,
        command="export",
        data={"session": session, "output_dir": str(output), "jobs": jobs, "options": options},
    )
    return _final(sink, request_id)


def _files(folder: Path) -> list[str]:
    return sorted(item.name for item in folder.iterdir()) if folder.exists() else []


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


# ----------------------------------------------------------------- marco único


def test_a_known_point_lands_in_the_same_place_in_preview_and_pdf(
    running,  # noqa: F811
    tmp_path: Path,
) -> None:
    sink, stdin = running
    loaded = _load(sink, stdin, _source(tmp_path / "plano.dxf"))
    session = loaded["session"]
    out = tmp_path / "paquetes"

    stdin.send(id="c-1", command="preview", data={"session": session, "ref": "Detalle#1"})
    preview = _final(sink, "c-1")["data"]
    answer = _export(
        sink, stdin, "c-2", session, out, [{"job": "t1", "ref": "Detalle#1", "scale": "1:100"}]
    )

    assert answer["type"] == "result", answer
    job = answer["data"]["jobs"][0]
    assert job["status"] == "published" and job["error"] is None
    manifest = json.loads(Path(job["package"]["manifest"]).read_text(encoding="utf-8"))
    a, b, c, d, e, f = manifest["pdf"]["drawing"]["cad_to_page"]["matrix"]
    end = _detail_point(10, 0)
    expected_mm = (a * end[0] + c * end[1] + e, b * end[0] + d * end[1] + f)
    with pymupdf.open(job["package"]["pdf"]) as pdf:
        red = [
            point
            for drawing in pdf[0].get_drawings()
            if drawing.get("color") and tuple(round(v, 2) for v in drawing["color"]) == (1, 0, 0)
            for item in drawing["items"]
            if item[0] == "l"
            for point in item[1:]
        ]
    measured = min(
        red,
        key=lambda p: math.dist((p.x * MM_PER_PT, p.y * MM_PER_PT), expected_mm),
    )
    assert math.dist((measured.x * MM_PER_PT, measured.y * MM_PER_PT), expected_mm) < 0.01
    # En la vista previa, el mismo punto cae en un píxel rojo (tolerancia de 1 píxel).
    pa, _pb, _pc, pd, pe, pf = preview["cad_to_px"]
    pixel = (int(pa * end[0] + pe), int(pd * end[1] + pf))
    image = Image.open(preview["png"])
    assert any(
        image.getpixel((pixel[0] + dx, pixel[1] + dy)) == (255, 0, 0, 255)
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
        if 0 <= pixel[0] + dx < image.width and 0 <= pixel[1] + dy < image.height
    )
    # La caja de «load» contiene la de lo dibujado en el PDF.
    box = loaded["blocks"][0]["bounds"]
    drawn = manifest["pdf"]["drawing"]["cad_bounds"]
    assert box["min"][0] <= drawn["min"][0] + 1e-6 and box["min"][1] <= drawn["min"][1] + 1e-6
    assert box["max"][0] >= drawn["max"][0] - 1e-6 and box["max"][1] >= drawn["max"][1] - 1e-6


# ----------------------------------------------------------------- paquetes y manifiesto 1.3


def test_export_publishes_packages_with_manifest_1_3(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    source = _source(tmp_path / "plano.dxf")
    loaded = _load(sink, stdin, source, units="m", margin_mm=3)
    out = tmp_path / "paquetes"
    jobs = [
        {"job": "t1", "ref": "Simple#1", "scale": "1:50", "package": "Simple_a"},
        {"job": "t2", "block": "Simple", "occurrence": 2, "scale": "1:20", "package": "Simple_b"},
        {"job": "t3", "ref": "Solido#1", "scale": "1:50", "package": "Solido"},
    ]

    answer = _export(
        sink,
        stdin,
        "c-1",
        loaded["session"],
        out,
        jobs,
        include_hidden_layers=["oculta"],
        write_summary=True,
        units="m",
        margin_mm=3.0,
    )

    data = answer["data"]
    assert [job["status"] for job in data["jobs"]] == ["published", "published", "failed"]
    assert data["totals"] == {"published": 2, "failed": 1, "cancelled": 0}
    failed = data["jobs"][2]
    assert failed["error"]["code"] == "EXPORT_FAILED" and "3DSOLID" in failed["error"]["message"]
    assert failed["package"] is None
    assert _files(out) == sorted(
        [
            *(f"Simple_{x}{ext}" for x in "ab" for ext in (".pdf", ".txt", ".manifest.json")),
            SUMMARY_NAME,
        ]
    )
    first = data["jobs"][0]
    manifest = json.loads(Path(first["package"]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == SCHEMA_VERSION == "1.5"
    assert validate_manifest(manifest) == ()
    assert verify_package(first["package"]["pdf"]) == ()
    assert manifest["source"]["dxf_sha256"] == loaded["dxf"]["sha256"]
    assert manifest["source"]["reference"]["handle"] == first["handle"]
    options = manifest["options"]
    assert options["units"] == "m" and options["margin_mm"] == 3.0
    assert options["include_hidden_layers"] == ["oculta"]
    assert options["instancing"] is True and options["xobject"] is True
    assert options["clip"] is None and options["exporter_version"]
    assert first["validation_status"] == manifest["validation"]["status"]
    assert first["page_size_mm"] == manifest["pdf"]["page_size_mm"]
    assert first["physical_scale_guaranteed"] is True
    codes = {notice["code"] for notice in first["notices"]}
    assert "HIDDEN_LAYERS_INCLUDED" in codes
    summary = json.loads((out / SUMMARY_NAME).read_text(encoding="utf-8"))
    assert data["summary"] == str(out / SUMMARY_NAME)
    assert summary["totals"]["packages"] == 2 and summary["totals"]["failed"] == 1
    assert [row["file_stem"] for row in summary["packages"]] == ["Simple_a", "Simple_b"]
    second = json.loads(Path(data["jobs"][1]["package"]["manifest"]).read_text(encoding="utf-8"))
    assert second["source"]["reference"]["occurrence"] == 2
    progress = [m["data"] for m in sink.messages if m["id"] == "c-1" and m["type"] == "progress"]
    percents = [item["percent"] for item in progress]
    assert percents == sorted(percents)
    assert {item["job"] for item in progress} == {"t1", "t2", "t3"}
    assert all(item["job_total"] == 3 for item in progress)
    assert {"extract", "draw", "pdf", "publish"} <= {item["stage"] for item in progress}


def test_the_default_package_name_is_the_file_stem(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    loaded = _load(sink, stdin, _source(tmp_path / "plano.dxf"))
    out = tmp_path / "paquetes"

    answer = _export(
        sink, stdin, "c-1", loaded["session"], out, [{"ref": "Simple#2", "scale": "1:50"}]
    )

    assert answer["data"]["jobs"][0]["job"] == "t1"
    assert (out / "Simple_002.manifest.json").is_file()
    assert answer["data"]["summary"] is None and not (out / SUMMARY_NAME).exists()


def test_existing_outputs_need_overwrite(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    loaded = _load(sink, stdin, _source(tmp_path / "plano.dxf"))
    out = tmp_path / "paquetes"
    jobs = [{"job": "t1", "ref": "Simple#1", "scale": "1:50", "package": "P"}]
    assert _export(sink, stdin, "c-1", loaded["session"], out, jobs)["type"] == "result"

    again = _export(sink, stdin, "c-2", loaded["session"], out, jobs)
    replaced = _export(sink, stdin, "c-3", loaded["session"], out, jobs, overwrite=True)

    assert again["error"]["code"] == protocol.OUTPUT_EXISTS
    assert replaced["data"]["jobs"][0]["status"] == "published"


@pytest.mark.parametrize(
    ("jobs", "options", "code"),
    [
        ([{"ref": "Simple#1", "scale": "1:50", "package": "con espacio"}], {}, "OUTPUT_INVALID"),
        ([{"ref": "Simple#1", "scale": "1:50", "package": "Ñandú"}], {}, "OUTPUT_INVALID"),
        ([{"ref": "Simple#1", "scale": "1:50", "package": "x" * 61}], {}, "OUTPUT_INVALID"),
        (
            [
                {"job": "a", "ref": "Simple#1", "scale": "1:50", "package": "P"},
                {"job": "b", "ref": "Simple#2", "scale": "1:50", "package": "p"},
            ],
            {},
            "OUTPUT_INVALID",
        ),
        (
            [
                {"job": "a", "ref": "Simple#1", "scale": "1:50"},
                {"job": "a", "ref": "Simple#2", "scale": "1:50"},
            ],
            {},
            "BAD_REQUEST",
        ),
        ([{"ref": "Simple#1", "scale": "1:0"}], {}, "BAD_REQUEST"),
        ([{"ref": "Simple#1"}], {}, "BAD_REQUEST"),
        ([{"ref": "Simple#9", "scale": "1:50"}], {}, "REFERENCE_NOT_FOUND"),
        ([{"ref": "NoExiste#1", "scale": "1:50"}], {}, "BLOCK_NOT_FOUND"),
        ([{"ref": "Simple#1", "scale": "1:50"}], {"units": "mm"}, "BAD_REQUEST"),
        ([{"ref": "Simple#1", "scale": "1:50"}], {"margin_mm": 7}, "BAD_REQUEST"),
        ([{"ref": "Simple#1", "scale": "1:50"}], {"hatch_mode": "x"}, "BAD_REQUEST"),
        ([{"ref": "Simple#1", "scale": "1:50"}], {"solids": "todo"}, "BAD_REQUEST"),
        (
            [{"ref": "Simple#1", "scale": "1:50"}],
            {"include_hidden_layers": ["NO_EXISTE"]},
            "BAD_REQUEST",
        ),
        (
            [{"ref": "Simple#1", "scale": "1:50"}],
            {"font_substitutions": "C:/no/existe.json"},
            "BAD_REQUEST",
        ),
        ([], {}, "BAD_REQUEST"),
    ],
)
def test_bad_exports_stop_before_writing_anything(
    running,  # noqa: F811
    tmp_path: Path,
    jobs,
    options,
    code,
) -> None:
    sink, stdin = running
    loaded = _load(sink, stdin, _source(tmp_path / "plano.dxf"))
    out = tmp_path / "paquetes"

    answer = _export(sink, stdin, "c-1", loaded["session"], out, jobs, **options)

    assert answer["type"] == "error" and answer["error"]["code"] == code, answer
    assert not out.exists()


def test_output_dir_must_be_absolute_and_short_enough(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    loaded = _load(sink, stdin, _source(tmp_path / "plano.dxf"))
    jobs = [{"ref": "Simple#1", "scale": "1:50", "package": "P" * 60}]

    relative = _export(sink, stdin, "c-1", loaded["session"], Path("relativa"), jobs)
    deep = tmp_path / ("carpeta_larga_" * 20)
    too_long = _export(sink, stdin, "c-2", loaded["session"], deep, jobs)

    assert relative["error"]["code"] == protocol.OUTPUT_INVALID
    assert too_long["error"]["code"] == protocol.OUTPUT_INVALID
    assert "240" in too_long["error"]["message"]


def test_export_needs_the_current_session(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    _load(sink, stdin, _source(tmp_path / "plano.dxf"))
    jobs = [{"ref": "Simple#1", "scale": "1:50"}]

    answer = _export(sink, stdin, "c-1", "s-99", tmp_path / "p", jobs)

    assert answer["error"]["code"] == protocol.NO_SESSION


def test_export_cleans_dead_temporaries_in_the_output_dir(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    loaded = _load(sink, stdin, _source(tmp_path / "plano.dxf"))
    out = tmp_path / "paquetes"
    stale = out / f"{TEMP_PREFIX}viejo"
    stale.mkdir(parents=True)
    (stale / "pid").write_text("999999999", encoding="ascii")

    _export(sink, stdin, "c-1", loaded["session"], out, [{"ref": "Simple#1", "scale": "1:50"}])

    assert not stale.exists()
    assert not [name for name in _files(out) if name.startswith(TEMP_PREFIX)]


# ----------------------------------------------------------------- cancelar y DXF cambiado


def test_cancel_keeps_published_packages_and_drops_the_rest(
    running,  # noqa: F811
    tmp_path: Path,
    monkeypatch,
) -> None:
    sink, stdin = running
    loaded = _load(sink, stdin, _source(tmp_path / "plano.dxf"))
    out = tmp_path / "paquetes"
    drawing, accepted = threading.Event(), threading.Event()
    real = block_commands.export_vector_outputs
    calls = 0

    def slow(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:  # el segundo trabajo: se cancela mientras dibuja
            drawing.set()
            assert accepted.wait(TIMEOUT)
        return real(*args, **kwargs)

    monkeypatch.setattr(block_commands, "export_vector_outputs", slow)
    jobs = [
        {"job": "t1", "ref": "Simple#1", "scale": "1:50", "package": "A"},
        {"job": "t2", "ref": "Simple#2", "scale": "1:50", "package": "B"},
        {"job": "t3", "ref": "Detalle#1", "scale": "1:50", "package": "C"},
    ]
    stdin.send(
        id="c-1",
        command="export",
        data={"session": loaded["session"], "output_dir": str(out), "jobs": jobs},
    )
    assert drawing.wait(TIMEOUT)
    stdin.send(id="c-2", command="cancel", data={"target": "c-1"})
    assert _final(sink, "c-2")["data"] == {"accepted": True}
    accepted.set()

    answer = _final(sink, "c-1")

    assert answer["type"] == "error" and answer["error"]["code"] == protocol.CANCELLED
    details = answer["error"]["details"]
    assert [job["status"] for job in details["jobs"]] == ["published", "cancelled", "cancelled"]
    assert details["totals"] == {"published": 1, "failed": 0, "cancelled": 2}
    assert "1 publicados" in answer["error"]["message"]
    assert _files(out) == ["A.manifest.json", "A.pdf", "A.txt"]  # sin temporales
    stdin.send(id="c-3", command="status")
    assert _final(sink, "c-3")["data"]["busy"] is False


def test_a_changed_dxf_stops_the_remaining_jobs(
    running,  # noqa: F811
    tmp_path: Path,
    monkeypatch,
) -> None:
    sink, stdin = running
    source = _source(tmp_path / "plano.dxf")
    loaded = _load(sink, stdin, source)
    out = tmp_path / "paquetes"
    real = block_commands.run_extract_block

    def touching(*args, **kwargs):
        result = real(*args, **kwargs)
        later = time.time() + 10
        os.utime(source, (later, later))  # «AutoCAD guardó» tras el primer trabajo
        return result

    monkeypatch.setattr(block_commands, "run_extract_block", touching)
    jobs = [
        {"job": "t1", "ref": "Simple#1", "scale": "1:50", "package": "A"},
        {"job": "t2", "ref": "Simple#2", "scale": "1:50", "package": "B"},
    ]

    answer = _export(sink, stdin, "c-1", loaded["session"], out, jobs)

    assert answer["error"]["code"] == protocol.DXF_CHANGED
    statuses = [job["status"] for job in answer["error"]["details"]["jobs"]]
    assert statuses == ["published", "cancelled"]
    assert _files(out) == ["A.manifest.json", "A.pdf", "A.txt"]
