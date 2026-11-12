"""Orden «cad extract-all-blocks»: un paquete por bloque del Model Space (un proyecto)."""

import json
from pathlib import Path

import ezdxf
import pytest

from cde_exporter.cad import block_commands
from cde_exporter.cad.file_names import safe_file_stem, unique_stem
from cde_exporter.cad.manifest import verify_package
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cad.project_commands import (
    REASON_ANONYMOUS,
    REASON_NO_DEFINITION,
    REASON_NOTHING_VISIBLE,
    REASON_XREF,
    SUMMARY_NAME,
    plan_project,
    read_scale_map,
)
from cde_exporter.cli import main


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def _document():
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    return document


def _block(document, name: str, *, layer: str = "0"):
    block = document.blocks.new(name)
    block.add_line((0, 0), (10, 0), dxfattribs={"layer": layer})
    block.add_line((0, 0), (0, 4), dxfattribs={"layer": layer})
    return block


def _project_source(path: Path) -> Path:
    """DXF con dos bloques buenos (uno con dos referencias), uno de nombre raro,
    uno anónimo y uno vacío."""
    document = _document()
    modelspace = document.modelspace()
    _block(document, "DET")
    _block(document, "Detalle 1.5 (Ñ)")
    _block(document, "MURO")
    document.blocks.new("VACIO")
    modelspace.add_blockref("DET", (0, 0))
    modelspace.add_blockref("Detalle 1.5 (Ñ)", (30, 0))
    modelspace.add_blockref("MURO", (60, 0))
    modelspace.add_blockref("MURO", (90, 0))
    modelspace.add_blockref("VACIO", (120, 0))
    anonymous = document.blocks.new_anonymous_block("U")
    anonymous.add_line((0, 0), (1, 1))
    modelspace.add_blockref(anonymous.name, (150, 0))
    document.saveas(path)
    return path


def _run(source: Path, out: Path, *extra: str) -> int:
    return main(["cad", "extract-all-blocks", str(source), "--output-dir", str(out), *extra])


def _names(folder: Path) -> list[str]:
    return sorted(item.name for item in folder.iterdir()) if folder.exists() else []


# ----------------------------------------------------------------- nombres de archivo


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Perfil_K0010", "Perfil_K0010"),
        ("Detalle 1.5 (Ñ)", "Detalle 1_5 (Ñ)"),
        ('a<b>c:d"e\\f|g?h*i', "a_b_c_d_e_f_g_h_i"),
        ("  Sección   Ñandú  ", "Sección Ñandú"),
        ("CON", "CON_"),
        ("nul", "nul_"),
        ("...", "___"),
        ("", "bloque"),
        ("   ", "bloque"),
        ("x" * 200, "x" * 80),
    ],
)
def test_safe_file_stem(name: str, expected: str) -> None:
    assert safe_file_stem(name) == expected


def test_unique_stem_avoids_collisions_ignoring_case_and_numbering() -> None:
    used: set[str] = set()
    assert unique_stem("Muro", 1, used) == "Muro"
    assert unique_stem("MURO", 1, used) == "MURO_2"
    # Dos referencias reservan Det_001 y Det_002; un bloque «Det_002» no puede pisarlas.
    assert unique_stem("Det", 2, used) == "Det"
    assert unique_stem("Det_002", 1, used) == "Det_002_2"


# ------------------------------------------------------------------- plan del proyecto


def test_plan_omits_anonymous_xref_missing_and_invisible_blocks() -> None:
    document = _document()
    modelspace = document.modelspace()
    document.layers.add("APAGADA").off()
    document.layers.add("CONGELADA").freeze()
    _block(document, "BIEN")
    _block(document, "APAGADO", layer="APAGADA")
    document.blocks.new("VACIO")
    document.blocks.new(
        "XR", dxfattribs={"flags": ezdxf.entities.Block.XREF, "xref_path": "base.dxf"}
    )
    anonymous = document.blocks.new_anonymous_block("U")
    anonymous.add_line((0, 0), (1, 1))
    for name in ("BIEN", "APAGADO", "VACIO", "XR", anonymous.name, "AUSENTE"):
        modelspace.add_blockref(name, (0, 0))
    # Contenido en la capa «0»: hereda la capa del INSERT, aquí congelada.
    _block(document, "HEREDA")
    modelspace.add_blockref("HEREDA", (0, 0), dxfattribs={"layer": "CONGELADA"})

    plan = plan_project(document, {}, "1:50")

    assert [package.block_name for package in plan.packages] == ["BIEN"]
    reasons = {item.block_name: item.reason for item in plan.omitted}
    assert reasons == {
        "APAGADO": REASON_NOTHING_VISIBLE,
        "VACIO": REASON_NOTHING_VISIBLE,
        "XR": REASON_XREF,
        anonymous.name: REASON_ANONYMOUS,
        "AUSENTE": REASON_NO_DEFINITION,
        "HEREDA": REASON_NOTHING_VISIBLE,
    }


def test_plan_numbers_references_and_omits_an_invisible_one() -> None:
    document = _document()
    modelspace = document.modelspace()
    _block(document, "MURO")
    modelspace.add_blockref("MURO", (0, 0))
    modelspace.add_blockref("MURO", (20, 0), dxfattribs={"invisible": 1})
    modelspace.add_blockref("MURO", (40, 0))

    plan = plan_project(document, {"muro": "1:20"}, "1:50")

    assert [(p.stem, p.occurrence, p.scale) for p in plan.packages] == [
        ("MURO_001", 1, "1:20"),
        ("MURO_003", 3, "1:20"),
    ]
    assert [(item.occurrence, item.reason) for item in plan.omitted] == [
        (2, REASON_NOTHING_VISIBLE)
    ]


def test_plan_rejects_scale_map_names_that_do_not_exist() -> None:
    document = _document()
    _block(document, "DET")
    document.modelspace().add_blockref("DET", (0, 0))
    with pytest.raises(CadProcessingError, match="«DETALLE»"):
        plan_project(document, {"DETALLE": "1:20"}, "1:50")


# ------------------------------------------------------------------ mapa de escalas


def test_scale_map_accepts_bom_and_normalizes(tmp_path: Path) -> None:
    path = tmp_path / "escalas.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"DET": " 1:50.0 "}).encode("utf-8"))
    assert read_scale_map(path) == {"DET": "1:50"}


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('{"DET": "50"}', "escala no válida"),
        ('{"DET": 50}', "entre comillas"),
        ('["DET"]', "objeto JSON"),
        ('{"DET": "1:50",}', "no es un JSON válido"),
    ],
)
def test_scale_map_errors_are_clear(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "escalas.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(CadProcessingError, match=message):
        read_scale_map(path)


# ---------------------------------------------------------------------- --list


def test_list_writes_nothing_and_template_has_every_block(tmp_path: Path, capsys) -> None:
    source = _project_source(tmp_path / "obra.dxf")
    out = tmp_path / "proyecto"
    overrides = tmp_path / "ajustes.json"
    overrides.write_text('{"MURO": "1:20"}', encoding="utf-8")
    template = tmp_path / "escalas.json"

    code = _run(
        source,
        out,
        "--list",
        "--scale",
        "1:50",
        "--scale-map",
        str(overrides),
        "--write-scale-map",
        str(template),
    )

    assert code == 0
    assert not out.exists()
    assert json.loads(template.read_text(encoding="utf-8")) == {
        "DET": "1:50",
        "Detalle 1.5 (Ñ)": "1:50",
        "MURO": "1:20",
    }
    text = capsys.readouterr().out
    assert "«MURO» (ocurrencia 2 de 2) → MURO_002.pdf | 1:20" in text
    assert "«VACIO»: sin entidades visibles" in text


def test_template_needs_a_default_scale(tmp_path: Path, capsys) -> None:
    source = _project_source(tmp_path / "obra.dxf")
    template = tmp_path / "escalas.json"
    assert _run(source, tmp_path / "p", "--write-scale-map", str(template)) == 1
    assert "--scale 1:N" in capsys.readouterr().err
    assert not template.exists()


# ------------------------------------------------------------------ exportación


def test_exports_every_block_as_its_own_package(tmp_path: Path, capsys) -> None:
    source = _project_source(tmp_path / "obra.dxf")
    out = tmp_path / "via_la_union"

    assert _run(source, out, "--scale", "1:50") == 0

    stems = ["DET", "Detalle 1_5 (Ñ)", "MURO_001", "MURO_002"]
    assert _names(out) == sorted(
        [f"{stem}{suffix}" for stem in stems for suffix in (".manifest.json", ".pdf", ".txt")]
        + [SUMMARY_NAME]
    )
    for stem in stems:
        assert not verify_package(out / f"{stem}.pdf")
    summary = json.loads((out / SUMMARY_NAME).read_text(encoding="utf-8"))
    assert summary["project"] == "obra"
    assert summary["source_dxf"] == "obra.dxf"
    assert summary["default_scale"] == "1:50"
    assert summary["totals"] == {"packages": 4, "omitted": 2, "failed": 0}
    first = summary["packages"][1]
    assert first["block_name"] == "Detalle 1.5 (Ñ)"
    assert first["pdf"] == "Detalle 1_5 (Ñ).pdf"
    assert first["status"] in {"complete", "complete_with_warnings"}
    assert [item["reason"] for item in summary["omitted"]] == [
        REASON_NOTHING_VISIBLE,
        REASON_ANONYMOUS,
    ]
    manifest = json.loads((out / "MURO_002.manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"]["reference"]["occurrence"] == 2
    text = capsys.readouterr().out
    assert "Paquetes publicados: 4 de 4" in text
    assert "Omitidos: 2" in text
    assert "Fallidos: 0" in text


def test_a_failed_block_does_not_stop_the_others(tmp_path: Path, monkeypatch, capsys) -> None:
    source = _project_source(tmp_path / "obra.dxf")
    out = tmp_path / "proyecto"
    real = block_commands.run_extract_block

    def fail_for_det(args, document=None):
        if args.block_name == "DET":
            raise CadProcessingError("fallo simulado de DET")
        return real(args, document)

    monkeypatch.setattr(block_commands, "run_extract_block", fail_for_det)

    assert _run(source, out, "--scale", "1:50") == 1

    names = _names(out)
    assert not any(name.startswith("DET.") for name in names)
    assert "MURO_001.pdf" in names and "MURO_002.manifest.json" in names
    summary = json.loads((out / SUMMARY_NAME).read_text(encoding="utf-8"))
    assert summary["totals"] == {"packages": 3, "omitted": 2, "failed": 1}
    assert summary["failed"][0]["block_name"] == "DET"
    assert summary["failed"][0]["reason"] == "fallo simulado de DET"
    captured = capsys.readouterr()
    assert "Fallidos: 1" in captured.out
    assert "«DET»: fallo simulado de DET" in captured.out
    assert "1 paquete(s) fallaron" in captured.err


def test_missing_scale_stops_before_creating_anything(tmp_path: Path, capsys) -> None:
    source = _project_source(tmp_path / "obra.dxf")
    out = tmp_path / "proyecto"
    scales = tmp_path / "escalas.json"
    scales.write_text('{"DET": "1:20"}', encoding="utf-8")

    assert _run(source, out, "--scale-map", str(scales)) == 1

    assert not out.exists()
    err = capsys.readouterr().err
    assert "Falta la escala de" in err and "«MURO»" in err and "«DET»" not in err


def test_existing_outputs_need_overwrite(tmp_path: Path, monkeypatch, capsys) -> None:
    source = _project_source(tmp_path / "obra.dxf")
    out = tmp_path / "proyecto"
    out.mkdir()
    (out / "MURO_002.pdf").write_bytes(b"previo")
    monkeypatch.setattr(
        block_commands,
        "run_extract_block",
        lambda *args, **kwargs: pytest.fail("no debería exportar nada"),
    )

    assert _run(source, out, "--scale", "1:50") == 1

    assert _names(out) == ["MURO_002.pdf"]
    assert "--overwrite" in capsys.readouterr().err


def test_dxf_without_exportable_blocks_is_an_error(tmp_path: Path, capsys) -> None:
    document = _document()
    document.blocks.new("VACIO")
    document.modelspace().add_blockref("VACIO", (0, 0))
    source = tmp_path / "vacio.dxf"
    document.saveas(source)

    assert _run(source, tmp_path / "p", "--scale", "1:50") == 1
    assert not (tmp_path / "p").exists()
    assert "no tiene bloques que exportar" in capsys.readouterr().err


def test_long_failure_reasons_are_shortened_only_in_the_terminal(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    source = _project_source(tmp_path / "obra.dxf")
    out = tmp_path / "proyecto"
    reason = "ezdxf omite entidades: " + ", ".join(["3DSOLID: unsupported"] * 500)

    def always_fail(args, document=None):
        raise CadProcessingError(reason)

    monkeypatch.setattr(block_commands, "run_extract_block", always_fail)

    assert _run(source, out, "--scale", "1:50") == 1

    summary = json.loads((out / SUMMARY_NAME).read_text(encoding="utf-8"))
    assert summary["failed"][0]["reason"] == reason
    captured = capsys.readouterr()
    assert reason not in captured.out + captured.err
    assert f"(completo en {SUMMARY_NAME})" in captured.out
