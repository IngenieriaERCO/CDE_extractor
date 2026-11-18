"""Avance y cancelación de una exportación, para quien la lanza (el trabajador).

El flujo de ``extract-block`` avisa al empezar cada etapa (``stage``) y el frontend del
exportador, al dibujar cada entidad (``entity``, con su profundidad: 0 es el INSERT del Model
Space, 1 las entidades del bloque…). Sin ganchos activos no hace nada. Un gancho puede lanzar
una excepción para cancelar (``exporters.DrawingCancelled`` o una subclase: el frontend no la
toma por datos inválidos de la entidad).

Etapas de cada trabajo de ``export`` (docs/protocolo_trabajador.md, sección 6): ``extract``,
``analyze``, ``draw``, ``pdf``, ``checks`` y ``publish``. Solo usa la biblioteca estándar.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Callable, Iterator

STAGES = ("extract", "analyze", "draw", "pdf", "checks", "publish")


class DrawingCancelled(Exception):
    """Quien exporta pidió parar (el trabajador al cancelar). El frontend del exportador y
    ``reader.safe_extents`` nunca la toman por datos inválidos: siempre llega a quien llamó."""


_STAGE: contextvars.ContextVar[Callable[[str], None] | None] = contextvars.ContextVar(
    "cde_stage_hook", default=None
)
_ENTITY: contextvars.ContextVar[Callable[[int], None] | None] = contextvars.ContextVar(
    "cde_entity_hook", default=None
)


def stage(name: str) -> None:
    """Empieza la etapa ``name`` (una de ``STAGES``)."""
    hook = _STAGE.get()
    if hook is not None:
        hook(name)


def entity(depth: int) -> None:
    """Se va a dibujar una entidad a esa profundidad (−1: se va a medir su caja)."""
    hook = _ENTITY.get()
    if hook is not None:
        hook(depth)


def entity_hook_active() -> bool:
    """¿Hay un gancho de entidad? (sin él, quien recorre entidades no necesita envolverlas)"""
    return _ENTITY.get() is not None


@contextlib.contextmanager
def hooks(
    on_stage: Callable[[str], None] | None = None,
    on_entity: Callable[[int], None] | None = None,
) -> Iterator[None]:
    """Activa los ganchos mientras dura el bloque ``with``."""
    stage_token = _STAGE.set(on_stage)
    entity_token = _ENTITY.set(on_entity)
    try:
        yield
    finally:
        _STAGE.reset(stage_token)
        _ENTITY.reset(entity_token)
