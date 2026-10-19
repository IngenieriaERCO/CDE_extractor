
from pathlib import Path
import ezdxf
from ezdxf import bbox

RUTA = Path("Pruebas/Prueba_Andes_.dxf")

# Ventana local alrededor de la pilastra y el anclaje.
X_CENTRO = 4768739.5
Y_CENTRO = 1960573.5
RADIO_X = 1.5
RADIO_Y = 1.5

# Evita que círculos u objetos enormes contaminen la caja.
TAMANO_MAXIMO = 5.0


def obtener_caja(ent):
    try:
        ext = bbox.extents([ent], fast=False)
        if not ext.has_data:
            return None

        return (
            ext.extmin.x,
            ext.extmin.y,
            ext.extmax.x,
            ext.extmax.y,
        )
    except Exception:
        return None


def intersecta(c):
    return not (
        c[2] < X_CENTRO - RADIO_X
        or c[0] > X_CENTRO + RADIO_X
        or c[3] < Y_CENTRO - RADIO_Y
        or c[1] > Y_CENTRO + RADIO_Y
    )


def combinar(cajas):
    if not cajas:
        return None

    return (
        min(c[0] for c in cajas),
        min(c[1] for c in cajas),
        max(c[2] for c in cajas),
        max(c[3] for c in cajas),
    )


doc = ezdxf.readfile(RUTA)
msp = doc.modelspace()

geometria = []
anotaciones = []
excluidos = []

for ent in msp:
    c = obtener_caja(ent)

    if c is None or not intersecta(c):
        continue

    ancho = c[2] - c[0]
    alto = c[3] - c[1]

    # Las llamadas se reportan aparte.
    if ent.dxftype() == "MULTILEADER":
        anotaciones.append((ent, c))
        continue

    # No permitir que objetos de gran extensión contaminen el bbox.
    if ancho > TAMANO_MAXIMO or alto > TAMANO_MAXIMO:
        excluidos.append((ent, c))
        continue

    geometria.append((ent, c))

caja_principal = combinar([c for _, c in geometria])

print("\nCAJA PRINCIPAL: GEOMETRÍA LOCAL")
if caja_principal:
    xmin, ymin, xmax, ymax = caja_principal
    print(f"XMIN = {xmin:.3f}")
    print(f"YMIN = {ymin:.3f}")
    print(f"XMAX = {xmax:.3f}")
    print(f"YMAX = {ymax:.3f}")
    print(f"Ancho = {xmax - xmin:.3f} m")
    print(f"Alto  = {ymax - ymin:.3f} m")
else:
    print("No se pudo calcular una caja principal.")

print(f"\nEntidades incluidas: {len(geometria)}")
print(f"Anotaciones MULTILEADER separadas: {len(anotaciones)}")
print(f"Objetos extensos excluidos: {len(excluidos)}")

print("\nCAPAS INCLUIDAS")
for capa in sorted({
    ent.dxf.get("layer", "<sin capa>")
    for ent, _ in geometria
}):
    print(" -", capa)

print("\nANOTACIONES QUE DEBEN REVISARSE")
for ent, c in anotaciones:
    print(
        f"Handle={ent.dxf.handle} "
        f"Caja=({c[0]:.3f}, {c[1]:.3f}, "
        f"{c[2]:.3f}, {c[3]:.3f})"
    )

print("\nOBJETOS EXTENSOS EXCLUIDOS")
for ent, c in excluidos:
    print(
        f"Handle={ent.dxf.handle} "
        f"Tipo={ent.dxftype()} "
        f"Capa={ent.dxf.get('layer', '<sin capa>')} "
        f"Caja=({c[0]:.3f}, {c[1]:.3f}, "
        f"{c[2]:.3f}, {c[3]:.3f})"
    )

print("\nSolo se leyó el DXF; no se guardaron cambios.")
