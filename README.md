# CDE_manager
Interfaz de servicios para operar sobre el CDE

## Fuentes CAD

La configuración persistente de fuentes se define en `.env` (consulta `.env.example`):
`CDE_FONT_FALLBACK` indica el archivo de respaldo, `CDE_FONT_SUBSTITUTIONS` la ruta de un
JSON familia→archivo y `CDE_FONT_FOLDER` una carpeta de fuentes del proyecto. Las opciones
`--font-substitutions` y `--font-folder` de `cad extract` y `cad extract-block` prevalecen
sobre estos valores.

Ambos comandos usan `--hatch-mode auto` por defecto. Se pueden elegir explícitamente
`normal`, `solid`, `outline` e `ignore`.

`cad extract` está **en desuso**: sigue funcionando igual (publica `.dxf`, `.svg`, `.pdf` y
`_fidelidad.txt`), pero avisa en la terminal. Para exportar una zona del Model Space use
`cad extract-modelspace --window …` (ver «Exportar el Model Space»), que publica el paquete
completo con sus comprobaciones.

## Exportar todos los bloques de un DXF (un proyecto)

`cad extract-all-blocks` exporta cada bloque referenciado en el Model Space como su propio
paquete (`.pdf`, `.txt` y `.manifest.json`) dentro de una carpeta de proyecto, con el mismo
flujo que `cad extract-block`. En PowerShell, con el `.venv` activado:

```powershell
# 1. Ver qué se exportaría y escribir una plantilla de escalas (no crea paquetes)
cde-exporter cad extract-all-blocks "data\input\via_la_union.dxf" --output-dir "data\output\via_la_union" --list --scale 1:50 --write-scale-map "data\input\escalas.json"

# 2. Editar escalas.json y cambiar solo las escalas que difieran de 1:50

# 3. Exportar el proyecto
cde-exporter cad extract-all-blocks "data\input\via_la_union.dxf" --output-dir "data\output\via_la_union" --scale 1:50 --scale-map "data\input\escalas.json"
```

- **Escala:** `--scale 1:N` es la escala por defecto; `--scale-map` es un JSON UTF-8
  `{"nombre exacto del bloque": "1:N"}` que la cambia para los bloques que nombra. Si a algún
  bloque le falta la escala, o el mapa nombra un bloque que no existe, la orden se detiene
  antes de crear nada. Esta orden no pregunta la escala en la terminal.
- **Varias referencias** del mismo bloque: un paquete por referencia, `<bloque>_001`,
  `<bloque>_002`… (el número es la ocurrencia en el DXF).
- **Se omiten con aviso** (no cuentan como fallo): bloques anónimos (`*U…`, `*D…`, `*X…`),
  referencias a un bloque sin definición, XREF y referencias sin nada visible (bloque vacío,
  o todo en capas apagadas o congeladas, o invisible).
- **Nombres de archivo:** los caracteres que Windows no admite (`< > : " / \ | ? *`) y el
  punto se cambian por `_`; se conservan acentos y eñes; los nombres reservados (`CON`,
  `NUL`, `COM1`…) llevan `_` al final; si dos bloques darían el mismo archivo (sin distinguir
  mayúsculas), el segundo lleva `_2`. El nombre original sigue en el TXT y en el manifiesto.
- **Si falla un bloque**, los demás se publican igual; el que falló no deja archivos y la
  orden termina con código 1. Antes de empezar se comprueba que ningún archivo de salida
  exista ya (si existe, hace falta `--overwrite`).
- **Resumen:** al final, en la terminal, y en `resumen_proyecto.json` dentro de la carpeta:
  `project` (nombre del DXF sin extensión), `generated_at`, `source_dxf`, `default_scale`,
  `scale_map`, `totals`, `packages` (bloque, ocurrencia, archivos, escala y `status`
  `complete` o `complete_with_warnings`, el mismo del manifiesto), `omitted` y `failed`, cada
  uno con su `reason`.
- **Capas ocultas:** cada paquete dice en `hidden_layers` qué capas apagadas o congeladas
  tienen contenido del bloque que no se dibujó, y en `modelspace_hidden_overlap` qué
  entidades sueltas del Model Space, en capas ocultas, están encima de la zona del bloque.
  `modelspace_loose` cuenta todo lo suelto del Model Space (fuera de cualquier bloque) por capa
  y estado: esta orden no lo exporta.

## Capas apagadas o congeladas

Lo que está en una capa apagada o congelada no se dibuja, igual que en el CAD. `cad
extract-block` y `cad extract-all-blocks` lo avisan en la terminal, en el TXT («A tener en
cuenta»), en el manifiesto y en `resumen_proyecto.json`:

- `HIDDEN_LAYERS_CONTENT` (info): capa oculta con entidades **del bloque** que no se dibujan.
  En la capa «0» de un bloque cuenta la capa del INSERT que lo contiene.
- `MODELSPACE_HIDDEN_OVERLAP` (advertencia): entidades **sueltas** del Model Space, en una capa
  oculta, encima de la zona del bloque. No forman parte del bloque, así que no salen en el PDF
  (tampoco con `--include-hidden-layers`). Es el caso de unas curvas de nivel en una capa
  congelada: AutoCAD no deja seleccionarlas al crear el bloque.

Para dibujar a propósito lo que está en capas ocultas del bloque, `--include-hidden-layers`
(todas) o `--include-hidden-layers "Capa1,Capa2"` (solo esas; si alguna no existe, la orden se
detiene sin crear nada). Ponla después del DXF. Solo cambia la copia de trabajo, nunca el DXF
de origen; el TXT y el manifiesto (`options.include_hidden_layers`) lo registran:

```powershell
cde-exporter cad extract-block "data\input\obra.dxf" --block-name DET --scale 1:50 --output-prefix "data\output\det" --include-hidden-layers "Curvas"
```

## Exportar el Model Space (sin bloque)

`cad extract-modelspace` exporta lo que hay en el Model Space, aunque no esté en ningún bloque
(por ejemplo, unas curvas de nivel sueltas), como un paquete normal (`.pdf`, `.txt` y
`.manifest.json`), con el mismo flujo y las mismas comprobaciones que `cad extract-block`:

```powershell
# Ver qué entraría (entidades, vértices, tamaño y si cabe en A1), sin crear nada
cde-exporter cad extract-modelspace "data\input\obra.dxf" --output-prefix "data\output\curvas" --layers "Curvas*" --include-hidden-layers --window-from-block BLOQUE1 --scale 1:2000 --list

# Exportar
cde-exporter cad extract-modelspace "data\input\obra.dxf" --output-prefix "data\output\curvas" --layers "Curvas*" --include-hidden-layers --window-from-block BLOQUE1 --scale 1:2000
```

- **Todo:** sin filtros entra todo el Model Space (también los INSERT, enteros).
- **Por capas:** `--layers` y `--exclude-layers`, separadas por comas, con comodines `*` y `?`
  y sin distinguir mayúsculas. Un INSERT se elige por su propia capa. Si ninguna capa
  coincide, o todas las pedidas están apagadas o congeladas y no se usa
  `--include-hidden-layers`, la orden se detiene sin crear nada.
- **Por zona:** `--window XMIN YMIN XMAX YMAX` (coordenadas CAD) o `--window-from-block
  <bloque>` (la caja de una referencia; con varias, `--occurrence N`). Entra cada entidad cuya
  caja toca la zona. Lo que no tiene caja no se puede situar: no entra y se avisa
  (`MODELSPACE_WITHOUT_BOUNDS`).
- **Recorte a la zona** (por defecto con zona): las LINE y las polilíneas de tramos rectos
  (POLYLINE 3D y 2D, LWPOLYLINE) se cortan de verdad en el borde, con su cota, su ancho y sus
  propiedades; una curva que entra y sale varias veces queda en varios tramos. La página mide
  **exactamente la zona** (`MODELSPACE_CLIPPED`). Lo demás (arcos, círculos, texto, HATCH,
  INSERT, polilíneas con arcos…) entra entero; si cruza el borde, la página se amplía para que
  quepa y se avisa (`MODELSPACE_NOT_CLIPPED`). `--no-clip` deja todo entero, como antes.
- Las líneas y polilíneas de longitud nula no se dibujan (`MODELSPACE_ZERO_LENGTH`).
- Las coordenadas del manifiesto son las del Model Space: con `--window-from-block`, recorte y
  la misma escala que el bloque, las dos páginas miden lo mismo y se superponen exactas.
- El TXT (sección B) y el manifiesto (`selection`) dicen qué se pidió y cuántas entidades
  entraron. La selección se agrupa en un bloque de trabajo («Model_Space») que solo existe en la
  copia: es el nombre que sale como bloque.

## Inventario en JSON y trabajador (para el Componedor)

`--list --json` escribe por la salida estándar un solo JSON con cada bloque exportable (caja en
coordenadas del Model Space, tamaño, unidades, nombre del paquete), los que no se exportan con
su motivo, las capas con sus colores y lo suelto del Model Space. No crea ningún archivo:

```powershell
cde-exporter cad extract-all-blocks "data\input\obra.dxf" --output-dir "data\output\obra" --list --json --scale 1:50
```

(En Windows PowerShell 5.1, `>` guarda el archivo en UTF-16: el Componedor lee la salida
directamente, sin pasar por un archivo.)

`cde-exporter worker` es un proceso que el Componedor deja abierto: lee el DXF una vez y
atiende pedidos en JSON, uno por línea. El formato está en `docs/protocolo_trabajador.md`.
Atiende `load`, `status`, `cancel`, `close`, `preview` (vista previa PNG de un bloque,
guardada en una caché de la carpeta de trabajo de 500 MB como mucho; se cambia con
`--preview-cache-mb`) y `export` (varios paquetes, cada uno con su escala y su nombre, con el
mismo resultado que `cad extract-block`, sin volver a leer el DXF; se puede cancelar).

## DXF muy grandes

ezdxf carga todo el DXF en memoria. Si el DXF pesa 200 MB o más, `cad extract-block`,
`cad extract-all-blocks` y `cad extract-modelspace` lo leen desde una **copia temporal** sin el
contenido de los bloques que no se usan en ningún sitio (por ejemplo, un bloque pegado en el
dibujo y luego borrado del Model Space). La copia se hace leyendo el archivo por partes, sin
cargarlo entero, dentro de la carpeta de salida, y se borra en cuanto se ha leído. El DXF de
origen no cambia: el manifiesto guarda su hash y el TXT lo dice en «A tener en cuenta»
(`SOURCE_UNUSED_BLOCKS_SKIPPED`). No hace falta hacer PURGE en AutoCAD.

Un bloque se conserva si lo nombra o lo apunta cualquier cosa (otro bloque usado, una cota, un
estilo, un objeto…). El umbral se cambia con la variable de entorno `CDE_LARGE_DXF_MB` (en
PowerShell, `$env:CDE_LARGE_DXF_MB = "0"` la usa siempre).
