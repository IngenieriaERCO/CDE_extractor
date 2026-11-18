"""Lectura selectiva de DXF muy grandes (``large_dxf.write_selective`` y ``dxf_session``): la
copia guarda del Model Space solo los INSERT (con sus ATTRIB) y el Paper Space entero; lo suelto
se resume por capa. La copia se guarda en caché por el SHA-256 del DXF. Si no cabe en memoria,
``DXF_TOO_LARGE``."""

import hashlib
import json
from pathlib import Path

import ezdxf
import pytest
from ezdxf import bbox
from test_worker import TIMEOUT, running  # noqa: F401 - fixture del trabajador en el proceso

from cde_exporter.cad import dxf_session
from cde_exporter.cad import worker_protocol as protocol
from cde_exporter.cad.dxf_session import (
    CACHE_DIR,
    CACHE_ENTRIES,
    TEMP_PREFIX,
    DxfTooLargeError,
    load_dxf,
)
from cde_exporter.cad.inventory import build_inventory
from cde_exporter.cad.large_dxf import LARGE_DXF_ENV, SELECTIVE_DXF_ENV, write_selective
from cde_exporter.cad.manifest import verify_package
from cde_exporter.cad.reader import read_dxf


@pytest.fixture(autouse=True)
def _selective(monkeypatch):
    """Lectura selectiva siempre (en un DXF real, desde 200 MB y una copia de 500 MB)."""
    monkeypatch.setenv(LARGE_DXF_ENV, "0")
    monkeypatch.setenv(SELECTIVE_DXF_ENV, "0")
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def _source(path: Path, extra: str = "") -> Path:
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    document.layers.add("APAGADA").off()
    block = document.blocks.new("B")
    block.add_line((0, 0), (10, 0))
    block.add_line((10, 0), (10, 4))
    block.add_attdef("ROTULO", (0, 5))
    unused = document.blocks.new("SIN_USAR")
    for index in range(30):
        unused.add_line((0, index), (5, index))
    modelspace = document.modelspace()
    insert = modelspace.add_blockref("B", (1000, 2000))
    insert.add_attrib("ROTULO", "Texto Ñ", (1000, 2005))
    modelspace.add_line((0, 0), (50, 0), dxfattribs={"layer": "L1"})
    modelspace.add_lwpolyline([(0, 0), (10, 10), (20, -5)], dxfattribs={"layer": "L1"})
    modelspace.add_polyline2d(
        [(100, 100), (110, 100), (110, 120), (100, 120)], dxfattribs={"layer": "P"}
    )
    modelspace.add_circle((300, 300), 2, dxfattribs={"layer": "C"})
    hatch = modelspace.add_hatch(dxfattribs={"layer": "H"})
    hatch.paths.add_polyline_path([(500, 500), (510, 500), (510, 510)], is_closed=True)
    hatch.set_seed_points([(-9999, -9999)])  # una semilla lejana: no cuenta para la caja
    modelspace.add_text("oculto", dxfattribs={"layer": "APAGADA", "insert": (700, 700)})
    document.layouts.get("Layout1").add_line((0, 0), (1, 1), dxfattribs={"layer": "PAPEL"})
    if extra:
        document.header["$PROJECTNAME"] = extra
    document.saveas(path)
    return path


def _loose(rows: list[dict]) -> dict[str, dict]:
    return {row["layer"]: row for row in rows}


# ----------------------------------------------------------------- la copia


def test_selective_copy_keeps_inserts_and_paper_space_only(tmp_path: Path) -> None:
    source = _source(tmp_path / "plano.dxf")
    copy = tmp_path / "copia.dxf"

    written, loose = write_selective(source, copy, {"sin_usar"})

    assert written == copy.stat().st_size < source.stat().st_size
    document = read_dxf(copy)
    modelspace = list(document.modelspace())
    assert [entity.dxftype() for entity in modelspace] == ["INSERT"]
    assert [attrib.dxf.text for attrib in modelspace[0].attribs] == ["Texto Ñ"]
    assert [e.dxftype() for e in document.layouts.get("Layout1")] == ["LINE"]
    assert len(document.blocks["B"]) == 3 and len(document.blocks["SIN_USAR"]) == 0
    assert document.audit().has_errors is False
    rows = {item.layer: item for item in loose}
    assert {name: rows[name].entities for name in rows} == {
        "APAGADA": 1,
        "C": 1,
        "H": 1,
        "L1": 2,
        "P": 1,
    }
    assert rows["L1"].types == {"LINE": 1, "LWPOLYLINE": 1}
    assert rows["L1"].vertices == 3 and rows["P"].vertices == 4
    # Cajas aproximadas: las de ezdxf (sin la cabecera (0, 0) de la POLYLINE, con el radio del
    # círculo y sin la semilla del HATCH).
    original = read_dxf(source)
    for layer in ("L1", "P", "C", "H"):
        exact = bbox.extents(e for e in original.modelspace() if e.dxf.layer == layer)
        assert rows[layer].bounds == pytest.approx(
            (exact.extmin.x, exact.extmin.y, exact.extmax.x, exact.extmax.y), abs=1e-6
        )


# ----------------------------------------------------------------- load_dxf y la caché


def test_load_reads_the_selective_copy_with_the_same_blocks(tmp_path: Path) -> None:
    source = _source(tmp_path / "plano.dxf")

    loaded = load_dxf(source, tmp_path / "trabajo")

    assert loaded.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert loaded.source_copy["mode"] == "selective" and loaded.source_copy["cached"] is False
    assert loaded.source_copy["loose_entities"] == 6
    codes = [notice["code"] for notice in loaded.notices]
    assert codes == ["SOURCE_UNUSED_BLOCKS_SKIPPED", "SOURCE_MODELSPACE_LOOSE_SKIPPED"]
    selective = build_inventory(loaded.document, source_copy=loaded.pruned)
    normal = build_inventory(read_dxf(source))
    assert [(b["ref"], b["bounds"], b["entities"]) for b in selective["blocks"]] == [
        (b["ref"], b["bounds"], b["entities"]) for b in normal["blocks"]
    ]
    rows = _loose(selective["modelspace_loose"])
    assert rows["APAGADA"]["state"] == "apagada" and rows["L1"]["state"] == "visible"
    assert rows["P"]["vertices"] == 4 and rows["C"]["bounds_approx"] == {
        "min": [298.0, 298.0],
        "max": [302.0, 302.0],
    }
    normal_counts = {(r["layer"], r["state"]): r["entities"] for r in normal["modelspace_loose"]}
    assert {(r["layer"], r["state"]): r["entities"] for r in rows.values()} == normal_counts
    assert not list((tmp_path / "trabajo").glob(f"{TEMP_PREFIX}*"))


def test_the_copy_is_cached_by_the_dxf_hash(tmp_path: Path) -> None:
    source = _source(tmp_path / "plano.dxf")
    work = tmp_path / "trabajo"
    first = load_dxf(source, work)

    again = load_dxf(source, work)

    assert again.pruned.cached is True and again.source_copy["cached"] is True
    assert again.sha256 == first.sha256
    entries = list((work / CACHE_DIR).iterdir())
    assert [entry.name for entry in entries] == [first.sha256]
    info = json.loads((entries[0] / "copia.json").read_text(encoding="utf-8"))
    assert info["fingerprint"] == first.fingerprint and info["bytes"] == first.bytes
    # Otro contenido (otro hash): otra copia.
    _source(source, extra="otro")
    changed = load_dxf(source, work)
    assert changed.pruned.cached is False and changed.sha256 != first.sha256


def test_the_cache_keeps_the_most_recent_copies(tmp_path: Path) -> None:
    work = tmp_path / "trabajo"
    hashes = []
    for index in range(CACHE_ENTRIES + 1):
        source = _source(tmp_path / f"plano{index}.dxf", extra=f"p{index}")
        hashes.append(load_dxf(source, work).sha256)

    kept = sorted(entry.name for entry in (work / CACHE_DIR).iterdir())

    assert kept == sorted(hashes[1:])


def test_a_dxf_that_does_not_fit_is_too_large(tmp_path: Path, monkeypatch) -> None:
    source = _source(tmp_path / "plano.dxf")
    monkeypatch.setattr(dxf_session, "available_memory_bytes", lambda: 1024)

    with pytest.raises(DxfTooLargeError, match="No hay memoria para leer este DXF"):
        load_dxf(source, tmp_path / "trabajo")


def test_a_cancelled_copy_leaves_nothing(tmp_path: Path) -> None:
    source = _source(tmp_path / "plano.dxf")
    work = tmp_path / "trabajo"

    def cancel(stage, fraction, text):
        if fraction > 0:
            raise RuntimeError("cancelado")

    with pytest.raises(RuntimeError):
        load_dxf(source, work, cancel)

    assert not list(work.glob(f"{TEMP_PREFIX}*")) and not (work / CACHE_DIR).exists()


# ----------------------------------------------------------------- trabajador


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


def test_worker_exports_from_the_selective_copy(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    source = _source(tmp_path / "plano.dxf")
    stdin.send(id="c-1", command="load", data={"dxf": str(source)})
    loaded = _final(sink, "c-1")["data"]
    out = tmp_path / "paquetes"

    stdin.send(
        id="c-2",
        command="export",
        data={
            "session": loaded["session"],
            "output_dir": str(out),
            "jobs": [{"ref": "B#1", "scale": "1:100"}],
        },
    )
    answer = _final(sink, "c-2")

    assert loaded["dxf"]["source_copy"]["mode"] == "selective"
    assert "SOURCE_MODELSPACE_LOOSE_SKIPPED" in {n["code"] for n in loaded["notices"]}
    job = answer["data"]["jobs"][0]
    assert job["status"] == "published", answer
    assert "SOURCE_MODELSPACE_LOOSE_SKIPPED" in {n["code"] for n in job["notices"]}
    manifest = json.loads(Path(job["package"]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["source"]["dxf_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert verify_package(job["package"]["pdf"]) == ()


def test_worker_says_dxf_too_large(running, tmp_path: Path, monkeypatch) -> None:  # noqa: F811
    sink, stdin = running
    source = _source(tmp_path / "plano.dxf")
    monkeypatch.setattr(dxf_session, "available_memory_bytes", lambda: 1024)

    stdin.send(id="c-1", command="load", data={"dxf": str(source)})
    answer = _final(sink, "c-1")

    assert answer["error"]["code"] == protocol.DXF_TOO_LARGE
    assert "GB" in answer["error"]["message"] and answer["error"]["advice"]
