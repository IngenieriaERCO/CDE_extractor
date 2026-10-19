
from pathlib import Path
import ezdxf

ruta = Path("Pruebas/Prueba_Andes_.dxf")
doc = ezdxf.readfile(ruta)
msp = doc.modelspace()

print("\nBLOQUES INSERTADOS RELACIONADOS CON CERRAMIENTOS Y ANCLAJES\n")

encontrados = 0

for insert in msp.query("INSERT"):
    nombre = insert.dxf.name
    texto = nombre.casefold()

    if any(p in texto for p in (
        "anclaje",
        "pilastra",
        "malla",
        "cerramiento",
        "tensor",
        "4388942",
        "4389985",
    )):
        print(f"Nombre: {nombre}")
        print(f"Handle: {insert.dxf.handle}")
        print(
            f"Coordenadas: "
            f"X={insert.dxf.insert.x:.3f}, "
            f"Y={insert.dxf.insert.y:.3f}"
        )
        print("-" * 70)
        encontrados += 1

print(f"\nTotal de bloques encontrados: {encontrados}")
print("El archivo original no se ha modificado.")
