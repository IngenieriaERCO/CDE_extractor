"""Tablas de AutoCAD (ACAD_TABLE) dibujadas desde su bloque interno «*T».

ezdxf 1.4.4 carga las ACAD_TABLE como ``AcadTableBlockContent`` (una ``DXFTagStorage``): sin
capa, color ni grosor leídos, así que el exportador las tomaba por entidades de un complemento y
no salían ni en el PDF ni en las vistas previas (en PRGD-25, la «TABLA DE GANCHO ESTÁNDAR», la
de estribos y una fila del «CUADRO DESPIECE»). AutoCAD guarda con cada tabla un bloque anónimo
«*T» con su dibujo (líneas y MTEXT), que nadie más inserta.

``convert_tables`` sustituye, en el documento ya leído (nunca en el archivo), cada tabla por un
INSERT de su bloque «*T» con el mismo handle, en el mismo puesto del orden de dibujo y con la
capa, el color, el tipo de línea, el grosor y la transparencia de los códigos del DXF. Desde ahí
la selección de zonas, el recorte, la caja, el PDF y las vistas previas la tratan como un bloque.
Una tabla sin su bloque «*T» o girada (ezdxf solo sabe trasladar el bloque) se queda como está y
se omite con el aviso ``ACAD_TABLE_NOT_DRAWN`` (``table_problem`` da el motivo).
"""

from __future__ import annotations

import math
from collections import Counter

from ezdxf.entities import Insert
from ezdxf.entities.acad_table import AcadTableBlockContent
from ezdxf.lldxf import const

TABLE_BLOCK_PREFIX = "*T"
NO_BLOCK = "sin su bloque interno «*T»"
ROTATED = "girada (solo se sabe dibujar sin giro)"
# Propiedades de AcDbEntity que se copian al INSERT (código de grupo → atributo).
_ENTITY_CODES = {
    8: "layer",
    6: "linetype",
    62: "color",
    420: "true_color",
    370: "lineweight",
    48: "ltscale",
    440: "transparency",
    60: "invisible",
}


def is_table(entity) -> bool:
    return isinstance(entity, AcadTableBlockContent)


def is_table_insert(entity) -> bool:
    """Un INSERT de una tabla convertida: AutoCAD nunca inserta los bloques «*T» de otro modo."""
    return entity.dxftype() == "INSERT" and str(entity.dxf.get("name", "")).upper().startswith(
        TABLE_BLOCK_PREFIX
    )


def _subclass(table, name: str):
    for tags in table.xtags.subclasses:
        if tags and tags[0] == (100, name):
            return tags
    return ()


def _entity_properties(table) -> dict:
    found: dict = {}
    for code, value in _subclass(table, "AcDbEntity"):
        attribute = _ENTITY_CODES.get(code)
        if attribute is not None and attribute not in found:
            found[attribute] = value
    return found


def _extrusion(table) -> tuple[float, float, float]:
    for code, value in _subclass(table, "AcDbBlockReference"):
        if code == 210:
            return tuple(float(part) for part in value)  # type: ignore[return-value]
    return (0.0, 0.0, 1.0)


def table_problem(table) -> str | None:
    """Por qué no se puede dibujar esta tabla, o ``None`` si se puede."""
    document = table.doc
    name = table.get_block_name()
    if document is None or not name or name not in document.blocks:
        return NO_BLOCK
    direction = table.dxf.get("horizontal_direction", (1.0, 0.0, 0.0))
    angle = math.atan2(float(direction[1]), float(direction[0]))
    x, y, z = _extrusion(table)
    if abs(angle) > 1e-9 or abs(x) > 1e-12 or abs(y) > 1e-12 or z <= 0:
        return ROTATED
    return None


def _layout_of(document, table):
    """El layout (Model Space, Paper Space o bloque) que tiene la tabla: por su dueño o, si no
    lo trae bien, buscándola."""
    owner = table.dxf.get("owner")
    try:
        layout = document.layouts.get_layout_by_key(owner)
    except (const.DXFKeyError, KeyError):
        record = document.entitydb.get(owner)
        layout = None
        if record is not None and record.dxftype() == "BLOCK_RECORD":
            layout = document.blocks.get(record.dxf.name)
    if layout is not None and table in layout.entity_space.entities:
        return layout
    for block in document.blocks:
        if table in block.entity_space.entities:
            return block
    return None


def convert_tables(document) -> int:
    """Sustituye las tablas que se pueden dibujar por un INSERT de su bloque «*T». Devuelve
    cuántas. Las demás se quedan (se omiten con aviso al exportar)."""
    tables = [entity for entity in document.entitydb.values() if is_table(entity)]
    converted = 0
    for table in tables:
        if not table.is_alive or table_problem(table) is not None:
            continue
        layout = _layout_of(document, table)
        if layout is None:
            continue
        space = layout.entity_space.entities
        try:
            position = space.index(table)
        except ValueError:
            continue
        attributes = {
            key: value
            for key, value in _entity_properties(table).items()
            if key in ("layer", "linetype", "color", "true_color", "lineweight", "ltscale",
                       "transparency", "invisible")
        }
        attributes.update(name=table.get_block_name(), insert=table.get_insert_location())
        handle = table.dxf.handle
        layout.delete_entity(table)
        insert = Insert.new(dxfattribs=attributes, doc=document)
        insert.dxf.handle = handle
        document.entitydb.add(insert)
        layout.add_entity(insert)
        # En el mismo puesto del orden de dibujo (y con el mismo handle para ACAD_SORTENTS).
        space.remove(insert)
        space.insert(position, insert)
        converted += 1
    return converted


def tables_in_block(document, block_name: str) -> tuple[int, Counter]:
    """En el bloque ``block_name`` y sus anidados (cada definición una vez): cuántas tablas se
    dibujan desde su bloque «*T» y, por motivo, cuántas no se pueden dibujar."""
    drawn, problems = 0, Counter()
    seen: set[str] = set()

    def visit(name: str) -> None:
        nonlocal drawn
        if name in seen or name not in document.blocks:
            return
        seen.add(name)
        for entity in document.blocks[name]:
            if is_table(entity):
                problems[table_problem(entity) or NO_BLOCK] += 1
            elif entity.dxftype() == "INSERT":
                if is_table_insert(entity):
                    drawn += 1
                else:
                    visit(entity.dxf.name)

    visit(block_name)
    return drawn, problems


def drawn_tables_message(count: int) -> str:
    """Aviso ``ACAD_TABLE_FROM_BLOCK``."""
    one = count == 1
    return (
        f"{count} {'tabla' if one else 'tablas'} de AutoCAD (ACAD_TABLE) "
        f"{'dibujada' if one else 'dibujadas'} desde el bloque interno «*T» que AutoCAD guarda "
        "con cada tabla, con su capa y su color."
    )


def tables_not_drawn_message(problems: Counter) -> str:
    """Aviso ``ACAD_TABLE_NOT_DRAWN``: cuántas tablas no salen y por qué."""
    total = sum(problems.values())
    reasons = ", ".join(f"{count} {reason}" for reason, count in sorted(problems.items()))
    one = total == 1
    return (
        f"{total} {'tabla' if one else 'tablas'} de AutoCAD (ACAD_TABLE) no "
        f"{'sale' if one else 'salen'} en el PDF: {reasons}."
    )
