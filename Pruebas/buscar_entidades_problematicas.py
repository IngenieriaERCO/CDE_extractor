
from pathlib import Path
from collections import Counter

import ezdxf
from ezdxf import bbox


# Archivo de prueba. Solo se lee; no se modifica.
RUTA = Path("Pruebas/Prueba_Andes_.dxf")

# Ventana que queremos revisar para extraer el detalle.
XMIN = 4768731.50
YMIN = 1960572.50
XMAX = 4768740.20
YMAX = 1960575.30


def obtener_atributo(entidad, atributo, predeterminado="No disponible"):
    """Obtiene un atributo DXF sin detener el diagnóstico si falla."""
    try:
        return entidad.dxf.get(atributo, predeterminado)
    except Exception:
        return predeterminado


def main():
    if not RUTA.exists():
        print("ERROR: No se encontró el archivo:")
        print(RUTA.resolve())
        return

    print("Archivo:", RUTA.resolve())
    print("Modo de diagnóstico: solo lectura.")
    print("No se guardarán cambios en el DXF.\n")

    # Leer el DXF.
    try:
        doc = ezdxf.readfile(RUTA)
    except Exception as error:
        print("ERROR al leer el DXF:")
        print(repr(error))
        return

    msp = doc.modelspace()

    conteo_tipos = Counter()
    fallos = []
    cercanas = []
    entidades_sin_caja = 0

    # Revisar las entidades del espacio modelo.
    for entidad in msp:
        try:
            tipo = entidad.dxftype()
        except Exception:
            tipo = "TIPO DESCONOCIDO"

        conteo_tipos[tipo] += 1

        handle = obtener_atributo(
            entidad, "handle", "Sin handle"
        )
        capa = obtener_atributo(
            entidad, "layer", "Sin capa o atributo no disponible"
        )

        # Calcular la caja delimitadora de cada entidad.
        try:
            caja = bbox.extents([entidad], fast=False)

            if not caja.has_data:
                entidades_sin_caja += 1
                fallos.append({
                    "handle": handle,
                    "tipo": tipo,
                    "capa": capa,
                    "error": "La entidad no produjo una caja delimitadora."
                })
                continue

            xmin, ymin, _ = caja.extmin
            xmax, ymax, _ = caja.extmax

            # Comprobar si la caja de la entidad cruza la ventana.
            cruza_ventana = (
                xmax >= XMIN
                and xmin <= XMAX
                and ymax >= YMIN
                and ymin <= YMAX
            )

            if cruza_ventana:
                cercanas.append({
                    "handle": handle,
                    "tipo": tipo,
                    "capa": capa,
                    "xmin": xmin,
                    "ymin": ymin,
                    "xmax": xmax,
                    "ymax": ymax,
                })

        except Exception as error:
            fallos.append({
                "handle": handle,
                "tipo": tipo,
                "capa": capa,
                "error": repr(error),
            })

    # Mostrar resumen.
    print("=" * 65)
    print("RESUMEN DE TIPOS DE ENTIDADES")
    print("=" * 65)

    for tipo, cantidad in sorted(conteo_tipos.items()):
        print(f"{tipo}: {cantidad}")

    # Mostrar entidades problemáticas.
    print("\n" + "=" * 65)
    print("ENTIDADES SIN CAJA DELIMITADORA VÁLIDA")
    print("=" * 65)
    print("Cantidad:", len(fallos))
    print("Entidades sin caja de datos:", entidades_sin_caja)

    for item in fallos:
        print("-" * 55)
        print("Handle:", item["handle"])
        print("Tipo:", item["tipo"])
        print("Capa:", item["capa"])
        print("Error:", item["error"])

    # Mostrar entidades dentro o cruzando la ventana.
    print("\n" + "=" * 65)
    print("ENTIDADES CUYAS CAJAS CRUZAN LA VENTANA")
    print("=" * 65)
    print("Cantidad:", len(cercanas))

    for item in cercanas:
        print("-" * 55)
        print(
            f"Handle: {item['handle']} | "
            f"Tipo: {item['tipo']} | "
            f"Capa: {item['capa']}"
        )
        print(
            f"X: {item['xmin']:.6f} a "
            f"{item['xmax']:.6f}"
        )
        print(
            f"Y: {item['ymin']:.6f} a "
            f"{item['ymax']:.6f}"
        )

    print("\n" + "=" * 65)
    print("DIAGNÓSTICO TERMINADO")
    print("No se guardaron cambios en el archivo.")
    print("=" * 65)


if __name__ == "__main__":
    main()

