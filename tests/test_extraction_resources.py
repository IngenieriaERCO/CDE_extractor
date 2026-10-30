"""La extracción conserva los recursos por defecto del original (Standard, capa 0...)."""

import hashlib
from pathlib import Path

import ezdxf
import pytest
from ezdxf import bbox, xref
from ezdxf.entities.mleader import MultiLeader
from ezdxf.math import Vec2
from ezdxf.render.mleader import ConnectionSide

from cde_exporter.cad.block_extractor import extract_block_reference
from cde_exporter.cli import main


def _source(path: Path, *, ttf: bool = False) -> Path:
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Millimeters
    standard = document.styles.get("Standard")
    if ttf:
        standard.dxf.font = "arial.ttf"
        standard.set_extended_font_data("Arial", italic=True, bold=False)
    else:
        standard.dxf.font = "simplex.shx"
    standard.dxf.width = 0.8
    standard.dxf.oblique = 15
    layer_zero = document.layers.get("0")
    layer_zero.dxf.color = 3
    layer_zero.dxf.lineweight = 50
    document.layers.get("Defpoints").dxf.color = 5
    document.header["$PDMODE"] = 32
    document.header["$PDSIZE"] = 2.5
    document.header["$LTSCALE"] = 5.0
    document.header["$MEASUREMENT"] = 1
    document.styles.add("Propio", font="romans.shx")
    block = document.blocks.new("Detalle")
    block.add_line((0, 0), (10, 5))
    block.add_mtext("texto con Standard")
    block.add_text("texto propio", height=1, dxfattribs={"style": "Propio"})
    document.modelspace().add_blockref("Detalle", (0, 0))
    document.saveas(path)
    return path


def _extract(tmp_path: Path, source: Path):
    target = tmp_path / "extraido.dxf"
    extract_block_reference(source, "Detalle", 1, target, overwrite=True)
    return ezdxf.readfile(target)


def test_standard_text_style_keeps_original_values(tmp_path: Path) -> None:
    extracted = _extract(tmp_path, _source(tmp_path / "origen.dxf"))

    standard = extracted.styles.get("Standard")
    assert standard.dxf.font == "simplex.shx"
    assert standard.dxf.width == 0.8
    assert standard.dxf.oblique == 15
    assert not standard.has_extended_font_data
    assert extracted.styles.get("Propio").dxf.font == "romans.shx"


def test_standard_ttf_family_in_xdata_is_kept(tmp_path: Path) -> None:
    extracted = _extract(tmp_path, _source(tmp_path / "origen.dxf", ttf=True))

    standard = extracted.styles.get("Standard")
    assert standard.dxf.font == "arial.ttf"
    assert standard.get_extended_font_data() == ("Arial", True, False)


def test_default_layers_keep_original_values(tmp_path: Path) -> None:
    extracted = _extract(tmp_path, _source(tmp_path / "origen.dxf"))

    assert extracted.layers.get("0").dxf.color == 3
    assert extracted.layers.get("0").dxf.lineweight == 50
    assert extracted.layers.get("Defpoints").dxf.color == 5


def test_header_variables_read_by_the_renderer_are_kept(tmp_path: Path) -> None:
    extracted = _extract(tmp_path, _source(tmp_path / "origen.dxf"))

    assert extracted.header["$PDMODE"] == 32
    assert extracted.header["$PDSIZE"] == 2.5
    assert extracted.header["$LTSCALE"] == 5.0
    assert extracted.header["$MEASUREMENT"] == 1


def test_source_is_not_modified(tmp_path: Path) -> None:
    source = _source(tmp_path / "origen.dxf")
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    _extract(tmp_path, source)

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_txt_reports_the_original_standard_font(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)
    source = _source(tmp_path / "origen.dxf")
    out = tmp_path / "salida"

    arguments = [
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
    assert main(arguments) == 0

    report = (out / "detalle.txt").read_text(encoding="utf-8")
    assert "txt →" not in report
    assert "Fuente sustituida: txt" not in report
    assert "simplex.shx" in report


def _multileader_source(path: Path) -> Path:
    """MULTILEADER con estilo de texto propio y entidades antes, para que los handles crucen."""
    document = ezdxf.new("R2018")
    document.styles.add("MiEstilo", font="arial.ttf")
    block = document.blocks.new("Detalle")
    for index in range(30):
        block.add_line((index, 0), (index, 1))
    builder = block.add_multileader_mtext("Standard")
    builder.set_content("Nota", style="MiEstilo")
    builder.add_leader_line(ConnectionSide.left, [(0, 0), (3, 3)])
    builder.build(insert=Vec2(10, 10))
    document.modelspace().add_blockref("Detalle", (0, 0))
    document.saveas(path)
    return path


def _text_style_of_multileader(document) -> object:
    multileader = document.blocks.get("Detalle").query("MULTILEADER")[0]
    return document.entitydb.get(multileader.context.mtext.style_handle)


def test_multileader_text_style_survives_extraction(tmp_path: Path) -> None:
    original = MultiLeader.map_resources
    extracted = _extract(tmp_path, _multileader_source(tmp_path / "origen.dxf"))

    style = _text_style_of_multileader(extracted)
    assert style is not None and style.dxftype() == "STYLE"
    assert style.dxf.name == "MiEstilo"
    for insert in extracted.modelspace().query("INSERT"):
        assert bbox.extents(insert.virtual_entities()).has_data
    multileader = extracted.blocks.get("Detalle").query("MULTILEADER")[0]
    assert [entity.dxftype() for entity in multileader.virtual_entities()]
    assert MultiLeader.map_resources is original  # el método de ezdxf queda como estaba


def test_ezdxf_still_maps_multileader_resources_twice(tmp_path: Path) -> None:
    """Vigilancia: si ezdxf corrige la doble traducción, el arreglo de la extracción sobra."""
    source = ezdxf.readfile(_multileader_source(tmp_path / "origen.dxf"))
    target = ezdxf.new("R2018")
    loader = xref.Loader(source, target)
    loader.load_block_layout(source.blocks.get("Detalle"))
    loader.execute()

    style = _text_style_of_multileader(target)
    if style is not None and style.dxftype() == "STYLE" and style.dxf.name == "MiEstilo":
        pytest.fail(
            "ezdxf ya traduce una sola vez los handles del MULTILEADER: "
            "valore retirar _multileader_mapping_once de block_extractor."
        )
