"""Utilidades compartidas por las pruebas."""

import pytest

from cde_exporter.cad import block_commands
from cde_exporter.cad.detail_model import detail_to_dict


@pytest.fixture
def model_details(monkeypatch) -> list[dict]:
    """Modelo de cada detalle extraído (``detail_to_dict``), en el orden de las referencias.

    Sustituye al antiguo --detail-json: se toma del flujo justo cuando construye el
    manifiesto, con el detalle ya finalizado.
    """
    details: list[dict] = []
    original = block_commands.build_manifest

    def capture(detail, **kwargs):
        details.append(detail_to_dict(detail))
        return original(detail, **kwargs)

    monkeypatch.setattr(block_commands, "build_manifest", capture)
    return details
