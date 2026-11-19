"""``dxf.crs`` de ``load`` y ``--list --json``: el sistema de coordenadas del GEODATA tal como
viene, sin interpretarlo, o ``null`` si el DXF no tiene GEODATA."""

import json
from pathlib import Path

import ezdxf
import pytest
from test_worker import TIMEOUT, running  # noqa: F401 - fixture del trabajador en el proceso

from cde_exporter.cad.crs import crs_from_definition
from cde_exporter.cad.dxf_session import load_dxf
from cde_exporter.cli import main

# Como el de PRGD: la descripción dice CTM12 y la autoridad trae 3115, sin alias del sistema.
PRGD_XML = """<?xml version="1.0" encoding="UTF-16" standalone="no" ?>
<Dictionary version="1.0" xmlns="http://www.osgeo.org/mapguide/coordinatesystem">
  <ProjectedCoordinateSystem id="Colombia CTM12">
    <Name>Colombia CTM12</Name>
    <Description>MAGNA_Colombia CTM12</Description>
    <Authority>EPSG, V6.11.2, 3115 [Instituto Geografico Agustin Caduzzi (I]</Authority>
    <DatumId>MAGNA</DatumId>
  </ProjectedCoordinateSystem>
  <GeodeticDatum id="MAGNA">
    <Name>MAGNA</Name>
    <Authority>EPSG, V6.11.2, 6686 [EPSG]</Authority>
  </GeodeticDatum>
  <Alias id="6686" type="Datum">
    <ObjectId>MAGNA</ObjectId>
    <Namespace>EPSG Code</Namespace>
  </Alias>
</Dictionary>"""

PRGD_CRS = {
    "source": "GEODATA",
    "format": "xml",
    "id": "Colombia CTM12",
    "name": "Colombia CTM12",
    "description": "MAGNA_Colombia CTM12",
    "authority": "EPSG, V6.11.2, 3115 [Instituto Geografico Agustin Caduzzi (I]",
    "epsg": 3115,
    "epsg_from": "authority",
}


def _saved(path: Path, definition: str | None) -> Path:
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Meters
    block = document.blocks.new("DETALLE")
    block.add_line((0, 0), (10, 4))
    modelspace = document.modelspace()
    modelspace.add_blockref("DETALLE", (0, 0))
    if definition is not None:
        geodata = modelspace.new_geodata()
        geodata.coordinate_system_definition = definition
    document.saveas(path)
    return path


def test_load_dxf_copies_the_geodata_system_as_it_comes(tmp_path: Path) -> None:
    source = _saved(tmp_path / "con_geodata.dxf", PRGD_XML)

    data = load_dxf(source, tmp_path / "trabajo").dxf_json()

    assert data["crs"] == PRGD_CRS  # nombre y código no coinciden: se copian igual


def test_without_geodata_crs_is_null(tmp_path: Path) -> None:
    source = _saved(tmp_path / "sin_geodata.dxf", None)

    assert load_dxf(source, tmp_path / "trabajo").dxf_json()["crs"] is None


def test_list_json_carries_the_crs(tmp_path: Path, capsys) -> None:
    source = _saved(tmp_path / "con_geodata.dxf", PRGD_XML)
    plain = _saved(tmp_path / "sin_geodata.dxf", None)
    base = ["cad", "extract-all-blocks", "--output-dir", str(tmp_path / "x"), "--list", "--json"]

    assert main([*base[:2], str(source), *base[2:]]) == 0
    with_crs = json.loads(capsys.readouterr().out)
    assert main([*base[:2], str(plain), *base[2:]]) == 0
    without = json.loads(capsys.readouterr().out)

    assert with_crs["dxf"]["crs"] == PRGD_CRS
    assert without["dxf"]["crs"] is None


def test_worker_load_carries_the_crs(running, tmp_path: Path) -> None:  # noqa: F811
    sink, stdin = running
    source = _saved(tmp_path / "con_geodata.dxf", PRGD_XML)

    stdin.send(id="c-1", command="load", data={"dxf": str(source)})
    with sink.condition:
        assert sink.condition.wait_for(
            lambda: any(m["id"] == "c-1" and m["type"] == "result" for m in sink.messages),
            TIMEOUT,
        )
    result = next(m for m in sink.messages if m["id"] == "c-1" and m["type"] == "result")

    assert result["data"]["dxf"]["crs"] == PRGD_CRS


def test_an_epsg_alias_of_the_system_wins_over_the_authority() -> None:
    xml = PRGD_XML.replace(
        "</Dictionary>",
        '<Alias id="9377" type="CoordinateSystem"><ObjectId>Colombia CTM12</ObjectId>'
        "<Namespace>EPSG Code</Namespace></Alias></Dictionary>",
    )

    crs = crs_from_definition(xml)

    assert (crs["epsg"], crs["epsg_from"]) == (9377, "alias")
    assert crs["authority"] == PRGD_CRS["authority"]  # la autoridad sigue tal como viene


def test_wkt_definitions_are_read_too() -> None:
    wkt = (
        'PROJCS["MAGNA-SIRGAS / Colombia West zone",GEOGCS["MAGNA-SIRGAS",'
        'AUTHORITY["EPSG","4686"]],UNIT["metre",1],AUTHORITY["EPSG","3115"]]'
    )

    crs = crs_from_definition(wkt)

    assert crs["format"] == "wkt"
    assert crs["name"] == "MAGNA-SIRGAS / Colombia West zone"
    assert (crs["authority"], crs["epsg"]) == ("EPSG:3115", 3115)


@pytest.mark.parametrize("definition", ["", "no es un sistema", "<Dictionary><roto", "<a/>"])
def test_unreadable_definitions_never_fail(definition: str) -> None:
    crs = crs_from_definition(definition)

    assert crs["source"] == "GEODATA"
    assert crs["name"] is None and crs["epsg"] is None
