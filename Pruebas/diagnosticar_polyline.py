
from pathlib import Path
import ezdxf

RUTA = Path("Pruebas/Prueba_Andes_.dxf")

doc = ezdxf.readfile(RUTA)
msp = doc.modelspace()

print("\nPOLYLINE encontradas en el dibujo\n")

for entidad in msp.query("POLYLINE"):
    print("=" * 60)
    print("Handle:", entidad.dxf.get("handle"))
    print("Capa:", entidad.dxf.get("layer"))
    print("Tipo DXF:", entidad.dxftype())

    try:
        vertices = list(entidad.vertices)
        print("Cantidad de vértices:", len(vertices))

        for i, vertice in enumerate(vertices[:10], start=1):
            print(
                f"  Vértice {i}:",
                vertice.dxf.location
            )

        if len(vertices) > 10:
            print("  (Se muestran solo los primeros 10 vértices)")

    except Exception as error:
        print("No se pudieron leer los vértices:", error)

    try:
        from ezdxf import bbox

        caja = bbox.extents([entidad], fast=False)
        print("Caja válida:", caja.has_data)

        if caja.has_data:
            print("Mínimo:", caja.extmin)
            print("Máximo:", caja.extmax)

    except Exception as error:
        print("Error al calcular la caja:", error)

print("\nDiagnóstico terminado. No se guardaron cambios.")
