# Estado del proyecto — Exportador Vectorial

Último hito: ajuste de la Fase 3 — entidades no visibles, omisión y huella de sólidos 3D (consulta git log para el hash). 245 pruebas pasando, ruff limpio.

## Plan en 4 fases
1. Selección de bloque + escala — hecha.
2. Modelo interno del detalle (v2) + informe TXT A–G + contrato de salida PDF+TXT — hecha.
3. Motor de representación vectorial (política de fuentes, rayados, entidades) — en curso.
4. Manifiesto JSON + validación — pendiente.
Después: componedor de PDF (plancha A1) y guía de usuario en el README.

## Pendientes menores
- Decidir si `--scale` pasa a ser obligatorio sin terminal (rompería ~12 pruebas).

## Fase 3
Hecho:
- Entidades no visibles (capa apagada o congelada, o entidad invisible): ya no detienen la exportación; se cuentan y se avisan en "A tener en cuenta".
- `--omit-unsupported`: las entidades que ezdxf omite por "unsupported" se cuentan por tipo y capa, salen como "omitidas" en el inventario y se avisan en el TXT y al final de la terminal. Por defecto sigue estricto.
- `--solids footprint`: los 3DSOLID convertibles con `ezdxf.acis` se dibujan como su huella en planta (una vez por definición, en una copia de render); la caja, el contenido y la página salen de esa copia. Probado con Plano_completo: 17739 sólidos (162 distintos), error máximo 0,001 mm en el papel.

Pendiente:
- Fuentes: procesar solo el detalle (hoy se recorre todo el dibujo antes de elegir bloque y se imprime mucho ruido); unificar criterio de "uso de fuente" (terminal 112 vs informe 82).
- Rendimiento: leer el DXF una sola vez y no dibujar el SVG si no se pide (Plano_completo tarda 145 s).
- Inventario de entidades no visibles: hoy salen como "conservadas" en la sección E.
- MULTILEADER marcados como "no soportados" en el inventario aunque se dibujan con su gráfico proxy.
- Heurística de unidades con falsos positivos (INSERT con escala 0.001 y coordenadas georreferenciadas de millones).
- Ruido de ezdxf en la terminal (STYLE inexistente, handles duplicados, DIMASSOC).
- Z mínima sospechosa de −1,5 millones en la caja de Plano_completo (sección C): buscar qué entidad la produce.
- REGION no soportado por ezdxf (aborta el render, p. ej. Cont. 40ft HC (planta)).
- Aviso "relative point size is not supported" mostrado como nota del renderizador.
- Caché de fuentes usa `_font_cache` privado de ezdxf.

## Fase 4
- Condensar advertencias de handles duplicados; rutas con `/` en el JSON.

## Otros
- Hablar con Alan: ¿limpiar historial de Git (SVG/TXT de salidas ya commiteados)? Investigar handles duplicados del DXF.
- Informes Word de Fase 1 y 2 desactualizados (4 archivos de salida, escala solo por flag): rehacer con guía de uso actual (data/input → escala por pregunta → PDF+TXT).
