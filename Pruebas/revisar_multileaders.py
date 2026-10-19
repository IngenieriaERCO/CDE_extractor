

from pathlib import Path
import ezdxf

RUTA = Path("Pruebas/Prueba_Andes_.dxf")

HANDLES = [
    "159E5", "15A91", "15AB7", "169C4", "169D7",
    "169EA", "169FD", "16A10", "16A23", "16A36", "16A49"
]

doc = ezdxf.readfile(RUTA)

def mostrar(nombre, valor):
    try:
        print(f"{nombre}: {valor}")
    except Exception:
        print(f"{nombre}: <no se pudo mostrar>")

for handle in HANDLES:
    ent = doc.entitydb.get(handle)

    print("\n" + "=" * 65)
    print("HANDLE:", handle)

    if ent is None:
        print("Entidad no encontrada.")
        continue

    mostrar("Tipo", ent.dxftype())
    mostrar("Capa", ent.dxf.get("layer", "Sin capa"))

    # Intentar recuperar el contenido de texto.
    for metodo in ("get_mtext_content", "get_block_content"):
        try:
            funcion = getattr(ent, metodo, None)
            if callable(funcion):
                contenido = funcion()
                print(f"\nResultado de {metodo}():")
                if contenido is None:
                    print("Sin contenido recuperado.")
                elif hasattr(contenido, "text"):
                    mostrar("Texto", contenido.text)
                else:
                    mostrar("Contenido", contenido)
        except Exception as e:
            print(f"{metodo}() no disponible: {e}")

    # Inspeccionar los datos internos del contexto.
    try:
        contexto = ent.context
        print("\nAtributos del contexto:")
        nombres = [
            n for n in dir(contexto)
            if not n.startswith("_") and not callable(
                getattr(contexto, n, None)
            )
        ]

        for nombre in nombres:
            try:
                valor = getattr(contexto, nombre)
                mostrar(nombre, valor)
            except Exception:
                pass
    except Exception as e:
        print("No se pudo inspeccionar el contexto:", e)

print("\nFin de inspección. No se guardaron cambios.")
