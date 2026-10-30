"""Lectura del ACIS de un 3DSOLID que admite el transform binario de AutoCAD 2018+.

ezdxf 1.4.4 lee el registro ``transform`` del SAB como texto, como lo escribe él mismo;
AutoCAD (``ASM BinaryFile4``) lo guarda como 4 vectores (las 3 filas del giro y la
traslación), la escala y tres indicadores (giro, reflejo, deformación). SAT y SAB guardan
los mismos campos en el mismo orden y ezdxf ya lee así el SAT: primero las filas y luego
la traslación, el convenio de vector fila de ``Matrix44``. Aquí solo se añade esa forma
binaria; lo demás lo lee ezdxf sin cambios.

Usa clases internas de ezdxf (``SabLoader`` y ``SabDataLoader``), solo en esta lectura;
tests/test_acis_transform.py avisa si cambian o si ezdxf empieza a leerlo por sí mismo.
"""

from __future__ import annotations

from ezdxf.acis import api as acis
from ezdxf.acis.const import Tags
from ezdxf.acis.entities import SabLoader
from ezdxf.acis.sab import SabDataLoader

_UNIT_SCALE_TOLERANCE = 1e-9


class UnsupportedTransform(ValueError):
    """Transform binario que se lee pero no se interpreta (escala o deformación)."""


class _TransformTolerantDataLoader(SabDataLoader):
    def __init__(self, data, version: int, loader: _TransformTolerantSabLoader) -> None:
        super().__init__(data, version)
        self._loader = loader

    def read_transform(self) -> list[float]:
        if self.data[self.index].tag in (Tags.STR, Tags.LITERAL_STR):
            return super().read_transform()
        values = [value for _ in range(4) for value in self.read_vec3()]
        scale = self.read_double()
        self.read_bool("rotate", "no_rotate")
        self.read_bool("reflect", "no_reflect")
        shear = self.read_bool("shear", "no_shear")
        # Como en el SAT, ezdxf no aplica la escala ni la deformación: se rechazan.
        if abs(scale - 1.0) > _UNIT_SCALE_TOLERANCE:
            raise UnsupportedTransform("transform ACIS con escala distinta de 1")
        if shear:
            raise UnsupportedTransform("transform ACIS con deformación")
        self._loader.own_transform = True
        return values


class _TransformTolerantSabLoader(SabLoader):
    def __init__(self, data: bytes | bytearray) -> None:
        super().__init__(data)
        self.own_transform = False

    def make_data_loader(self, data) -> SabDataLoader:
        return _TransformTolerantDataLoader(data, self.version, self)


def load_bodies(solid) -> tuple[list[acis.Body], bool]:
    """Cuerpos ACIS de ``solid`` y si hizo falta el lector propio del transform binario.

    El SAT (texto) lo lee ezdxf tal cual, con ``acis.load_dxf``.
    """
    if not solid.has_binary_data:
        return acis.load_dxf(solid), False
    data = solid.sab
    if not data:
        return [], False
    loader = _TransformTolerantSabLoader(data)
    loader.load_entities()
    return loader.bodies(), loader.own_transform
