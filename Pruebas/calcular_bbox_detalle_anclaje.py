
from pathlib import Path
import ezdxf
from ezdxf import bbox

RUTA = Path("Pruebas/Prueba_Andes_.dxf")
HANDLE_OBJETIVO = "168DB"
RADIOS = [2.0, 5.0, 10.0]


def caja(entidad):
    try:
        ext = bbox.extents([entidad], fast=False)
        if not ext.has_data:
            return None

        return (
            ext.extmin.x,
            ext.extmin.y,
            ext.extmax.x,
            ext.extmax.y,
        )
    except Exception as error:
        return None


def combinar(cajas):
    cajas = [c for c in cajas if c is not None]
    if not cajas:
        return None

    return (
        min(c[0] for c in cajas),
        min(c[1] for c in cajas),
        max(c[2] for c in cajas),
        max(c[3] for c in cajas),
    )


def mostrar(titulo, c):
    print(f"\n{titulo}")
    if c is None:
        print("No se pudo calcular.")
        return

    xmin, ymin, xmax, ymax = c
    print(f"XMIN = {xmin:.3f}")
    print(f"YMIN = {ymin:.3f}")
    print(f"XMAX = {xmax:.3f}")
    print(f"YMAX = {ymax:.3f}")
    print(f"Ancho = {xmax - xmin:.3f}")
    print(f"Alto  = {ymax - ymin:.3f}")


def toca_ventana(c, x, y, r):
    return not (
        c[2] < x-r or c[0] > x+r
        or c[3] < y-r or c[1] > y+r
    )


def main():
    if not RUTA.exists():
        raise FileNotFoundError(RUTA.resolve())

    # Leer el original; no se guarda ni modifica.
    doc = ezdxf.readfile(RUTA)
    msp = doc.modelspace()

    objetivo = None
    for ent in msp.query("INSERT"):
        if ent.dxf.handle.upper() == HANDLE_OBJETIVO:
            objetivo = ent
            break

    if objetivo is None:
        print("No se encontró el INSERT con handle", HANDLE_OBJETIVO)
        print("Comprueba que estás usando el mismo DXF.")
        return

    x = objetivo.dxf.insert.x
    y = objetivo.dxf.insert.y

    print("BLOQUE OBJETIVO")
    print("Nombre:", objetivo.dxf.name)
    print("Handle:", objetivo.dxf.handle)
    print(f"Inserción: X={x:.3f}, Y={y:.3f}")

    caja_objetivo = caja(objetivo)
    mostrar("1. Caja geométrica del bloque insertado", caja_objetivo)

    # Calcular cajas de las demás entidades del espacio modelo.
    registros = []
    sin_caja = []

    for ent in msp:
        c = caja(ent)
        if c is None:
            sin_caja.append(ent)
        else:
            registros.append((ent, c))

    print(f"\nEntidades del modelo sin caja calculable: {len(sin_caja)}")

    for radio in RADIOS:
        vecinos = []

        for ent, c in registros:
            if ent.dxf.handle == HANDLE_OBJETIVO:
                continue
            if toca_ventana(c, x, y, radio):
                vecinos.append((ent, c))

        # Caja exploratoria: bloque + todas las entidades cercanas.
        caja_combinada = combinar(
            [caja_objetivo] + [c for _, c in vecinos]
        )

        mostrar(
            f"2. Caja exploratoria con entidades cercanas "
            f"(radio {radio:g} m)",
            caja_combinada,
        )

        print("Entidades cercanas:", len(vecinos))
        print("Tipos:", {
            tipo: sum(1 for ent, _ in vecinos if ent.dxftype() == tipo)
            for tipo in sorted({ent.dxftype() for ent, _ in vecinos})
        })

        # Listado de candidatos para revisar pertenencia al detalle.
        if radio == 5.0:
            print("\nCandidatos a 5 m:")
            for ent, c in vecinos:
                print(
                    f"Handle={ent.dxf.handle} "
                    f"Tipo={ent.dxftype()} "
                    f"Capa={ent.dxf.get('layer', '<sin capa>')} "
                    f"Caja=({c[0]:.3f}, {c[1]:.3f}, "
                    f"{c[2]:.3f}, {c[3]:.3f})"
                )

    print("\nDiagnóstico terminado. No se guardó el DXF.")


if __name__ == "__main__":
    main()
