# Protocolo del trabajador (`cde-exporter worker`), versión 1.0

Este documento es para quien programe la integración del **Componedor** con el exportador. Es el contrato entre los dos programas: el Componedor lanza el trabajador como **comando externo** (no importa código del exportador), le envía pedidos y lee respuestas, siempre en JSON por líneas.

Estado: **contrato aprobado por Luis Angel (2026-11-17)**, con las observaciones del agente del Componedor incorporadas (sección 14). Implementado por etapas: 1 (arranque, `load`, `--list --json`) y 2 (`preview`) con visto bueno; 3 (`export`, avance, cancelar y manifiesto 1.3) hecha, pendiente de visto bueno; 4 (DXF pequeño de prueba para el Componedor) pendiente.

Decisiones ya tomadas (Luis Angel, 2026-11-17):
- Un proceso de trabajo persistente que **lee el DXF una sola vez** y atiende tres órdenes: cargar (`load`), vista previa (`preview`) y exportar (`export`), más cancelar (`cancel`).
- Vista previa raster **PNG de 1 500 px** en el lado mayor, **sin la huella de los 3DSOLID** (la exportación final sí los dibuja).
- La caja de cada bloque se calcula **por definición** (una vez por bloque y transformada por la matriz de cada INSERT); en bloques anidados girados puede ser algo mayor que la exacta. La caja exacta la da el manifiesto del paquete exportado.

## 1. Arranque, transporte y ciclo de vida

```powershell
cde-exporter worker --work-dir "C:\ruta\carpeta_de_trabajo" [--preview-cache-mb 500]
```

- **Entrada (stdin):** una línea JSON por pedido, UTF-8 sin BOM, terminada en `\n`. Se lee en un **hilo aparte**: `status` y `cancel` reciben respuesta **siempre** y enseguida, también mientras otro pedido está leyendo el DXF con ezdxf o dibujando un rayado largo (sección 7).
- **Salida (stdout):** **siempre en UTF-8** (sin depender de la consola de Windows) y **solo JSON**, una línea por mensaje (respuestas y eventos de avance). Nada más sale por stdout: lo que el exportador escribe hoy en la terminal y el registro de las librerías (por ejemplo `WARNING:ezdxf:…`) van a **stderr**.
- **stderr:** registro libre en texto, solo para diagnóstico. El Componedor no debe interpretarlo (puede guardarlo en un archivo de registro).
- `--work-dir`: carpeta del trabajador para sus temporales y la caché de vistas previas. Si no se indica, se usa una carpeta temporal del sistema que se borra al cerrar.
- `--preview-cache-mb`: tamaño máximo de la caché de vistas previas (por defecto 500 MB). Al superarlo se borran las imágenes usadas hace más tiempo.
- **Temporales** (sección 1.1): en `work-dir` y, si hace falta alguno en la carpeta de salida, con el prefijo fijo `.cde-tmp-`.
- Al arrancar, el trabajador limpia los temporales que hayan quedado (1.1) y escribe un primer mensaje `ready` (sección 2.4) antes de aceptar pedidos.
- **Un pedido a la vez.** Mientras se atiende uno, solo se aceptan `cancel` (siempre), `status` y `close`; cualquier otro recibe el error `BUSY` (sección 8). El Componedor debe esperar la respuesta final de cada pedido antes de enviar el siguiente.
- **Cierre:** el pedido `close` o el fin de stdin (EOF) terminan el proceso limpio: se cancela lo que esté en curso, se borran los temporales y el código de salida es 0. Si el Componedor mata el proceso, no queda ningún paquete a medias (la publicación es atómica); los temporales que queden se borran al arrancar el siguiente trabajador o en el siguiente `export` a esa carpeta.
- **Sesión:** un trabajador tiene como máximo **un DXF cargado** a la vez (`session`). Un `load` nuevo reemplaza al anterior y libera su memoria.

### 1.1 Temporales y publicación atómica

- Cada paquete se prepara en una carpeta temporal y se publica **moviendo** los archivos a `output_dir` (manifiesto el último). Mover es atómico solo **dentro del mismo disco**.
- Por eso los temporales de cada trabajo de un `export` van **siempre en `output_dir`** (el mismo disco), en una carpeta con el prefijo fijo **`.cde-tmp-`** (por ejemplo `.cde-tmp-3f9a1c`) que se borra al terminar el trabajo, también si falla o se cancela. Los de `load` y de la vista previa van en `work-dir`.
- **Limpieza:** al arrancar, el trabajador borra las carpetas `.cde-tmp-*` de `work-dir`; al empezar un `export`, las de `output_dir` que tengan ese prefijo y no sean de otro trabajador en marcha (cada carpeta lleva dentro un archivo con el PID que la creó). Nunca borra nada sin ese prefijo.
- El Componedor puede ignorar cualquier archivo o carpeta de `output_dir` que empiece por `.cde-tmp-`.

## 2. Formato de los mensajes

### 2.1 Convenciones

- Claves y valores enumerados en **inglés y minúsculas** (como el manifiesto); los mensajes para personas, en **español**.
- **Rutas:** absolutas, tal como las usa Windows (en JSON, `\` se escribe `\\`).
- **Marco de coordenadas (uno solo para todo):** el **Model Space del DXF de origen, en WCS**, en unidades del dibujo, **con el INSERT ya aplicado** (posición, giro, escala y extrusión de la referencia). Lo usan, sin excepción:
  - `blocks[].bounds` de `load` (sección 3.1);
  - `bounds` y `cad_to_px` de `preview` (sección 4);
  - `pdf.drawing.cad_bounds` y `pdf.drawing.cad_to_page` del manifiesto de cada paquete.

  Así, un mismo punto del dibujo tiene las mismas coordenadas en la lista, en la vista previa y en el PDF final.
- **Papel:** en **mm**.
- **Valores ausentes:** `null`. Las claves documentadas aparecen siempre.
- **`handle` nunca es `null`:** toda referencia lleva el handle de su INSERT en el DXF de origen (en hexadecimal, en mayúsculas, como lo escribe AutoCAD).
- Campos desconocidos en un pedido se ignoran (para que versiones menores nuevas sigan siendo compatibles).

### 2.2 Pedido

```json
{"protocol": "1.0", "id": "c-17", "command": "load", "data": {"dxf": "C:\\obras\\GUAMO_prueba.dxf"}}
```

- `protocol`: versión del protocolo que habla el Componedor. Si la **versión mayor** no coincide con la del trabajador, la respuesta es el error `PROTOCOL_VERSION`.
- `id`: texto elegido por el Componedor, **único por pedido**; todas las respuestas y eventos de ese pedido llevan el mismo `id`.
- `command`: `load`, `preview`, `export`, `cancel`, `status` o `close`.
- `data`: los parámetros de la orden (secciones 3 a 7).

### 2.3 Respuestas y eventos

Todo mensaje del trabajador tiene `protocol`, `id` (el del pedido; `null` en `ready`) y `type`:

| `type` | Cuándo | Contenido |
|---|---|---|
| `ready` | Una vez, al arrancar | Versiones (2.4) |
| `progress` | Durante un pedido largo, varias veces | Avance (sección 6) |
| `result` | **Fin correcto** del pedido (una sola vez) | `data` con el resultado |
| `error` | **Fin con error** del pedido (una sola vez) | `error` (sección 8) |

Cada pedido termina con **exactamente un** `result` o un `error`. Ejemplo:

```json
{"protocol": "1.0", "id": "c-17", "type": "result", "command": "load", "data": {"...": "..."}}
```

### 2.4 `ready`

```json
{"protocol": "1.0", "id": null, "type": "ready",
 "data": {"protocol_version": "1.0", "exporter_version": "0.1.0", "ezdxf": "1.4.4", "pymupdf": "1.28.2",
          "manifest_schema_version": "1.3", "work_dir": "C:\\ruta\\carpeta_de_trabajo"}}
```

## 3. `load` — cargar un DXF

Pedido:

```json
{"protocol": "1.0", "id": "c-1", "command": "load",
 "data": {"dxf": "C:\\obras\\GUAMO_prueba.dxf", "units": null, "margin_mm": 5.0, "include_layers": true}}
```

- `units`: `null` (usar `$INSUNITS`) o `mm`, `cm`, `m`, `in`, `ft` (como `--units`). **Vale para toda la sesión:** la vista previa y la exportación usan estas unidades.
- `margin_mm`: margen de página (por defecto 5). **Vale para toda la sesión**: es el que usará `export` y el que entra en el tamaño de página que calcula el Componedor.
- `include_layers` (opcional, por defecto `true`): con `false` no se calcula la tabla de capas (`layers` es `[]`), lo que ahorra 1–2 s en bloques muy grandes.

Qué hace: lee el DXF **una vez** (con la copia temporal sin bloques sin usar si pasa de 200 MB, como hoy), calcula la huella y el SHA-256 (sección 10), la lista de bloques y la tabla de capas. Tiempos medidos con los prototipos: GUAMO_prueba.dxf (6,8 MB) unos 3 s; PRUEBAEXPORTADORSUPERFICIES.dxf (830 MB) unos 100–110 s, casi todo leer.

Respuesta (`data`):

```json
{
  "session": "s-1",
  "dxf": {
    "name": "GUAMO_prueba.dxf",
    "path": "C:\\obras\\GUAMO_prueba.dxf",
    "bytes": 6781020,
    "modified": "2026-11-12T16:17:03-05:00",
    "fingerprint": "8f0c…",
    "sha256": "4b1d…",
    "dxf_version": "AC1032",
    "read_seconds": 1.5,
    "source_copy": null
  },
  "units": {
    "declared_code": 6, "name": "m", "millimeters_per_unit": 1000.0,
    "status": "declaradas en el DXF y coherentes", "verified": true, "warnings": []
  },
  "margin_mm": 5.0,
  "blocks": [ { "...": "ver 3.1" } ],
  "omitted": [ {"name": "*U1095", "occurrence": 1, "total_occurrences": 1, "handle": "9A4EE5",
                "reason": "bloque anónimo (lo crea AutoCAD para cotas, rayados o bloques dinámicos)"} ],
  "layers": [ { "...": "ver 3.2" } ],
  "modelspace_loose": [ {"layer": "LaEstación1_Contours", "state": "congelada", "entities": 8386} ],
  "notices": [ {"code": "SOURCE_UNUSED_BLOCKS_SKIPPED", "severity": "info", "message": "…", "advice": null} ]
}
```

- `source_copy`: `null`, o los datos de la copia reducida si el DXF era grande (200 MB o más): `mode` (`"unused_blocks"`: sin el contenido de los bloques sin usar; `"selective"`: además sin lo suelto del Model Space), `cached` (se tomó de la caché), `source_bytes`, `copy_bytes`, `seconds` (lo que tardó hacerla), `emptied_blocks` y `loose_entities` (entidades sueltas que la copia selectiva no lleva).
- **Lectura selectiva:** si la copia sin bloques sin usar mediría 500 MB o más (variable `CDE_SELECTIVE_DXF_MB`), la copia guarda del Model Space solo los INSERT (con sus atributos) y todo el Paper Space. Los bloques, sus cajas y sus paquetes son los mismos; lo suelto solo se resume en `modelspace_loose`, que entonces lleva además `vertices`, `types` y `bounds_approx` (caja aproximada de los puntos guardados). Aviso `SOURCE_MODELSPACE_LOOSE_SKIPPED` (info) en `notices` y en cada paquete.
- **Caché de copias:** la copia reducida se guarda en `work-dir\dxf-cache\<SHA-256 del DXF>\` (las 3 usadas más recientemente). Otro `load` del mismo DXF la encuentra por la huella y el tamaño, comprueba el SHA-256 completo (una lectura seguida, sin interpretar) y la reutiliza sin repetir las pasadas.
- **Memoria:** antes de leer con ezdxf se estima la memoria (unos 7 bytes por byte de DXF o de copia, más 300 MB). Si el equipo no la tiene libre (memoria más archivo de paginación), `load` termina con `DXF_TOO_LARGE` y dice cuánta haría falta.
- `omitted`: referencias que no se pueden exportar, con el mismo motivo que hoy da `extract-all-blocks` (anónimo, sin definición, XREF, sin nada visible).
- `modelspace_loose`: entidades del Model Space fuera de cualquier bloque, por capa y estado (no se exportan con `export`; ver `extract-modelspace`).
- `unknown_entities`: entidades de tipos que ezdxf no sabe leer (de complementos de AutoCAD, p. ej. `VehicleTrackingPath`, `PLGRIDSYSTEM`, `GEOMAPIMAGE`), por tipo: `{"type", "modelspace", "in_blocks"}` (sueltas en el Model Space y en las definiciones de bloque). No se dibujan en ningún PDF ni en la vista previa; si hay alguna, `notices` lleva el aviso `UNKNOWN_ENTITIES_OMITTED` (warning). Nunca hacen fallar `load`, `preview` ni `export`.

### 3.1 Cada bloque (`blocks[]`)

Una entrada por **referencia** (INSERT) exportable del Model Space, en el orden del DXF:

```json
{
  "ref": "BLOQUE1#1",
  "name": "BLOQUE1",
  "handle": "50CA",
  "occurrence": 1,
  "total_occurrences": 1,
  "layer": "CERCA",
  "insert": {"point": [0.0, 0.0, 0.0], "rotation_deg": 0.0, "scale": [1.0, 1.0, 1.0]},
  "bounds": {"min": [899699.777, 937885.353], "max": [900656.957, 939235.372], "exact": false},
  "bounds_with_hidden": null,
  "size_cad": [957.180, 1350.019],
  "size_m": [957.180, 1350.019],
  "units": {"name": "m", "millimeters_per_unit": 1000.0, "verified": true, "status": "declaradas en el DXF y coherentes", "warnings": []},
  "entities": 156749,
  "contains": {"solids_3d": 0, "regions": 0, "proxies": 0, "texts": 1883, "hatches": 44137, "images": 0},
  "hidden_layer_entities": 0,
  "file_stem": "BLOQUE1"
}
```

- `ref`: identificador estable de la referencia dentro de la sesión (`nombre#ocurrencia`). Se puede usar en `preview` y `export` en lugar de nombre y ocurrencia.
- `handle`: el del INSERT en el DXF de origen; **nunca `null`**.
- `bounds`: caja en el marco único (sección 2.1), **calculada por definición** (decisión aceptada), de lo que **se dibujará al exportar**:
  - **incluye la huella de los 3DSOLID** (la que dibuja la exportación con `solids: "footprint"`), aunque la vista previa no los muestre;
  - **no incluye** lo que está en capas apagadas o congeladas.
  - `exact` es `false` si algún INSERT anidado está girado (la caja puede ser algo mayor que la exacta); `true` en otro caso.
  - **La posición útil para cuadrar es esta caja**, no `insert.point` (en los DXF de prueba, BLOQUE1/2/3 están insertados en (0, 0)).
- `bounds_with_hidden`: la misma caja contando también las capas ocultas (la que valdrá con `include_hidden_layers`); `null` si el bloque no tiene nada oculto (`hidden_layer_entities` = 0).
- `size_m`: `size_cad` por los metros de cada unidad; `null` si las unidades no se conocen (`units.verified = false` y sin `units` en `load`).
- **Tamaño en papel** (lo calcula el Componedor, sin pedir nada): `page_mm = size_m × 1000 ÷ N + 2 × margin_mm` por eje, para la escala 1:N. Es el tamaño que tendrá la página del PDF (salvo `exact = false`, donde será algo menor o igual).
- `units`: unidades **de esa referencia**, con la misma verificación que la exportación (incluye las del bloque y los indicios de escala del INSERT).
- `contains`: lo que puede cambiar el resultado. `solids_3d` > 0 significa que la vista previa no los mostrará y la exportación los dibujará como huella (sección 5, opción `solids`).
- `hidden_layer_entities`: entidades del bloque en capas apagadas o congeladas (no se dibujan salvo con `include_hidden_layers`).
- `unknown_entities`: las de tipos desconocidos del bloque y sus anidados (cada definición una vez), `[{"type", "count"}]`; no cuentan en `entities` ni en la caja. El paquete exportado las declara con el aviso `UNKNOWN_ENTITIES_OMITTED`.
- `file_stem`: nombre de paquete que se propone, ya válido según la sección 5.1 (con `_001`, `_002`… si hay varias referencias).

### 3.2 Cada capa (`layers[]`)

Todas las capas de la tabla del DXF, con lo necesario para armar la leyenda (vacío si `include_layers` es `false`):

```json
{
  "name": "C-TOPO",
  "color": {"aci": 7, "rgb": "#ffffff"},
  "effective_colors": [{"rgb": "#a57c00", "entities": 85}, {"rgb": "#a55200", "entities": 46}, {"rgb": "#ffff00", "entities": 1}],
  "lineweight_mm": null,
  "linetype": {"name": "Continuous", "description": "Solid line"},
  "state": {"on": true, "frozen": false, "plot": true},
  "description": "",
  "usage": [{"ref": "BLOQUE1#1", "types": {"LWPOLYLINE": 132}}]
}
```

- `color`: el de la capa (ACI y RGB; con color verdadero, `aci` es `null`).
- `effective_colors`: los colores con que **se dibujan de verdad** sus entidades (resolviendo PorCapa, PorBloque y el color propio de cada entidad), con cuántas entidades de cada uno. Hace falta porque el color de la capa no siempre es el que se ve (en GUAMO, `C-TOPO` es blanca pero sus curvas se ven naranjas). Se calcula al cargar, sin dibujar (unos 1–2 s para 156 000 entidades). El color 7 (blanco/negro) se da como `#000000`, que es como sale sobre papel blanco.
- `lineweight_mm`: grosor de la capa en mm, o `null` si es «Por defecto».
- `usage`: por referencia, qué tipos de objeto tiene la capa (contados en todos los niveles de anidado). Capas sin uso en ningún bloque exportable llevan `usage: []`.

## 4. `preview` — vista previa

Pedido:

```json
{"protocol": "1.0", "id": "c-2", "command": "preview",
 "data": {"session": "s-1", "ref": "BLOQUE1#1", "long_side_px": 1500, "include_hidden_layers": null}}
```

- En lugar de `ref` se puede dar `block` y `occurrence`, o `block` y `handle`.
- `long_side_px`: entero de 64 a 8 000 (1 500 si no se da).
- `include_hidden_layers`: como en `export` (`null`, `["*"]` o una lista de capas). Si se pide, la vista previa dibuja también esas capas ocultas.

Qué hace: dibuja **solo esa referencia** con el motor del exportador en modo ligero (texto como rectángulos, rayados rellenos, sin tipos de línea; reutilización de bloques repetidos) y la pinta en una imagen PNG sin pasar por el PDF. **No dibuja los 3DSOLID** (decisión aceptada). Se pinta en el orden de la grabación (el mismo que el PDF). Medido con la implementación: BLOQUE1 de GUAMO (156 749 entidades) 12,6 s (unos 9 s de dibujo y 3 de pintado); desde la caché, 0,01 s.

Respuesta (`data`):

```json
{
  "png": "C:\\ruta\\carpeta_de_trabajo\\previews\\8f0c…\\50CA_1500_visible.png",
  "ref": "BLOQUE1#1",
  "handle": "50CA",
  "px": [1069, 1500],
  "bounds": {"min": [899699.777, 937885.353], "max": [900656.957, 939235.372]},
  "cad_to_px": [1.0947, 0.0, 0.0, -1.0947, -984891.2, 1026714.6],
  "omitted": {"3DSOLID": 0, "REGION": 0},
  "include_hidden_layers": null,
  "cached": false,
  "seconds": 10.0
}
```

- `bounds`: la caja, en el marco único (sección 2.1), que representa la imagen (la de lo dibujado; puede ser algo menor que la de `load`, por ejemplo porque no lleva los 3DSOLID). Si no hay nada que dibujar (por ejemplo, un bloque solo con 3DSOLID), `bounds` y `cad_to_px` son `null` y el PNG es de 1 × 1 píxel transparente.
- `include_hidden_layers`: las capas ocultas que se dibujaron (en minúsculas), `["*"]` o `null`.
- `cad_to_px`: matriz afín `[a, b, c, d, e, f]` del marco único a píxeles: `px = a·x + c·y + e`, `py = b·x + d·y + f`. Siempre con **`b = c = 0`** (sin giro) y **`a = −d`** (la misma escala en X e Y; Y hacia abajo en la imagen).
- **Píxeles:** origen en la esquina **superior izquierda**; el píxel (0, 0) ocupa de 0 a 1 en X y en Y (su centro está en (0,5; 0,5)).
- **Imagen:** PNG con **fondo transparente** (canal alfa); el color 7 (blanco/negro) se dibuja en **negro**, como en el PDF.
- `omitted`: lo que la vista previa no dibuja, por tipo (los 3DSOLID, por decisión; lo que ezdxf no sabe dibujar, incluidos los tipos desconocidos de `unknown_entities`).
- **Caché:** el PNG se guarda en `work-dir\previews\<huella del DXF>\<handle>_<px>_<capas>.png`, con un `.json` del mismo nombre que guarda el resto de la respuesta. `<capas>` es `visible`, `hidden-all` (con `["*"]`) o `hidden-` y 6 cifras del SHA-1 de las capas pedidas. Si ya existe con la misma clave (huella, handle, tamaño y capas ocultas pedidas), se devuelve sin dibujar (`cached: true`) y cuenta como usada ahora. Si la caché pasa de `--preview-cache-mb` (500 MB por defecto), se borran las imágenes usadas hace más tiempo (nunca la que se acaba de devolver). Una vista previa cancelada no deja nada en la caché.

## 5. `export` — exportar

Pedido:

```json
{"protocol": "1.0", "id": "c-3", "command": "export",
 "data": {
   "session": "s-1",
   "output_dir": "C:\\obras\\GUAMO\\paquetes",
   "jobs": [
     {"job": "t1", "ref": "BLOQUE1#1", "scale": "1:2000", "package": "BLOQUE1_1-2000"},
     {"job": "t2", "block": "Carro frontal 1", "occurrence": 2, "scale": "1:50", "package": "Carro_2"}
   ],
   "options": {
     "hatch_mode": "auto", "hatch_threshold": 50000,
     "solids": "footprint", "omit_unsupported": true, "text_layer": true,
     "include_hidden_layers": null, "font_substitutions": null, "font_folder": null,
     "font_fallback": null, "overwrite": false, "write_summary": false
   }
 }}
```

- `jobs[]`: cada trabajo dice **qué** referencia (`ref`, o `block` + `occurrence`, o `block` + `handle`), **a qué escala** y **con qué nombre de paquete** (`package`, sección 5.1). Se publican `<package>.pdf`, `.txt` y `.manifest.json` en `output_dir`. Cada trabajo tiene su propia escala: se pueden repetir bloques y ocurrencias a escalas distintas con paquetes distintos.
- `options`: comunes a todos los trabajos; los mismos significados y valores por defecto que las opciones de `extract-block` (`solids: "footprint"` y `omit_unsupported: true` son los recomendados para que salga todo).
- **`units` y `margin_mm` son los de `load`.** No hace falta enviarlos; si se envían en `options` con un valor **distinto** del de `load`, el pedido termina con `BAD_REQUEST` (para cambiarlos, hay que volver a hacer `load`).
- **Antes de empezar** se comprueba todo: que cada referencia existe, que las escalas son válidas, que cada `package` cumple la sección 5.1, que no hay dos trabajos con el mismo `package`, que ningún archivo de salida pisa el DXF y que no existen ya (salvo `overwrite: true`). Si algo falla, error y **no se exporta nada**.
- Los trabajos se hacen **en orden**, sobre el DXF ya cargado (sin releerlo). Si uno falla, los demás siguen (como `extract-all-blocks`); el que falla no deja archivos.
- `write_summary`: por defecto `false`. Si es `true`, además se escribe `resumen_proyecto.json` en `output_dir`, con el mismo formato que hoy (y la respuesta lo nombra en `summary`; si no, `summary` es `null`).

### 5.1 Nombres de paquete

`package` debe cumplir, o el pedido termina con `OUTPUT_INVALID` antes de exportar nada:

- Solo **letras sin acentos (A–Z, a–z), números, punto, guion y guion bajo**; sin espacios, sin ruta y sin empezar por punto.
- **Como mucho 60 caracteres.**
- La ruta completa más larga (`output_dir\<package>.manifest.json`) **no pasa de 240 caracteres**.

Los `file_stem` que propone `load` ya cumplen estas reglas:
- los acentos y la eñe se cambian por su letra sin tilde (`Á` → `A`, `ñ` → `n`);
- cualquier otro carácter, por `_`;
- si el nombre pasa de 60 caracteres, se recorta y se añade un **sufijo corto de hash** del nombre original para que siga siendo único, por ejemplo `HSS-Tubo_estructural_redondo_Tubo_metalico_galv_3f9a1c`.

El nombre original del bloque sigue en el TXT y en el manifiesto.

### 5.2 Respuesta

Respuesta (`data`):

```json
{
  "output_dir": "C:\\obras\\GUAMO\\paquetes",
  "jobs": [
    {
      "job": "t1", "ref": "BLOQUE1#1", "status": "published",
      "package": {"pdf": "C:\\obras\\GUAMO\\paquetes\\BLOQUE1_1-2000.pdf",
                  "txt": "C:\\obras\\GUAMO\\paquetes\\BLOQUE1_1-2000.txt",
                  "manifest": "C:\\obras\\GUAMO\\paquetes\\BLOQUE1_1-2000.manifest.json"},
      "validation_status": "complete_with_warnings",
      "physical_scale_guaranteed": true,
      "page_size_mm": [488.590, 685.010],
      "notices": [{"code": "FONT_SUBSTITUTED", "severity": "info", "message": "…", "advice": null}],
      "error": null,
      "seconds": 94.0
    },
    {"job": "t2", "ref": "Carro frontal 1#2", "status": "failed", "package": null, "validation_status": null,
     "physical_scale_guaranteed": null, "page_size_mm": null, "notices": [],
     "error": {"code": "EXPORT_FAILED", "message": "…"}, "seconds": 2.1}
  ],
  "summary": null,
  "totals": {"published": 1, "failed": 1, "cancelled": 0}
}
```

- Cada trabajo lleva también `handle` (el de su INSERT). Sin `job`, se numeran `t1`, `t2`…; sin `package`, se usa el `file_stem` de `load`.
- `status`: `published`, `failed` o `cancelled` (no empezó, se interrumpió por `cancel` o el DXF cambió).
- `error` de un trabajo: `{"code", "message", "details"}`; con `CRITICAL_NOTICE`, `details.notices` es la lista de `{code, message}` de los avisos críticos.
- Opciones por defecto (las de `extract-block`): `hatch_mode: "auto"`, `hatch_threshold: 50000`, `solids: null`, `omit_unsupported: false`, `text_layer: true`, `include_hidden_layers: null`, fuentes `null` (se usan las de la configuración `.env`, como en la línea de órdenes), `overwrite: false`, `write_summary: false`.
- `notices`: los mismos avisos del manifiesto (lista única de avisos). La fuente de verdad del paquete sigue siendo **su manifiesto**: el Componedor debe validarlo como hoy.
- El SHA-256 del DXF que va en cada manifiesto es el calculado en `load` (no se vuelve a leer el archivo por paquete).
- Si el DXF cambió desde `load`, el pedido termina con el error `DXF_CHANGED` (sección 9).

### 5.3 Opciones en el manifiesto (requiere el esquema 1.3)

Para que un paquete diga **todo lo que cambia su dibujo**, el `options` de su manifiesto llevará, además de lo que ya lleva (`hatch_mode`, `hatch_threshold`, `solids`, `omit_unsupported`, `text_layer`, `font_substitutions`, `font_folder`, `font_fallback`, `margin_mm`, `include_hidden_layers`):

- `units`: las unidades forzadas en `load` (o `null`); hoy ya están en `units.user_override` y se repiten aquí para tener todo junto;
- `instancing` y `xobject`: si se usó la reutilización de bloques repetidos y el Form XObject (hoy dependen de las variables `CDE_INSTANCING` y `CDE_XOBJECT`);
- `clip`: si `extract-modelspace` recortó a la zona (hoy en `selection.clip`);
- `exporter_version`: la versión del exportador (hoy ya en `exporter.version`; se repite aquí).

Es un cambio del manifiesto (**esquema 1.3**, solo añade claves; los 1.0–1.2 siguen siendo válidos con `manifest._ADDED_IN`). Aprobado por Luis Angel (2026-11-17) e implementado con la etapa 3; también lo usan las órdenes de la línea de órdenes (`docs/manifiesto.md`). `clip` es `null` salvo en los paquetes de `extract-modelspace`.

## 6. Avance (`progress`)

Durante `load`, `preview` y `export` el trabajador envía eventos (como mucho unos 4 por segundo):

```json
{"protocol": "1.0", "id": "c-3", "type": "progress",
 "data": {"job": "t1", "job_index": 1, "job_total": 2,
          "stage": "draw", "stage_index": 3, "stage_total": 6,
          "percent": 41.5, "message": "Dibujando BLOQUE1: 61 200 de 156 749 entidades"}}
```

- `percent`: avance del **pedido completo** (0 a 100), estimado; solo sube.
- Etapas:
  - `load`: `read` (incluye la copia de un DXF grande), `hash`, `blocks`, `layers`.
  - `preview`: `draw`, `paint`.
  - `export`, por trabajo: `extract`, `analyze` (fidelidad e inventario), `draw`, `pdf`, `checks`, `publish`.
- En `draw` el porcentaje se calcula con las entidades del primer nivel del bloque ya dibujadas (cada INSERT anidado cuenta una, con todo su contenido); en las demás etapas, por etapa empezada. Pesos de cada etapa dentro de un trabajo: `extract` 10 %, `analyze` 10 %, `draw` 50 %, `pdf` 15 %, `checks` 10 %, `publish` 5 %.
- `job`, `job_index` y `job_total` son `null` fuera de `export`.

## 7. `cancel` y `status`

Los dos se atienden **siempre y enseguida** (stdin se lee en un hilo aparte), aunque el pedido en curso esté dentro de una operación de ezdxf que no se puede interrumpir.

```json
{"protocol": "1.0", "id": "c-4", "command": "cancel", "data": {"target": "c-3"}}
```

- Respuesta inmediata: `result` con `{"accepted": true}` (o `false` si `target` no está en curso).
- El pedido cancelado termina con `error` de código `CANCELLED`. En `export`, ese error trae en `details.jobs` el resultado por trabajo: los ya publicados **se quedan** (cada paquete es atómico), el que estaba en curso se descarta sin dejar archivos (se borra su carpeta temporal) y los pendientes quedan `cancelled`. Un `load` cancelado deja el trabajador sin sesión.
- **Cuándo termina el pedido cancelado:** entre etapas y, mientras se dibuja, cada pocos cientos de entidades: en general en menos de 1–2 s. Dentro de una operación de ezdxf que no se puede interrumpir (leer el DXF, un rayado complejo, que tiene un límite de 30 s), la respuesta a `cancel` llega enseguida, pero el `error` `CANCELLED` del pedido llega al terminar esa operación.

`status`:

```json
{"protocol": "1.0", "id": "c-5", "command": "status", "data": {}}
```

Respuesta: `{"busy": true, "current": "c-3", "stage": "read", "session": "s-1", "dxf": "GUAMO_prueba.dxf", "memory_mb": 603}`.

`close`: respuesta `result` con `{}` y el proceso termina (sección 1).

## 8. Errores

```json
{"protocol": "1.0", "id": "c-3", "type": "error",
 "error": {"code": "BLOCK_NOT_FOUND", "message": "No existe el bloque «BLOQUE9» en el DXF cargado.",
           "advice": "Revise la lista de bloques que devolvió «load».", "details": {"block": "BLOQUE9"}}}
```

- `code`: estable (para el programa); `message`: en español, para mostrar; `advice`: qué hacer, o `null`; `details`: datos útiles, o `{}`.

| Código | Cuándo |
|---|---|
| `PROTOCOL_VERSION` | La versión mayor del pedido no es la del trabajador |
| `BAD_REQUEST` | Línea que no es JSON, falta un campo obligatorio, un valor no es válido (escala, rutas…), o `units`/`margin_mm` de `export` distintos de los de `load` |
| `UNKNOWN_COMMAND` | `command` desconocido |
| `BUSY` | Llega un pedido distinto de `cancel`, `status` o `close` mientras otro está en curso |
| `NO_SESSION` | `preview` o `export` sin DXF cargado, o con un `session` que no es el actual |
| `DXF_NOT_FOUND` | La ruta del DXF no existe o no se puede leer |
| `DXF_INVALID` | El archivo no es un DXF que ezdxf pueda leer (incluye DXF dañados) |
| `DXF_TOO_LARGE` | No hay memoria para leerlo ni con la copia reducida (sección 3: el mensaje dice cuánta haría falta y cuánta hay libre) |
| `DXF_CHANGED` | El tamaño o la fecha del DXF cambiaron desde `load` (sección 9) |
| `BLOCK_NOT_FOUND` / `REFERENCE_NOT_FOUND` | No existe el bloque, o esa ocurrencia o handle |
| `NOT_EXPORTABLE` | La referencia está en `omitted` (anónima, XREF…) |
| `OUTPUT_EXISTS` | Un archivo de salida ya existe y `overwrite` es `false` |
| `OUTPUT_INVALID` | `output_dir` no es una carpeta válida, `package` no cumple la sección 5.1, o dos trabajos usan el mismo `package` |
| `CANCELLED` | El pedido se canceló |
| `INTERNAL` | Fallo inesperado del programa; `details.traceback` solo con `CDE_DEBUG=1` |

Errores **por trabajo** de `export` (dentro de `jobs[].error`, sin terminar el pedido):

| Código | Cuándo |
|---|---|
| `EXPORT_FAILED` | La exportación de ese trabajo falló (el mensaje es el mismo que hoy da la terminal; por ejemplo, entidades no soportadas sin `omit_unsupported`) |
| `CRITICAL_NOTICE` | Un aviso crítico lo detuvo (XREF o bloque sin resolver); `details.notices` los lista |
| `VALIDATION_FAILED` | Una comprobación que bloquea no pasó (PDF ilegible, tamaño de página…) |

## 9. Si el DXF cambia mientras el trabajador está abierto

- En `load` se guardan el **tamaño** y la **fecha de modificación** del archivo.
- **Antes de cada `preview` y de cada `export`** (y antes de cada trabajo de un `export`) se vuelven a mirar (unos microsegundos).
- **Si cambió el tamaño o la fecha, el pedido termina con `DXF_CHANGED`**, sin más comprobaciones (no se compara la huella). En un `export`, los trabajos ya publicados se quedan y el resto no se hace. El Componedor debe hacer un `load` nuevo.
- Motivo: el trabajador dibuja lo que leyó en `load`, y el manifiesto tiene que llevar el hash del archivo que hay en disco; mezclar los dos daría paquetes con un hash que no corresponde a su dibujo.
- El trabajador **nunca** modifica ni bloquea el DXF de origen. Si AutoCAD lo está guardando justo durante `load`, la lectura puede fallar (`DXF_INVALID`); basta con repetir `load`.

## 10. Huella y hash del DXF

Hay dos valores distintos:

- **Huella (`fingerprint`)**, solo para la **caché de vistas previas** (para saber si una imagen guardada es de este DXF): SHA-256 del tamaño del archivo más **64 trozos de 64 KiB** repartidos por todo el archivo (incluidos el primero y el último). Medido: **17 ms** con 830 MB; con 2,9 GB, del mismo orden (lee siempre 4 MiB). No sirve como identidad criptográfica del archivo.
- **SHA-256 completo (`sha256`)**, el que va en cada manifiesto: se calcula **una sola vez por `load`** y se reutiliza en todos los paquetes de la sesión (hoy se calcula de nuevo en cada paquete).
  - En un DXF de 200 MB o más se calcula **en la misma pasada** que ya lee el archivo entero para la copia sin bloques sin usar: **no añade lectura**.
  - En uno menor, se lee una vez (GUAMO, 6,8 MB: milisegundos).
  - Medido: **1,7 s** para 830 MB con el archivo en la caché del sistema (unos 495 MB/s). Con 2,9 GB, unos 6 s en caliente; en frío depende del disco (puede ser bastante más). Que un DXF de 2,9 GB quepa en memoria se está midiendo.

## 11. Versión simple sin trabajador: `--list --json`

Para usos puntuales (scripts, pruebas) sin mantener un proceso abierto:

```powershell
cde-exporter cad extract-all-blocks "C:\obras\GUAMO_prueba.dxf" --list --json [--units m] [--margin-mm 5] [--no-layers] [--scale 1:2000] [--scale-map escalas.json]
```

- Escribe por stdout (en UTF-8) **un solo documento JSON** con el mismo contenido que el `data` de `load` (secciones 3, 3.1 y 3.2), sin `session`. Con `--scale` o `--scale-map`, cada bloque lleva además `scale` y `page_size_mm`. `--no-layers` equivale a `include_layers: false`.
- Todo lo demás (avisos de la terminal y registro de las librerías) va por stderr.
- Código de salida: 0 si se pudo leer; 1 con un error, que también se escribe como JSON por stdout con la forma de la sección 8 (`{"error": {...}}`).
- Usa la misma caja por definición, así que es mucho más rápido que el `--list` de hoy (GUAMO: unos 3 s frente a 112 s). Lee el DXF en cada llamada: con 830 MB son unos 100 s por llamada; para varias llamadas seguidas conviene el trabajador.

## 12. Ejemplo de sesión

```text
← {"protocol":"1.0","id":null,"type":"ready","data":{"protocol_version":"1.0", …}}
→ {"protocol":"1.0","id":"c-1","command":"load","data":{"dxf":"C:\\obras\\GUAMO_prueba.dxf"}}
← {"protocol":"1.0","id":"c-1","type":"progress","data":{"stage":"read","percent":20.0, …}}
← {"protocol":"1.0","id":"c-1","type":"result","command":"load","data":{"session":"s-1","blocks":[…], …}}
→ {"protocol":"1.0","id":"c-2","command":"preview","data":{"session":"s-1","ref":"BLOQUE1#1"}}
← {"protocol":"1.0","id":"c-2","type":"result","command":"preview","data":{"png":"C:\\…\\50CA_1500_visible.png", …}}
→ {"protocol":"1.0","id":"c-3","command":"export","data":{"session":"s-1","output_dir":"C:\\obras\\GUAMO\\paquetes","jobs":[…],"options":{…}}}
← {"protocol":"1.0","id":"c-3","type":"progress","data":{"job":"t1","stage":"draw","percent":41.5, …}}
→ {"protocol":"1.0","id":"c-4","command":"cancel","data":{"target":"c-3"}}
← {"protocol":"1.0","id":"c-4","type":"result","command":"cancel","data":{"accepted":true}}
← {"protocol":"1.0","id":"c-3","type":"error","error":{"code":"CANCELLED","message":"Exportación cancelada: 0 publicados, 2 cancelados.","advice":null,"details":{"jobs":[…]}}}
→ {"protocol":"1.0","id":"c-5","command":"close","data":{}}
← {"protocol":"1.0","id":"c-5","type":"result","command":"close","data":{}}
```

(← mensajes del trabajador por stdout; → pedidos del Componedor por stdin.)

## 13. Pruebas que acompañarán la implementación

Además de las pruebas de cada orden, estas comprueban el contrato:

- **Marco único con un INSERT movido y girado:** un DXF sintético con un bloque insertado en (1 000, 2 000), girado 30° y con escala 2. Se comprueba que:
  - las cuatro esquinas de `blocks[].bounds` (`load`) contienen a las de lo dibujado;
  - un punto conocido del bloque (por ejemplo, el extremo de una línea) cae en el mismo sitio según `cad_to_px` de `preview` (con tolerancia de 1 píxel) y según `pdf.drawing.cad_to_page` del manifiesto (con tolerancia de 0,01 mm), medido en el PNG y en el PDF.
- `cad_to_px` cumple `b = c = 0` y `a = −d`; el PNG tiene canal alfa y fondo transparente.
- `status` y `cancel` responden mientras un `load` lee un DXF grande (con un DXF de prueba de lectura lenta).
- stdout es solo JSON en UTF-8, también cuando ezdxf avisa (`WARNING:ezdxf:…` va a stderr).
- `units` o `margin_mm` distintos en `export` dan `BAD_REQUEST`.
- `DXF_CHANGED` al cambiar la fecha o el tamaño del DXF entre `load` y `export`.
- Nombres de paquete: acentos, eñes, caracteres raros, más de 60 caracteres (sufijo de hash) y ruta de más de 240 caracteres.
- Temporales `.cde-tmp-` limpiados al arrancar y al empezar un `export`.

## 14. Decisiones y observaciones incorporadas

Visto bueno de Luis Angel (2026-11-17):

1. Claves y valores enumerados en inglés; mensajes (y errores) en español.
2. Un pedido a la vez, con `BUSY` para el resto; `cancel` se acepta siempre.
3. `resumen_proyecto.json` solo si se pide (`write_summary: true`).
4. Caché de vistas previas con límite configurable (`--preview-cache-mb`, 500 MB por defecto), borrando las más antiguas.
5. Versión del protocolo: empieza en `1.0`; las versiones menores solo añaden campos.

Observaciones del agente del Componedor (2026-11-17), todas incorporadas:

1. Un cambio de tamaño o fecha del DXF da `DXF_CHANGED`, sin comparar la huella (sección 9).
2. Un solo marco de coordenadas para `bounds`, `cad_to_px` y `pdf.drawing.cad_to_page`: Model Space en WCS con el INSERT aplicado (sección 2.1), con prueba de INSERT movido y girado (sección 13).
3. Temporales en `work-dir` (mismo disco que la salida) o con prefijo `.cde-tmp-` en la salida, y limpieza al arrancar (sección 1.1).
4. `status` y `cancel` responden siempre, con stdin en un hilo aparte (secciones 1 y 7).
5. stdout siempre UTF-8 y solo JSON; el registro de las librerías va a stderr (sección 1).
6. `units` y `margin_mm` valen los de `load`; distintos en `export`, `BAD_REQUEST` (secciones 3 y 5).
7. `bounds` incluye la huella de los 3DSOLID y no las capas ocultas (con `bounds_with_hidden` aparte); `preview` acepta `include_hidden_layers`, que entra en la clave de la caché (secciones 3.1 y 4).
8. `cad_to_px` con `a = −d`, `b = c = 0`; píxel (0, 0) de 0 a 1; fondo transparente; color 7 en negro (sección 4).
9. `handle` nunca `null` (secciones 2.1 y 3.1).
10. Nombres de paquete: letras sin acentos, números, punto, guion y guion bajo; máximo 60 caracteres con sufijo de hash; ruta completa de 240 caracteres como mucho (sección 5.1).
11. El `options` del manifiesto con todas las opciones que cambian el dibujo y la versión del exportador (sección 5.3; **esquema 1.3, pendiente de visto bueno**).
12. Opcional: `include_layers: false` en `load` (secciones 3 y 11).
