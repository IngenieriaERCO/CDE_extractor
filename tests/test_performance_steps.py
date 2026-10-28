"""Comportamiento que garantizan los ajustes de rendimiento de la Fase 3."""

from pathlib import Path

import ezdxf
import pytest

from cde_exporter.cli import main


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def _source(path: Path) -> Path:
    """Detalle con una fuente inexistente; fuera del detalle, otra fuente inexistente."""
    document = ezdxf.new()
    document.units = ezdxf.units.InsertUnits.Millimeters
    document.styles.add("Detalle", font="FuenteDelDetalleQueNoExiste.ttf")
    document.styles.add("Fuera", font="FuenteFueraDelDetalle.ttf")
    detail = document.blocks.new("Detalle")
    detail.add_line((0, 0), (10, 5))
    detail.add_text("dentro", height=1, dxfattribs={"style": "Detalle"})
    unused = document.blocks.new("SinUsar")
    unused.add_text("fuera", height=1, dxfattribs={"style": "Fuera"})
    document.modelspace().add_text("suelto", height=1, dxfattribs={"style": "Fuera"})
    document.modelspace().add_blockref("Detalle", (0, 0))
    document.saveas(path)
    return path


def _run(source: Path, out: Path) -> int:
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
        ]
    )


def test_source_dxf_is_read_only_once(tmp_path: Path, monkeypatch) -> None:
    source = _source(tmp_path / "origen.dxf")
    reads = []
    original = ezdxf.readfile

    def counting(path, *args, **kwargs):
        reads.append(Path(path).resolve())
        return original(path, *args, **kwargs)

    monkeypatch.setattr(ezdxf, "readfile", counting)

    assert _run(source, tmp_path / "salida") == 0

    assert reads.count(source.resolve()) == 1


def test_fonts_are_counted_only_in_the_extracted_detail(tmp_path: Path, capsys) -> None:
    source = _source(tmp_path / "origen.dxf")

    assert _run(source, tmp_path / "salida") == 0

    printed = capsys.readouterr().out
    substituted = [line for line in printed.splitlines() if line.startswith("Fuente sustituida")]
    assert any("FuenteDelDetalleQueNoExiste.ttf" in line for line in substituted)
    assert not any("FuenteFueraDelDetalle" in line for line in substituted)


def test_modelspace_bounds_are_computed_once(tmp_path: Path, monkeypatch) -> None:
    import types

    from ezdxf import bbox
    from ezdxf.layouts import Modelspace

    from cde_exporter.cad import exporters, reader

    calls = []

    def counting(entities, *args, **kwargs):
        if isinstance(entities, Modelspace):
            calls.append(entities)
        return bbox.extents(entities, *args, **kwargs)

    # Las cajas del flujo se calculan con reader.safe_extents; el exportador, con la suya.
    fake = types.SimpleNamespace(extents=counting)
    monkeypatch.setattr(reader, "bbox", fake)
    monkeypatch.setattr(exporters, "bbox", fake)

    assert _run(_source(tmp_path / "origen.dxf"), tmp_path / "salida") == 0
    assert len(calls) == 1
    calls.clear()

    out = tmp_path / "huellas"
    arguments = [
        "cad",
        "extract-block",
        str(_source(tmp_path / "origen2.dxf")),
        "--block-name",
        "Detalle",
        "--scale",
        "1:50",
        "--solids",
        "footprint",
        "--output-prefix",
        str(out / "detalle"),
    ]
    assert main(arguments) == 0
    assert len(calls) == 1


# Un 3DSOLID sin datos ACIS no se guarda en el DXF; R2000 admite SAT en texto.
_SAT = ["400 0 1 0", "@33 Spatial Corp.", "1 9.9999999999999995e-07 1e-10", "End-of-ACIS-data"]


def _source_with_warnings(path: Path) -> Path:
    """Detalle que provoca un aviso por cada vía: no visible, omitida y del renderizador."""
    document = ezdxf.new("R2000")
    document.units = ezdxf.units.InsertUnits.Millimeters
    document.layers.add("Apagada").off()
    detail = document.blocks.new("Detalle")
    detail.add_line((0, 0), (10, 5))
    detail.add_line((0, 5), (10, 0), dxfattribs={"layer": "Apagada"})
    detail.add_3dsolid(dxfattribs={"layer": "SOLIDOS"}).sat = _SAT
    document.modelspace().add_blockref("Detalle", (0, 0))
    document.saveas(path)
    return path


def _report_without_date(path: Path) -> str:
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("Fecha y hora")
    )


def test_without_svg_the_pdf_provides_the_same_warnings_and_txt(
    tmp_path: Path, monkeypatch
) -> None:
    from cde_exporter.cad import exporters

    source = _source_with_warnings(tmp_path / "origen.dxf")
    arguments = ["--omit-unsupported"]

    def run(out: Path) -> int:
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
                *arguments,
            ]
        )

    monkeypatch.setenv("CDE_KEEP_INTERMEDIATES", "1")
    with_svg = tmp_path / "con_svg"
    assert run(with_svg) == 0
    assert (with_svg / "detalle.svg").exists()

    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Sin CDE_KEEP_INTERMEDIATES no se debe dibujar el SVG")

    monkeypatch.setattr(exporters, "_draw_to_svg", forbidden)
    without_svg = tmp_path / "sin_svg"
    assert run(without_svg) == 0

    names = sorted(item.name for item in without_svg.iterdir())
    assert names == ["detalle.manifest.json", "detalle.pdf", "detalle.txt"]
    expected = _report_without_date(with_svg / "detalle.txt")
    actual = _report_without_date(without_svg / "detalle.txt")
    assert actual == expected
    notes = " ".join(actual.split("A tener en cuenta", 1)[1].split())
    assert "Se ignoró 1 entidad no visible" in notes
    assert "Se omitió 1 entidad no soportada (--omit-unsupported)" in notes
    assert "Aviso del renderizador (ezdxf): relative point size is not supported" in notes


def test_fonts_are_not_configured_when_the_block_does_not_exist(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from cde_exporter.cad import block_commands

    def fail_if_fonts_run(*_args, **_kwargs):
        raise AssertionError("Las fuentes no deben configurarse antes de validar el bloque")

    monkeypatch.setattr(block_commands, "configure_fonts", fail_if_fonts_run)
    source = _source(tmp_path / "origen.dxf")

    assert (
        main(
            [
                "cad",
                "extract-block",
                str(source),
                "--block-name",
                "NoExiste",
                "--scale",
                "1:50",
                "--output-prefix",
                str(tmp_path / "salida" / "detalle"),
            ]
        )
        == 1
    )
    assert "No existe el bloque 'NoExiste'" in capsys.readouterr().err
