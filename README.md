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
