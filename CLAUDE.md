# Exportador Vectorial (paquete `cde_exporter`) — ERCO

Proyecto de Luis Angel (práctica profesional en ERCO, Medellín). Extrae un bloque (detalle) de un DXF y exporta un PDF vectorial a escala nominal 1:N, más un informe TXT de trazabilidad. Después vendrá un "componedor de PDF" (plancha A1).

## Cómo trabajar con Luis Angel
- Responde y escribe commits SIEMPRE en español.
- Nivel de programación básico: explica paso a paso, de forma progresiva, con comandos exactos (PowerShell en Windows, entorno `.venv`).
- Prefiere contenido personalizado, no genérico. Los informes se entregan como Word listo para enviar.
- Lee cada archivo antes de editarlo. Pide confirmación antes de cambios grandes.

## Verificación (siempre antes de dar algo por terminado)
- `python -m pytest -q --tb=short` (referencia: 483 pruebas pasando).
- `ruff check src tests`.
- NO ejecutar `ruff format` sobre archivos existentes (solo en archivos nuevos).
- NO usar `pytest -s` (las pruebas del aviso de escala esperarían entrada).
- Regresión gráfica: el SVG de Perfil_K0010 (`data/input/referencia/prueba_erco_perfil.dxf`) a 1:50 con `--hatch-mode auto --font-substitutions Pruebas/fonts/sustituciones_arial.json` debe tener hash SHA256 `A2560C1160FC06179FFEA3E87836D93468738259146749D57F0B1473400D3FF9` (se obtiene con `CDE_KEEP_INTERMEDIATES=1`). Cambió a propósito por el paso 4 de la Fase 3 (antes `BD488686…8194B6E1`): los HATCH ANSI35 y ANSI37 de Perfil, dentro de un INSERT, se dibujan ahora con su patrón y con la escala correcta (antes salían como relleno). El cambio anterior (antes `973120C3…7B83D`) fue por el arreglo de fuentes instaladas: 4 textos pasaron de Arial a Verdana. Nunca comparar DXF por hash (no es determinista).
- Juego mínimo de referencia (ignorado por Git, como todo `data/`; cada carpeta tiene su `LEEME.txt`):
  - `data/input/referencia/`: los DXF de origen `prueba_erco_perfil.dxf` (regresión gráfica), `VIA_2MA.dxf` y `Plano_completo.dxf`.
  - `data/output/referencia/`: paquetes de Perfil_K0010, VIA_MA y Plano_completo en el formato actual, la regresión de Perfil con su SVG (hash de arriba), el manifiesto de ejemplo con su script y los scripts `prototipo_texto.py` y `medir.py`.
- NO borrar nunca: las carpetas `data/input/referencia/` y `data/output/referencia/` completas, `data/input/.gitkeep`, `data/output/.gitkeep` y `Pruebas/fonts/`. Lo demás de `data/input` y `data/output` se puede borrar (son pruebas sueltas que se regeneran).

## Git
- Añadir archivos por nombre; nunca `git add .`.
- No hacer commit ni push sin que Luis Angel lo pida.
- Un commit por fase terminada; antes, solo si Luis Angel lo pide. Los ajustes menores se agrupan en el commit de la fase en curso.
- Mensajes en español: "Fase N: ..." o "Ajuste Fase N (x): ...".
- `core.autocrlf` = `input`; los archivos van en LF.
- Repo privado `IngenieriaERCO/CDE_manager`. `data/input` y `data/output` están ignorados: son datos confidenciales de ERCO, no se suben.

## Reglas del programa
- Única entrada: DXF; se lee el Model Space.
- DXF de 200 MB o más (`CDE_LARGE_DXF_MB`): las tres órdenes de exportación lo leen desde una copia temporal sin el contenido de los bloques sin usar, hecha por partes (`large_dxf.py`, dos pasadas, conservadora) en la carpeta de salida y borrada tras leerla (`block_commands.read_source_dxf`). Aviso `SOURCE_UNUSED_BLOCKS_SKIPPED` (info); el manifiesto guarda el hash del original.
- `cad extract` (ventana XY, publica `.dxf`/`.svg`/`.pdf`/`_fidelidad.txt` sin manifiesto) está en desuso: hace lo mismo, pero avisa por stderr y remite a `cad extract-modelspace`. Paso 2 pendiente (decisión de Luis Angel): convertirla en alias cuando se confirme que nadie usa sus archivos.
- Salidas publicadas: PDF + TXT (informe con secciones A–G generado desde el modelo interno) + manifiesto `<prefijo>.manifest.json`, publicados en ese orden (el manifiesto el último: un PDF sin manifiesto es un paquete incompleto; `cad verify-package` lo comprueba). Si la extracción falla, no queda ningún archivo. Con `--all`, todo o nada: se preparan todas las referencias antes de publicar ninguna. `--detail-json` ya no existe (las pruebas usan el fixture `model_details` de `tests/conftest.py`).
- Nunca modificar el DXF de origen.
- Todo se trabaja en carpetas temporales dentro de la carpeta de salida (una por referencia, que se borran al terminar la orden) y se publica de forma atómica (`_publish_files`).
- La escala se pide al usuario tras identificar el bloque (`--scale 1:N` o pregunta interactiva). Sin terminal y sin `--scale`: aviso.
- Factor de salida = mm por unidad CAD × escala del INSERT ÷ N. No afirmar escala física si las unidades no están verificadas.
- La escala del dibujo se mide en el propio PDF (comprobación "Escala del dibujo" de `pdf_checks`: puntos de los trazos frente a la caja que grabó ezdxf; tolerancia 0,01 mm por eje). Es una advertencia, no bloquea; la escala física solo es "garantizada" si las unidades están verificadas y esa comprobación pasa. La página PDF se crea con su tamaño exacto en puntos (`_PdfBackend`; ezdxf la truncaba a puntos enteros).
- `detail_model.py` solo usa la biblioteca estándar (sin ezdxf).
- Variable de desarrollo `CDE_KEEP_INTERMEDIATES=1` conserva `.dxf` y `.svg` (con `--solids footprint`, el `.dxf` es la copia de render).
- Los errores inesperados se muestran sin traceback ("Error CAD inesperado …"); `CDE_DEBUG=1` muestra el traceback completo.

## Reglas de uso (qué sale en el PDF)
- El PDF contiene solo lo que está dentro del bloque elegido.
- Lo apagado o congelado en el CAD no sale (se cuenta y se avisa en el TXT).
- El texto va en el PDF como trazados vectoriales y, encima, una capa de texto invisible (TextWriter + Helvetica) que se puede buscar, seleccionar y copiar; se desactiva con `--no-text-layer`. La comprobación "Texto buscable: N de N" de `pdf_checks` lo verifica; si falta alguna línea es una advertencia (no impide publicar). El TXT lo declara en "A tener en cuenta".
- El TXT registra cada HATCH por tipo, patrón y escala con su tratamiento (conservado, relleno sólido, solo contorno u omitido) y el motivo.
- Capas ocultas: `HIDDEN_LAYERS_CONTENT` (info, contenido del bloque en capa apagada o congelada), `MODELSPACE_HIDDEN_OVERLAP` (warning, entidades sueltas del Model Space en capa oculta sobre la zona del bloque) y `HIDDEN_LAYERS_INCLUDED` (info). `--include-hidden-layers [CAPAS]` las dibuja cambiando solo la copia de trabajo. Manifiesto en esquema 1.1 (añade `options.include_hidden_layers` y `selection`; `validate_manifest` acepta los 1.0 sin esas claves: `manifest._ADDED_IN`).
- Los objetos de Civil 3D requieren `EXPORTTOAUTOCAD` antes de guardar el DXF.
- Los sólidos 3D (3DSOLID) se dibujan como huella en planta con `--solids footprint` o se omiten con `--omit-unsupported`; sin ninguna de las dos opciones, la extracción se detiene.
- El ACIS de los 3DSOLID se lee con `acis_reader.load_bodies`, que admite el transform binario de AutoCAD 2018+ (PVcase) que ezdxf 1.4.4 no lee; los sólidos leídos así llevan el aviso `SOLIDS_ACIS_OWN_READER` (info). Un transform con escala ≠ 1 o deformación no se convierte (motivo en el TXT).
- Al extraer, cada MULTILEADER copiado traduce sus handles una sola vez (`block_extractor._multileader_mapping_once`): ezdxf 1.4.4 lo hacía dos veces y el estilo del texto podía acabar apuntando a otra entidad.

## Avisos y manifiesto (Fase 4)
- Lista única de avisos: `notices.collect_notices(detail)` genera todos los avisos (código, severidad `info`/`warning`/`critical`, categoría y consejo). La usan el TXT ("A tener en cuenta") y el manifiesto; el estado final es "con advertencias" si hay algún aviso `warning`. Los avisos `critical` (`notices.BLOCKING_SEVERITIES`: XREF o bloque sin resolver) bloquean: sin archivos, código 1 y un mensaje que dice qué falta; se comprueban antes de dibujar y antes de publicar. Todo código de aviso nuevo necesita su categoría en `notices.WARNING_CODES` (una prueba lo exige). La sustitución de fuente es `FONT_SUBSTITUTED` (info); `GLYPHS_MISSING` es warning. Los avisos del modelo (`ExtractedDetail.warnings`) también pueden ser `Severity.INFO` (p. ej. `SOLIDS_ACIS_OWN_READER`).
- Manifiesto v1 (`manifest.py`, documentado en `docs/manifiesto.md`): `build_manifest` y `validate_manifest` (esquema propio, sin jsonschema). Se valida contra su esquema, se escribe y se comprueba contra el PDF (`pdf_checks.check_manifest_coherence`: hash, bytes, páginas, tamaño y nombre) antes de publicar. Guarda nombres de archivo, nunca rutas, y la matriz CAD → página real (`ExportReport.cad_to_page_pt`).
- Comprobaciones: toda comprobación está en `detail_model.BLOCKING_CHECKS` (si falla, no se publica nada) o en `ADVISORY_CHECKS` (solo avisa); `check_blocks` falla con una sin clasificar.
- La página PDF se crea con su tamaño exacto y el dibujo centrado en ella: ezdxf la trunca a puntos enteros y además la redondea a 0,1 mm (`Page.width_in_mm` redondea al leerse); `_PdfBackend` compensa las dos cosas.

## Arquitectura (src/cde_exporter/cad)
`detail_model.py` (modelo v2), `entity_policy.py` (tabla de política por tipo de entidad y `decide`; solo biblioteca estándar, como `detail_model.py`), `detail_builder.py`, `detail_extras.py`, `detail_text.py` (TXT), `pdf_checks.py`, `block_commands.py` (flujo), `scale.py`, `fidelity.py`, `exporters.py`, `solids.py` (huella de 3DSOLID), `acis_reader.py` (lectura del ACIS con el transform binario; aísla las clases internas de ezdxf), `block_catalog.py`, `block_extractor.py`, `notices.py` (lista única de avisos), `manifest.py` (manifiesto v1), `project_commands.py` (orden `extract-all-blocks`: un paquete por bloque y `resumen_proyecto.json`), `file_names.py` (nombres de archivo seguros), `hidden_layers.py` (capas apagadas o congeladas: qué ocultan en el detalle y sueltas en el Model Space; `--include-hidden-layers`), `modelspace_commands.py` (orden `extract-modelspace`: elige del Model Space por capas o zona, sin recorte, lo agrupa en un bloque de trabajo «Model_Space» en una copia y lo exporta con el flujo de `extract-block`). `large_dxf.py` (copia por partes de un DXF muy grande sin bloques sin usar). `notices.py`, `manifest.py`, `file_names.py` y `large_dxf.py` solo usan la biblioteca estándar.

## Estado y pendientes
Ver `docs/estado_proyecto.md`.
