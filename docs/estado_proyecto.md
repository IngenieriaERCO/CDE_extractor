# Estado del proyecto — Exportador Vectorial

Último hito: Fase 4, pasos 3 y 4 — manifiesto v1 (aún sin publicar) y lista única de avisos (consulta git log para el hash). 407 pruebas pasando, ruff limpio.

## Plan en 4 fases
1. Selección de bloque + escala — hecha.
2. Modelo interno del detalle (v2) + informe TXT A–G + contrato de salida PDF+TXT — hecha.
3. Motor de representación vectorial (política de fuentes, rayados, entidades) — hecha (quedan pendientes menores, abajo).
4. Manifiesto JSON + validación — en curso (pasos 0, 2, 3 y 4 hechos; quedan 5, 6 y 7).
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

Prototipo de texto buscable (2026-10-27; script fuera del proyecto en `data/output/auditoria_fase3/texto_buscable/prototipo_texto.py`):
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
- El JSON pasa a ser el tercer archivo publicado: PDF + TXT + JSON (manifiesto `<prefijo>.json`), publicado el último. Se retira `--detail-json`. El manifiesto guarda el nombre del DXF y su hash, no la ruta absoluta.
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
- Paso 3, manifiesto v1 (`manifest.py`, solo biblioteca estándar; documento `docs/manifiesto.md` para el componedor): `document_type` "cde-detail-manifest", `schema_version` "1.0" y `model_version` como trazabilidad; DXF y PDF por nombre (nunca ruta), SHA-256 y bytes; bloque, handle y ocurrencia; unidades y escala con `physical_scale_guaranteed` (booleano, nunca null) y su motivo; PDF con páginas, tamaño medido, caja de lo dibujado en CAD y en página y la matriz CAD → página real (mm, origen arriba a la izquierda, Y hacia abajo; coordenadas CAD = Model Space de origen); conteos por tipo y por capa, no conservadas con motivo y HATCH; fuentes y dependencias; avisos por categoría con recuentos por severidad; comprobaciones con `blocking`; versiones (exportador, ezdxf, PyMuPDF) y opciones usadas. `validate_manifest` comprueba un esquema propio (todas las claves, ninguna de más) y la coherencia (versión mayor, páginas, SHA-256, nombres sin ruta, recuentos, estado). Se construye y se valida en cada extracción (Perfil: 0,1–0,3 s, incluido el hash del DXF de 58 MB); si no fuera válido no se publica nada. Todavía no se escribe como archivo. Ejemplo real de Perfil_K0010 en `data/output/auditoria_fase4/manifiesto/perfil_k0010.manifest.json` (12,4 KB; 6 avisos: 4 info y 2 warning; escala física garantizada).
- Dos correcciones encontradas al contrastar la matriz con el PDF:
  - Precisión: la matriz se sacaba restando puntos transformados; con coordenadas georreferenciadas (~4,9 millones de m, traslación de ~10⁸ mm) la escala salía 20,0000000096 y un punto se desplazaba 0,047 mm. Ahora `affine_2d` lee los coeficientes: matriz y PDF coinciden en 0,000005 mm.
  - Centrado: ezdxf redondea la página final a 0,1 mm (`Page.width_in_mm` redondea al leerse) y centra el dibujo en esa página; al ajustarla después al tamaño pedido, el dibujo quedaba desplazado (0,02 mm en X y 0,003 mm en Y; era la asimetría 10,441/10,421 mm vista en el paso 2). `_PdfBackend` traslada el dibujo al centro de la página exacta: Perfil queda a 10,4310/10,4310 y 5,7444/5,7444 mm. El SVG no cambia.

Pendiente (plan aprobado):
- Paso 5: los avisos `CRITICAL` (XREF o bloque sin resolver) bloquean: sin archivos y código 1.
- Paso 6: publicar el manifiesto como tercer archivo (`<prefijo>.json`), en orden PDF → TXT → JSON (el manifiesto el último), con comprobación de coherencia manifiesto–PDF antes de publicar (hash, páginas, tamaño, nombre); `--all` todo o nada; retirar `--detail-json`; rutas con `/` también en el TXT.
- Paso 7: pruebas y documentación (contrato de salida de CLAUDE.md: PDF + TXT + JSON; informe y guía de uso).
- Nota: el PDF no es determinista (el `/ID` del trailer es aleatorio): el hash identifica el archivo publicado, pero la regresión gráfica sigue siendo con el SVG.
- Condensar advertencias de handles duplicados.

## Otros
- Hablar con Alan: ¿limpiar historial de Git (SVG/TXT de salidas ya commiteados)? Investigar handles duplicados del DXF.
- Informes Word de Fase 1 y 2 desactualizados (4 archivos de salida, escala solo por flag): rehacer con guía de uso actual (data/input → escala por pregunta → PDF+TXT).
