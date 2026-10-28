import hashlib
import math
from pathlib import Path

import ezdxf
import pymupdf
import pytest
from ezdxf import disassemble
from ezdxf.acis import api as acis
from ezdxf.render import forms

from cde_exporter.cad import block_commands
from cde_exporter.cad.detail_model import (
    FOOTPRINT_REASON,
    DetailSource,
    EntityCount,
    EntityDisposition,
    EntityStatus,
    ExtractedDetail,
    SelectedReference,
    UnitInfo,
    mark_footprint_solids,
)
from cde_exporter.cad.exporters import export_vector_outputs
from cde_exporter.cad.models import CadProcessingError
from cde_exporter.cli import main

# Caja de 4 × 2 × 1 centrada en el origen del bloque: su huella es (-2, -1)–(2, 1).
_BOX = forms.cube().scale(4, 2, 1)
_L_PROFILE = [(0, 0), (3, 0), (3, 1), (1, 1), (1, 3), (0, 3)]


def _add_solid(layout, mesh=_BOX, **attributes):
    solid = layout.add_3dsolid(dxfattribs={"layer": "SOLIDOS", **attributes})
    acis.export_dxf(solid, [acis.body_from_mesh(mesh)])
    return solid


def _source(
    path: Path,
    *,
    units=ezdxf.units.InsertUnits.Millimeters,
    inserts=None,
    extra=None,
    version: str = "R2018",
):
    """Bloque «Detalle» con una línea y referencias al bloque «Modulo» (una caja ACIS).

    R2018 guarda el ACIS en binario (SAB) y admite color real; R2000 solo SAT en texto.
    """
    document = ezdxf.new(version)
    document.units = units
    module = document.blocks.new("Modulo")
    _add_solid(module, color=3, true_color=ezdxf.rgb2int((10, 20, 30)))
    detail = document.blocks.new("Detalle")
    detail.add_line((0, 0), (1, 1))
    for attributes in inserts if inserts is not None else [{"insert": (10, 0)}]:
        attributes = dict(attributes)
        detail.add_blockref("Modulo", attributes.pop("insert"), dxfattribs=attributes)
    if extra is not None:
        extra(document, detail)
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
            "--output-prefix",
            str(out / "detalle"),
            *extra,
        ]
    )


def _names(folder: Path) -> list[str]:
    return sorted(item.name for item in folder.iterdir()) if folder.exists() else []


def _flat(text: str) -> str:
    return " ".join(text.split())


def _notes(report: str) -> str:
    return _flat(report.split("A tener en cuenta", 1)[1])


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def test_default_stays_strict(tmp_path: Path, capsys) -> None:
    out = tmp_path / "salida"

    assert _run(_source(tmp_path / "origen.dxf"), out, "--scale", "1:50") == 1

    captured = capsys.readouterr()
    message = _flat(captured.out + captured.err)
    assert "3DSOLID: unsupported" in message
    assert "--solids footprint (dibujar los 3DSOLID como huella 2D)" in message
    assert "--omit-unsupported (omitirlos)" in message
    assert message.count("--omit-unsupported") == 1
    assert _names(out) == []


def test_exporter_without_extract_block_signal_keeps_generic_hint(tmp_path: Path) -> None:
    flat = tmp_path / "plano.dxf"
    document = ezdxf.new("R2018")
    document.modelspace().add_line((0, 0), (1, 1))
    _add_solid(document.modelspace())
    document.saveas(flat)

    # Sin la señal de extract-block (p. ej. otros comandos) se mantiene la pista genérica.
    with pytest.raises(CadProcessingError) as error:
        export_vector_outputs(flat, tmp_path / "a.svg", tmp_path / "a.pdf")
    assert "Puede usar --omit-unsupported en cad extract-block" in str(error.value)
    assert "--solids footprint" not in str(error.value)


def test_footprint_marks_solids_as_transformed(tmp_path: Path, capsys) -> None:
    out = tmp_path / "salida"
    source = _source(
        tmp_path / "origen.dxf",
        inserts=[{"insert": (10, 0)}, {"insert": (20, 0)}, {"insert": (30, 0)}],
    )

    assert _run(source, out, "--scale", "1:50", "--solids", "footprint") == 0

    assert _names(out) == ["detalle.manifest.json", "detalle.pdf", "detalle.txt"]
    report = (out / "detalle.txt").read_text(encoding="utf-8")
    assert "3DSOLID | SOLIDOS | transformadas | 3" in report
    assert f"3DSOLID | SOLIDOS | transformadas | {FOOTPRINT_REASON} | 3" in report
    notes = _notes(report)
    assert (
        "Se dibujaron 3 3DSOLID como huella 2D (proyección en planta; 1 sólido distinto); "
        "error máximo estimado 0.000 mm en el papel."
    ) in notes
    assert "La huella muestra la planta del sólido, no su volumen ni su altura." in notes
    assert "Estado final: COMPLETA CON ADVERTENCIAS" in report
    assert (
        "Huellas: 3 3DSOLID dibujados como huella 2D (1 sólido distinto)."
        in capsys.readouterr().out
    )


def test_footprint_replaces_once_per_definition_and_keeps_style(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CDE_KEEP_INTERMEDIATES", "1")
    out = tmp_path / "salida"
    source = _source(
        tmp_path / "origen.dxf",
        inserts=[{"insert": (10, 0)}, {"insert": (20, 0)}, {"insert": (30, 0)}],
    )

    assert _run(source, out, "--scale", "1:50", "--solids", "footprint") == 0

    render = ezdxf.readfile(out / "detalle.dxf")
    module = list(render.blocks.get("Modulo"))
    assert [entity.dxftype() for entity in module] == ["LWPOLYLINE"]
    polyline = module[0]
    assert polyline.closed
    assert polyline.dxf.layer == "SOLIDOS"
    assert polyline.dxf.color == 3
    assert polyline.dxf.true_color == ezdxf.rgb2int((10, 20, 30))
    assert sorted(polyline.get_points("xy")) == [(-2, -1), (-2, 1), (2, -1), (2, 1)]


def test_bylayer_and_byblock_colors_are_kept(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CDE_KEEP_INTERMEDIATES", "1")
    out = tmp_path / "salida"

    def byblock(document, detail):
        block = document.blocks.new("PorBloque")
        _add_solid(block, color=0)
        detail.add_blockref("PorBloque", (40, 0))

    source = _source(tmp_path / "origen.dxf", extra=byblock)

    assert _run(source, out, "--scale", "1:50", "--solids", "footprint") == 0

    render = ezdxf.readfile(out / "detalle.dxf")
    assert next(iter(render.blocks.get("PorBloque"))).dxf.color == 0


def _drawn_footprints(path: Path) -> list[list[tuple[float, float]]]:
    render = ezdxf.readfile(path)
    return [
        sorted((round(v.x, 6), round(v.y, 6)) for v in entity.vertices_in_wcs())
        for entity in disassemble.recursive_decompose(render.modelspace())
        if entity.dxftype() == "LWPOLYLINE"
    ]


def test_rotation_nonuniform_scale_and_mirror_match_exact_projection(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CDE_KEEP_INTERMEDIATES", "1")
    out = tmp_path / "salida"
    attributes = {"insert": (10, 5), "rotation": 37, "xscale": -2, "yscale": 0.5, "zscale": 3}
    source = _source(tmp_path / "origen.dxf", inserts=[attributes])

    assert _run(source, out, "--scale", "1:50", "--solids", "footprint") == 0

    document = ezdxf.new()
    document.blocks.new("Modulo")
    reference = document.modelspace().add_blockref(
        "Modulo", (10, 5), dxfattribs={"rotation": 37, "xscale": -2, "yscale": 0.5, "zscale": 3}
    )
    moved = reference.matrix44().transform_vertices(_BOX.vertices)
    exact = sorted({(round(v.x, 6), round(v.y, 6)) for v in moved})
    assert _drawn_footprints(out / "detalle.dxf") == [exact]


def test_page_section_c_and_model_use_render_bounds(tmp_path: Path, model_details) -> None:
    out = tmp_path / "salida"
    source = _source(tmp_path / "origen.dxf")
    arguments = ("--scale", "1:50", "--solids", "footprint")

    assert _run(source, out, *arguments) == 0

    data = model_details[0]
    assert data["bounds"]["min_xyz"][:2] == pytest.approx([0, -1])
    assert data["bounds"]["max_xyz"][:2] == pytest.approx([12, 1])
    assert data["content_size_mm"] == pytest.approx([12 * 0.02, 2 * 0.02])
    with pymupdf.open(out / "detalle.pdf") as pdf:
        measured = (pdf[0].rect.width * 25.4 / 72, pdf[0].rect.height * 25.4 / 72)
    assert data["page_size_mm"] == pytest.approx(list(measured), abs=0.01)
    assert "ENTITIES_WITHOUT_BOUNDS" not in [item["code"] for item in data["warnings"]]
    assert data["model_version"] == "2"
    solids = [item for item in data["dispositions"] if item["entity_type"] == "3DSOLID"]
    assert [(item["status"], item["reason"]) for item in solids] == [
        ("transformed", FOOTPRINT_REASON)
    ]
    report = (out / "detalle.txt").read_text(encoding="utf-8")
    assert "(0.000, -1.000" in report
    assert "(12.000, 1.000" in report


def _tilted(degrees: float) -> dict:
    angle = math.radians(degrees)
    return {"insert": (10, 0), "extrusion": (0, math.sin(angle), math.cos(angle))}


def test_small_tilt_is_accepted_and_reports_its_error(tmp_path: Path) -> None:
    out = tmp_path / "salida"
    source = _source(
        tmp_path / "origen.dxf", units=ezdxf.units.InsertUnits.Meters, inserts=[_tilted(0.3)]
    )

    assert _run(source, out, "--scale", "1:50", "--solids", "footprint") == 0

    # Media altura 0.5 m × sen(0.3°) × 20 mm por metro a 1:50 ≈ 0.052 mm.
    notes = _notes((out / "detalle.txt").read_text(encoding="utf-8"))
    assert "error máximo estimado 0.052 mm en el papel" in notes


def test_large_tilt_is_not_converted_and_follows_strict_rule(tmp_path: Path, capsys) -> None:
    out = tmp_path / "salida"
    source = _source(
        tmp_path / "origen.dxf", units=ezdxf.units.InsertUnits.Meters, inserts=[_tilted(30)]
    )

    assert _run(source, out, "--scale", "1:50", "--solids", "footprint") == 1

    captured = capsys.readouterr()
    message = _flat(captured.out + captured.err)
    assert "3DSOLID: unsupported" in message
    assert "INSERT inclinado: error de huella 5.000 mm > 0.1 mm (1)" in message
    # Con --solids footprint ya activo, solo tiene sentido sugerir la omisión.
    assert "Puede usar --omit-unsupported en cad extract-block" in message
    assert "dibujar los 3DSOLID como huella 2D" not in message
    assert _names(out) == []


def test_large_tilt_with_omit_unsupported_is_omitted_with_reason(tmp_path: Path) -> None:
    out = tmp_path / "salida"
    source = _source(
        tmp_path / "origen.dxf", units=ezdxf.units.InsertUnits.Meters, inserts=[_tilted(30)]
    )
    arguments = ("--scale", "1:50", "--solids", "footprint", "--omit-unsupported")

    assert _run(source, out, *arguments) == 0

    report = (out / "detalle.txt").read_text(encoding="utf-8")
    assert "3DSOLID | SOLIDOS | omitidas | 1" in report
    assert (
        "1 sólido distinto no se pudo convertir en huella 2D: "
        "INSERT inclinado: error de huella 5.000 mm > 0.1 mm (1)."
    ) in _notes(report)


def test_non_convex_and_invalid_acis_are_not_converted(tmp_path: Path) -> None:
    out = tmp_path / "salida"

    def bad_solids(document, detail):
        prism = forms.extrude(_L_PROFILE, [(0, 0, 0), (0, 0, 1)], close=True, caps=True)
        _add_solid(detail, prism)
        empty = detail.add_3dsolid(dxfattribs={"layer": "SOLIDOS"})
        empty.sat = ["400 0 1 0", "@33 Spatial Corp.", "1 1e-06 1e-10", "End-of-ACIS-data"]

    source = _source(tmp_path / "origen.dxf", extra=bad_solids, version="R2000")
    arguments = ("--scale", "1:50", "--solids", "footprint", "--omit-unsupported")

    assert _run(source, out, *arguments) == 0

    report = (out / "detalle.txt").read_text(encoding="utf-8")
    assert "3DSOLID | SOLIDOS | omitidas | 2" in report
    assert "3DSOLID | SOLIDOS | transformadas | 1" in report
    notes = _notes(report)
    assert "2 sólidos distintos no se pudieron convertir en huella 2D" in notes
    assert "huella no convexa (1)" in notes


def test_no_footprint_line_when_the_block_has_no_solids(tmp_path: Path, capsys) -> None:
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Millimeters
    document.blocks.new("Detalle").add_line((0, 0), (10, 5))
    document.modelspace().add_blockref("Detalle", (0, 0))
    source = tmp_path / "origen.dxf"
    document.saveas(source)

    assert _run(source, tmp_path / "salida", "--scale", "1:50", "--solids", "footprint") == 0

    assert "Huellas:" not in capsys.readouterr().out


def test_source_dxf_is_never_modified(tmp_path: Path) -> None:
    source = _source(tmp_path / "origen.dxf")
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    assert _run(source, tmp_path / "salida", "--scale", "1:50", "--solids", "footprint") == 0

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_fit_mode_without_scale_includes_footprints(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(block_commands, "_is_interactive", lambda: False)
    out = tmp_path / "salida"

    assert _run(_source(tmp_path / "origen.dxf"), out, "--solids", "footprint") == 0

    with pymupdf.open(out / "detalle.pdf") as pdf:
        width = pdf[0].rect.width * 25.4 / 72
    # 12 mm de geometría (línea + huella) a 1:1 más 5 mm de margen por lado.
    assert width == pytest.approx(22, abs=0.5)


def _model() -> ExtractedDetail:
    return ExtractedDetail(
        source=DetailSource("entrada.dxf", "Detalle"),
        reference=SelectedReference(1, "A1", "0", (0, 0, 0), 0, (1, 1, 1)),
        units=UnitInfo(4, "mm", "declaradas en el DXF y coherentes", 1.0, ()),
        scale=None,
        bounds=None,
        inventory=(
            EntityCount("3DSOLID", "S", EntityStatus.UNSUPPORTED, 3),
            EntityCount("REGION", "S", EntityStatus.UNSUPPORTED, 1),
        ),
        dispositions=(
            EntityDisposition(0, "A", "3DSOLID", "S", EntityStatus.UNSUPPORTED, "x"),
            EntityDisposition(1, "A", "3DSOLID", "S", EntityStatus.UNSUPPORTED, "x"),
            EntityDisposition(2, "B", "3DSOLID", "S", EntityStatus.UNSUPPORTED, "x"),
            EntityDisposition(3, "A", "REGION", "S", EntityStatus.UNSUPPORTED, "x"),
        ),
        dependencies=(),
        warnings=(),
    )


def test_mark_footprint_uses_handles_of_3dsolid_only() -> None:
    marked = mark_footprint_solids(_model(), ("A",), (("huella no convexa", 1),), 0.25)

    assert [item.status for item in marked.dispositions] == [
        EntityStatus.TRANSFORMED,
        EntityStatus.TRANSFORMED,
        EntityStatus.UNSUPPORTED,
        EntityStatus.UNSUPPORTED,
    ]
    assert [(row.entity_type, row.status, row.count) for row in marked.inventory] == [
        ("3DSOLID", EntityStatus.TRANSFORMED, 2),
        ("3DSOLID", EntityStatus.UNSUPPORTED, 1),
        ("REGION", EntityStatus.UNSUPPORTED, 1),
    ]
    assert [warning.code for warning in marked.warnings] == [
        "SOLIDS_AS_FOOTPRINT",
        "SOLIDS_NOT_CONVERTED",
    ]
    assert "error máximo estimado 0.250 mm" in marked.warnings[0].message


def test_mark_footprint_without_changes_returns_same_detail() -> None:
    detail = _model()

    assert mark_footprint_solids(detail, ()) is detail
