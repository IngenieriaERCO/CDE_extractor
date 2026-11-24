# Manifiesto de detalle (`cde-detail-manifest`, esquema 1.5)

Este documento es para quien programe el **componedor de PDF** (la plancha A1). El manifiesto es el contrato técnico de cada detalle extraído: con él se puede identificar, validar y colocar el PDF sin volver a leer el DXF de origen.

Cada extracción publica tres archivos con el mismo prefijo: `<prefijo>.pdf`, `<prefijo>.txt` y `<prefijo>.manifest.json`, en ese orden. El manifiesto se publica **el último**: un PDF sin su manifiesto es un paquete **incompleto** (por ejemplo, un corte a mitad de la publicación) y no debe usarse. Con `--all`, los nombres llevan el número de referencia (`<prefijo>_001.pdf`, `<prefijo>_001.manifest.json`…), y se publica todo o nada.

El código está en `src/cde_exporter/cad/manifest.py` (`build_manifest`, `validate_manifest` y `verify_package`). Hay un ejemplo real de Perfil_K0010 en `data/output/referencia/manifiesto_ejemplo/perfil_k0010.manifest.json` (fuera de Git: son datos de ERCO).

## Cómo usarlo desde el componedor

1. Busca el manifiesto junto al PDF (`<prefijo>.manifest.json`). Si no está, el paquete está incompleto: no uses el PDF.
2. Lee el JSON (UTF-8) y comprueba `document_type == "cde-detail-manifest"` y que la versión mayor de `schema_version` sea `1`.
3. Comprueba `validation.status`. Solo se publican paquetes completos (`complete` o `complete_with_warnings`): si una comprobación que bloquea falla, o hay un aviso crítico, no se publica nada.
4. Calcula el SHA-256 del PDF y compáralo con `pdf.sha256`. Si no coincide, el PDF no es el que describe el manifiesto (p. ej. se reemplazó sin su manifiesto): no lo uses.
5. Si necesitas escala física, exige `scale.physical_scale_guaranteed == true`. Si es `false`, `scale.physical_scale_reason` dice por qué.
6. Para colocar el detalle, usa `pdf.page_size_mm` y `pdf.drawing.page_bounds_mm`. Para relacionar coordenadas del CAD con el PDF, usa la matriz `pdf.drawing.cad_to_page.matrix`.
7. Si `pdf.drawing.mask` no es null (zona del Model Space con máscara de recorte), mide los trazos como dice [Cómo medir los trazos con máscara](#cómo-medir-los-trazos-con-máscara-desde-la-15).

Ejemplo en Python, solo con la biblioteca estándar:

```python
import hashlib, json

manifest = json.load(open("detalle.json", encoding="utf-8"))
assert manifest["document_type"] == "cde-detail-manifest"
assert manifest["schema_version"].split(".")[0] == "1"
pdf_bytes = open(manifest["pdf"]["file_name"], "rb").read()
assert hashlib.sha256(pdf_bytes).hexdigest() == manifest["pdf"]["sha256"]

a, b, c, d, e, f = manifest["pdf"]["drawing"]["cad_to_page"]["matrix"]
def cad_to_page_mm(x, y):          # coordenadas del Model Space -> mm de página
    return a * x + c * y + e, b * x + d * y + f
```

## Convenciones

- **Unidades del papel:** milímetros.
- **Coordenadas de página:** origen en la esquina **superior izquierda**, X hacia la derecha e **Y hacia abajo** (la misma convención que PyMuPDF). En coordenadas PDF nativas (origen abajo a la izquierda), la Y es `alto_de_página − Y`.
- **Coordenadas CAD:** las del **Model Space del DXF de origen** (WCS), en unidades CAD. El INSERT elegido se copia sin mover, así que su posición, giro y escala ya están incluidos.
- **Rutas:** el manifiesto guarda **nombres de archivo**, nunca rutas, y nunca contienen `\` ni `/`.
- **Enumerados:** en inglés y en minúsculas (`complete`, `transformed`, `warning`…). Los mensajes, en español.
- **Valores ausentes:** `null`. Todas las claves del esquema aparecen siempre.

## Versiones

- `schema_version` versiona este contrato. Un cambio de la versión menor (`1.1`) solo añade información; un cambio de la mayor (`2.0`) puede romper a quien lo lea.
- `1.1` (2026-11-11) añade `options.include_hidden_layers`, `selection` (paquetes de `cad extract-modelspace`) y los avisos de capas ocultas (`HIDDEN_LAYERS_CONTENT`, `HIDDEN_LAYERS_INCLUDED`, `MODELSPACE_HIDDEN_OVERLAP`) y del Model Space (`MODELSPACE_NOT_COPIED`, `MODELSPACE_ZERO_LENGTH`, `MODELSPACE_WITHOUT_BOUNDS`). Un lector de la `1.0` puede leerlo si ignora las claves que no conoce (comprobado con el componedor el 2026-11-12: integridad y coherencia pasan).
- `1.2` (2026-11-12) añade `selection.clip` (recorte a la zona) y los avisos `MODELSPACE_CLIPPED` (info) y `MODELSPACE_NOT_CLIPPED` (warning).
- `1.3` (2026-11-18, aprobada por Luis Angel) añade a `options` todo lo que cambia el dibujo, junto: `units`, `instancing`, `xobject`, `clip` y `exporter_version`. Los manifiestos `1.0`–`1.2` siguen siendo válidos sin esas claves.
- `1.4` (2026-11-19) añade a `options` `plot_style` y `print_transparency` (`--plot-style` y `--no-print-transparency`) y los avisos `PLOT_STYLE_APPLIED` y `HATCH_TRANSPARENT_OMITTED` (info). Sin esas opciones valen `null` y `true` y el dibujo no cambia. Los manifiestos `1.0`–`1.3` siguen siendo válidos sin esas claves.
- `1.5` (2026-11-24) añade `pdf.drawing.mask` (zona de la máscara de recorte del PDF, o null) y, con máscara, `pdf.drawing.cad_bounds` y `page_bounds_mm` pasan a ser lo que se ve: lo dibujado recortado a la zona, siempre dentro de la página (antes contaban enteras las entidades que corta la máscara y se salían de la página). Los manifiestos `1.0`–`1.4` siguen siendo válidos sin `mask`. También desde la 1.5 el trabajador exporta zonas (`docs/protocolo_trabajador.md`); sus paquetes son como los de `cad extract-modelspace`.
- `model_version` es la versión del modelo interno del detalle (hoy `"2"`). Es un dato de trazabilidad: el componedor no debería depender de ella.

## Campos

### Raíz

| Campo | Tipo | Significado |
|---|---|---|
| `document_type` | texto | Siempre `"cde-detail-manifest"`. |
| `schema_version` | texto | Versión de este esquema (`"1.5"`). |
| `model_version` | texto | Versión del modelo interno (`"2"`). |
| `generated_at` | texto o null | Fecha y hora de la extracción (ISO 8601 con zona horaria). |

### `exporter`

`version` (versión del exportador), `ezdxf` y `pymupdf` (versiones de las bibliotecas que dibujan el PDF). Los tres son texto o null.

### `source`: de dónde sale el detalle

| Campo | Tipo | Significado |
|---|---|---|
| `dxf_name` | nombre | Nombre del DXF de origen (sin ruta). |
| `dxf_sha256`, `dxf_bytes` | texto, entero | Hash y tamaño del DXF: identifican la versión exacta del dibujo. |
| `block_name` | texto | Nombre del bloque (detalle). |
| `block_definition_handle` | texto o null | Handle de la definición del bloque. |
| `reference.handle`, `reference.layer` | texto | Handle y capa del INSERT elegido. |
| `reference.occurrence`, `reference.total_occurrences` | entero | Qué referencia es (desde 1) y cuántas hay en el Model Space. |
| `reference.selection_method` | texto o null | `automatic`, `occurrence`, `handle` o `all`. |
| `reference.insertion_xyz`, `rotation_deg`, `scale_xyz` | números | Posición, giro y escala del INSERT. |

### `units`

`declared_code` (código `$INSUNITS`), `name`, `status` (verificación de las unidades, en palabras), `millimeters_per_unit` y `user_override` (lo indicado con `--units`, o null).

### `scale`

| Campo | Tipo | Significado |
|---|---|---|
| `requested` | texto o null | Escala pedida, p. ej. `"1:50"`; null si no se pidió escala nominal. |
| `denominator` | número o null | El N de 1:N. |
| `factor_mm_per_cad_unit` | número o null | mm de papel por unidad CAD aplicados. |
| `physical_scale_guaranteed` | **booleano** | `true` solo si las unidades están verificadas **y** la escala medida en el PDF coincide con la nominal (comprobación «Escala del dibujo», tolerancia 0,01 mm). Nunca es null. |
| `physical_scale_reason` | texto o null | Si no está garantizada, por qué. |

### `pdf`

| Campo | Tipo | Significado |
|---|---|---|
| `file_name`, `bytes`, `sha256` | nombre, entero, texto | El PDF exacto que describe el manifiesto. El PDF no es determinista (su `/ID` es aleatorio): el hash identifica el archivo, no sirve para comparar dos extracciones. |
| `page_count` | entero | Páginas (siempre 1 por detalle). |
| `page_size_mm` | [ancho, alto] | Tamaño de página **medido** en el PDF. |
| `expected_page_size_mm` | [ancho, alto] o null | Tamaño calculado por la escala (contenido + márgenes). |
| `margin_mm`, `content_size_mm` | número, [ancho, alto] | Margen pedido y tamaño del contenido según la caja del CAD. |
| `drawing.cad_bounds` | `{min: [x, y], max: [x, y]}` | Caja de lo dibujado, en coordenadas CAD. |
| `drawing.page_bounds_mm` | `{x0, y0, x1, y1}` | La misma caja en la página (mm, origen arriba a la izquierda). |
| `drawing.mask` | objeto o null | Desde la 1.5. Null sin máscara de recorte (bloques, y zonas en las que todo lo que cruza el borde se pudo recortar). Con máscara: `zone_cad` (`[xmin, ymin, xmax, ymax]`, la zona en coordenadas CAD), `zone_page_mm` (`{x0, y0, x1, y1}`, la zona en la página) y `bleed_mm` (lo que la máscara sobresale de la zona por cada lado, 0,5 mm: el PDF recorta en `zone_page_mm` ampliada `bleed_mm`). Con máscara, `cad_bounds` y `page_bounds_mm` son lo dibujado **recortado a la zona**. |
| `drawing.cad_to_page` | objeto | `units: "mm"`, `origin: "top-left"`, `y_axis: "down"` y `matrix: [a, b, c, d, e, f]`, con **X = a·x + c·y + e** e **Y = b·x + d·y + f**. Es la matriz real con la que se dibujó el PDF. |

`drawing` es null si no hubo nada que dibujar. La caja de lo dibujado puede ser algo menor que `content_size_mm`, porque la caja del CAD incluye cosas que no dejan trazo, como el recuadro de un MTEXT; el dibujo se centra en la página.

### `entities`

| Campo | Significado |
|---|---|
| `total` | Entidades del detalle (incluidas las de bloques anidados). |
| `by_type[]` | Por tipo: `entity_type`, `total` y `by_status` (`{estado: cantidad}`). |
| `by_layer[]` | Por tipo, capa y estado (`entity_type`, `layer`, `status`, `count`). |
| `not_conserved[]` | Entidades no conservadas, agrupadas por tipo, capa, estado y motivo (`reason`). |
| `hatches` | `mode`, `total`, `simplified` y `groups[]` (`kind`, `pattern`, `scale`, `count`, `treatment`, `reason`). |

Estados: `conserved`, `transformed` (p. ej. texto con fuente sustituida), `simplified`, `omitted`, `unsupported` y `not_visible` (capa apagada o congelada, o entidad invisible: no sale en el PDF porque tampoco se ve en el CAD).

### `fonts` y `dependencies`

- `fonts[]`: `requested` (fuente pedida por el CAD), `resolved` (la usada), `found`, `uses` y `origin` (`configurada` o `respaldo automático`).
- `dependencies[]`: dependencias externas (`kind`, `reference` como nombre de archivo, `required`, `resolved` y `detail`).

### `notices`: avisos

Es la misma lista que muestra el TXT en «A tener en cuenta», generada en un solo sitio (`src/cde_exporter/cad/notices.py`).

- `counts`: `{info, warning, critical}`, cuántos avisos hay de cada severidad.
- `by_category`: un objeto con las ocho categorías, siempre presentes aunque estén vacías: `fonts`, `hatch`, `dependencies`, `units`, `text`, `renderer`, `scale` y `entities`. Cada aviso tiene `code`, `severity`, `message` y `advice` (consejo, o null).

Severidades:
- `info`: informa (fuente sustituida, capa de texto, sólidos leídos con el lector propio del transform ACIS). No cambia el estado.
- `warning`: el estado pasa a `complete_with_warnings`.
- `critical`: dependencias sin resolver (XREF o bloque). **Impiden publicar**: la extracción termina con código 1, sin archivos y con un mensaje que dice qué falta. Por eso un manifiesto publicado nunca tiene avisos críticos.

Códigos:

| Categoría | Códigos |
|---|---|
| `fonts` | `FONT_SUBSTITUTED` (info), `GLYPHS_MISSING` |
| `hatch` | `HATCH_DEGRADED` |
| `dependencies` | `DEPENDENCY_UNRESOLVED_XREF` (critical), `DEPENDENCY_UNRESOLVED_BLOCK` (critical), `DEPENDENCY_UNRESOLVED_IMAGE`, `DEPENDENCY_UNRESOLVED_UNDERLAY` |
| `units` | `UNITS_UNVERIFIED`, `UNITS_SUSPECT` |
| `text` | `TEXT_LAYER` (info), `TEXT_AS_PATHS` (info, con `--no-text-layer`), `TEXT_NOT_SEARCHABLE` |
| `renderer` | `RENDERER_MESSAGE` (mensajes de ezdxf, p. ej. «relative point size is not supported») |
| `scale` | `DRAWING_SCALE_MISMATCH`, `PHYSICAL_SCALE_NOT_GUARANTEED` |
| `entities` | `ENTITIES_WITHOUT_BOUNDS`, `UNSUPPORTED_OMITTED`, `ENTITIES_INVALID_DATA`, `SOLIDS_AS_FOOTPRINT`, `SOLIDS_ACIS_OWN_READER` (info), `SOLIDS_NOT_CONVERTED`, `FIDELITY_RECONCILIATION`, `MODELSPACE_NOT_COPIED` (warning), `MODELSPACE_ZERO_LENGTH` (info), `MODELSPACE_WITHOUT_BOUNDS` (warning), `HIDDEN_LAYERS_CONTENT` (info: capa apagada o congelada con entidades del bloque que no se dibujan), `HIDDEN_LAYERS_INCLUDED` (info: capa oculta dibujada con `--include-hidden-layers`), `MODELSPACE_HIDDEN_OVERLAP` (warning: entidades sueltas del Model Space, en capa oculta, sobre la zona del bloque) |

Si no se indica la severidad, es `warning`.

### `validation`

- `status`: `complete` (ningún aviso warning o critical) o `complete_with_warnings`.
- `checks[]`: `name`, `passed`, `blocking` y `detail`. Las que tienen `blocking: true` (PDF legible, una sola página, tamaño de página coherente con la escala) impiden publicar si fallan, así que en un manifiesto publicado siempre están en `passed: true`. Las que tienen `blocking: false` («Escala del dibujo» y «Texto buscable») solo avisan.

### `selection`

Desde la 1.1. `null` si se exportó un bloque (`cad extract-block`). En un paquete de `cad extract-modelspace`: `kind` (`"modelspace"`), `layers` y `exclude_layers` (patrones pedidos; lista vacía = todas), `window` (`[xmin, ymin, xmax, ymax]` en coordenadas CAD, o null), `window_from_block` (bloque del que se tomó la zona, o null), `entities` (entidades del Model Space elegidas; un INSERT cuenta una), `not_copied`, `zero_length_skipped`, `without_bounds_skipped` y, desde la 1.2, `clip`: null sin recorte (sin zona o con `--no-clip`), o `clipped_entities` (entidades recortadas), `pieces` (tramos que quedaron), `outside_removed` (las que solo tocaban la zona con su caja) y `not_clipped` (lista de `{type, count}`: lo que cruza el borde y no se puede recortar; desde el 2026-11-23 entra entero y la máscara de recorte del PDF lo corta en el borde, y su texto completo va en el TXT). Con recorte, `pdf.page_size_mm` es la zona más los márgenes (antes del 2026-11-23 se ampliaba con lo que entraba entero) y `pdf.drawing.cad_bounds` la caja de lo dibujado: menor si lo recortado no llega a los bordes; si algo cruza el borde entero (máscara), desde la 1.5 es la caja recortada a la zona y `pdf.drawing.mask` da la zona (en la 1.4 era mayor que la zona). Desde el 2026-11-23 se recortan también arcos, círculos, elipses y HATCH en planta, y `layers`/`exclude_layers` valen también dentro de los bloques (como congelar una capa en una ventana del CAD). Esto no cambió la versión del esquema. En esos paquetes, `source.block_name` es el bloque de trabajo que agrupa la selección («Model_Space») y `source.reference` su INSERT en (0, 0, 0): las coordenadas CAD siguen siendo las del Model Space de origen.

### `options`

Las opciones con las que se ejecutó la extracción: `hatch_mode`, `hatch_threshold`, `solids` (`footprint` o null), `omit_unsupported`, `text_layer`, `font_substitutions`, `font_folder` y `font_fallback` (estos tres, como nombres de archivo o null), `margin_mm` e `include_hidden_layers` (desde la 1.1: null si no se usó la opción, `["*"]` para todas las capas ocultas o la lista de capas pedidas).

Desde la 1.3, además:
- `units`: las unidades forzadas con `--units` (o en `load` del trabajador), o null. Es el mismo valor que `units.user_override`.
- `instancing`: si se reutilizó el dibujo de los INSERT repetidos (false con `CDE_INSTANCING=0`).
- `xobject`: si los bloques reutilizados van en el PDF como Form XObject (false con `CDE_XOBJECT=0` o sin reutilización).
- `clip`: en un paquete de `cad extract-modelspace`, si se recortó a la zona (false con `--no-clip` o sin zona); null en los demás.
- `exporter_version`: la versión del exportador (la misma que `exporter.version`).

Desde la 1.4, además:
- `plot_style`: la tabla de estilos de impresión con que se dibujó (`--plot-style`): `{"name": nombre del archivo .ctb, "sha256": SHA-256 del archivo}`, o null. Con ella, los colores y grosores de las entidades con color ACI son los de la tabla, no los del dibujo (los de color verdadero no cambian).
- `print_transparency`: false con `--no-print-transparency`: los HATCH con transparencia del 80 % o más no se dibujaron (cuántos y en qué capas, en el aviso `HATCH_TRANSPARENT_OMITTED` y en `entities.hatches`).

## Cómo medir los trazos con máscara (desde la 1.5)

Para el agente del componedor (su comprobación «h) Trazos del PDF vs page_bounds_mm»; la «g) page_bounds_mm dentro de la página» no cambia y ya pasa).

En una zona del Model Space, lo que cruza el borde y no se puede recortar (textos, cotas, INSERT, splines…) se escribe **entero** en el PDF y una máscara de recorte (`q x y w h re W n … Q`) oculta lo que sale de la zona. `page.get_drawings()` de PyMuPDF **no aplica la máscara**: devuelve los trazos enteros, que pueden salirse de la página. Por eso:

1. Si `pdf.drawing.mask` es null: sin cambios (los trazos sin el fondo blanco frente a `page_bounds_mm`, ±0,5 mm).
2. Si no es null:
   1. Toma `R = pdf.drawing.mask.zone_page_mm` (mm de página, origen arriba a la izquierda, Y hacia abajo; para PyMuPDF, multiplica por 72/25,4). Es la zona **sin** el `bleed_mm`.
   2. Para cada trazo de `page.get_drawings()` (sin el fondo blanco de página completa, como ahora), recorta su `rect` a `R` **a mano**: `x0 = max(rect.x0, R.x0)`, `y0 = max(rect.y0, R.y0)`, `x1 = min(rect.x1, R.x1)`, `y1 = min(rect.y1, R.y1)`. No uses `Rect.intersect` ni `&` de PyMuPDF: un segmento horizontal o vertical tiene un `rect` de alto o ancho cero, que PyMuPDF trata como vacío y perdería.
   3. Descarta los trazos con `x0 > x1` o `y0 > y1` (los que caen enteros fuera de la zona, en la franja de `bleed_mm` o más allá). Un trazo que solo toca el borde (ancho o alto cero) cuenta.
   4. La caja medida es el mínimo de los `x0`, `y0` y el máximo de los `x1`, `y1` de lo que queda.
   5. Compárala con `page_bounds_mm`: misma tolerancia de ±0,5 mm por lado.
3. Sin trazos tras el recorte: falla (como hoy «sin trazos»).

Por qué cuadra: el exportador calcula `cad_bounds` como la intersección de la caja de lo dibujado (la que graba ezdxf) con `zone_cad`, y `page_bounds_mm` es esa caja por la matriz; recortar cada trazo a la misma zona da la misma caja con la diferencia de siempre entre trazos y caja grabada. Lo comprueba `tests/test_zone_clipping.py::test_with_a_mask_the_manifest_drawing_is_the_visible_part`, que hace exactamente estos pasos.

Lo demás no cambia: el tamaño de página, la matriz y la comprobación «f) cad_bounds por la matriz = page_bounds_mm» siguen valiendo (`page_bounds_mm` es `cad_bounds` por la matriz también con máscara). Para colocar el detalle usa `page_bounds_mm` como siempre: es lo que se ve.

## Validación del manifiesto

`validate_manifest(datos)` devuelve una tupla de errores (vacía si es válido). No usa `jsonschema`: el esquema está declarado en el propio módulo y exige todas las claves, sin claves de más. Además comprueba:
- la versión mayor del esquema (un manifiesto de una versión menor anterior es válido sin las claves que se añadieron después: los paquetes 1.0 ya publicados siguen pasando `cad verify-package`);
- que haya al menos una página;
- que los hashes sean SHA-256 en hexadecimal;
- que los nombres de archivo no lleven ruta;
- que `notices.counts` cuadre con los avisos;
- que un estado `complete` no tenga avisos warning o critical.

El exportador valida cada manifiesto contra su esquema, lo escribe y lo comprueba contra el PDF (hash, bytes, páginas, tamaño de página y nombre) antes de publicar; si algo no cuadra, no se publica nada.

## Comprobar un paquete publicado

`verify_package(ruta)` (con la ruta del PDF o del manifiesto) devuelve los problemas del paquete, o una tupla vacía si está completo y es coherente: el manifiesto existe, cumple el esquema y el PDF que nombra existe junto a él con el mismo SHA-256 y los mismos bytes. Desde la terminal:

```powershell
.venv\Scripts\cde-exporter cad verify-package data\output\detalle.pdf
```

Termina con código 0 («Paquete completo y coherente») o con código 1 y la lista de problemas (p. ej. «Paquete incompleto: falta el manifiesto detalle.manifest.json.»).

## Ejemplo (extracto de Perfil_K0010 a 1:50)

```json
{
  "document_type": "cde-detail-manifest",
  "schema_version": "1.5",
  "model_version": "2",
  "source": {"dxf_name": "prueba_erco_perfil.dxf", "dxf_sha256": "1f1726e9…c07b38ed", "dxf_bytes": 58025354, "block_name": "Perfil_K0010"},
  "scale": {"requested": "1:50", "denominator": 50.0, "factor_mm_per_cad_unit": 20.0, "physical_scale_guaranteed": true, "physical_scale_reason": null},
  "pdf": {
    "file_name": "perfil_k0010.pdf", "page_count": 1,
    "page_size_mm": [416.97985, 119.40286],
    "drawing": {
      "cad_bounds": {"min": [4924093.7479, 2632176.2394], "max": [4924113.5538, 2632181.6351]},
      "page_bounds_mm": {"x0": 10.431, "y0": 5.744, "x1": 406.549, "y1": 113.658},
      "cad_to_page": {"units": "mm", "origin": "top-left", "y_axis": "down",
                      "matrix": [20.0, 0.0, 0.0, -20.0, -98481864.52706, 52643638.44637]},
      "mask": null
    }
  },
  "notices": {"counts": {"info": 4, "warning": 2, "critical": 0}},
  "validation": {"status": "complete_with_warnings"}
}
```
