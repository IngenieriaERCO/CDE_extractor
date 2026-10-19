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
