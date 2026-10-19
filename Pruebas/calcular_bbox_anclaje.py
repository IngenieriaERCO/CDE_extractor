
from pathlib import Path
from collections import Counter

import ezdxf
from ezdxf import bbox


# --------------------------------------------------
# CONFIGURACIÓN
# --------------------------------------------------

DXF_PATH = Path("Pruebas/Prueba_Andes_.dxf")

TARGET_TEXT = (
    "DETALLE ANCLAJE DE TUBO A PILASTRA "
    "Y MALLA EN CERRAMIENTO"
)

# El DXF está expresado en metros.
RADIOS = [5.0, 10.0, 20.0]

# Los elementos más grandes se reportan para revisión,
# pero no se incluyen automáticamente en el bbox combinado.
MAX_ENTITY_SIZE = 30.0


def get_bounds(entity):
    """Devuelve (xmin, ymin, xmax, ymax) o None si falla."""
    try:
        box = bbox.extents([entity], fast=False)

        if not box.has_data:
            return None

        return (
            float(box.extmin.x),
            float(box.extmin.y),
            float(box.extmax.x),
            float(box.extmax.y),
        )
    except Exception as exc:
        return None


def combine_bounds(bounds_list):
    """Combina varias cajas XY."""
    valid = [b for b in bounds_list if b is not None]

    if not valid:
        return None

    return (
        min(b[0] for b in valid),
        min(b[1] for b in valid),
        max(b[2] for b in valid),
        max(b[3] for b in valid),
    )


def print_bounds(label, bounds):
    print(f"\n{label}")

    if bounds is None:
        print("  No fue posible calcular el bounding box.")
        return

    xmin, ymin, xmax, ymax = bounds

    print(f"  X mínimo: {xmin:.3f}")
    print(f"  Y mínimo: {ymin:.3f}")
    print(f"  X máximo: {xmax:.3f}")
    print(f"  Y máximo: {ymax:.3f}")
    print(f"  Ancho X:  {xmax - xmin:.3f}")
    print(f"  Alto Y:   {ymax - ymin:.3f}")


def intersects_square(bounds, x, y, radius):
    """Comprueba si una caja toca la ventana XY de búsqueda."""
    xmin, ymin, xmax, ymax = bounds

    return not (
        xmax < x - radius
        or xmin > x + radius
        or ymax < y - radius
        or ymin > y + radius
    )


def main():
    if not DXF_PATH.exists():
        raise FileNotFoundError(
            f"No se encontró el DXF: {DXF_PATH.resolve()}"
        )

    print("Leyendo DXF en modo de solo lectura...")
    print(f"Archivo: {DXF_PATH.resolve()}")

    # readfile carga el dibujo en memoria; este script no lo guarda.
    doc = ezdxf.readfile(DXF_PATH)
    msp = doc.modelspace()

    # Buscar los INSERT cuyo nombre de bloque contiene el detalle.
    targets = []

    for entity in msp.query("INSERT"):
        block_name = entity.dxf.name

        if TARGET_TEXT.casefold() in block_name.casefold():
            targets.append(entity)

    print(f"\nBloques coincidentes encontrados: {len(targets)}")

    if not targets:
        print(
            "No se encontró el nombre esperado. "
            "Revisa los nombres de bloque del DXF."
        )
        return

    # Calcular una vez las cajas de las entidades del modelspace.
    records = []
    failed = 0

    for entity in msp:
        bounds = get_bounds(entity)

        if bounds is None:
            failed += 1
            continue

        records.append((entity, bounds))

    print(f"Entidades con bbox calculable: {len(records)}")
    print(f"Entidades cuyo bbox no se pudo calcular: {failed}")

    # Analizar cada inserción coincidente.
    for index, target in enumerate(targets, start=1):
        name = target.dxf.name
        x = float(target.dxf.insert.x)
        y = float(target.dxf.insert.y)

        print("\n" + "=" * 65)
        print(f"DETALLE COINCIDENTE #{index}")
        print(f"Nombre del bloque: {name}")
        print(f"Handle del INSERT: {target.dxf.handle}")
        print(f"Punto de inserción X,Y: {x:.3f}, {y:.3f}")

        target_bounds = get_bounds(target)
        print_bounds("A. Bounding box del bloque insertado", target_bounds)

        for radius in RADIOS:
            selected = []

            for entity, bounds in records:
                if entity.dxf.handle == target.dxf.handle:
                    continue

                if intersects_square(bounds, x, y, radius):
                    selected.append((entity, bounds))

            # Detectar elementos muy grandes que podrían extender
            # el bbox hacia geometría ajena al detalle.
            normal = []
            oversized = []

            for entity, bounds in selected:
                width = bounds[2] - bounds[0]
                height = bounds[3] - bounds[1]

                if width > MAX_ENTITY_SIZE or height > MAX_ENTITY_SIZE:
                    oversized.append((entity, bounds))
                else:
                    normal.append((entity, bounds))

            combined = combine_bounds(
                [target_bounds] + [b for _, b in normal]
            )

            print_bounds(
                f"B. Caja combinada: bloque + entidades próximas "
                f"(radio {radius:.0f} m)",
                combined,
            )

            print(f"  Entidades próximas seleccionadas: {len(selected)}")
            print(f"  Incluidas en la caja combinada: {len(normal)}")
            print(f"  Elementos grandes excluidos: {len(oversized)}")

            types = Counter(
                entity.dxftype() for entity, _ in normal
            )
            layers = Counter(
                entity.dxf.get("layer", "<sin capa>")
                for entity, _ in normal
            )

            print(f"  Tipos de entidad: {dict(types)}")
            print(f"  Capas principales: {layers.most_common(12)}")

            if radius == 10.0:
                print("\n  Candidatos dentro del radio de 10 m:")

                for entity, bounds in normal:
                    print(
                        f"    Handle={entity.dxf.handle} "
                        f"Tipo={entity.dxftype()} "
                        f"Capa={entity.dxf.get('layer', '<sin capa>')} "
                        f"XY=({bounds[0]:.3f}, {bounds[1]:.3f}, "
                        f"{bounds[2]:.3f}, {bounds[3]:.3f})"
                    )

                if oversized:
                    print("\n  Elementos grandes excluidos; revisar manualmente:")

                    for entity, bounds in oversized:
                        print(
                            f"    Handle={entity.dxf.handle} "
                            f"Tipo={entity.dxftype()} "
                            f"Capa={entity.dxf.get('layer', '<sin capa>')} "
                            f"XY=({bounds[0]:.3f}, {bounds[1]:.3f}, "
                            f"{bounds[2]:.3f}, {bounds[3]:.3f})"
                        )

    print("\nDiagnóstico terminado. No se guardó ni modificó el DXF.")


if __name__ == "__main__":
    main()
