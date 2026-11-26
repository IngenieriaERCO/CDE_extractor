# Estado del proyecto — Exportador Vectorial

Último hito: Fase 4, pasos 5, 6 y 7 — avisos críticos que bloquean, manifiesto publicado como tercer archivo y `--all` todo o nada (consulta git log para el hash). Después, ajuste para los planos de PVcase (3DSOLID con transform binario y MULTILEADER; sección «Ajuste Fase 4: planos de PVcase»). Después, PDF sin rectángulo blanco de fondo de página (sección «Ajuste: PDF sin fondo de página»). Después, arreglo de `--help` y orden `cad extract-all-blocks` (sección «Ajuste: `--help` y exportación de proyecto»). Último ajuste, en la rama `trabajo/capas-ocultas-model-space`: avisos de capas ocultas, `--include-hidden-layers`, orden `cad extract-modelspace` y copia por partes de DXF muy grandes y recorte a la zona en `extract-modelspace` (sección «Ajuste: capas ocultas y Model Space»; recorte sin commit); reutilización de bloques repetidos y unidades en metros sin aviso falso (sección «Ajuste: rendimiento con bloques repetidos», con el Form XObject). En curso: trabajador persistente para el Componedor, etapas 1 (commit `99761a4`), 2 (commit `5de8302`) y 3 (commit `6c9c5b7`) de 4 (sección «Trabajador persistente»); después, entidades de complementos y lectura selectiva (sección «Entidades de complementos, bloques anónimos y lectura selectiva», commit `114268f`) y bloques anónimos `*U` en el trabajador (sección «Bloques anónimos en el trabajador», commit `92c2c37`) y el sistema de coordenadas del GEODATA en `load` (sección «Sistema de coordenadas en load», commit `58fc279`); después, tabla de impresión CTB y transparencia de los rellenos (sección «Tabla de impresión (CTB) y transparencia», commit `7154704`) y CTB derivada del PDF de AutoCAD (sección «CTB derivada del PDF de AutoCAD», sin commit). En curso: zona del Model Space como detalle, subfases 1, 2 y 3 de 3 (secciones «Zona del Model Space como detalle», «Vista previa de una zona» y «Zonas en el trabajador», sin commit) y fondo de los HATCH de patrón (sección «Color de fondo de los HATCH») y orden de dibujo ACAD_SORTENTS (sección «Orden de dibujo»), commit `402ad72`; después, cuñas de los LEADER y medición de la vista previa (sección «Cuñas de los LEADER», commit `1af0712`); después, velocidad de la vista previa (sección «Velocidad de la vista previa», sin commit). 723 pruebas pasando, ruff limpio.

## Plan en 4 fases
1. Selección de bloque + escala — hecha.
2. Modelo interno del detalle (v2) + informe TXT A–G + contrato de salida PDF+TXT — hecha.
3. Motor de representación vectorial (política de fuentes, rayados, entidades) — hecha (quedan pendientes menores, abajo).
4. Manifiesto JSON + validación — hecha (pasos 0 a 7; queda pendiente menor: condensar advertencias de handles duplicados).
Después: componedor de PDF (plancha A1) y guía de usuario en el README.

## Pendientes menores
- Decidir si `--scale` pasa a ser obligatorio sin terminal (rompería ~12 pruebas).

## Fase 3
Hecho:
- Entidades no visibles (capa apagada o congelada, o entidad invisible): ya no detienen la exportación; se cuentan y se avisan en "A tener en cuenta".
- `--omit-unsupported`: las entidades que ezdxf omite por "unsupported" se cuentan por tipo y capa, salen como "omitidas" en el inventario y se avisan en el TXT y al final de la terminal. Por defecto sigue estricto.
- `--solids footprint`: los 3DSOLID convertibles con `ezdxf.acis` se dibujan como su huella en planta (una vez por definición, en una copia de render); la caja, el contenido y la página salen de esa copia. Probado con Plano_completo: 17739 sólidos (162 distintos), error máximo 0,001 mm en el papel.
- El error por 3DSOLID sin soporte sugiere `--solids footprint` (dibujarlos como huella 2D) o `--omit-unsupported` (omitirlos).
- Rendimiento (Plano_completo: de 166 s a ~85 s; Perfil_K0010: 18,5 s):
  - El DXF de origen se lee una sola vez (antes, tres).
  - Fuentes: se configuran después de validar el bloque y la escala, y solo con el detalle extraído (menos ruido en la terminal; terminal e informe cuentan lo mismo).
  - La caja geométrica se calcula una vez y se reutiliza (sección C, página y comprobación del exportador).
  - El SVG solo se dibuja con `CDE_KEEP_INTERMEDIATES`; sin él, los avisos salen del dibujo del PDF y el TXT queda igual.
- Extracción fiel a los recursos por defecto: el estilo de texto `Standard`, las capas `0` y `Defpoints` y las variables de cabecera que lee el dibujo (`$LTSCALE`, `$PDMODE`, `$PDSIZE`, `$MEASUREMENT`) toman los valores del original. Antes, `Standard` salía con la fuente `txt` de ezdxf en lugar de la del dibujo (p. ej. `simplex.shx`).
- Entidades con datos inválidos (caso VIA_2MA.dxf: un LEADER cuyo override de DIMSTYLE apuntaba a un XRECORD como estilo de texto, que tumbaba la extracción con traceback):
  - Caja entidad por entidad (`safe_extents` en `reader.py`): primero se mide todo de una vez; si falla, se mide entidad por entidad, entrando en los bloques anidados, y se salta solo la dañada. Se usa en todos los cálculos de caja.
  - Aviso `ENTITIES_INVALID_DATA` en la terminal y en "A tener en cuenta", con tipo, handle, capa y bloque, y lo que pasa realmente con la entidad (p. ej. "Se dibuja con el estilo de texto por defecto" si la extracción la reparó).
  - Al dibujar y en el inventario, una entidad que lanza error se trata como no soportada con el motivo "datos inválidos: …" (sin trazos a medias): con `--omit-unsupported` se omite con aviso; sin ella, la extracción se detiene con un mensaje claro. La prueba de caja del inventario solo corre en entidades compuestas (LEADER, DIMENSION, MLINE, ACAD_TABLE…), sin coste apreciable.
  - El aviso "entidades sin caja" se recalcula sobre lo que se dibuja (antes salía falso por la entidad inválida del origen), y la línea "Huellas: 0 3DSOLID…" ya no sale si el bloque no tiene sólidos.
- Registro de HATCH en el TXT (sección F): modo de rayado y una tabla "tipo | patrón | escala | tratamiento | cantidad" con todos los HATCH. El tratamiento es "conservado" o la operación aplicada (relleno sólido en lugar del patrón, solo contorno, no se dibuja) con su motivo. Las simplificaciones por umbral indican además la operación. Campos nuevos opcionales (`HatchGroup`, `HatchSummary.groups/mode`, `HatchDetail.operation`): `model_version` sigue en "2".
- Texto como trazados: "A tener en cuenta" declara cuántas entidades de texto van dibujadas como trazados vectoriales (no seleccionables ni buscables). Una prueba lo contrasta con el PDF real.
- Fuentes instaladas ya no se sustituyen por el respaldo (p. ej. Verdana). Había dos causas en `font_config.py`: `ezdxf.fonts.is_shx_font_name` da verdadero para cualquier nombre sin `.ttf` (los nombres de familia como "Verdana" nunca se comprobaban), y la comprobación abría `matched.filename` como si fuera una ruta (es solo el nombre). Ahora `font_is_installed` acepta la fuente si coincide la familia o el archivo, y el informe de fidelidad usa la misma regla. Cambió el hash del SVG de Perfil_K0010 (4 textos pasan de Arial a Verdana); el hash nuevo está pendiente de fijar en CLAUDE.md.
- Fuentes (paso 6): (1) el TXT indica si cada sustitución fue "configurada (JSON --font-substitutions)" o "automática (fuente de respaldo)" (`FontUse.origin`, opcional; la clave `origin` aparece en el JSON de `fonts`, `model_version` sigue en "2"); (2) aviso `GLYPHS_MISSING` si la fuente sustituta no tiene caracteres que usa el texto, con el carácter, su código y cuántos textos lo usan (terminal y TXT; cuenta como advertencia); se comprueba con `get_ttf_font().getBestCmap()` de ezdxf, sin coste medible (Plano_completo: 1,24 s con la comprobación, 1,31 s sin ella); (3) ezdxf 1.4 no ofrece API pública para registrar un solo archivo de fuente, así que el uso de `_font_cache` queda aislado en `register_font_file`: si ezdxf lo cambia, falla con un mensaje claro (versión de ezdxf y "1.4.x"), y una prueba avisa del cambio.
- Tabla de política por tipo de entidad (paso 5), en `entity_policy.py` (solo biblioteca estándar): `fidelity.entity_facts` reúne con ezdxf lo observado de cada entidad y `decide` da el estado, el motivo y la causa. El inventario (`detail_builder`), el informe de fidelidad (`inspect_fidelity`) y las pistas de error de `exporters` usan las mismas reglas; antes decidían por separado.
  - MULTILEADER: se conservan si ezdxf genera geometría con sus propios datos (no dependen del gráfico proxy). Antes salían siempre como "no soportados": en Plano_completo, 5 MULTILEADER con proxy se dibujaban y figuraban como no soportados. Si no generan geometría son "no soportados" y el exportador los detiene o los omite con `--omit-unsupported`, como el resto (ezdxf los descartaba en silencio).
  - Estado nuevo "no visibles" en la sección E (solo se imprime si hay alguna): capa apagada o congelada, entidad invisible, 3DFACE con todos los bordes invisibles, o contenido de un INSERT invisible. Se calcula con el mismo `RenderContext` que el exportador (un INSERT solo depende de su indicador `invisible`; en la capa "0" de un bloque se hereda la capa del INSERT). Una entidad no visible no cuenta como fuente sustituta, HATCH ni no soportada (un 3DSOLID oculto ya no detiene la extracción). La conciliación con lo que descarta el exportador sigue en tiempo de ejecución (`FIDELITY_RECONCILIATION`). Valor nuevo `not_visible` en `EntityStatus` (`model_version` sigue en "2").
- Error CAD sin traceback: cualquier excepción inesperada termina con "Error CAD inesperado (Tipo): mensaje. No se generaron archivos.", código 1 y sin archivos. Con la variable `CDE_DEBUG=1` se muestra el traceback completo.
- HATCH problemáticos (paso 4):
  - Estimación de tramos con el contorno real (`_estimate_hatch_segments`, exporters.py): por cada línea del patrón, cortes con el borde más guiones dentro del área (con huecos e islas). Se acerca a lo que dibuja ezdxf (ANSI35 de Perfil: 3 969 estimados, 3 867 reales; antes 522 385 con la caja). Si el contorno no se puede leer, se usa la estimación por la caja.
  - Corrección del patrón de los HATCH dentro de un INSERT (`restore_hatch_pattern`): ver «Fallo de ezdxf» más abajo. En Perfil, el ANSI35 (3 867 tramos) y el ANSI37 (46) de la capa MATERIALES pasan de relleno a su patrón; en Plano_completo cambian los 29 rayados de patrón (ANSI31, GRAVEL y FP_1); VIA_2MA no tiene rayados de patrón. Verificado dibujo a dibujo: solo cambian esos HATCH.
  - Semilla fija por HATCH: ezdxf desplaza el origen de las líneas del patrón con `random.random()`; con la semilla, el dibujo y el hash del SVG son reproducibles.
  - Un HATCH cuyo patrón agota el tiempo (30 s, el de ezdxf) o la memoria ya no aborta la extracción: se borran los trazos que dejó a medias y se rellena solo ese HATCH. El PDF reutiliza lo aprendido en el SVG (no espera otros 30 s) y ambos deben coincidir. En el TXT pasa a "simplificadas" con el motivo, y hay aviso `HATCH_DEGRADED` en la terminal y en "A tener en cuenta".
  - Líneas de patrón más juntas que 0,0001 unidades (ezdxf las rellena en silencio): la tabla de política las declara "simplificadas" con su motivo. El recuento de HATCH simplificados del informe de fidelidad sale de las mismas decisiones que el inventario.
  - Límites: la corrección del patrón supone INSERT con escala uniforme y sin simetría; la comprobación de líneas demasiado juntas se hace sobre la definición del HATCH.

- Texto buscable (paso 3), según el prototipo (ver «Prototipo de texto buscable» más abajo):
  - `exporters.py`: con `capture_text`, `_StrictFrontend` envuelve `pipeline.draw_text` y anota cada línea que ezdxf llega a dibujar (`TextRun`); solo durante el dibujo del PDF, así que el SVG y su hash no cambian. Una entidad que falla al dibujarse (omitida por datos inválidos) deshace también su texto; las no visibles no llegan a dibujarse. `_TextLayerPdfBackend` calcula la misma matriz CAD → página que `get_replay` y `write_text_layer` escribe la capa con `TextWriter` + Helvetica (`render_mode=3`, matriz `morph` corregida para el eje Y invertido de PyMuPDF) y reduce las fuentes a subconjunto.
  - Opción `--no-text-layer` (en `extract-block` y en `extract`); la capa está activada por defecto.
  - `pdf_checks.check_searchable_text`: comprobación "Texto buscable: N de N líneas de texto" con `search_for`; solo se cuentan las líneas dibujadas (un MTEXT vacío no cuenta). Es una comprobación de aviso (`ADVISORY_CHECKS` en `detail_model.py`): si falta alguna línea, el PDF se publica igual, la comprobación sale como `[AVISO]` en el TXT, el aviso `TEXT_NOT_SEARCHABLE` (cuántas líneas faltan y cuáles, hasta 10) va a "A tener en cuenta" y a la terminal, y el estado final es "completa con advertencias".
  - TXT: la nota de "A tener en cuenta" dice que el texto se puede buscar, seleccionar y copiar, que al copiar los saltos de línea siguen los cortes del dibujo y que la caja de búsqueda sigue la fuente usada en el dibujo. Con `--no-text-layer` queda la nota anterior.
  - Medido (antes → después; tiempo de la exportación completa, que varía unos segundos entre ejecuciones): Perfil_K0010 14,6 → 14,1 s, 206 686 → 247 744 bytes (+20 %), 120/120 líneas; VIA_2MA 54,1 → 52,3 s, 1 612 892 → 1 815 311 bytes (+13 %), 775/775; Plano_completo 75,5 → 68,5 s, 1 205 812 → 1 273 981 bytes (+6 %), 230/230. El coste de la capa queda por debajo de la variación entre ejecuciones.

Fallo de ezdxf 1.4.4 (documentado; todavía sin avisar a ezdxf): `DXFPolygon.transform` (ezdxf/entities/polygon.py, líneas 402-410) calcula bien la escala y el ángulo nuevos del patrón, pero los aplica enteros (`self.pattern.scale(factor, angle)`) sobre unas líneas que ya los incluían, en lugar de aplicar solo el cambio. Todo HATCH de patrón con escala ≠ 1 o ángulo ≠ 0 que se dibuja dentro de un INSERT sale con la escala aplicada dos veces y girado dos veces. Reproducción mínima (solo datos sintéticos):

```python
import ezdxf

doc = ezdxf.new()
block = doc.blocks.new("B")
hatch = block.add_hatch()
hatch.set_pattern_fill("ANSI31", scale=0.5, angle=30)
hatch.paths.add_polyline_path([(0, 0), (10, 0), (10, 10), (0, 10)], is_closed=True)
insert = doc.modelspace().add_blockref("B", (100, 0))  # solo traslación
virtual = next(e for e in insert.virtual_entities() if e.dxftype() == "HATCH")
print(hatch.pattern.lines[0])    # [75.0, (0.0, 0.0), (-1.5334072493, 0.4108752341), []]
print(virtual.pattern.lines[0])  # [105.0, (0.0, 0.0), (-0.7667036246, -0.2054376171), []]
# Esperado: el mismo ángulo (75°) y el mismo desplazamiento; ezdxf gira otros 30° y escala ×0,5.
```

Prototipo de texto buscable (2026-10-27; script fuera del proyecto en `data/output/referencia/scripts/prototipo_texto.py`):
- Idea: capturar cada línea de texto que ezdxf convierte en trazados (`RenderPipeline2d.draw_text`: texto, transformación, altura de mayúsculas, anchura de avance) y la matriz CAD → página (`Layout.get_placement_matrix`), y añadir encima de los glifos una capa de texto invisible (`render_mode=3`), estirada a la anchura dibujada.
- Perfil_K0010 (1:50): 120 líneas de 81 MTEXT con texto (el 82.º está vacío); `search_for` encuentra 120/120 en su sitio. Desviación inicio/fin de palabra (≥ 3 caracteres): mediana 0,11 mm, máx. 0,71 mm (márgenes laterales de las letras); giro máx. 0,05°. La caja de búsqueda sobresale 0,5–1,8 mm arriba/abajo (va de ascendentes a descendentes). PDF 206 686 → 247 744 bytes (+20 %); capa en 0,10 s. El SVG no cambia.
- DXF sintético (0°, 90°, 45°, 30° y 120° por anidamiento hasta 3 niveles, factor de anchura, oblicuo, MTEXT de varias líneas y con formato, `romans.shx`, fuente inexistente, Unicode): 32/32 encontrados, mismos órdenes de error.
- Variantes descartadas: `insert_text` + Helvetica (solo Latin‑1: ≥, ⌀, α, √ salen como «·»; 1,1 s por la copia del contenido de la página en cada inserción); Arial incrustada (el guion «-» se extrae como U+00AD y el espacio como U+00A0: «-9.70» no se encuentra; 92/120). Elegida: `TextWriter` + Helvetica (fuente Type0, Unicode con respaldo Noto en subconjunto).
- PyMuPDF aplica la matriz `morph` con el eje Y invertido: para la matriz de página (a, b, c, d) hay que pasar (a, −b, −c, d). Sin esa corrección los textos girados salen con el giro cambiado de signo y no se encuentran.
- MTEXT: cada línea dibujada es una línea de texto; las búsquedas que cruzan líneas funcionan; al copiar, los saltos siguen los cortes del dibujo (no se distingue `\P` del ajuste automático). El MTEXT con formato llega por palabras pero se extrae unido.
- Fuentes sustituidas: la búsqueda no depende de la fuente; la caja sigue la anchura de la fuente con la que se dibujó. Con `GLYPHS_MISSING`, el carácter está en la capa aunque no se vea en el dibujo.

Opcional (medido; hoy no compensa):
- Paso 2, parche a pymupdf: `Shape.finish` copia todo el contenido de la página en cada trazo (coste cuadrático). Ahorraría ~25 s en Plano_completo, pero en un detalle como Perfil_K0010 el PDF cuesta 1,1 s de 18,5 s, y el parche depende de una parte interna de pymupdf. Hacerlo solo si los planos grandes pasan a ser habituales; también se puede avisar del defecto a PyMuPDF.
- Paso 5, evitar relecturas de los DXF temporales: ~10 s en Plano_completo y 0,1 s en Perfil_K0010; el tiempo que queda es sobre todo leer el DXF de origen, que ya ocurre una sola vez.

Pendiente:
- Avisar a ezdxf del fallo de `DXFPolygon.transform` (reproducción arriba). Cuando lo corrijan, quitar `restore_hatch_pattern`; la prueba `test_ezdxf_still_scales_and_rotates_the_pattern_twice` avisará.
- Fuente inexistente: en un DXF sintético con un estilo que apunta a "Fuente Inexistente.ttf", el TXT y la terminal dicen "→ arial.ttf (respaldo automático)", pero ezdxf parece dibujarla con `ARIALUNI.ttf` (Arial Unicode MS: es la `FontFace` que llega a `draw_text`). Sin verificar; revisar si el informe declara la fuente que de verdad se usa.
- HATCH "degradado" de Civil 3D: los rellenos `SOLID` y `SOLID,_O` (19 de los 21 HATCH de Perfil_K0010) salen etiquetados como "degradado" en la tabla de HATCH del TXT, porque `has_gradient_data` de ezdxf es verdadero. Conviene revisar si de verdad son degradados o rellenos sólidos con datos de degradado.
- Heurística de unidades con falsos positivos (INSERT con escala 0.001 y coordenadas georreferenciadas de millones).
- Ruido de ezdxf en la terminal (STYLE inexistente, handles duplicados, DIMASSOC).
- Z mínima sospechosa de −1,5 millones en la caja de Plano_completo (sección C): buscar qué entidad la produce.
- REGION no soportado por ezdxf (aborta el render, p. ej. Cont. 40ft HC (planta)). Sigue "no soportada" con la pista de `--omit-unsupported`. Queda fuera por ahora dibujar su contorno con `ezdxf.acis`, como se hace con los 3DSOLID (`--solids footprint`); `solids.py` ya tiene la infraestructura.
- Aviso "relative point size is not supported" mostrado como nota del renderizador.
- Solicitudes de fuente: `_requests` (font_config.py) toma como nombre de fuente el primer texto del XDATA `ACAD`, que en los overrides de estilo de cota es `DSTYLE` (el nombre del grupo, no una fuente). Se vio al medir con el DXF de render de Plano_completo ("DSTYLE → arial.ttf", 11 textos); no se comprobó si ocurre en el flujo real, revisar.

## Fase 4
Decisiones (2026-10-27):
- El JSON pasa a ser el tercer archivo publicado: PDF + TXT + JSON (manifiesto `<prefijo>.manifest.json`), publicado el último. Se retira `--detail-json`. El manifiesto guarda el nombre del DXF y su hash, no la ruta absoluta.
- Fallos críticos: sin archivos y código 1, como hoy. Los avisos `CRITICAL` (XREF o bloque sin resolver) pasan a bloquear (hoy dan "completa con advertencias"). En el código se distinguen las comprobaciones que bloquean de las que solo avisan (`ADVISORY_CHECKS` en `detail_model.py`).
- Con `--all`: todo o nada (hoy cada referencia se publica al acabar su vuelta).
- Manifiesto con `schema_version` propio e identificador de tipo de documento; `model_version` se conserva como dato de trazabilidad. Incluye el hash SHA-256 del PDF, la relación CAD → página (la matriz que guarda `_PdfBackend.placement_matrix`), las versiones de ezdxf y PyMuPDF, y un campo explícito de escala física garantizada (sí/no) que el componedor pueda comprobar.

Hecho:
- Paso 0: `test_failed_pdf_validation_publishes_nothing` (test_output_contract.py) pasaba por un `TypeError` (su `lambda` tenía dos argumentos y el flujo pasa más desde la Fase 3), no por la validación fallida. Ahora acepta cualquier argumento y comprueba que el error es "La validación del PDF falló"; verificado que la versión antigua falla con las aserciones nuevas.
- Paso 2, escala exacta: ezdxf 1.4.4 (`get_coordinate_output_space`) toma el lado mayor de la página truncado a puntos enteros y dibuja cada unidad como 1 pt, así que el dibujo salía encogido en int(lado) ÷ lado. `_PdfBackend` (exporters.py) usa el lado exacto y `_ExactPageRenderBackend` crea la página con su tamaño exacto (también el fondo blanco, que antes dejaba una franja sin cubrir y desplazaba el dibujo al ampliar la página con `set_mediabox`). Medido:
  - Línea de 10 m a 1:1000 (página de 20 mm): 9,878 → 10,000 mm (de −1,222 % a 0,000 %).
  - Perfil_K0010 a 1:50: −0,0158 mm → −0,000003 mm; el dibujo queda centrado en vertical (márgenes 5,907/5,586 → 5,746/5,743 mm). Comparado a 300 ppp: tras alinear 2 px (0,17 mm) en vertical, solo difiere el 0,080 % de los píxeles (suavizado); mismo texto y mismos 281 trazos.
  - VIA_2MA y Plano_completo a 1:1000: diferencia < 0,0001 mm.
  - El SVG no cambia (hash de referencia igual). Sin escala nominal, la página pasa de 1009,65 × 509,76 a 1010 × 510 mm en el caso de prueba (la prueba se actualizó).
- Paso 2, comprobación "Escala del dibujo" (`pdf_checks.check_drawing_scale`): mide en el PDF el ancho y alto de todos los puntos de los trazos (sin el fondo) y los compara con la caja que grabó ezdxf (mismos puntos, en unidades CAD) por la escala. Tolerancia `DRAWING_SCALE_TOLERANCE_MM` = 0,01 mm por eje sobre todo el dibujo: el ruido medido es < 0,0001 mm y detecta el error antiguo más pequeño (0,016 mm de Perfil). Es una advertencia (`ADVISORY_CHECKS`): si no pasa, "[AVISO]" en el TXT, aviso `DRAWING_SCALE_MISMATCH` en "A tener en cuenta" y en la terminal, y la escala física pasa a "NO GARANTIZADA" con su motivo (`_confirm_physical_scale` en block_commands.py). La función `advisory_warnings` sustituye a `searchable_text_warnings`. Coste: ~1 s en VIA_2MA.
  - Límite: si el punto extremo de todo el dibujo fuera un punto de control de una curva cuadrática (glifos TrueType), la medida saldría algo menor y daría un aviso falso; no ocurre en los tres planos de prueba.

- Paso 4, lista única de avisos (`notices.py`, solo biblioteca estándar): `collect_notices(detail)` genera todos los avisos con código, severidad (`info`, `warning`, `critical`), categoría (`fonts`, `hatch`, `dependencies`, `units`, `text`, `renderer`, `scale` y `entities`, esta última para geometría sin caja, omitida, con datos inválidos o huellas) y consejo. La usan el TXT ("A tener en cuenta") y el manifiesto. Las sustituciones de fuente son el aviso `FONT_SUBSTITUTED` con severidad `info` (el estilo `Standard` usa `txt`, que casi siempre se sustituye; si contara, casi toda extracción saldría "con advertencias"); si a la sustituta le faltan caracteres, `GLYPHS_MISSING` es `warning`. El estado final sale de la misma lista: "con advertencias" si hay algún aviso `warning` o `critical`; el estado no cambia respecto a antes. En el TXT, cada fuente sustituida sale ahora también en "A tener en cuenta". Una prueba exige que todo código de aviso del código fuente tenga categoría.
- Paso 3, manifiesto v1 (`manifest.py`, solo biblioteca estándar; documento `docs/manifiesto.md` para el componedor): `document_type` "cde-detail-manifest", `schema_version` "1.0" y `model_version` como trazabilidad; DXF y PDF por nombre (nunca ruta), SHA-256 y bytes; bloque, handle y ocurrencia; unidades y escala con `physical_scale_guaranteed` (booleano, nunca null) y su motivo; PDF con páginas, tamaño medido, caja de lo dibujado en CAD y en página y la matriz CAD → página real (mm, origen arriba a la izquierda, Y hacia abajo; coordenadas CAD = Model Space de origen); conteos por tipo y por capa, no conservadas con motivo y HATCH; fuentes y dependencias; avisos por categoría con recuentos por severidad; comprobaciones con `blocking`; versiones (exportador, ezdxf, PyMuPDF) y opciones usadas. `validate_manifest` comprueba un esquema propio (todas las claves, ninguna de más) y la coherencia (versión mayor, páginas, SHA-256, nombres sin ruta, recuentos, estado). Se construye y se valida en cada extracción (Perfil: 0,1–0,3 s, incluido el hash del DXF de 58 MB); si no fuera válido no se publica nada. Se publica como archivo desde el paso 6. Ejemplo real de Perfil_K0010 en `data/output/referencia/manifiesto_ejemplo/perfil_k0010.manifest.json` (12,4 KB; 6 avisos: 4 info y 2 warning; escala física garantizada).
- Dos correcciones encontradas al contrastar la matriz con el PDF:
  - Precisión: la matriz se sacaba restando puntos transformados; con coordenadas georreferenciadas (~4,9 millones de m, traslación de ~10⁸ mm) la escala salía 20,0000000096 y un punto se desplazaba 0,047 mm. Ahora `affine_2d` lee los coeficientes: matriz y PDF coinciden en 0,000005 mm.
  - Centrado: ezdxf redondea la página final a 0,1 mm (`Page.width_in_mm` redondea al leerse) y centra el dibujo en esa página; al ajustarla después al tamaño pedido, el dibujo quedaba desplazado (0,02 mm en X y 0,003 mm en Y; era la asimetría 10,441/10,421 mm vista en el paso 2). `_PdfBackend` traslada el dibujo al centro de la página exacta: Perfil queda a 10,4310/10,4310 y 5,7444/5,7444 mm. El SVG no cambia.

- Paso 5, avisos críticos: los avisos `critical` (`notices.BLOCKING_SEVERITIES`: XREF o bloque sin resolver) bloquean: sin archivos, código 1 y un mensaje que dice qué falta y qué hacer (consejo nuevo para XREF —BIND o dejar el archivo junto al DXF— y para bloque ausente —AUDIT—). Se comprueban al construir el detalle, antes de dibujar (VIA_2MA con un XREF forzado se detiene en 19,6 s; la extracción completa tarda 59,6 s), y otra vez antes de publicar. Las comprobaciones están clasificadas de forma explícita en `detail_model.BLOCKING_CHECKS` (PDF legible, una página, tamaño de página, manifiesto coherente) y `ADVISORY_CHECKS` (escala del dibujo, texto buscable); `check_blocks` falla con una sin clasificar.
- Paso 6, publicación:
  - El manifiesto se publica como `<prefijo>.manifest.json`. Antes de publicar se valida contra su esquema, se escribe y se comprueba contra el PDF (`pdf_checks.check_manifest_coherence`: SHA-256, bytes, páginas, tamaño de página y nombre); si no cuadra, no se publica nada.
  - Orden: PDF y TXT (y, en desarrollo, DXF y SVG) de todas las referencias y, al final, los manifiestos. Un PDF sin su manifiesto es un paquete incompleto: `manifest.verify_package` y el subcomando nuevo `cad verify-package <PDF o manifiesto>` lo reconocen (código 1 y la lista de problemas); también detectan un PDF reemplazado sin su manifiesto (el hash no coincide).
  - `--all`, todo o nada: se comprueban los destinos de todas las referencias antes de empezar, cada una se prepara en su carpeta de trabajo (que dura hasta el final de la orden) y se publica todo junto. Si falla una, no se publica ninguna.
  - `--detail-json` retirado (solo existía en `extract-block`; `cad extract` no lo tenía). El TXT nombra su manifiesto en la sección G.
- Paso 7, pruebas: `tests/test_package.py` (8: XREF y bloque ausente bloquean sin dibujar, clasificación de comprobaciones, manifiestos los últimos, `--all` todo o nada, manifiesto incoherente, paquete incompleto o con otro PDF, manifiesto publicado); listas de archivos publicados actualizadas en 8 archivos de pruebas; las pruebas que leían `--detail-json` usan el fixture `model_details` de `tests/conftest.py` (el modelo del detalle tomado del flujo).
- Probado con datos reales: VIA_2MA (`--units m --solids footprint --scale 1:1000`): 3 archivos, manifiesto de 20,6 KB válido y coherente, escala física garantizada, 17 189 entidades, 6 avisos (4 info y 2 warning, ninguno crítico). Perfil_K0010 (1:50): 3 archivos, manifiesto de 12,1 KB válido y coherente, 6 avisos (4 info y 2 warning). Con el XREF forzado sobre una copia de VIA_2MA y `--overwrite`, el paquete anterior queda intacto (mismos hashes).

Pendiente:
- Nota: el PDF no es determinista (el `/ID` del trailer es aleatorio): el hash identifica el archivo publicado, pero la regresión gráfica sigue siendo con el SVG.
- Condensar advertencias de handles duplicados.

## Ajuste Fase 4: planos de PVcase (2026-10-29)
Caso: bloque SUP_TOPO_PANELES de `data/input/Prueba_final/Prueba_Final_Exportador_V0.dxf` (`--units m --scale 1:3500 --solids footprint --omit-unsupported`). Los paneles (21 384 3DSOLID, 81 distintos) no salían en el PDF y 2 MULTILEADER se omitían por datos inválidos.

Diagnóstico:
- Los 81 sólidos son SAB `ASM BinaryFile4` (ACIS 22300, «ASM 231.6.4»), cajas de 6 caras planas, 12 aristas rectas y 8 vértices. Fallo de ezdxf 1.4.4: `SabDataLoader.read_transform` (ezdxf/acis/sab.py) espera el registro `transform` como texto (así lo escribe ezdxf), pero AutoCAD lo guarda como 4 vectores binarios (3 filas del giro y la traslación), la escala y tres indicadores (giro, reflejo, deformación). Los 162 sólidos de Plano_completo no tienen registro `transform` y por eso funcionaban.
- MULTILEADER, fallo de ezdxf 1.4.4: `xref.Loader` traduce dos veces los handles de las entidades de un bloque copiado (desde `BlockRecord.map_resources` y desde `_Transfer.map_entity_resources`). El MULTILEADER traduce su contexto, flechas y atributos sobre la propia copia, así que la segunda vez toma un handle ya traducido como si fuera del origen. En este plano, el estilo del texto acababa apuntando a un 3DSOLID (`DXFAttributeError: Invalid DXF attribute "name" for entity 3DSOLID`); si cae en otro STYLE, el texto sale con otra fuente sin ningún error. En el DXF de origen están bien.

Hecho:
- `acis_reader.py` (nuevo): `load_bodies(solid)` lee el ACIS con subclases de `SabLoader` y `SabDataLoader` (internas de ezdxf, solo en esta lectura; no se cambia ezdxf para todo el programa) que aceptan el transform binario, en el mismo orden que ezdxf lee el SAT (filas y luego traslación, vector fila como `Matrix44`). Con escala ≠ 1 o deformación no se convierte (`UnsupportedTransform`, con su motivo), igual que ezdxf ignora esos casos en el SAT.
- `solids.py`: `FootprintResult.own_transform` (handles convertidos con el lector propio). Aviso nuevo `SOLIDS_ACIS_OWN_READER` (info, categoría `entities`, consejo «Compare una muestra de huellas con el CAD»), en el TXT, el manifiesto y la línea «Huellas» de la terminal; solo sale si se dibujó algún sólido. Para ello `detail_model.Severity` admite `INFO` (`notices` lo traduce tal cual y el TXT lo rotula «info»).
- `block_extractor._multileader_mapping_once`: durante `loader.execute()` cada MULTILEADER copiado se traduce una sola vez; el método de ezdxf se restaura al terminar.
- Pruebas (10, datos sintéticos): `tests/test_acis_transform.py` (giro de 30° comparado con la fórmula del giro, porque una caja centrada girada ±90° ocupa lo mismo y no delata la traspuesta; 90°; transform en texto sin lector propio; escala y deformación rechazadas; flujo completo con TXT, manifiesto y copia de render; vigilancia de que ezdxf sigue sin leerlo y de que existen sus clases internas) y dos en `tests/test_extraction_resources.py` (estilo del MULTILEADER tras la extracción y vigilancia de la doble traducción).
- Orientación comprobada con el DXF real: los 81 giros son inclinaciones alrededor del eje X. El ángulo del lado largo frente a los paneles vecinos no distingue la matriz de su traspuesta (81 de 81 a 0°/90° con las dos), pero la definición trae una POLYLINE 3D cerrada (el contorno de la mesa): la caja de las 81 huellas coincide con ella en tres bordes y difiere 0,0057 unidades en el cuarto; con la traspuesta, un borde se desvía 0,2993.
- Hash de regresión de Perfil_K0010 sin cambios (`A2560C11…3FF9`; ese bloque no tiene MULTILEADER ni 3DSOLID).
- Resultado real (`data/output/Prueba_final/verificacion_2026-10-29/`): 21 384 3DSOLID transformados (antes omitidos), 81 con el lector propio, error máximo de huella 0,001 mm en el papel; los 2 MULTILEADER se dibujan; desaparecen `SOLIDS_NOT_CONVERTED`, `ENTITIES_INVALID_DATA` (×2) y `FIDELITY_RECONCILIATION` (×2); quedan `ENTITIES_WITHOUT_BOUNDS`, `SOLIDS_AS_FOOTPRINT`, `UNSUPPORTED_OMITTED` (20 REGION) y el nuevo `SOLIDS_ACIS_OWN_READER`. Escala del dibujo [OK], texto buscable 406/406, `cad verify-package` correcto. Medido con el código del último commit frente al nuevo (mismas opciones, una ejecución tras otra): tiempo 330 s → 477–515 s (dibujar las 21 384 huellas en el PDF; ver el coste de `Shape.finish` en «Opcional»), pico de memoria 1,05 → 0,93 GiB, PDF 3 940 264 → 4 334 492 bytes (+10 %).

Pendiente:
- Avisar a ezdxf de los dos fallos (texto de los issues redactado el 2026-10-29, sin enviar). Cuando los corrijan, retirar `acis_reader` y `_multileader_mapping_once`; `test_ezdxf_still_cannot_read_the_binary_transform` y `test_ezdxf_still_maps_multileader_resources_twice` avisarán.
- Revisión visual de una mesa del PDF sobre el plano en AutoCAD.

## Ajuste: PDF sin fondo de página (2026-11-05)
Caso: el componedor (proyecto aparte, plancha A1) superpone varios PDF de detalle. Cada PDF traía un rectángulo blanco relleno del tamaño de la página que tapaba lo que quedaba debajo (borde del área de dibujo, rosa de los vientos, otros detalles).

Diagnóstico:
- El fondo lo pintaba ezdxf: `PyMuPdfRenderBackend.set_background` (ezdxf/addons/drawing/pymupdf.py, líneas 250-257) dibuja un rectángulo del tamaño de la página con el color de fondo. Quiere saltarlo cuando es blanco, pero compara el texto `"#ffffff"` con la tupla `(1.0, 1.0, 1.0)` y lo pinta siempre (fallo de ezdxf 1.4.4).
- El color 7 y el WIPEOUT no dependen de ese rectángulo, sino del color de fondo lógico del frontend (`LayoutProperties.set_colors`): con fondo blanco, el color 7 sale negro y el WIPEOUT se rellena de blanco.

Hecho:
- `_ExactPageRenderBackend.set_background` (exporters.py) no pinta nada. Se mantienen `BackgroundPolicy.WHITE` y `set_colors("#ffffff")`: los colores no cambian y los rellenos blancos del dibujo (HATCH sólido blanco, WIPEOUT) se conservan. Se descartó `BackgroundPolicy.OFF`: deja el fondo lógico transparente, así que los WIPEOUT tampoco taparían y cambiaría el SVG.
- Solo afecta al PDF: el SVG no cambia (hash de regresión `A2560C11…3FF9` igual). Manifiesto (esquema 1.0), TXT, tamaño de página, margen, matriz CAD → página y texto buscable sin cambios.
- `pdf_checks._drawn_extent_pt` sigue saltando un relleno del tamaño de la página, para medir bien los PDF anteriores.
- Pruebas: `tests/test_pdf_background.py` (3, DXF sintético: sin relleno del tamaño de la página; HATCH sólido blanco y WIPEOUT conservados; color 7 y negro siguen negros) y `test_small_detail_is_drawn_at_its_exact_scale` ahora exige que no haya ningún relleno (antes exigía el fondo).
- Probado con Perfil_K0010 (1:50, `data/output/sin_fondo_2026-11-05/`): 281 → 280 trazos (solo falta el fondo); trazos y rellenos por color iguales; render a 200 ppp sobre blanco con 0 píxeles distintos; comprobaciones en [OK] (escala 1:50,0000, texto 120/120); `cad verify-package` correcto; el manifiesto solo cambia en fecha, `sha256` y `bytes` del PDF.

Pendiente:
- El paquete de `data/output/referencia/perfil_k0010/` sigue siendo el anterior (con fondo); regenerarlo si se quiere que la referencia sea la nueva.
- Avisar a ezdxf del fallo de `set_background` (comparación de texto con tupla).

## Ajuste: `--help` y exportación de proyecto (2026-11-11)
Hecho:
- `cde-exporter cad extract-block --help` (y `cad extract --help`) terminaba con `UnicodeEncodeError`: la ayuda de `--font-substitutions` lleva «→» y la consola de Windows (cp1252) no lo puede escribir. `main` pasaba la consola a UTF-8 después de `parse_args`, pero `--help` imprime y termina dentro de `parse_args`; ahora se hace antes. Prueba: `tests/test_cli_help.py` (consola cp1252 simulada).
- Orden nueva `cad extract-all-blocks <dxf> --output-dir <carpeta>` (`project_commands.py`; uso en el README): un paquete por cada referencia de bloque del Model Space, con el mismo flujo de `extract-block` (`block_commands.run_extract_block`, que ahora acepta el DXF ya leído: se lee una sola vez). Decisiones de Luis Angel:
  - Escala: `--scale` por defecto y `--scale-map` (JSON `{"bloque": "1:N"}`, admite BOM; los nombres se comparan sin mayúsculas). `--list` muestra cada paquete con su escala y su tamaño en mm sin crear nada; `--write-scale-map <json>` escribe la plantilla con todos los bloques exportables. Sin escala para algún bloque, o con un nombre del mapa que no existe, se detiene antes de crear nada.
  - Varias referencias: `<bloque>_001`, `<bloque>_002`… (número = ocurrencia; se exporta cada una con `--occurrence`).
  - Si falla una referencia, las demás se publican y la orden termina con código 1 (distinto de `--all`, que sigue todo o nada). Los omitidos no cuentan como fallo.
  - Resumen en la terminal y en `resumen_proyecto.json` (`document_type` "cde_exporter.project_summary", `schema_version` "1"; claves en inglés como el manifiesto; nombres de archivo, nunca rutas).
- Omitidos con motivo: anónimos (`*…`), sin definición, XREF y sin nada visible. Lo visible se decide con las reglas del dibujo: `fidelity.iter_detail_entities_with_visibility` acepta ahora `roots` (una sola referencia). Antes, un bloque vacío o apagado fallaba con «empty bounding box».
- Nombres de archivo: `file_names.py` (solo biblioteca estándar): caracteres prohibidos en Windows y el punto → `_` (el punto se tomaría como extensión), espacios unidos, nombres reservados con `_`, máximo 80 caracteres, choques sin mayúsculas → `_2`, `_3`…
- Pruebas: `tests/test_project_commands.py` (27) y `tests/test_cli_help.py` (2): 457 pruebas pasando, ruff limpio. Hash de regresión de Perfil_K0010 sin cambios (`A2560C11…3FF9`).
- Probado con `data/input/Prueba_final/modulacion_bloques_v11LAdxf.dxf` a 1:50: 12 de 12 paquetes en 19 s, `cad verify-package` correcto en los 12; mismo contenido de PDF que con `extract-block`.
- Probado con `prueba_erco_perfil.dxf` (1:50, opciones de la regresión): 112 paquetes publicados, 648 bloques anónimos omitidos y 268 fallos esperados (264 con 3DSOLID sin `--solids footprint` y 4 con REGION sin `--omit-unsupported`); el SVG de Perfil_K0010 sale con el hash de regresión. En la terminal, el motivo de un fallo se recorta a 300 caracteres (ezdxf lista cada entidad); el JSON lo guarda completo.

Pendiente:
- Bloques de símbolo muy repetidos: Plano_completo daría 225 paquetes (209 de «PC - PT») y `prueba_erco_perfil.dxf` 380 (264 de un bloque de panel PVcase). Valorar una opción para exportar solo la primera referencia o excluir bloques por nombre.
- Bloques dinámicos: una referencia a un bloque dinámico con parámetros cambiados apunta a un bloque anónimo `*U…` y hoy se omite. Valorar resolver su nombre real (en Perfil hay 648 anónimos; no se ha comprobado cuántos son bloques dinámicos).
- Resumen de la terminal largo con muchos omitidos (648 líneas en Perfil): valorar agruparlos por motivo.

## Ajuste: capas ocultas y Model Space (2026-11-11)
Caso: en `PRUEBAEXPORTADORSUPERFICIES.dxf` (830 MB) las curvas de nivel son 8 386 POLYLINE 3D (147 606 vértices) sueltas en el Model Space, en la capa congelada «LaEstación1_Contours». AutoCAD no deja seleccionar lo congelado, así que no entraron en BLOQUE1 al crearlo, y el exportador no avisaba de nada. Plan aprobado por Luis Angel, en este orden y en la rama `trabajo/capas-ocultas-model-space`: (1) avisos y `--include-hidden-layers`; (2) `cad extract-modelspace` sin recorte; (3) copia temporal sin bloques sin usar para DXF muy grandes; (4) prueba con las curvas reales; (5) más adelante, recorte y `--include-modelspace`.

Hecho (punto 1):
- `hidden_layers.py` (nuevo): estado de capa («apagada», «congelada», «apagada y congelada»); `hidden_layer_counts` (entidades del detalle ocultas por su capa, con las reglas del dibujo: en la capa «0» de un bloque cuenta la del INSERT); `loose_modelspace_entities` (lo que no es INSERT en el Model Space de origen, con la caja solo de lo oculto; una vez por documento, en caché); `hidden_overlap` (lo suelto oculto cuya caja toca la del detalle); `reveal_layers` y `parse_layer_selection` para la opción. `fidelity.EntityVisibility.hidden_layer` dice qué capa oculta cada entidad.
- Avisos: `HIDDEN_LAYERS_CONTENT` (info), `HIDDEN_LAYERS_INCLUDED` (info) y `MODELSPACE_HIDDEN_OVERLAP` (warning), uno por capa, categoría `entities`. Salen en la terminal, en «A tener en cuenta» del TXT y en el manifiesto. `ExtractedDetail` guarda `hidden_layers`, `included_hidden_layers` y `modelspace_hidden_overlap` (modelo v2, campos nuevos con valor por defecto).
- `--include-hidden-layers [CAPAS]` en `extract-block` y `extract-all-blocks`: sin lista, todas; con lista, solo esas (una capa inexistente detiene la orden sin crear nada). Enciende y descongela las capas en la copia de trabajo (que se guarda de nuevo); el DXF de origen no cambia (comprobado por hash en la prueba). No incluye lo marcado como invisible ni lo suelto del Model Space.
- Manifiesto: esquema `1.1` (solo añade): `options.include_hidden_layers` (null, `["*"]` o la lista) y los avisos nuevos; `docs/manifiesto.md` actualizado.
- `resumen_proyecto.json`: por paquete `hidden_layers`, `included_hidden_layers` y `modelspace_hidden_overlap`; `modelspace_loose` (lo suelto del Model Space por capa y estado) y tres totales nuevos. La terminal agrupa las capas ocultas de todos los paquetes y lista lo suelto (también con `--list`).
- Fallo encontrado con Perfil: una GEOMAPIMAGE suelta (ezdxf no la interpreta y no tiene capa) detenía el recorrido; se salta. Coste del recorrido en Perfil: 0,5 s para 13 179 entidades sueltas.
- Pruebas: `tests/test_hidden_layers.py` (9). 466 pruebas pasando, ruff limpio. Hash de regresión de Perfil_K0010 sin cambios (`A2560C11…3FF9`); Perfil no tiene capas ocultas (mismos 6 avisos).

Compatibilidad del esquema 1.1 (comprobada el 2026-11-12, antes del commit del punto 1):
- Componedor (`Documents\Componedor`, `componedor.manifiesto.leer_manifiesto`): acepta cualquier versión mayor 1 e ignora los campos que no conoce. Con su propio entorno y sin tocar ese proyecto, `verificar_paquete` (integridad y coherencia a–i) pasa en Perfil 1.0 de referencia, Perfil 1.1 (con y sin `--include-hidden-layers`), BLOQUE1 1.1 y un paquete de `extract-modelspace`.
- Fallo encontrado y corregido: nuestro `validate_manifest` exige todas las claves, así que `cad verify-package` rechazaba los paquetes 1.0 ya publicados («options.include_hidden_layers: falta la clave»). Ahora `manifest._ADDED_IN` dice en qué versión menor apareció cada clave y un manifiesto anterior es válido sin ella (prueba en `tests/test_manifest.py`). Los paquetes de `data/output/referencia/` (Perfil, VIA_MA, Plano_completo) vuelven a pasar.

Hecho (punto 2, `cad extract-modelspace`, sin recorte):
- `modelspace_commands.py` (nuevo): selección por capas (`--layers`, `--exclude-layers`, comodines, sin mayúsculas; un INSERT por su capa) y por zona (`--window` o `--window-from-block` con `--occurrence`), con entidades enteras. Lo elegido se copia (xref.Loader, como `extract-block`) a un DXF nuevo en memoria, se agrupa en el bloque de trabajo «Model_Space» (con `_2`… si existe) insertado en (0, 0, 0) y se exporta con `block_commands.run_extract_block`: mismo paquete, comprobaciones y publicación. `--list` muestra entidades por capa, vértices, caja, página y si cabe en A1.
- Capas ocultas: lo oculto entra en la selección y no se dibuja (`HIDDEN_LAYERS_CONTENT`); si `--layers` solo pide capas ocultas, se detiene y pide `--include-hidden-layers` (decisión de Luis Angel). Si ninguna capa coincide, también se detiene.
- Avisos nuevos: `MODELSPACE_NOT_COPIED` (warning), `MODELSPACE_ZERO_LENGTH` (info; longitud XY < 1e-9 unidades), `MODELSPACE_WITHOUT_BOUNDS` (warning, solo con zona). Modelo: `ModelSpaceSelection` en `ExtractedDetail.modelspace_selection`; el TXT (sección B) y el manifiesto (`selection`, 1.1) lo describen.
- `cad extract` en desuso (paso 1 de la unificación, aprobado por Luis Angel): hace lo mismo que antes, pero avisa por stderr y en su ayuda, y remite a `extract-modelspace --window`. Diferencias: solo zona; se detiene si hay entidades sin caja en todo el dibujo (salvo `--allow-unbounded`); solo `--scale-factor`; publica `.dxf`, `.svg`/`.pdf`, `_fidelidad.txt` y `_omitidas.txt`, sin TXT A–G, manifiesto ni comprobaciones, y no de forma atómica. Paso 2, pendiente: convertirla en alias de `extract-modelspace` cuando se confirme que nadie usa esos archivos (habría que añadir `--scale-factor` a la orden nueva, rechazar `--format svg|pdf` y reescribir las 5 pruebas de la línea de órdenes de `tests/test_cad_dxf.py`).
- Pruebas: `tests/test_modelspace_commands.py` (9), `tests/test_manifest.py` (+1, manifiestos 1.0) y `tests/test_cad_dxf.py` (+1, aviso de desuso): 477 pruebas pasando, ruff limpio, hash de Perfil sin cambios. Probado con `prueba_erco_perfil.dxf --window-from-block Perfil_K0010` a 1:50: 40 s, 1 entidad (el INSERT) y 22 sin caja avisadas; página 416,980 × 119,403 mm, como la del bloque.

Commits en la rama `trabajo/capas-ocultas-model-space` (2026-11-12): `87dcc89` (A: `--help` y `extract-all-blocks`, 457 pruebas, comprobado solo en un worktree temporal) y `ad1c1c9` (B: capas ocultas, `extract-modelspace` y desuso de `cad extract`, 477 pruebas).

Hecho (punto 3, DXF muy grandes):
- `large_dxf.py` (nuevo, solo biblioteca estándar): lee el DXF por pares código/valor, línea a línea. Primera pasada: qué bloques no usa nada (cuenta como uso cualquier nombre en los códigos 1, 2, 3 o 1000 y cualquier handle de su BLOCK_RECORD en punteros 331–369, 390–399, 480, 481 o 1005, desde la cabecera, las tablas, las entidades, los objetos u otro bloque usado; el 330 no cuenta porque es el dueño; `*Model_Space`, `*Paper_Space…` y las XREF siempre se conservan). Segunda pasada: copia byte a byte dejando vacíos esos bloques (cabecera BLOCK, ENDBLK y BLOCK_RECORD se conservan). Un DXF binario no se toca.
- `block_commands.read_source_dxf`: desde 200 MB (`CDE_LARGE_DXF_MB`; 0 = siempre) lee la copia, hecha en una carpeta temporal de la carpeta de salida y borrada tras leerla (si la carpeta de salida no existía y queda vacía, como con `--list`, se borra). La usan `extract-block`, `extract-all-blocks` (una vez para todo el proyecto; `resumen_proyecto.json` → `source_copy`) y `extract-modelspace`. Aviso `SOURCE_UNUSED_BLOCKS_SKIPPED` (info) en terminal, TXT y manifiesto; el manifiesto guarda el hash del original.
- Con el DXF real `PRUEBAEXPORTADORSUPERFICIES.dxf` (830,1 MB): copia de 106,7 MB con 366 bloques vaciados (el mayor, `A$Cb846a617`, 714,4 MB) en 48–86 s según la caché del disco, con un pico de 82 MB de memoria en uso (300 MB reservada). La copia se carga con ezdxf en 78 s (pico 498 MB en uso, 753 MB reservada). `extract-modelspace --list --layers LaEstación1_Contours --include-hidden-layers --window-from-block BLOQUE1 --scale 1:2000` de punta a punta: 136–246 s y pico de 585–615 MB en uso (antes ezdxf no podía abrir el DXF con 7,7 GB de RAM); 2 682 curvas y 86 202 vértices, ninguna sin caja. Como entran enteras, su caja (1 741 × 2 405 m) es casi todo el terreno: a 1:2000 la página sería de 880 × 1 213 mm y no cabe en A1. Para encajarlas en la zona de BLOQUE1 hace falta el recorte (punto 5).
- Encontrado con el DXF real: 22 polilíneas de 2–3 mm (coordenadas de millones de metros) a las que ezdxf no calcula caja; `extract-modelspace` usa entonces la caja de sus vértices (`_points_bounds`).
- Pruebas: `tests/test_large_dxf.py` (5: qué bloques se vacían, la copia es el original sin unas líneas y pasa la auditoría, umbral y DXF binario, `extract-block` con la copia da el mismo dibujo y el hash del original, proyecto y Model Space) y una en `tests/test_modelspace_commands.py`. 483 pruebas pasando, ruff limpio, hash de Perfil sin cambios.

Commit `fb68fd4` (C: copia por partes, 483 pruebas).

Hecho (recorte a la zona, sin commit; antes era el punto 5 y Luis Angel lo adelantó porque sin recorte la página de las curvas cubría todo el terreno):
- `clipping.py` (nuevo): Liang–Barsky en XY por segmento; Z y ancho (constante o por vértice) interpolados en el corte; un tramo por cada paso dentro de la ventana; las cerradas quedan en tramos abiertos y, si el corte cae en el comienzo del anillo, el último tramo se une al primero. Se recortan LINE, POLYLINE 3D y POLYLINE 2D/LWPOLYLINE de tramos rectos y planta XY; lo demás (arcos, círculos, splines, texto, HATCH, INSERT, mallas, polilíneas con bulge o ajustadas) entra entero. Lo entero dentro no se toca.
- `extract-modelspace`: con zona recorta por defecto (`--no-clip` para no recortar). La página es la zona: `exporters.export_vector_outputs(render_box=…)` fija el rectángulo que se coloca en la página y el flujo usa la zona como caja efectiva (`page_window`); si algo que no se recorta cruza el borde, la página se amplía con su caja. Lo dibujado (`drawn_size`, `drawn_bounds`, `cad_bounds` del manifiesto) se sigue midiendo en la geometría real, así que «Escala del dibujo» y la coherencia del componedor siguen valiendo.
- Avisos `MODELSPACE_CLIPPED` (info) y `MODELSPACE_NOT_CLIPPED` (warning); `--list` da la página con recorte. Manifiesto 1.2: `selection.clip` (`_ADDED_IN`: los 1.1 sin esa clave siguen siendo válidos). TXT: línea «Recorte a la zona».
- Pruebas: `tests/test_clipping.py` (15: segmentos, zigzag que entra y sale tres veces, anillo cerrado cortado en su comienzo, cerrado dentro, Z y ancho interpolados, LINE y LWPOLYLINE con ancho constante y propiedades, bulge y círculo enteros, curva 3D de 2 001 vértices en 7 tramos, POLYLINE 2D cerrada con ancho por vértice, superposición exacta con el PDF de `extract-block`, arco que cruza y amplía la página) y las de `test_modelspace_commands.py` actualizadas (`--no-clip` y recorte). 499 pruebas pasando, ruff limpio, hash de Perfil sin cambios.
- Prueba real (DXF original de 830 MB, `--layers LaEstación1_Contours --include-hidden-layers --window-from-block BLOQUE1`, carpeta `data/output/curvas_recorte_2026-11-12/`):
  - Curvas a 1:2000: página 405,840 × 496,072 mm (la zona de BLOQUE1), 2 682 curvas (28 recortadas en 62 tramos, 1 quitada, 0 enteras), 2 706 trazados, 39 216 segmentos, PDF de 337 KB, pico de 688 MB en uso; todas las comprobaciones en [OK]. Tardó 6 200 s, pero con el equipo sin memoria (189 MB libres): la misma exportación a 1:5000 tardó 167 s.
  - Curvas a 1:5000: página 168,336 × 204,429 mm (cabe en A1), mismos trazos, PDF de 341 KB, 167 s, pico de 703 MB.
  - BLOQUE1 a 1:2000 (`--solids footprint --omit-unsupported`): página 405,840 × 496,072 mm, 69 304 trazados, PDF de 1,76 MB, 812 s, pico de 814 MB.
  - Superposición: página de las curvas y de BLOQUE1 a 1:2000 idéntica (diferencia 0,000 mm) y matriz CAD → página igual salvo 0,0007 pt (0,0003 mm) en la traslación X.
  - El aviso «el bloque contiene entidades sin caja» de las curvas lo dan las polilíneas de 2–3 mm que ezdxf no mide; no afecta al dibujo.

Límites:
- Lo suelto que se cuenta es todo lo que no es un INSERT.
- (Resuelto el 2026-11-23, sección «Zona del Model Space como detalle»: arcos, círculos, elipses y HATCH se recortan; lo demás lo corta la máscara del PDF y la página ya no se amplía.)
- Un tipo de línea discontinuo vuelve a empezar su patrón en cada tramo recortado. Un INSERT suelto en una capa oculta (cuyo contenido en la capa «0» no se vería) no se cuenta.
- La zona del detalle es su caja: algo oculto puede «tocar» la caja sin estar encima del dibujo.

## Ajuste: rendimiento con bloques repetidos (2026-11-13)
Caso: `data/input/GUAMO_prueba.dxf` (Civil 3D con PROXYGRAPHICS=1, sin EXPORTTOAUTOCAD; sin objetos AECC ni proxies: todo son entidades normales). BLOQUE1 tiene 156 749 entidades (44 128 INSERT de módulo con su LWPOLYLINE y su HATCH sólido); `extract-block` a 1:2000 tardaba 2 760 s.

Hecho:
- Reutilización en `exporters._StrictFrontend.draw_composite_entity`: el primer INSERT de un bloque se dibuja entero; sus registros (los `records` del backend grabador de ezdxf) se guardan llevados a coordenadas del bloque con la inversa de su matriz, y cada INSERT idéntico copia esos registros y los coloca con su matriz (`DataRecord.transform_inplace`), sin volver a pasar por el frontend ni la cadena de dibujo. Clave: bloque, propiedades resueltas del INSERT (color, pen, tipo de línea y su patrón y escala, grosor, capa, visibilidad) y escala con signo. Se descarta con atributos, MINSERT, XCLIP activo, extrusión fuera de planta, recorte activo en la cadena, o si el bloque (en cualquier nivel) tiene texto, cotas, MULTILEADER, tablas, imágenes, WIPEOUT, puntos, sólidos, proxies o HATCH con patrón; y si el primer dibujo deja algo más que trazos (omisión, aviso, texto, HATCH degradado). Lo invisible se suma en cada copia para que la conciliación cuadre. `CDE_INSTANCING=0` lo desactiva.
- Commit `4ad198e`. Al principio no era un Form XObject: se ahorraba el trabajo de ezdxf por cada INSERT, pero el PDF seguía llevando cada trazo. La medición posterior (473 s) mostró que el 82 % del tiempo era escribir los trazos con PyMuPDF (unos 310 s de los módulos), así que se hizo el Form XObject (ver abajo).
- Unidades: con `$INSUNITS` en metros ya no se avisa de «posibles milímetros» por medir 1000 unidades o más (GUAMO mide 957 × 1350 m); la prueba que lo exigía ahora exige lo contrario.
- Medido con GUAMO, BLOQUE1 a 1:2000 (`data/output/guamo_prueba_2026-11-13/`; antes en `antes/`): 2 760 s → 849 s (×3,2); pico de memoria en uso 531 → 583 MB (las grabaciones guardadas). PDF: misma página (488,59 × 685,01 mm), mismos 111 215 trazados con los mismos colores, 0 píxeles distintos a 150 ppp; solo 4 trazados verdes agrupan un tramo de otra forma (13+1 frente a 14 tramos, misma caja). El manifiesto pasa de 3 a 1 advertencia (sin UNITS_SUSPECT ni PHYSICAL_SCALE_NOT_GUARANTEED) y la escala física queda garantizada. Esquema del manifiesto sin cambios.
- Pruebas: `tests/test_instancing.py` (5: módulos con PorBloque, PorCapa, giro, simetría y escala; anidados con capa apagada; texto, atributo y patrón no se reutilizan; en todos, el mismo PDF con y sin reutilización) y la de unidades cambiada en `tests/test_cad_blocks.py`. 504 pruebas, ruff limpio, hash de Perfil sin cambios.

### Form XObject (2026-11-13)
- En el PDF, la reutilización deja una marca (`InstanceRecord`) en la grabación en vez de copiar los trazos. Al escribir el PDF (`_replay_with_instances`, como `Player.replay` de ezdxf), la primera marca de cada bloque dibuja sus trazos en la forma de contenido de la página, aparta ese trozo como contenido de un Form XObject (BBox amplio, recursos = los de la página al terminar: capas como contenido opcional) y cada marca escribe en su sitio del orden `q <matriz> cm /FmCdeN Do Q`. La matriz lleva del primer dibujo al de esa marca en espacio PDF (`pctm · relativa · ipctm`); solo gira, desplaza o hace simetría (la escala está en la clave). Los bloques anidados reutilizados van como XObject dentro del XObject. Los números van en decimal fijo (PDF no admite «1e-13»). Los recursos de la página pueden ser un objeto indirecto: `_add_page_xobject` sigue la referencia.
- El SVG copia los trazos (no sabe de marcas): el hash de Perfil no cambia. `CDE_XOBJECT=0` quita solo el XObject; `CDE_INSTANCING=0`, toda la reutilización.
- Medido (`data/output/guamo_xobject_2026-11-13/`): recorte de 2 016 módulos (`data/input/estudio_xobject/GUAMO_recorte_2000_modulos.dxf`) 69,3 s → 46,7 s (−33 %), PDF 2,07 → 1,89 MB; BLOQUE1 completo 439,6 s → 93,6 s (−79 %), PDF 2,99 → 1,93 MB, pico de memoria 638 → 603 MB, 162 XObject. Mismos 111 215 trazados, 461 204 segmentos y colores (get_drawings entra en los XObject); cada trazado en el mismo sitio a menos de 0,00037 pt (0,00013 mm); 3 410 píxeles distintos de 11,7 millones a 150 ppp (suavizado; máximo 20/255 en un canal), por el redondeo de PyMuPDF a 6 cifras significativas (cada trazo en su sitio frente a una vez en el XObject). Perfil: 0 píxeles distintos.
- Comprobaciones: «Escala del dibujo» 1:2000,0000 en X e Y y «Texto buscable» 1883/1883 en [OK]; `cad verify-package` correcto. Componedor: integridad y coherencia (a–i) correctas, el editor acepta el paquete y `colocar` en A1 (recorte a 1:3000) da la misma plancha con y sin XObject (27 527 trazados y colores iguales, 94 píxeles de suavizado distintos de 17,4 millones).
- Pruebas: `tests/test_instancing.py` compara con tolerancia de 0,01 pt por trazado (antes, igualdad a 0,01 pt redondeado, que falla en el borde del redondeo) y suma 2 (copia de trazos con `CDE_XOBJECT=0`; paquete con XObject: comprobaciones y `verify-package`). 506 pruebas, ruff limpio, hash de Perfil sin cambios.

## Trabajador persistente (en curso)
Contrato: `docs/protocolo_trabajador.md` (versión 1.0, con las 12 observaciones del Componedor). Se implementa por etapas, cada una con pruebas y con el visto bueno de Luis Angel: (1) arranque, `load` y `--list --json`; (2) `preview` con caché; (3) `export`, avance y cancelar; (4) un DXF pequeño de prueba para el Componedor.

Etapa 1 (hecha, con visto bueno de Luis Angel, 2026-11-17):
- `cde-exporter worker [--work-dir CARPETA] [--preview-cache-mb 500]`: mensaje `ready` con las versiones; `load`, `status`, `cancel` y `close`. `preview` y `export` responden `UNKNOWN_COMMAND` («todavía no está implementada»).
- `cde-exporter cad extract-all-blocks <dxf> --output-dir <carpeta> --list --json [--scale 1:N | --scale-map JSON] [--no-layers]`: el mismo inventario por stdout, sin crear la carpeta de salida.
- `file_names.package_name`: nombres de paquete ASCII de 60 caracteres como mucho (con 6 cifras del SHA-1 si se corta); `large_dxf.unused_blocks` calcula el SHA-256 del original en la misma pasada.
- Medido con GUAMO: `load` en 8,5 s (unos 4 de arranque); `--list --json` en 6,5 s frente a 112 s del `--list` anterior.
- Pruebas: `tests/test_worker.py` (26: protocolo, BUSY, status y cancelar durante un `load`, error inesperado sin caerse, proceso real con stdout solo JSON y limpieza de temporales) y `tests/test_worker_inventory.py` (22: cajas de INSERT movido, girado y escalado frente a la caja exacta, giro oblicuo, capas ocultas, huella de 3DSOLID, anidados y atributos, omitidos, capas, unidades, hash y huella, copia por partes, `--list --json`).

Etapa 2 (hecha, con visto bueno de Luis Angel, 2026-11-17):
- `preview` (`preview.py`): dibuja solo la referencia con `_StrictFrontend` en modo ligero (texto como rectángulos, rayados sólidos, sin tipos de línea, sin 3DSOLID; con la reutilización y las marcas de bloques repetidos) sobre un `Recorder` y la pinta con Pillow en un PNG RGBA transparente. La caja se calcula con numpy sobre los puntos ya aplanados (la de ezdxf, `player.bbox()`, tardaba 21 s en GUAMO por los 44 000 bloques reutilizados). Rellenos con huecos por par-impar. Se pinta en el orden de la grabación (un primer intento pintaba los bloques reutilizados encima de todo).
- Caché `previews/<huella>/<handle>_<px>_<capas>.png` + `.json`; al pasar de `--preview-cache-mb` se borran las usadas hace más tiempo.
- `exporters.DrawingCancelled`: `_StrictFrontend.draw_entity` la deja pasar (antes, cualquier error dentro de un INSERT se tomaba por datos inválidos de la entidad). El hash de Perfil no cambia.
- El trabajador acepta una BOM de UTF-8 al principio de un pedido (la pone PowerShell en una tubería).
- Medido con GUAMO (BLOQUE1, 1 500 px): 12,6 s la primera vez, 0,01 s desde la caché; misma caja que `load`.
- Pruebas: `tests/test_worker_preview.py` (20: INSERT en (1000, 2000) a 30° y escala 2 con puntos conocidos por `cad_to_px`, color 7 en negro, fondo transparente, rayado con hueco, 3DSOLID y capa apagada, caché por tamaño y capas, errores, `DXF_CHANGED`, cancelar mientras dibuja, cancelar dentro de un INSERT anidado, dibujo vacío y recorte de la caché).
- El Python del sistema (3.12, `pip install -e`, ezdxf 1.4.5) ya ve la orden `worker`; con él pasan las pruebas del trabajador, de sólidos, fuentes y reutilización (86).

Etapa 2: commit `5de8302`. El hash de Perfil es el mismo con ezdxf 1.4.4 (`.venv`) y 1.4.5 (Python del sistema).

Etapa 3 (hecha, pendiente de visto bueno):
- `export` (`worker_export.py`): antes de exportar nada comprueba referencias, escalas, nombres de paquete (sección 5.1, también repetidos sin mayúsculas), ruta ≤ 240, que nada pise el DXF ni exista (salvo `overwrite`), `units`/`margin_mm` iguales a los de `load` y las opciones. Cada trabajo usa `block_commands.run_extract_block` sobre el DXF ya leído (argumentos del CLI de `extract-block`, con las fuentes de `.env` como `cli.main`), con el SHA-256 de `load` y el informe de la copia por partes. Si uno falla (`EXPORT_FAILED`, `CRITICAL_NOTICE`, `VALIDATION_FAILED`), los demás siguen. `write_summary` escribe `resumen_proyecto.json` (`project_commands.project_summary`, sacada de `run_extract_all_blocks` sin cambiar el formato).
- Avance y cancelación: `run_hooks.py` (ganchos de etapa y de entidad). Cancelar responde en 0,02–0,03 s también mientras se mide la caja del bloque (`reader.safe_extents` comprueba entre entidades) o se dibuja; el trabajo en curso no deja archivos y los publicados se quedan. `DXF_CHANGED` entre trabajos.
- Temporales: las carpetas de trabajo de cada referencia (también en la línea de órdenes) se llaman `.cde-tmp-…` y llevan el PID; `export` limpia las huérfanas de la carpeta de salida.
- Manifiesto 1.3 (aprobado): `options.units`, `instancing`, `xobject`, `clip` y `exporter_version`; los 1.0–1.2 siguen siendo válidos (`docs/manifiesto.md`).
- Si stdout se cierra (el Componedor terminó), el trabajador cancela y termina sin traza.
- Comprobado: Perfil exportado por el trabajador da el SVG con el hash de referencia; GUAMO BLOQUE1 a 1:2000 (`solids: footprint`, `omit_unsupported`) publicado en 166,6 s, igual que en el mismo proceso sin trabajador (156–177 s; con la línea de órdenes, 220 s ese día: el equipo va más lento que el 2026-11-13) y `verify-package` correcto. Una primera medida de 438 s se debió a procesos huérfanos de pruebas anteriores; ni los ganchos ni el inventario cambian el tiempo, y `gc.freeze()` tampoco (no se aplica).
- Avance en GUAMO: `extract` (caja del bloque y extracción, unos 35–50 s) no da eventos intermedios; `draw` es corto gracias a la reutilización y `pdf` (escribir el PDF con PyMuPDF, unos 60 s) tampoco los da. Cancelar responde enseguida mientras se mide la caja y mientras se dibuja; dentro de la copia del bloque con ezdxf y de la escritura del PDF, el error `CANCELLED` llega al terminar esa operación (la respuesta a `cancel`, enseguida).
- Pruebas: `tests/test_worker_export.py` (25: el mismo punto en la vista previa y en el PDF a menos de 0,01 mm, paquetes y manifiesto 1.3, nombre por defecto, `overwrite`, 16 errores antes de escribir nada, ruta relativa y de más de 240 caracteres, sesión, temporales huérfanos, cancelar con un paquete ya publicado y DXF cambiado entre trabajos) y 2 en `tests/test_manifest.py` (1.0–1.2 válidos sin las claves nuevas; tipos).

DXF de 2,74 GB (`Pruebas/plano_ERCO_prueba.dxf`): ni la copia sin bloques sin usar (2 217 MB) ni el original caben (se detuvieron a 5,5 GB). Estudio de lectura selectiva (prototipo fuera del repositorio): copia en streaming con solo los INSERT del Model Space y los bloques usados, 30,6 MB en 446 s con un pico de 302 MB; se carga en 13,2 s con 467 MB. Decisiones de Luis Angel (2026-11-17):
- La lectura selectiva se adopta para `load` en DXF muy grandes, después de la etapa 3. La copia reducida se guarda en caché con el hash del DXF para no repetir la pasada. Mientras no exista, un DXF que no quepa da `DXF_TOO_LARGE` con un mensaje claro. Hay que comprobar que ezdxf no repara nada a escondidas por las referencias rotas a entidades quitadas.
- Manifiesto 1.3 aprobado (compatible con 1.0–1.2): se hace junto con la etapa 3.

## Entidades de complementos, bloques anónimos y lectura selectiva (2026-11-18, sin commit)

Entidades de tipos desconocidos (error «Invalid DXF attribute "invisible" for entity VehicleTrackingPath» al cargar `PRUEBAEXPORTADORSUPERFICIES.dxf` en el trabajador):
- Causa: ezdxf guarda las entidades de complementos de AutoCAD como `DXFTagStorage`, sin `layer`, `color` ni `invisible`, y `entity.dxf.get("invisible", 0)` lanza `DXFAttributeError` en vez de dar el valor por defecto (también `RenderContext.resolve_visible` de ezdxf). En ese DXF hay 14: 12 `PLGRIDSYSTEM` en las presentaciones 01–04 y 10 (no afectan), 1 `GEOMAPIMAGE` suelta en el Model Space y 1 `VehicleTrackingPath` dentro de BLOQUE1.
- Arreglo: `reader.is_unknown_entity`, `reader.dxf_attr` (lectura segura de un atributo) y `reader.unknown_entity_counts`. Se saltan en el inventario (cajas y recorrido), en `fidelity.iter_detail_entities_with_visibility`, en el frontend del exportador (`_StrictFrontend.draw_entities`, que también usa la vista previa) y en «sin caja» (`block_extractor._definition_has_unbounded`: ya no da el falso `ENTITIES_WITHOUT_BOUNDS`). Aviso nuevo `UNKNOWN_ENTITIES_OMITTED` (warning, tipo y cantidad, con consejo) en `load`, en el TXT y en el manifiesto de cada paquete; `blocks[].unknown_entities` y `unknown_entities` en `load`; la vista previa las cuenta en `omitted`.
- Medido: el DXF de 830 MB carga entero en el trabajador en 155,4 s (con la copia por partes), 880 MB de memoria: 5 bloques, 1 omitido, 583 capas.
- Pruebas: `tests/test_unknown_entities.py` (6: una `VehicleTrackingPath` y un `PLGRIDSYSTEM` creados con la API de ezdxf, en un bloque, en un bloque anidado y sueltos; inventario, `--list --json`, `extract-block`, `extract-all-blocks`, `extract-modelspace`, `load`, vista previa y `export`).

Estudio de los bloques anónimos `*U` de `PRGD-26-1377-GEN-PL-101_1_V0.dxf` (solo estudio):
- `*U76`, `*U78` y `*U80` son tres mesas fotovoltaicas de 78 paneles (34,9 × 7,2 m; 312 entidades visibles cada una: 78 × `PANEL_615`, un bloque dinámico, con 1 HATCH y 3 LWPOLYLINE), insertadas una vez en el Model Space, giradas 180°, en las capas `I1.M1.PV1`, `I1.M1.PV2` e `I1.M2.PV4`, una al lado de la otra (x 4 706 970–4 707 075), a unos 850 m del bloque «via tunez». Sin textos. El DXF no dice de qué bloque dinámico vienen (sin XDATA `AcDbBlockRepBTag`).
- Si se ignoran se pierde el 10 % de lo que se dibuja con bloques (936 de 9 215 entidades visibles) y las únicas mesas del plano.
- Regla «exportar todos los *U del Model Space»: 3 paquetes en PRGD, pero 3 319 en Plano_completo (etiquetas de Civil 3D de 5 entidades) y 648 en Perfil (secciones `C-ROAD-*`). Regla propuesta: *U del Model Space con 100 entidades visibles o más → PRGD 3 (las mesas), Plano_completo 1 (tabla `C-ROAD-TABL`), Perfil 6 (tablas `C-ROAD-SHAP-TABL` y `C-ROAD-TABL`, una sección), 830 MB 1 (tabla). Pendiente de decisión.

Lectura selectiva para DXF muy grandes:
- `large_dxf.write_selective`: del Model Space solo los INSERT con sus ATTRIB y SEQEND, y el Paper Space entero; lo suelto se resume por capa (entidades, vértices, tipos y caja aproximada: sin la cabecera (0, 0) de las POLYLINE, la elevación ni las semillas de los HATCH, con el radio de círculos y arcos y la caja de las IMAGE). En PRGD, las cajas aproximadas coinciden con las de ezdxf en todas las capas.
- `dxf_session.load_dxf`: si la copia sin bloques sin usar mediría 500 MB o más (`CDE_SELECTIVE_DXF_MB`), la copia es selectiva. La copia reducida (de los dos tipos) se guarda en `<work-dir>/dxf-cache/<SHA-256>/` (3 como mucho) y se reutiliza tras comprobar el SHA-256. Antes de leer se estima la memoria (7 bytes por byte más 300 MB) frente a la libre (memoria más paginación): si no cabe, `DXF_TOO_LARGE` con lo que haría falta. Avance y cancelación durante las pasadas.
- Referencias rotas: ezdxf no avisa ni repara nada al leer la copia (no se llama a la auditoría). Su auditoría encuentra 1 arreglo más que en el original en PRGD (un `IMAGEDEF_REACTOR` de una IMAGE suelta quitada) y 0 en GUAMO; los 120 de más en GUAMO son de los bloques sin usar vaciados, igual que con la copia normal.
- Medido con `Pruebas/plano_ERCO_prueba.dxf` (2 947 MB): primer `load` 541 s (copia selectiva de 30,6 MB en 519 s), 635 MB de memoria; segundo `load`, desde la caché, 29 s. 522 bloques, 45 omitidos, 446 capas, 14 012 entidades sueltas (9 066 724 vértices). Un bloque exportado desde la copia: paquete correcto (`verify-package`).
- Aviso `SOURCE_MODELSPACE_LOOSE_SKIPPED` (info) en `load` y en cada paquete. Las órdenes de la línea de órdenes no usan la lectura selectiva.
- Pruebas: `tests/test_selective_read.py` (8). 615 pruebas en total, ruff limpio, hash de Perfil sin cambios.

## Bloques anónimos en el trabajador (2026-11-18, commit `92c2c37` «Fase 4b: bloques anónimos *U»)
- Commit anterior: `114268f` (entidades de complementos, lectura selectiva y estudio de los *U).
- Regla adoptada (Luis Angel): `load` ofrece los `*U` del Model Space con 100 entidades visibles o más (`CDE_ANON_MIN_ENTITIES`), con `anonymous: true`, `label` = capa + handle del INSERT y ese mismo `file_stem`; los menores van a `omitted` con `anonymous: true` y `visible_entities`. Todos los bloques llevan `anonymous`, `label` y `visible_entities`. `extract-all-blocks` de la línea de órdenes no cambia.
- Medido con `--list --json`: PRGD 3 de 3 (`I1.M1.PV1_12900`, `I1.M1.PV2_12962`, `I1.M2.PV4_129C4`, 312 entidades y 34,9 × 7,2 m cada una); Plano_completo 1 de 3 319 (`C-ROAD-TABL_EE4`, 295 entidades); Perfil 6 de 648 (4 `C-ROAD-SHAP-TABL`, 1 `C-ROAD-TABL`, 1 `C-ROAD-SCTN` de 121 entidades). `*U76` exportado con `extract-block`: 78 paneles, 312 trazados.
- Arreglo encontrado de paso: un `--output-prefix` con puntos (`I1.M1.PV1_12900`) se rechazaba como «con extensión» y `with_suffix` habría cortado el nombre. Ahora solo se rechazan las extensiones de salida (`block_commands.OUTPUT_EXTENSIONS`) y se usa `with_extension`; afecta también a `extract-modelspace`.
- `VehicleTrackingPath` de BLOQUE1 (830 MB): trae gráficos proxy legibles (316 bytes) y ezdxf los decodifica, pero son el recuadro genérico de AutoCAD (1 POLYLINE y el texto «atraPathData (ATRA_AutoTrack )», 75 × 92 m), no la trayectoria: la trayectoria está en 16 781 etiquetas propias de Autodesk Vehicle Tracking que ezdxf no interpreta. La `GEOMAPIMAGE` igual («AcDbGeoMap (AcGeolocationObj)», 1 935 × 1 685 m). No se dibujan: para que salgan hay que convertirlas en AutoCAD con el complemento instalado.
- Opción `anonymous_min_entities` de `load` (entero ≥ 0 o `null`): si viene, manda sobre `CDE_ANON_MIN_ENTITIES`; la respuesta (y `--list --json`) devuelve el umbral usado en `anonymous_min_entities`. Un valor no válido da `BAD_REQUEST`.
- Identificación entre sesiones: AutoCAD renumera los `*U` al guardar, así que el `ref` (`*U76#1`) solo vale dentro de la sesión. `preview` y `export` aceptan ahora **solo `handle`** (sin `block`; `worker.find_reference`): encuentra la referencia sea cual sea el nombre del bloque (`NOT_EXPORTABLE` si está en `omitted`, `REFERENCE_NOT_FOUND` si no existe, `BAD_REQUEST` si el handle está repetido). Comprobado con una prueba que renombra `*U` → `*U999` sin tocar el handle: mismo `label`, mismo paquete y el manifiesto con el nombre nuevo. Que AutoCAD conserve el handle del INSERT al guardar es su comportamiento normal (no se probó aquí con AutoCAD).
- Pruebas: `tests/test_anonymous_blocks.py` (11) y la de omitidos de `tests/test_worker_inventory.py` (con un `*D`). 626 pruebas, ruff limpio, hash de Perfil sin cambios.

## Sistema de coordenadas en load (2026-11-18, commit `58fc279`)
- `load` y `--list --json` devuelven `dxf.crs`: lo que declara el GEODATA del Model Space, sin interpretar (`crs.py`, solo biblioteca estándar; `dxf_session.LoadedDxf.crs`, calculado una vez al leer). Campos: `source` (`"GEODATA"`), `format` (`xml`, `wkt` o `unknown`), `id`, `name`, `description`, `authority` tal como vienen, `epsg` y `epsg_from` (`alias` o `authority`). Sin GEODATA, `crs: null`. Un GEODATA ilegible nunca hace fallar `load`.
- Medido con `load_dxf`: PRGD (`load` 14,0 s) y PRUEBAEXPORTADORSUPERFICIES de 830 MB (`load` 144,1 s, con la copia selectiva, que conserva el GEODATA): leer el CRS cuesta 0,3–0,5 ms. Los dos traen «Colombia CTM12» / «MAGNA_Colombia CTM12» con autoridad `EPSG, V6.11.2, 3115 […]`, sin alias del sistema (`epsg: 3115`, `epsg_from: "authority"`); 3115 no corresponde a CTM12, y se devuelve tal cual. Perfil trae lo mismo; GUAMO_prueba, «MAGNA-SIRGAS / Colombia Bogota zone» con alias EPSG 3116.
- Pruebas: `tests/test_crs.py` (10: con y sin GEODATA por `load_dxf`, `--list --json` y el trabajador; alias frente a autoridad; WKT; definiciones ilegibles). 636 pruebas, ruff limpio, hash de Perfil sin cambios.

## Tabla de impresión (CTB) y transparencia (2026-11-19, commit `7154704`)
- Motivo: las mesas FV de PRGD salían tenues en el Componedor frente al PDF de AutoCAD. Diagnóstico aprobado: AutoCAD imprime la presentación «01» con `CTB_ERCO_V2 7.ctb`, que da a las capas PV colores más claros (ACI 35 → (184, 138, 92), ACI 163 → (110, 138, 221), ACI 85 → (115, 184, 92)); nosotros usábamos el color del objeto. Además, el HATCH SOLID de `PANEL_615` tiene una transparencia del 90 % que reproducimos y que el PDF de AutoCAD no lleva.
- **Opción `--plot-style <archivo.ctb>`** (`extract-block`, `extract-all-blocks`, `extract-modelspace`) y `plot_style` en `export` y `preview` del trabajador (`plot_style.py`). Solo cuando se pide; sin ella el dibujo no cambia (hash de Perfil igual).
  - El `RenderContext(ctb=…)` de ezdxf 1.4.4 no sirve: usa la CTB solo como paleta ACI → RGB, deja el ACI 7 siempre negro, no aplica escala de grises ni tramado, y busca el grosor con el ACI escrito en la entidad, así que en las PorCapa o PorBloque (casi todas) nunca lo aplica. Por eso se resuelve a mano sobre las propiedades ya resueltas (`Frontend.push_property_override_function`), con el ACI resuelto (`properties.pen`): color propio, escala de grises, tramado y grosor; las entidades de color verdadero no cambian; el alfa se conserva.
  - Una PorBloque hereda del INSERT el color ya convertido: también cuenta como «de ACI» (la prueba lo encontró: sin eso, perdía el grosor).
  - Lectura propia de la CTB: `acadctb.load` de ezdxf falla con `CTB_ERCO_V2.ctb`, cuya descripción trae «ñ» en cp1252. Las `.stb` se rechazan.
  - Manifiesto **1.4**: `options.plot_style` (`{"name", "sha256"}` o null) y `options.print_transparency`. Hace falta subir la versión porque el esquema rechaza claves que no conoce; las 1.0–1.3 siguen valiendo (`_ADDED_IN`). Avisos `PLOT_STYLE_APPLIED` y `HATCH_TRANSPARENT_OMITTED` (info).
- **Opción `--no-print-transparency`** y `print_transparency: false`: los HATCH con transparencia del 80 % o más (`plot_style.HIGH_TRANSPARENCY`) **no se dibujan**; el resto, igual. Elegido omitir y no dibujar opaco: el relleno de los paneles es gris claro (199, 200, 202) y opaco taparía la ortofoto (peor que hoy), y el PDF de AutoCAD de PRGD no lo lleva. Se cuentan también en los INSERT que reutilizan la grabación (`_Instance.transparent`), el TXT los lleva por grupo (sin una línea por HATCH: una planta de PVcase tiene miles) y el SVG y el PDF tienen que coincidir.
- Vista previa: la caché añade `_p<6 cifras del SHA-256 de la CTB>` y `_t` al nombre solo si se piden (la ruta completa no puede pasar de 260 caracteres en Windows; la prueba lo encontró con `_ctb-…_notransp`).
- **`data/input/ctb/CTB_ERCO_V2.ctb` no es la de PRGD:** en ella los ACI 35, 163 y 85 usan el color del objeto, así que dan (127, 95, 63), (82, 103, 165) y (79, 127, 63), **no** los del PDF de AutoCAD. Solo cambia 16 colores (p. ej. ACI 7 → (46, 46, 46), ACI 9 → (220, 220, 220), con grosores de 0,05 a 0,35 mm). Hace falta `CTB_ERCO_V2 7.ctb` para que las mesas salgan como en AutoCAD.
- Mesa `I1.M1.PV1_12900` (`*U76`) a 1:75: sin opciones, 234 contornos (127, 95, 63) de 0,709 pt y 78 rellenos al 9,8 %; con `CTB_ERCO_V2.ctb`, lo mismo (ACI 35 por objeto); con `--no-print-transparency`, sin los 78 rellenos. Los tres pasan `verify-package`. Perfil con la CTB: ACI 7 → (46, 46, 46), ACI 9 → (220, 220, 220).
- Pruebas: `tests/test_plot_style.py` (14, con una CTB sintética guardada en cp1252) y 2 del manifiesto 1.4. 652 pruebas, ruff limpio, hash de Perfil sin cambios.

## CTB derivada del PDF de AutoCAD (2026-11-19, sin commit)
- Sin la `CTB_ERCO_V2 7.ctb` de PRGD, su tabla de colores se reconstruye comparando el DXF con el PDF que imprimió AutoCAD (`ctb_derive.py`; `python -m cde_exporter.cad.ctb_derive PLANO.dxf PLANO.pdf SALIDA.ctb`, que escribe también `SALIDA.json` con las pruebas de cada ACI).
  - Del DXF: por capa efectiva (la del INSERT para lo de la capa «0» de un bloque, como AutoCAD al crear las capas del PDF), los ACI resueltos y los colores verdaderos que se dibujan (Model Space y presentaciones).
  - Del PDF: los colores de trazo y relleno de cada capa (contenido opcional).
  - Por pasadas: en cada capa se quitan los colores verdaderos, los de los ACI ya medidos y el blanco (fondo de textos o máscaras: el ACI 5 salía blanco por dos trazos de la capa «3.LINEA TEXTO»); si queda un solo ACI sin medir, se le asigna el color dominante que queda (80 % o más). Si hay conflicto, gana el que confirman más capas.
  - No se usa la posición de las entidades: haría falta la transformación de cada ventana gráfica. Podría resolver los ACI que quedan sin medir.
  - Ojo: una capa con ACI y color verdadero imprime el color verdadero (la del rótulo es ACI 18 con RGB negro): esas entidades no cuentan como ACI.
- `plot_style.write_ctb` escribe la CTB (descripción en ASCII; la leen nuestra lectura y la de ezdxf): color medido si es distinto del de la paleta y «color del objeto» en los demás; grosores del objeto. `PlotStyleTable.description` va en el aviso `PLOT_STYLE_APPLIED` («Descripción de la tabla: Derivada del PDF de AutoCAD…»), así el TXT y el manifiesto dicen que es derivada, además del nombre y el SHA-256 de `options.plot_style` (sin cambio de esquema).
- **`data/input/ctb/CTB_ERCO_derivada_PRGD.ctb`** (SHA-256 `b4a909ea…97c9`) y su `.json`: 16 ACI se imprimen como ACI en PRGD; **12 medidos**: 5 → (0, 0, 0), 7 → (46, 46, 46), 8 → (150, 150, 150), 9 → (220, 220, 220), 10 → (255, 0, 0), 30 → (255, 127, 0), **35 → (184, 138, 92)**, **85 → (115, 184, 92)**, 150 → (0, 0, 0), **163 → (110, 138, 221)**, 250 → (0, 0, 0), 253 → (186, 186, 186). **4 sin medir** (color del objeto): 1, 2 y 3 (comparten «0-Detalles», donde quedan verde (0, 255, 0) y (66, 66, 66) para tres ACI) y 6 («offset», con el 2; «00_ZonaReposicion» no está en el PDF). Los 7, 8 y 9 coinciden con `CTB_ERCO_V2.ctb`.
- Mesas a 1:75 con la CTB derivada y `--no-print-transparency`: `*U76` (I1.M1.PV1_12900) 234 trazos (184, 138, 92); `*U78` (I1.M1.PV2_12962) (110, 138, 221); `*U80` (I1.M2.PV4_129C4) (115, 184, 92); sin los 78 rellenos al 90 %; grosor 0,709 pt. Los tres pasan `verify-package`.
- Pruebas: `tests/test_ctb_derive.py` (8, con una ida y vuelta: imprimir con una CTB, derivar desde ese PDF y recuperar los mismos colores). 660 pruebas, ruff limpio, hash de Perfil sin cambios.

## Zona del Model Space como detalle (2026-11-23, subfase 1, sin commit)
Objetivo (de Alan): los DXF de trabajo vendrán solo con Model Space, con los detalles sueltos uno junto al otro. El usuario elige una zona rectangular, las capas que se ven y la escala, y la zona sale como un detalle más (PDF + TXT + manifiesto), igual que un bloque; el título lo escribe en el Componedor. Plan aprobado por Luis Angel en 3 subfases: (1) el motor, probado desde la línea de órdenes; (2) vista previa con rectángulo y lista de capas; (3) el trabajador (`preview` y `export` con zona). Decisiones: lo que cruza el borde y no se puede recortar, con máscara de recorte en el PDF y su texto completo en el TXT; capas como lista blanca y como exclusión; nombre del paquete puesto por el usuario o generado (`Zona_01`…; subfase 3); las zonas las guarda el Componedor (más adelante).

Hecho (subfase 1, en `cad extract-modelspace`):
- `clipping.py`: además de LINE y polilíneas de tramos rectos, recorta **ARC, CIRCLE y ELLIPSE** en planta (extrusión 0, 0, ±1): `P(t) = C + A·cos t + B·sin t`, cortes con los cuatro lados resolviendo `A·cos t + B·sin t = k`, y se conservan los tramos con el punto medio dentro (el que pasa por el ángulo 0 de un círculo es uno solo); un círculo cortado da ARC con sus mismas propiedades; en un ARC espejado (extrusión −Z) se corta en sus propios ángulos. **HATCH** en planta: cada contorno que cruza el borde se aproxima por una poligonal (tolerancia: una millonésima de la diagonal de la zona) y se recorta con Sutherland–Hodgman; los contornos dentro se conservan tal cual; el patrón no cambia (va en coordenadas absolutas) y se quita la asociatividad. Los arcos de los contornos pasan por las curvas Bézier de ezdxf, como al dibujarlos (0,027 % del radio frente al círculo exacto).
- **Máscara de recorte** (`exporters.apply_clip_mask`): lo que no se puede recortar (texto, cotas, MULTILEADER, INSERT, splines, polilíneas con bulge…) entra entero y el contenido de la página va dentro de `q x y w h re W n … Q`; la máscara sobresale 0,5 mm (`MASK_BLEED_MM`) para no partir el grosor de una línea que va justo por el borde. La página ya no se amplía: mide la zona. Solo se pone si algo cruza el borde entero (`mask_box`); los bloques no la usan nunca. «Texto buscable» no exige las líneas que cruzan la máscara (`exporters.run_inside`).
- TXT: «Textos que cruzan el borde (completos)» con tipo, capa y texto (TEXT, MTEXT, atributos de INSERT, cotas, MULTILEADER; `clipping.entity_texts`). Aviso nuevo `MODELSPACE_MASKED` (info) en vez de `MODELSPACE_NOT_CLIPPED` (warning); este último ya no sale en las exportaciones (solo se conserva para modelos sin máscara).
- **Capas visibles también dentro de los bloques** (`modelspace_commands.filter_block_layers`): con `--layers` (lista blanca) y/o `--exclude-layers`, se quita de las definiciones de bloque de la copia lo que está en una capa que no se ve, como congelar una capa en una ventana del CAD (un INSERT anidado en esa capa desaparece entero; lo de la capa «0» toma la del INSERT y se queda). Aviso `MODELSPACE_LAYERS_IN_BLOCKS` (info) y línea «Quitado dentro de los bloques» en el TXT. `--list` muestra además las capas de dentro de los bloques elegidos.
- Sin cambio de esquema del manifiesto (sigue 1.4): `selection.clip.not_clipped` ahora es lo que corta la máscara (`docs/manifiesto.md`). `working_document` sigue igual; la versión con capas es `build_working_document`.
- Pruebas: `tests/test_zone_clipping.py` (22) y 2 de `tests/test_clipping.py` adaptadas (el círculo y el arco que cruzaban ahora se recortan y la página ya no se amplía). 684 pruebas, ruff limpio, hash de Perfil sin cambios.
- Prueba real con `PRGD-25-1365-CIV-PL-106_1_VA.dxf` (97 MB, metros; leerlo tarda ~45 s y cada exportación 25–60 s más), en `data/verificacion/zonas_1/`:
  - `Zona_01`, `Zona_02`, `Zona_03` a 1:10: las tres secciones de pedestal vecinas («SECCIÓN-PEDESTAL 0.40 X 0.40», «0.30 X 0.30» y «SECCIÓN-V-F 0.30 X 0.15»), con bordes en x = 4736058,98 / 4736059,65 / 4736060,24 / 4736061,00 e y = 2234185,80…2234186,45. Los recuadros de título y las cotas se solapan entre detalles: la máscara corta lo del vecino (2, 5 y 3 entidades) y el TXT lista su texto.
  - `Zona_04_poste_cortado` a 1:5: el poste del cerramiento sobre su pilastra, con la zona cortando la pilastra y los tubos: 18 entidades recortadas en 20 tramos (HATCH de las caras y contornos), 3 INSERT con máscara.
  - `Zona_05_sin_cotas_ni_textos` (`--exclude-layers EDD_COTAS,_000_Textos_General`) y `Zona_06_solo_0_y_001` (`--layers "0,_001_*"`, quita 1 entidad dentro de un bloque).
  - Las seis pasan `cad verify-package` y todas las comprobaciones (estado «con advertencias» por los 3 OLE2FRAME, ver límites).

Límites y pendientes de la subfase 1:
- Los 3 OLE2FRAME del DXF no tienen caja para ezdxf: cada zona avisa «3 entidades sin caja… no se incluyeron» (`MODELSPACE_WITHOUT_BOUNDS`, warning). Va con la decisión sobre los OLE (leer su posición del DXF y avisar solo si caen en la zona).
- No se recortan (van con máscara): splines, polilíneas con bulge o ajustadas, SOLID/TRACE/3DFACE, WIPEOUT, imágenes. El SVG intermedio (`CDE_KEEP_INTERMEDIATES`) no lleva la máscara.
- La capa de texto invisible de un texto cortado por la máscara solo deja buscar lo que queda dentro de la página.
- **El Componedor rechaza las zonas con máscara** (encontrado el 2026-11-24 con `verificar-paquete` del Componedor): `pdf.drawing.cad_bounds`/`page_bounds_mm` cuentan enteras las entidades que corta la máscara y se salen de la página (Zona_01: x1 = 84,9 mm en una página de 77 mm), así que falla su comprobación «g) page_bounds_mm dentro de la página». Las zonas sin máscara (Zona_05) pasan. **Resuelto en la subfase 3 con la opción B** (sección «Zonas en el trabajador»): manifiesto 1.5.
- El borde blanco alrededor del dibujo es el margen de `--margin-mm` (5 mm por lado, como en los bloques): la página mide la zona más los márgenes; el dibujo mide exactamente la zona.

## Vista previa de una zona (2026-11-24, subfase 2, sin commit)
Orden nueva `cad preview-modelspace DXF --output vista.png` (`zone_preview.py`), para elegir zona y capas antes de `extract-modelspace`:
- Vista: todo el Model Space (con un 3 % de aire), un trozo con `--view X1 Y1 X2 Y2` o, con `--window` y sin `--view`, la zona con un 25 % de contexto por lado. La zona (`--window` o `--window-from-block`) se marca en rojo.
- En la vista de todo el Model Space se marcan en azul los **grupos de dibujo** (`drawing_groups`: cortes sucesivos en X e Y donde hay huecos de más del 0,5 % del lado mayor) y la orden da la caja de cada uno lista para `--view` (en el DXF de PRGD, 6 grupos repartidos en 270 × 590 km).
- Capas: el mismo filtro que la exportación (`--layers`, `--exclude-layers`, `--include-hidden-layers`) en todos los niveles: las capas que no se ven se ocultan en el `RenderContext` (`hide_layers`), salvo la «0»; un INSERT anidado en una capa oculta desaparece entero. No recorta: lo que cruza el borde de la zona se ve entero.
- Cuadrícula con las coordenadas CAD en los bordes (paso 1, 2 o 5 × 10ⁿ) y, con `--scale 1:N`, una franja al pie con el tamaño de la zona (o de la vista) en la hoja, la hoja con márgenes y si cabe en A1. La franja va debajo: no cambia `cad_to_px`.
- `vista.json` al lado (formato `cde-zone-preview` 1): vista, zona, `cad_to_px` (misma convención que `preview.py`), unidades, escala, tamaño en papel, capas (entidades en la vista, estado en el CAD y si se ven), filtros, grupos y lo no dibujado (3DSOLID, como en la vista previa del trabajador).
- Dibujo ligero de `preview.py` (texto como rectángulos, rayados rellenos); `preview.paint` admite ahora `frame` (la imagen se ajusta a esa caja). Sin `frame`, igual que antes.
- Cajas rápidas (`select_view`): la de un INSERT sale de la de su definición (`inventory._Bounds`, una vez por bloque) llevada por su matriz; puede ser algo mayor que la exacta (solo dibuja de más fuera de la vista). Con la caja exacta de `select_modelspace`, cada vista del DXF de PRGD tardaba 89 s; ahora unos 15–20 s más la lectura del DXF (~45 s). En el trabajador (subfase 3) se podrán guardar entre vistas.
- Pruebas: `tests/test_zone_preview.py` (9). 693 pruebas, ruff limpio, hash de Perfil sin cambios.
- Prueba real en `data/verificacion/zonas_2/`: 01 todo el Model Space con los grupos, 02 la zona 1 con contexto a 1:10, 03 sin cotas ni textos, 04 solo «0» y `_001_*`, 05 las tres secciones con la zona 2, 06 el poste a 1:5 y 07 el grupo G1 entero.

## Zonas en el trabajador (2026-11-24, subfase 3, sin commit)
**Manifiesto 1.5 (opción B, decisión de Luis Angel):** con máscara, `pdf.drawing.cad_bounds` y `page_bounds_mm` son lo dibujado recortado a la zona (dentro de la página) y la clave nueva `pdf.drawing.mask` da la zona (`zone_cad`, `zone_page_mm`, `bleed_mm`); null sin máscara. `manifest.PdfFacts.mask_box` (lo pasa `block_commands` desde `args.mask_box`); `MASK_BLEED_MM` vive ahora en `manifest.py` (biblioteca estándar) y `exporters` lo importa. La comprobación «Escala del dibujo» sigue con la caja sin recortar (`drawn_size`). En `docs/manifiesto.md`, «Cómo medir los trazos con máscara»: lo que debe hacer la comprobación h del Componedor (recortar cada trazo a `zone_page_mm` a mano y comparar con `page_bounds_mm`, ±0,5 mm). El Componedor no se tocó: Luis Angel se lo pasa a su agente.
- Con los paquetes del trabajador sobre PRGD, el `verificar-paquete` actual del Componedor ya pasa la g; la h falla en los que tienen máscara hasta que aplique esa medición. La medición documentada, hecha con su propia función `trazos_sin_fondo`, da 0,000 mm de diferencia en los cuatro.

**Trabajador, protocolo 1.1** (`docs/protocolo_trabajador.md`, secciones 4.1 y 5.4; `worker_zone.py`):
- `preview` con `zone` (o `view`; `zone: null` = todo el Model Space con sus `groups`), `layers`, `exclude_layers` y `scale` opcional: PNG transparente con `cad_to_px` como el de un bloque, `layer_rows` (para la lista de capas), `entities`, `paper` (hoja a 1:N y si cabe en A1). Caché con clave `ms-<SHA-1>` de zona, vista y capas; el papel no entra en la clave.
- `export` con trabajos `{"zone", "layers", "exclude_layers", "scale" (N o "1:N"), "file_stem"}`; sin nombre o vacío, `Zona_01`, `Zona_02`… (salta los usados en el pedido y los que ya tienen archivos en la salida; nunca pisa nada). Se pueden mezclar con trabajos de bloque. Filas nuevas en la respuesta: `kind`, `zone`, `package_name`, `name_generated`. Las zonas no van en `resumen_proyecto.json`.
- Se comprueba todo antes de empezar (zona, escala, capas, que haya algo en la zona, nombre). La zona se prepara con `modelspace_commands.prepare_modelspace_export` (separada de `run_extract_modelspace`, que hace lo mismo que antes) sobre el DXF ya leído.
- Rendimiento: cajas rápidas del Model Space una vez por sesión (`_Session.modelspace_boxes`); la selección exacta solo mira sus candidatos (`select_modelspace(..., candidates)`; la caja rápida incluye ahora los atributos de cada INSERT, para contener siempre la exacta).
- Pruebas: `tests/test_worker_zone.py` (9) y la de la caja con máscara en `tests/test_zone_clipping.py`; 4 adaptadas por las versiones (manifiesto 1.5 en `test_manifest`, `test_plot_style` y `test_worker_export`; protocolo en `test_worker`). 703 pruebas, ruff limpio, hash de Perfil sin cambios.
- Prueba real con PRGD por el trabajador como proceso (`data/verificacion/zonas_3/`, con `respuestas.json` y `trabajador_stderr.log`): cuatro zonas en un solo `export` de 9,4 s (Zona_01 y Zona_02 con nombre generado, `Seccion_VF_solo_001` con `layers` y `Poste_1a5` a 1:5), las cuatro publicadas «con advertencias» (los 3 OLE). Vista previa de la zona 1: 1,0 s; desde la caché, 0,01 s. Los tiempos de `load` (398 s) y de la primera vista completa (183 s, que calcula las cajas) salieron inflados porque las pruebas corrían a la vez.

## Color de fondo de los HATCH (2026-11-24, sin commit)
Defecto que vio Luis Angel: en AutoCAD el concreto del pedestal y de la viga de PRGD tiene un fondo gris medio detrás de las rayitas; en nuestros PDF salían solo las rayitas y el contorno sobre blanco (ya en `zonas_1`).
- **Qué hace el gris:** un HATCH de patrón **AR-CONC** (capa «.») con **color de fondo** RGB 147, 149, 152, guardado en el XDATA `HATCHBACKGROUNDCOLOR` (código 1071). En Zona_01 está dentro del bloque «COLUMNA 50X50»; en la sección VF, suelto en el Model Space. En PRGD hay 259 HATCH con fondo de 2 157: 247 de color verdadero, 7 ACI y 5 «ninguno» (0xC8); 141 son SOLID, en los que el fondo no se ve.
- **Por qué el poste sí salía gris:** allí el gris son HATCH **SOLID** (ACI 251 y 253), no un color de fondo. El FP_8 del poste también tiene fondo (gris 192), que tampoco salía.
- **Causa:** el motor de dibujo de ezdxf 1.4.4 nunca pinta el color de fondo (en bloques tampoco). La copia de trabajo y el recorte sí conservan el XDATA. Además, `bgcolor` de ezdxf no mira el tipo del color (un ACI lo leería como RGB).
- **Arreglo** (`exporters.hatch_background_color` y `_StrictFrontend._draw_hatch_background`): antes del patrón se rellena el contorno del HATCH con su color de fondo (0xC2 color verdadero; 0xC3 ACI, con el 7 en negro; 0xC8, PorCapa y PorBloque no pintan nada), con la capa y la transparencia del HATCH. Solo cuando el patrón se dibuja (`--hatch-mode normal` o `auto`, sin simplificar): relleno sólido, solo contorno u omitido no llevan fondo. Vale para bloques y zonas recortadas. La CTB no cambia el fondo (es casi siempre color verdadero).
- La vista previa (PNG) no cambia: ya pinta los rayados rellenos.
- Pruebas: `tests/test_hatch_background.py` (4: lectura del color, bloque con el fondo antes del patrón, zona recortada y modo solo contorno). 707 pruebas, ruff limpio. Hash de Perfil **sin cambios** (Perfil no tiene HATCH de patrón con fondo).
- Prueba real en `data/verificacion/zonas_4/` (las mismas zonas de `zonas_3`, por el trabajador): Zona_01 y Zona_02 con el concreto gris debajo de las rayitas; `Poste_1a5` con el fondo gris 192 del FP_8; `Seccion_VF_solo_001` igual que antes (su lista `layers` 0 y `_001_*` deja fuera la capa «.» del concreto: no hay patrón ni fondo); y `Seccion_VF_todas_las_capas` (nueva, sin filtro) con el concreto gris.

## Orden de dibujo (ACAD_SORTENTS) (2026-11-24, sin commit)
Defecto que vio Luis Angel en `zonas_4/Seccion_VF_todas_las_capas`: el relleno gris del concreto quedaba encima del acero (los dos puntos azules se dibujaban antes que el gris y quedaban tapados). En AutoCAD el concreto queda detrás.
- **Cómo lo ordena AutoCAD:** con la tabla de orden de dibujo `ACAD_SORTENTS` (la que escribe DRAWORDER) del Model Space y de cada bloque: cada entidad tiene un handle de orden y se dibujan de menor a mayor; sin tabla, en el orden guardado. En PRGD el Model Space tiene tabla (25 772 entradas) y 25 bloques también. El AR-CONC de la sección VF está guardado casi al final (posición 25 744), pero su orden es 2121, antes del acero (`*U113`, 2123 y 2124).
- **Por qué fallaba:** ezdxf 1.4.4 solo respeta la tabla del layout que dibuja (`draw_layout`), no la de los bloques (`# TODO: redraw_order=True?` en su frontend), y `xref.Loader` no copia ninguna tabla. La zona se copia a un bloque de trabajo, así que se dibujaba en el orden guardado y el concreto quedaba el último. En los bloques con tabla pasaba lo mismo, también al exportar bloques.
- **Arreglo** (`draw_order.py`, `exporters._StrictFrontend._draw_insert`):
  - el contenido de cada INSERT se dibuja en el orden de la tabla de su bloque si la tiene (`virtual_entities(redraw_order=True)`); sin tabla, en el orden guardado, como siempre;
  - `copy_block_draw_order` pasa las tablas de los bloques a la copia de trabajo, en `block_extractor` y en la zona. El bloque se reconoce por su nombre o, si el Loader renombró un `*U`, por ser el único con la misma secuencia de tipos;
  - la zona da al bloque «Model_Space» una tabla con el orden de dibujo del Model Space de origen. Las piezas nuevas del recorte (el arco de un círculo, los tramos de una polilínea) heredan el puesto de su entidad (`clip_layout(..., ranks)`);
  - **sin tabla**, el fondo de los HATCH de patrón de un bloque se pinta antes que todo el bloque (`_draw_backgrounds_first`), debajo de lo que lo cruza; el patrón sigue en su sitio.
- Pruebas: `tests/test_draw_order.py` (5): tabla escrita y leída; tabla copiada también a un `*U` renombrado; bloque dibujado en su orden; fondo debajo sin tabla; zona en el orden del Model Space con un círculo recortado en su sitio. 712 pruebas, ruff limpio. Hash de Perfil **sin cambios**.
- Prueba real en `data/verificacion/zonas_5/` (trabajador): Zona_01, Zona_02, Seccion_VF_todas_las_capas, Seccion_VF_solo_001 y Poste_1a5. Comprobado en el PDF (orden de los trazos, con `get_drawings`): cada fondo gris va antes que todo lo de color que se solapa con él. En `zonas_4`, la sección VF tenía 3 elementos tapados (los 2 puntos azules y la línea del terreno); ahora ninguno. Zona_01 (estribo y puntos verdes), Zona_02 y el poste ya estaban bien y siguen igual.

## Cuñas de los LEADER (2026-11-25, sin commit)
Defecto que vio Luis Angel en la vista previa del Componedor con PRGD: cuñas grises oscuras enormes que salían de los detalles y no existen en AutoCAD.
- **DXF:** el Componedor usa `Downloads\PRGD-25-1365-CIV-PL-106_1_VA.dxf` (105,6 MB, del 24-11), una versión más nueva que la de `data\input` (96,8 MB, del 23-11, con otras coordenadas). Con la de `data\input` no se reproducía.
- **Qué las produce:** 4 **LEADER** (directrices antiguas) de la capa «0», handles 26A19, 26AA5, 26AA6 y 26AA7, junto a los detalles (x ≈ 4 695 170, y ≈ 2 229 978), con el estilo de cota «ESC 1-75»: DIMASZ 75 (75 m en un dibujo en metros) y primeros tramos de 0,3 a 1,4 m. AutoCAD no dibuja la punta de un LEADER si su primer tramo mide menos que dos veces la flecha; ezdxf la dibujaba siempre, de 62 a 76 m: las cuñas. No eran MULTILEADER ni cotas. Lo grabado por ezdxf no se estiraba (el mayor trazo, 207 m): la cuña era la propia punta.
- **También en la exportación:** una zona que las cruza (`cunas/antes_zona_cunas.pdf`) las tenía, en negro (4 rellenos de más de 20 mm a 1:1000). En una exportación de bloque, además, la página contaba la flecha (prueba: 677 mm en vez de 110).
- **Arreglo** (`leader_arrows.py`): la regla de AutoCAD en `ezdxf.render.leader.virtual_entities`, que ezdxf llama en cada uso del LEADER (dibujo del PDF/SVG, vista previa, cajas y explosión); si la punta no cabe, se usan las piezas de una copia sin punta. Se instala al importar `cde_exporter.cad`. El DXF no cambia.
- Resultado en `data/verificacion/cunas/`: `antes_*` y `despues_*` de la vista completa, de 3 700 m y de 915 m (G1) con la CTB derivada y sin transparencia, como las pide el Componedor; `antes_zona_cunas.pdf` frente a `despues_zona_cunas.pdf` (0 rellenos gigantes; `verify-package` pasa); `entidades_en_las_cunas.json`; `tiempos_vista_previa*.json`.
- Pruebas: `tests/test_leader_arrow.py` (2). 714 pruebas, ruff limpio, hash de Perfil sin cambios.

Medición de la vista previa con PRGD (tarea 2, sin programar; 1 500 px, CTB, sin transparencia):

| Paso | Vista completa / 3,7 km / 915 m (G1: 25 822 entidades, 159 000 trazos) | 200 m (1 490 entidades) | 50 m (1 300) | 10 m (12) |
|---|---|---|---|---|
| Leer el DXF (una vez, en `load`) | 42 s | | | |
| Cajas rápidas del Model Space (una vez, la primera vista) | 17–19 s | | | |
| Seleccionar por cajas | 0,15 s | 0,03 s | 0,02 s | 0,02 s |
| Dibujar con ezdxf (recorrer entidades y bloques anidados) | **41–45 s** | 1,7 s | 0,8 s | 0,15 s |
| Colocar y aplanar | 1,5–2,2 s | 0,05 s | 0,07 s | 0 s |
| Pintar con Pillow | 0,5–0,7 s | 0,05 s | 0,03 s | 0 s |
| Codificar el PNG | 0,05 s | 0,07 s | 0,04 s | 0,01 s |
| **Total** | **43–48 s** (la primera, más 17–19 s de cajas) | 1,9 s | 0,95 s | 0,19 s |

No hay recorte en la vista previa. Casi todo el tiempo es ezdxf dibujando con todo detalle lo que en esa vista mide menos de un píxel; la vista se vuelve a dibujar entera en cada zoom.

## Velocidad de la vista previa (2026-11-25, sin commit)
Decisiones de Luis Angel:
- preparar la vista previa en segundo plano, con aviso y avance;
- medir la memoria antes de programar y mantenerla razonable;
- se acepta perder detalle diminuto de lejos;
- hacer las propuestas 1, 2 y 3 (la 4 y la 5, después si hace falta).

Hecho (protocolo 1.2, `docs/protocolo_trabajador.md`, sección 4.2; `preview_index.py`):
- **Propuesta 1:** las cajas del Model Space se calculan una vez, en segundo plano, tras `load`. El cálculo es compartido y reanudable (`zone_preview.ModelspaceBoxes`, `boxes_for`): si un pedido de zona las necesita antes, sigue donde iba la preparación, y al revés.
- **Propuesta 2:** todo el Model Space se graba una vez por sesión, en un hilo aparte que cede el turno a cada pedido antes de cada entidad, a cualquier profundidad (`Gate`). Cada entidad del Model Space se indexa al terminar y su grabación se descarta. Se indexan 166 956 trazos (559 348 puntos) y el índice ocupa 17,6 MB.
- **Propuesta 3:** cada vista filtra los trazos por caja y por capas, con numpy, y pinta con Pillow en el orden de dibujo. Un trazo de menos de un píxel se pinta como un punto. La vista, la matriz `cad_to_px` y los recuentos son los de siempre.
- **Eventos** `preview_progress`, `preview_ready` y `preview_failed`. `load` acepta `prepare_preview` (la CTB y la transparencia que se prepararán) y devuelve `preview_preparation`, como `status`. La respuesta de `preview` de zona trae `prepared` y `lod`. Si llega otra configuración, se prepara la nueva.
- **Arreglos encontrados por el camino:**
  - `exporters._draw_insert` ya no copia en memoria todo el contenido de un bloque antes de dibujarlo. Solo lo hace si el bloque tiene achurados con fondo, que deben ir debajo. Con un bloque enorme de PRGD eran 21 s sin poder ceder el turno y unos 300 MB.
  - `zone_preview._box_of` dejaba de lado la cancelación: tomaba la orden de parar como «datos inválidos».

**Medición con PRGD** (`Downloads`, 105 MB; trabajador como proceso; CTB derivada, sin transparencia; `data/verificacion/velocidad/`):

| | Antes | Ahora |
|---|---|---|
| `load` | 58 s | 58 s (igual) |
| Hasta `preview_ready` | — | 50–52 s tras `load`, en segundo plano |
| Todo el Model Space / 3,7 km / 915 m (G1) | 44–48 s, más 17–19 s de cajas la primera | **0,41–0,46 s** |
| 200 m / 50 m / 10 m | 1,9 / 0,95 / 0,19 s | **0,10 / 0,13 / 0,06 s** |
| Vista cercana mientras se prepara | — | 3,6–16 s (espera a las cajas si aún no estaban) |
| `status` mientras se prepara | — | 0,09 s |
| Memoria del proceso | 1 048 MB tras `load` | 1 262 MB (+214 MB, de ellos unos 190 MB son las cajas, que ya hacían falta para las zonas) |

- Pruebas: `tests/test_preview_index.py` (9). Se adaptaron 2 pruebas del trabajador: los eventos que llegan entre mensajes en `test_worker`, y en `test_worker_zone` esperar `preview_ready` y la versión 1.2.
- Pendiente:
  - el Componedor debe enviar la CTB en `load`, mostrar el avance y, hasta `preview_ready`, pedir solo vistas cercanas;
  - la propuesta 4 (caché por niveles) y la 5 (geometría al navegador), si hacen falta.

## Textos en la vista previa y escala de la zona (2026-11-25, sin commit)
Problema: en la vista previa de zona los textos salen como cajas vacías, así que no se lee «ESC 1:25» para decidir la escala antes de exportar.

Decisiones de Luis Angel:
- una sola fuente (Arial o equivalente) para la vista previa;
- el texto se ve a partir de 6 píxeles de altura;
- para la escala cuentan TEXT, MTEXT, atributos y textos de directrices; las cotas se dibujan solo si es fácil;
- una escala: se rellena y el usuario la confirma o la cambia; varias: se muestran todas; ninguna: nada.

**Subfase A, hecha** (protocolo 1.3, `docs/protocolo_trabajador.md`, sección 4.3):
- Orden `texts`: los textos que se ven en una zona, con su posición, altura, giro y capa efectiva, también dentro de bloques, con el filtro de capas de la exportación (`zone_texts.py`).
- Escala propuesta en `scale_candidates` y `proposed_scale` (`scale_text.py`, solo biblioteca estándar).
- No espera a las cajas del Model Space.

Probado con el trabajador sobre PRGD (`PRGD-25-1365-CIV-PL-106_1_VA.dxf`):

| Zona | Textos | Tiempo | Resultado |
|---|---|---|---|
| «PLANTA / SECCIÓN … ESCALA 1:25» | 80 | 0,32 s (la primera de la sesión) | propone 1:25 (8 textos) |
| «SECCIÓN-V-F 0.30 X 0.15 / ESC-1:10» | 11 | 0,18 s | propone 1:10 (3 textos) |
| Rejillas, con 1:25 y 1:12.5 | 184 | 0,19 s | sin propuesta: 1:25 (14) y 1:12.5 (2) |
| Zona vacía | 0 | 0,04 s | nada |

- En todo el Model Space de PRGD-25 hay 1 635 textos; 57 llevan escala, con 7 escalas distintas (1:5, 1:10, 1:12.5, 1:15, 1:20, 1:25 y 1:35), escritas como «ESCALA 1:5», «ESC-1:15» o «Esc: 1:10».
- En PRGD-26 (`PRGD-26-1377-GEN-PL-101_1_V0.dxf`) solo hay 17 textos y ninguno con escala.
- Pruebas: `tests/test_zone_texts.py` (33). En `test_worker_zone` la versión pasa a 1.3.

- Commit `99ae62c`, subido a `origin/dev`.

**Subfase B, hecha (2026-11-26, sin commit)** (protocolo 1.4, `docs/protocolo_trabajador.md`, sección 4.2, «Texto»; `preview_index.py`):
- Decisiones de Luis Angel:
  - la misma fuente que en el PDF;
  - letras desde 6 píxeles, raya de 1 a 6 y punto por debajo;
  - siempre `Pruebas/fonts/sustituciones_arial.json`, sin opción en `load`;
  - hasta +30 MB y +15 s de preparación.
- Al preparar, cada texto (TEXT, MTEXT, ATTRIB, MULTILEADER y el texto de las cotas) se graba con sus letras, como en el PDF. Se envuelve `pipeline.draw_text` y lo que deja es un solo trazo del índice, con su altura de mayúscula y su línea media.
- Los huecos de las letras se guardan marcados (anillos de mayor a menor, con la orientación contraria a la del mayor). Así cada texto se pinta en una sola máscara, sin la regla par-impar.
- La respuesta de `preview` de zona trae `texts` (`drawn`, `as_lines`, `as_dots`), y `preview_ready` trae `texts`. Las vistas preparadas se guardan en la caché con la marca `_it`, para no reutilizar las de la 1.3, que llevan cajas.
- **Arreglo de velocidad:** la vista de todo el Model Space tardaba de media 0,66 s (0,79, 0,59 y 0,60), más que el límite de 0,6 s. El tiempo se iba en agrupar por color los 167 000 puntos de lejos (`np.unique`). Ahora se escriben de una vez en un arreglo de píxeles: pintar esa vista pasa de 0,23–0,35 s a 0,05–0,07 s, y por el trabajador tarda de media 0,35 s (0,48, 0,27 y 0,30).
- Sin cambiar, y documentado: la vista previa de un bloque, las zonas sin preparar y `preview-modelspace` siguen con la caja del texto.

Medido con PRGD (el de `Downloads`, 105 MB, el mismo que en la 1.2; `data/verificacion/velocidad_texto/`):

| | 1.2 | 1.4 |
|---|---|---|
| Índice | 17,6 MB | 33,4 MB (+15,8 MB) |
| Preparación | 49,9 s | 52,9–64,0 s, en tres mediciones (de +3 a +14 s) |
| Memoria del proceso | 1 262 MB | 1 338–1 341 MB |
| Todo el Model Space | 0,46 s | 0,35 s de media |
| 3,7 km / 915 m / 200 m / 50 m / 10 m | 0,41 / 0,43 / 0,10 / 0,13 / 0,06 s | 0,18 / 0,27 / 0,14 / 0,16 / 0,07 s |
| Junto a «ESCALA 1:12.5»: 100 m / 30 m / 8 m | — | 0,12 / 0,14 / 0,16 s |

- Pruebas: `tests/test_preview_text.py` (4), una de ellas compara las letras de la vista previa con los trazos del PDF publicado (a menos de 1,5 píxeles). En `test_worker_zone` y `test_zone_texts`, la versión pasa a 1.4.
- Pendiente: el texto real también fuera de la vista preparada, si hace falta; y `font_substitutions` en `load`, si hace falta.

## Letras que faltaban en textos de estilos TrueType sin archivo (2026-11-26, sin commit)
Error confirmado por Luis Angel en el PDF exportado y compuesto de PRGD-25 (cajas de paso): «CAJA DE PASO 1.30m x 1.50m» salía «CA  DA  A», y faltaban letras en los encabezados del «CUADRO DESPIECE». En AutoCAD se ve completo. Pasaba igual en la vista previa (subfase B).

- **Causa:** el estilo «ARIAL» de PRGD no tiene archivo de fuente; AutoCAD guarda la familia («Sora ExtraLight», no instalada) en el XDATA del **estilo**.
  - Nuestras sustituciones (`font_config._requests`) y el informe (`fidelity._style_and_font`) solo miraban el XDATA de la entidad, así que no se sustituía y el TXT decía «Fuentes sustituidas: no hay».
  - ezdxf caía en su fuente por defecto («sans-serif»), que en un equipo con AutoCAD es `AcadEref.ttf` (`C:\Windows\Fonts`, de símbolos): solo tiene C, A, D, N, O, B, x y pocas más.
  - No es la conversión a trazos ni el relleno.
- **Arreglo** (aprobado por Luis Angel):
  - `font_config.style_font_request` lee la familia del XDATA del estilo; si no está instalada, se sustituye por Arial (respaldo automático, `FONT_SUBSTITUTED`).
  - Red de seguridad: con las fuentes configuradas, la familia «sans-serif» va a la de respaldo, nunca a `AcadEref.ttf`.
  - La vista previa preparada (subfase B) instala las mismas fuentes, así que se corrige igual.
- **Comprobado:**
  - `tests/test_style_font_family.py` (4 pruebas, más 4 con `CDE_REFERENCE_TESTS=1`): la familia del estilo; la sustitución con su aviso; una familia inventada dibuja los mismos contornos que Arial; sin fuente, la de respaldo. Sin el arreglo, 3 de las 4 fallan («12 anillos frente a 32»).
  - Comprobación por texto: cada texto que se dibuja tiene todas sus letras latinas en la fuente con la que se dibuja, y ninguno usa `AcadEref.ttf`. Pasa en Perfil, Plano_completo, VIA_2MA y PRGD-25 (unos 15 minutos).
  - Antes y después de la zona de las cajas de paso de PRGD-25 a 1:50 (`data/verificacion/fuentes_estilo/prgd/`): títulos, encabezados de la tabla y nombres de vista completos. «SECCIOÓN A-A» viene así en el DXF (O y Ó seguidas).
  - **Paquetes de referencia:** Perfil_K0010, 0 píxeles distintos; Plano_completo, 9 de 11,8 millones, un matiz de borde (`data/verificacion/fuentes_estilo/antes_despues_*.png`). Los textos «Epifanía», «SimHei» y «Raspoutine Medium» están fuera de esos bloques, o llevan su fuente en el texto (`Verdana`). **El hash de Perfil no cambia** (`A2560C11…3FF9`): no hace falta regenerar los paquetes de referencia.
- **Pendiente:**
  - Comparar con el PDF que imprimió AutoCAD del PRGD-25: no está en el equipo (solo el de PRGD-26, que no tiene estilos afectados).
  - Revisar el texto de los MULTILEADER, cuyo estilo no mira `_requests` (en PRGD salen bien).

## Otros
- Hablar con Alan: ¿limpiar historial de Git (SVG/TXT de salidas ya commiteados)? Investigar handles duplicados del DXF.
- Informes Word de Fase 1 y 2 desactualizados (4 archivos de salida, escala solo por flag): rehacer con guía de uso actual (data/input → escala por pregunta → PDF+TXT).
