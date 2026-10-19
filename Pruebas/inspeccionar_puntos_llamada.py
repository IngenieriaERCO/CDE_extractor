
from pathlib import Path
import ezdxf

RUTA = Path("Pruebas/Prueba_Andes_.dxf")

HANDLES = [
    "159E5", "15A91", "15AB7", "169C4", "169D7",
    "169EA", "169FD", "16A10", "16A23", "16A36", "16A49"
]


def imprimir_vertices(linea):
    """Intenta leer los vértices de una línea de llamada."""
    encontrados = False

    # Inspeccionar la estructura interna sin modificarla.
    try:
        datos = vars(linea)
    except TypeError:
        datos = {}

    for nombre, valor in datos.items():
        if any(p in nombre.lower() for p in ("vertex", "point", "vert", "line")):
            print(f"      {nombre}: {valor}")
            encontrados = True

    # Algunas versiones de ezdxf guardan los datos en DXFTagStorage.
    try:
        for nombre in dir(linea):
            if nombre.startswith("_"):
                continue

            try:
                valor = getattr(linea, nombre)
            except Exception:
                continue

            if callable(valor):
                continue

            if any(p in nombre.lower() for p in ("vertex", "point", "vert")):
                print(f"      {nombre}: {valor}")
                encontrados = True
    except Exception as error:
        print("      Inspección adicional:", error)

    if not encontrados:
        print("      No se encontraron vértices mediante los atributos públicos.")
        print("      Tipo interno:", type(linea))
        print("      Representación:", repr(linea))


def main():
    if not RUTA.exists():
        print("No se encontró:", RUTA.resolve())
        return

    print("Archivo:", RUTA.resolve())
    print("Modo de solo lectura: no se guardarán cambios.\n")

    try:
        doc = ezdxf.readfile(RUTA)
    except Exception as error:
        print("Error leyendo el DXF:", error)
        return

    for handle in HANDLES:
        entidad = doc.entitydb.get(handle)

        print("\n" + "=" * 60)
        print("HANDLE:", handle)

        if entidad is None:
            print("Entidad no encontrada.")
            continue

        print("Tipo:", entidad.dxftype())
        print("Capa:", entidad.dxf.get("layer", "Sin capa"))

        try:
            print("Texto:", entidad.get_mtext_content())
        except Exception:
            print("Texto: no disponible mediante este método.")

        try:
            contexto = entidad.context
            print("Origen del texto:", contexto.base_point)
            print("Origen del plano:", contexto.plane_origin)

            for i, leader in enumerate(contexto.leaders, start=1):
                print(f"\n  Llamada {i}")
                print("  Último punto:", leader.last_leader_point)

                for j, linea in enumerate(leader.lines, start=1):
                    print(f"    Segmento {j}")
                    imprimir_vertices(linea)

        except Exception as error:
            print("No se pudo inspeccionar la llamada:", error)

    print("\nInspección terminada. No se guardó ningún cambio.")


if __name__ == "__main__":
    main()


