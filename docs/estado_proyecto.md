# Estado del proyecto — Exportador Vectorial

Último hito: cierre de la Fase 2 (consulta git log para el hash). 211 pruebas pasando, ruff limpio.

## Plan en 4 fases
1. Selección de bloque + escala — hecha.
2. Modelo interno del detalle (v2) + informe TXT A–G + contrato de salida PDF+TXT — hecha.
3. Motor de representación vectorial (política de fuentes, rayados, entidades) — pendiente.
4. Manifiesto JSON + validación — pendiente.
Después: componedor de PDF (plancha A1) y guía de usuario en el README.

## Pendientes menores
- Decidir si `--scale` pasa a ser obligatorio sin terminal (rompería ~12 pruebas).

## Fase 3
- Fuentes: procesar solo el detalle (hoy se recorre todo el dibujo antes de elegir bloque y se imprime mucho ruido); unificar criterio de "uso de fuente" (terminal 112 vs informe 82).
- REGION no soportado por ezdxf (aborta el render, p. ej. Cont. 40ft HC (planta)).
- Heurística de unidades con falsos positivos (INSERT con escala 0.001 y coordenadas georreferenciadas de millones).
- Aviso "relative point size is not supported" mostrado como nota del renderizador.
- Caché de fuentes usa `_font_cache` privado de ezdxf.

## Fase 4
- Condensar advertencias de handles duplicados; rutas con `/` en el JSON.

## Otros
- Hablar con Alan: ¿limpiar historial de Git (SVG/TXT de salidas ya commiteados)? Investigar handles duplicados del DXF.
- Informes Word de Fase 1 y 2 desactualizados (4 archivos de salida, escala solo por flag): rehacer con guía de uso actual (data/input → escala por pregunta → PDF+TXT).
