"""Sistema de coordenadas que declara el GEODATA del DXF (``dxf.crs`` de ``load``).

Se copia lo que trae el GEODATA del Model Space **sin interpretarlo**: el nombre, la
descripción y la autoridad tal como vienen, y el código EPSG que traiga, aunque no coincidan
entre sí (PRGD dice «MAGNA_Colombia CTM12» y su autoridad es «EPSG, V6.11.2, 3115 …»). No
se comprueba contra ninguna base de datos de sistemas.

- XML de Autodesk (MapGuide, ``<Dictionary>``): el primer elemento ``…CoordinateSystem``;
  ``epsg`` sale de un ``<Alias>`` con ``Namespace`` «EPSG Code» de ese sistema o, si no hay,
  del primer número de su ``<Authority>`` cuando empieza por «EPSG».
- WKT (``PROJCS[…]``, ``PROJCRS[…]``…): el primer nombre entre comillas y la última
  ``AUTHORITY``/``ID`` (la del sistema completo).

Solo usa la biblioteca estándar. Un GEODATA raro nunca hace fallar ``load``: lo que no se
pueda leer queda en ``null``.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree

SOURCE = "GEODATA"
_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)
_WKT_START = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*\[", re.IGNORECASE)
_WKT_NAME = re.compile(r'^\s*[A-Z][A-Z0-9_]*\s*\[\s*"([^"]*)"', re.IGNORECASE)
_WKT_AUTHORITY = re.compile(
    r'\b(?:AUTHORITY|ID)\s*\[\s*"([^"]*)"\s*,\s*"?([^"\],]*)"?', re.IGNORECASE
)


def document_crs(document) -> dict | None:
    """``dxf.crs`` del documento: ``None`` si el Model Space no tiene GEODATA."""
    try:
        geodata = document.modelspace().get_geodata()
    except Exception:  # noqa: BLE001 - un GEODATA ilegible no debe tumbar «load»
        return None
    if geodata is None:
        return None
    try:
        definition = str(geodata.coordinate_system_definition or "")
    except Exception:  # noqa: BLE001
        definition = ""
    return crs_from_definition(definition)


def crs_from_definition(definition: str) -> dict:
    """Los campos del sistema tal como vienen en ``definition`` (XML o WKT)."""
    crs = {
        "source": SOURCE,
        "format": None,
        "id": None,
        "name": None,
        "description": None,
        "authority": None,
        "epsg": None,
        "epsg_from": None,
    }
    text = definition.strip()
    if not text:
        return crs
    if text.startswith("<"):
        crs["format"] = "xml"
        try:
            _from_xml(text, crs)
        except (ElementTree.ParseError, ValueError):
            pass
    elif _WKT_START.match(text):
        crs["format"] = "wkt"
        _from_wkt(text, crs)
    else:
        crs["format"] = "unknown"
    return crs


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element, name: str) -> str | None:
    for child in element:
        if _local(child.tag) == name:
            value = (child.text or "").strip()
            return value or None
    return None


def _from_xml(text: str, crs: dict) -> None:
    root = ElementTree.fromstring(_XML_DECLARATION.sub("", text, count=1))
    system = next(
        (item for item in root.iter() if _local(item.tag).endswith("CoordinateSystem")), None
    )
    if system is None:
        return
    system_id = system.get("id")
    crs["id"] = system_id
    crs["name"] = _child_text(system, "Name")
    crs["description"] = _child_text(system, "Description")
    crs["authority"] = _child_text(system, "Authority")
    for alias in root.iter():
        if _local(alias.tag) != "Alias":
            continue
        namespace = (_child_text(alias, "Namespace") or "").casefold()
        if "epsg" in namespace and _child_text(alias, "ObjectId") == system_id:
            code = (alias.get("id") or "").strip()
            if code.isdigit():
                crs["epsg"], crs["epsg_from"] = int(code), "alias"
                return
    authority = crs["authority"] or ""
    parts = [part.strip() for part in authority.split(",")]
    if parts and parts[0].upper() == "EPSG":
        for part in parts[1:]:
            number = part.split()[0] if part.split() else ""
            if number.isdigit():
                crs["epsg"], crs["epsg_from"] = int(number), "authority"
                return


def _from_wkt(text: str, crs: dict) -> None:
    name = _WKT_NAME.match(text)
    if name:
        crs["name"] = name.group(1)
    authorities = _WKT_AUTHORITY.findall(text)
    if authorities:
        body, code = authorities[-1]
        code = code.strip()
        crs["authority"] = f"{body}:{code}" if code else body
        if body.upper() == "EPSG" and code.isdigit():
            crs["epsg"], crs["epsg_from"] = int(code), "authority"
