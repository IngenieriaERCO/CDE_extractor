# Exportador Vectorial (paquete `cde_exporter`) — ERCO

Proyecto de Luis Angel (práctica profesional en ERCO, Medellín). Extrae un bloque (detalle) de un DXF y exporta un PDF vectorial a escala nominal 1:N, más un informe TXT de trazabilidad. Después vendrá un "componedor de PDF" (plancha A1).

## Cómo trabajar con Luis Angel
- Responde y escribe commits SIEMPRE en español.
- Nivel de programación básico: explica paso a paso, de forma progresiva, con comandos exactos (PowerShell en Windows, entorno `.venv`).
- Prefiere contenido personalizado, no genérico. Los informes se entregan como Word listo para enviar.
- Lee cada archivo antes de editarlo. Pide confirmación antes de cambios grandes.

## Verificación (siempre antes de dar algo por terminado)
- `python -m pytest -q --tb=short` (referencia: 383 pruebas pasando).
- `ruff check src tests`.
- NO ejecutar `ruff format` sobre archivos existentes (solo en archivos nuevos).
- NO usar `pytest -s` (las pruebas del aviso de escala esperarían entrada).
- Regresión gráfica: el SVG de Perfil_K0010 a 1:50 con `--hatch-mode auto --font-substitutions Pruebas/fonts/sustituciones_arial.json` debe tener hash SHA256 `A2560C1160FC06179FFEA3E87836D93468738259146749D57F0B1473400D3FF9` (se obtiene con `CDE_KEEP_INTERMEDIATES=1`). Cambió a propósito por el paso 4 de la Fase 3 (antes `BD488686…8194B6E1`): los HATCH ANSI35 y ANSI37 de Perfil, dentro de un INSERT, se dibujan ahora con su patrón y con la escala correcta (antes salían como relleno). El cambio anterior (antes `973120C3…7B83D`) fue por el arreglo de fuentes instaladas: 4 textos pasaron de Arial a Verdana. Nunca comparar DXF por hash (no es determinista).

## Git
- Añadir archivos por nombre; nunca `git add .`.
- No hacer commit ni push sin que Luis Angel lo pida.
- Un commit por fase terminada; antes, solo si Luis Angel lo pide. Los ajustes menores se agrupan en el commit de la fase en curso.
- Mensajes en español: "Fase N: ..." o "Ajuste Fase N (x): ...".
- `core.autocrlf` = `input`; los archivos van en LF.
- Repo privado `IngenieriaERCO/CDE_manager`. `data/input` y `data/output` están ignorados: son datos confidenciales de ERCO, no se suben.

## Reglas del programa
- Única entrada: DXF; se lee el Model Space.
- Salidas publicadas: solo PDF + TXT (informe con secciones A–G generado desde el modelo interno). Si la extracción falla, no queda ningún archivo.
- Nunca modificar el DXF de origen.
- Todo se trabaja en un directorio temporal dentro de la carpeta de salida y se publica de forma atómica (`_publish_files`).
- La escala se pide al usuario tras identificar el bloque (`--scale 1:N` o pregunta interactiva). Sin terminal y sin `--scale`: aviso.
- Factor de salida = mm por unidad CAD × escala del INSERT ÷ N. No afirmar escala física si las unidades no están verificadas.
- `detail_model.py` solo usa la biblioteca estándar (sin ezdxf).
- Variable de desarrollo `CDE_KEEP_INTERMEDIATES=1` conserva `.dxf` y `.svg` (con `--solids footprint`, el `.dxf` es la copia de render).
- Los errores inesperados se muestran sin traceback ("Error CAD inesperado …"); `CDE_DEBUG=1` muestra el traceback completo.

## Reglas de uso (qué sale en el PDF)
- El PDF contiene solo lo que está dentro del bloque elegido.
- Lo apagado o congelado en el CAD no sale (se cuenta y se avisa en el TXT).
- El texto va en el PDF como trazados vectoriales y, encima, una capa de texto invisible (TextWriter + Helvetica) que se puede buscar, seleccionar y copiar; se desactiva con `--no-text-layer`. La comprobación "Texto buscable: N de N" de `pdf_checks` lo verifica; si falta alguna línea es una advertencia (no impide publicar). El TXT lo declara en "A tener en cuenta".
- El TXT registra cada HATCH por tipo, patrón y escala con su tratamiento (conservado, relleno sólido, solo contorno u omitido) y el motivo.
- Los objetos de Civil 3D requieren `EXPORTTOAUTOCAD` antes de guardar el DXF.
- Los sólidos 3D (3DSOLID) se dibujan como huella en planta con `--solids footprint` o se omiten con `--omit-unsupported`; sin ninguna de las dos opciones, la extracción se detiene.

## Arquitectura (src/cde_exporter/cad)
`detail_model.py` (modelo v2), `entity_policy.py` (tabla de política por tipo de entidad y `decide`; solo biblioteca estándar, como `detail_model.py`), `detail_builder.py`, `detail_extras.py`, `detail_text.py` (TXT), `pdf_checks.py`, `block_commands.py` (flujo), `scale.py`, `fidelity.py`, `exporters.py`, `solids.py` (huella de 3DSOLID), `block_catalog.py`, `block_extractor.py`.

## Estado y pendientes
Ver `docs/estado_proyecto.md`.
