"""Transform ACIS binario de AutoCAD 2018+ (acis_reader): orientación, rechazo y vigilancia.

AutoCAD guarda el registro ``transform`` del SAB como 4 vectores, la escala y tres
indicadores; ezdxf lo escribe como texto. Los sólidos de prueba se exportan con ezdxf y
su transform se reescribe en la forma binaria, en el mismo orden (filas y traslación).
"""

import json
import math
import struct
from pathlib import Path

import ezdxf
import pytest
from ezdxf import disassemble
from ezdxf.acis import api as acis
from ezdxf.acis import sab
from ezdxf.acis.const import ParsingError, Tags
from ezdxf.acis.entities import SabLoader
from ezdxf.math import Matrix44
from ezdxf.render import forms

from cde_exporter.cad.acis_reader import load_bodies
from cde_exporter.cad.solids import solid_footprint
from cde_exporter.cli import main

# Caja de 10 × 2 × 1; ezdxf la centra en su origen y guarda la posición en el transform.
_BOX = forms.cube().scale(10, 2, 1).translate(1, 1, 0)
_CENTER = (100.0, 50.0)


@pytest.fixture(autouse=True)
def _no_intermediates(monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)


def _binary_transform(
    data: bytes, matrix: Matrix44, *, scale: float = 1.0, shear: bool = False
) -> bytes:
    """Sustituye el transform en texto que escribe ezdxf por la forma binaria de AutoCAD.

    Los vectores salen de ``matrix`` y no del texto, que ezdxf redondea a 6 cifras.
    """
    start = data.index(bytes([Tags.LITERAL_STR]), data.index(b"\x0d\x09transform"))
    length = struct.unpack_from("<i", data, start + 1)[0]
    end = start + 5 + length
    values = [value for row in matrix.rows() for value in row[:3]]
    tokens = b"".join(
        bytes([Tags.DIRECTION_VEC]) + struct.pack("<3d", *values[index : index + 3])
        for index in range(0, 12, 3)
    )
    tokens += bytes([Tags.DOUBLE]) + struct.pack("<d", scale)
    tokens += bytes([Tags.BOOL_TRUE, Tags.BOOL_FALSE])  # rotate, no_reflect
    tokens += bytes([Tags.BOOL_TRUE if shear else Tags.BOOL_FALSE])
    return data[:start] + tokens + data[end:]


def _solid(layout, degrees: float, *, binary: bool = True, **options):
    body = acis.body_from_mesh(_BOX)
    matrix = Matrix44.z_rotate(math.radians(degrees)) @ Matrix44.translate(*_CENTER, 0)
    body.transform.matrix = matrix
    solid = layout.add_3dsolid(dxfattribs={"layer": "SOLIDOS"})
    acis.export_dxf(solid, [body])
    if binary:
        solid.sab = _binary_transform(solid.sab, matrix, **options)
    return solid


def _modelspace():
    return ezdxf.new("R2018").modelspace()


def _expected_corners(degrees: float) -> list[tuple[float, float]]:
    """Esquinas de la caja girada con la fórmula del giro, sin pasar por Matrix44."""
    cos, sin = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return sorted(
        (round(x * cos - y * sin + _CENTER[0], 6), round(x * sin + y * cos + _CENTER[1], 6))
        for x in (-5, 5)
        for y in (-1, 1)
    )


def _same_matrix(a, b) -> bool:
    return all(math.isclose(x, y, abs_tol=1e-12) for x, y in zip(a, b, strict=True))


def _long_side_degrees(hull) -> float:
    edges = [(hull[i], hull[i - 1]) for i in range(len(hull))]
    (x1, y1), (x2, y2) = max(edges, key=lambda e: math.dist(*e))
    return math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180


def test_rotation_of_30_degrees_is_not_transposed() -> None:
    # Una caja centrada girada +90° y −90° ocupa lo mismo: con 30° se distingue la traspuesta.
    hull, _z, _half, own = solid_footprint(_solid(_modelspace(), 30))

    assert own
    assert sorted((round(x, 6), round(y, 6)) for x, y in hull) == _expected_corners(30)
    assert sum(x for x, _y in hull) / 4 == pytest.approx(_CENTER[0], abs=1e-9)
    assert sum(y for _x, y in hull) / 4 == pytest.approx(_CENTER[1], abs=1e-9)
    assert _long_side_degrees(hull) == pytest.approx(30, abs=1e-6)  # traspuesta: 150


def test_rotation_of_90_degrees_puts_the_long_side_on_y() -> None:
    hull, _z, _half, _own = solid_footprint(_solid(_modelspace(), 90))

    xs, ys = [x for x, _y in hull], [y for _x, y in hull]
    assert max(xs) - min(xs) == pytest.approx(2)
    assert max(ys) - min(ys) == pytest.approx(10)


def test_text_transform_is_read_by_ezdxf_without_own_reader() -> None:
    solid = _solid(_modelspace(), 30, binary=False)

    bodies, own = load_bodies(solid)

    assert not own
    assert _same_matrix(bodies[0].transform.matrix, acis.load_dxf(solid)[0].transform.matrix)
    hull, _z, _half, own = solid_footprint(solid)
    assert not own
    # ezdxf escribe el transform en texto con 6 cifras significativas.
    for point, expected in zip(sorted(hull), _expected_corners(30), strict=True):
        assert point == pytest.approx(expected, abs=1e-4)


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"scale": 2.0}, "transform ACIS con escala distinta de 1"),
        ({"shear": True}, "transform ACIS con deformación"),
    ],
)
def test_scale_or_shear_is_rejected_with_its_reason(options, reason) -> None:
    with pytest.raises(ValueError, match=reason):
        solid_footprint(_solid(_modelspace(), 30, **options))


def test_ezdxf_still_cannot_read_the_binary_transform() -> None:
    """Vigilancia: si ezdxf empieza a leerlo, hay que comparar y valorar retirar el lector."""
    solid = _solid(_modelspace(), 30)
    try:
        native = acis.load_dxf(solid)
    except ParsingError:
        return
    own = load_bodies(solid)[0]
    same = _same_matrix(native[0].transform.matrix, own[0].transform.matrix)
    pytest.fail(
        "ezdxf ya lee el transform ACIS binario (misma matriz que acis_reader: "
        f"{same}); compare con el lector propio y valore retirarlo."
    )


def test_ezdxf_internals_used_by_acis_reader_still_exist() -> None:
    assert callable(getattr(SabLoader, "make_data_loader", None))
    assert callable(getattr(SabLoader, "load_entities", None))
    assert callable(getattr(SabLoader, "bodies", None))
    for name in ("read_transform", "read_vec3", "read_double", "read_bool"):
        assert callable(getattr(sab.SabDataLoader, name, None)), name
    assert sab.SabDataLoader.__init__.__code__.co_varnames[1:3] == ("data", "version")


def _source(path: Path) -> Path:
    document = ezdxf.new("R2018")
    document.units = ezdxf.units.InsertUnits.Millimeters
    module = document.blocks.new("Modulo")
    _solid(module, 30)
    detail = document.blocks.new("Detalle")
    detail.add_line((0, 0), (1, 1))
    detail.add_blockref("Modulo", (10, 0))
    document.modelspace().add_blockref("Detalle", (0, 0))
    document.saveas(path)
    return path


def test_binary_transform_solid_is_drawn_and_reported(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CDE_KEEP_INTERMEDIATES", "1")
    out = tmp_path / "salida"
    arguments = [
        "cad",
        "extract-block",
        str(_source(tmp_path / "origen.dxf")),
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

    report = (out / "detalle.txt").read_text(encoding="utf-8")
    notes = " ".join(report.split("A tener en cuenta", 1)[1].split())
    assert "3DSOLID | SOLIDOS | transformadas | 1" in report
    assert "SOLIDS_ACIS_OWN_READER (info)" in report
    assert "SOLIDS_NOT_CONVERTED" not in report
    assert "UNSUPPORTED_OMITTED" not in report
    assert (
        "1 sólido distinto se leyó con el lector propio del transform ACIS binario "
        "(ezdxf no lo admite). Compare una muestra de huellas con el CAD."
    ) in notes
    manifest = json.loads((out / "detalle.manifest.json").read_text(encoding="utf-8"))
    entities = manifest["notices"]["by_category"]["entities"]
    assert {"code": "SOLIDS_ACIS_OWN_READER", "severity": "info"}.items() <= next(
        notice for notice in entities if notice["code"] == "SOLIDS_ACIS_OWN_READER"
    ).items()
    # La copia de render tiene la huella en su sitio: caja girada 30° más el INSERT (10, 0).
    render = ezdxf.readfile(out / "detalle.dxf")
    drawn = [
        sorted((round(v.x - 10, 6), round(v.y, 6)) for v in entity.vertices_in_wcs())
        for entity in disassemble.recursive_decompose(render.modelspace())
        if entity.dxftype() == "LWPOLYLINE"
    ]
    assert drawn == [_expected_corners(30)]
    assert "1 con el lector propio del transform ACIS" in capsys.readouterr().out
