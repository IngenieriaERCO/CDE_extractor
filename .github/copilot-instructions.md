# Workspace Setup

- [x] Requisitos del proyecto confirmados: paquete Python 3.11+, layout `src/`, PyMuPDF, Pydantic Settings, pytest y Ruff.
- [x] Estructura inicial creada y personalizada para CDE Exportador Vectorial.
- [x] Documentación, configuración de entorno y CLI incluidas.
- [x] Extensiones Python y Python Environments instaladas.
- [x] Pruebas, lint, formato y ayuda CLI verificados correctamente.
- [x] No se requiere una tarea de VS Code ni iniciar un servicio para este paquete.
- [x] README y estas instrucciones de workspace incluidos.

## Guía del repositorio

Mantén las dependencias de runtime mínimas. Usa `src/cde_exporter` para el código, conserva las operaciones SharePoint explícitamente no implementadas hasta integrar Microsoft Graph y no guardes credenciales en archivos del repositorio. Ejecuta `python -m pytest` y `ruff check .` al modificar el paquete.
