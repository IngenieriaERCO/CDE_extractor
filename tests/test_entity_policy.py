"""Tabla de política por tipo de entidad: una decisión por conjunto de hechos."""

import ast
from pathlib import Path

import pytest

from cde_exporter.cad import entity_policy
from cde_exporter.cad.detail_model import EntityStatus
from cde_exporter.cad.entity_policy import (
    HIDDEN_BY_ANCESTOR,
    HIDDEN_OWN,
    HINT_OMIT_UNSUPPORTED,
    HINT_SOLIDS_FOOTPRINT,
    EntityFacts,
    PolicyContext,
    decide,
    hatch_simplification_reason,
    unsupported_hint,
)

_INVALID = "datos inválidos: DXFAttributeError: sin estilo"


def test_policy_module_uses_only_the_standard_library() -> None:
    tree = ast.parse(Path(entity_policy.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module.split(".")[0] if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [None])
        if node.module != "__future__" or isinstance(node, ast.Import)
    }
    assert "ezdxf" not in imported


def test_plain_geometry_is_conserved() -> None:
    decision = decide(EntityFacts("LINE"))

    assert decision.status is EntityStatus.CONSERVED
    assert decision.reason is None and decision.cause is None


def test_hidden_entities_are_not_visible_and_beat_every_other_rule() -> None:
    own = decide(EntityFacts("3DSOLID", visible=False, drawable=False))
    nested = decide(EntityFacts("LINE", visible=False, hidden_by_ancestor=True))
    invalid = decide(EntityFacts("LEADER", visible=False, invalid_error=_INVALID))

    assert (own.status, own.reason) == (EntityStatus.NOT_VISIBLE, HIDDEN_OWN)
    assert (nested.status, nested.reason) == (EntityStatus.NOT_VISIBLE, HIDDEN_BY_ANCESTOR)
    assert invalid.status is EntityStatus.NOT_VISIBLE
    assert own.cause is None


def test_invalid_data_is_unsupported_with_its_own_reason() -> None:
    decision = decide(EntityFacts("LEADER", invalid_error=_INVALID))

    assert decision.status is EntityStatus.UNSUPPORTED
    assert decision.cause == _INVALID
    assert decision.reason == "Datos inválidos: DXFAttributeError: sin estilo."


@pytest.mark.parametrize("kind", ["3DSOLID", "REGION", "BODY"])
def test_types_ezdxf_cannot_draw_are_unsupported(kind: str) -> None:
    decision = decide(EntityFacts(kind, drawable=False))

    assert decision.status is EntityStatus.UNSUPPORTED
    assert decision.cause == "sin soporte de dibujo"
    assert decision.reason == "Tipo no soportado o condicionado: sin soporte de dibujo."


def test_image_is_unsupported_even_though_ezdxf_has_a_handler() -> None:
    decision = decide(EntityFacts("IMAGE", drawable=True))

    assert decision.status is EntityStatus.UNSUPPORTED
    assert decision.cause == "requiere archivo raster externo"


@pytest.mark.parametrize(
    ("has_proxy", "cause"),
    [(True, "proxy gráfico disponible"), (False, "proxy gráfico ausente")],
)
def test_proxy_entity_depends_on_its_proxy(has_proxy: bool, cause: str) -> None:
    decision = decide(EntityFacts("ACAD_PROXY_ENTITY", has_proxy=has_proxy))

    assert decision.status is EntityStatus.UNSUPPORTED and decision.cause == cause


@pytest.mark.parametrize("kind", ["MULTILEADER", "MLEADER"])
def test_multileader_is_conserved_only_if_it_has_geometry(kind: str) -> None:
    drawn = decide(EntityFacts(kind, drawable=False, has_proxy=False, has_geometry=True))
    empty = decide(EntityFacts(kind, drawable=False, has_geometry=False))
    broken = decide(EntityFacts(kind, invalid_error=_INVALID))

    assert drawn.status is EntityStatus.CONSERVED
    assert empty.status is EntityStatus.UNSUPPORTED and empty.cause == "sin geometría dibujable"
    assert broken.status is EntityStatus.UNSUPPORTED and broken.cause == _INVALID


@pytest.mark.parametrize(
    ("mode", "kind", "estimate", "expected"),
    [
        ("normal", "patrón", 10**9, None),
        ("auto", "patrón", 50000, None),
        ("auto", "patrón", 50001, "HATCH simplificado por --hatch-threshold 50000; estimación"),
        ("solid", "patrón", 0, "HATCH convertido a relleno sólido por --hatch-mode solid"),
        ("solid", "sólido", 0, None),
        ("solid", "degradado", 0, "HATCH convertido a relleno sólido por --hatch-mode solid"),
        ("outline", "sólido", 0, "HATCH mostrado solo como contorno por --hatch-mode outline"),
        ("ignore", "sólido", 0, "HATCH omitido deliberadamente por --hatch-mode ignore"),
    ],
)
def test_hatch_rules_by_mode(mode: str, kind: str, estimate: int, expected: str | None) -> None:
    reason = hatch_simplification_reason(kind, estimate, mode, 50000)
    decision = decide(
        EntityFacts("HATCH", hatch_kind=kind, hatch_estimate=estimate), PolicyContext(mode, 50000)
    )

    if expected is None:
        assert reason is None and decision.status is EntityStatus.CONSERVED
    else:
        assert reason.startswith(expected)
        assert decision.status is EntityStatus.SIMPLIFIED and decision.reason == reason


@pytest.mark.parametrize("kind", ["TEXT", "MTEXT", "DIMENSION", "ATTRIB"])
def test_text_with_a_substituted_font_is_transformed(kind: str) -> None:
    substituted = decide(EntityFacts(kind, font=("Standard", "Roboto", "arial.ttf", False)))
    found = decide(EntityFacts(kind, font=("Standard", "Arial", "arial.ttf", True)))

    assert substituted.status is EntityStatus.TRANSFORMED
    assert substituted.reason == "Fuente sustituida: Roboto se representa con arial.ttf."
    assert found.status is EntityStatus.CONSERVED


def test_a_font_on_a_non_text_entity_is_ignored() -> None:
    decision = decide(EntityFacts("LINE", font=("Standard", "Roboto", "arial.ttf", False)))

    assert decision.status is EntityStatus.CONSERVED


def test_unsupported_hints_keep_the_existing_messages() -> None:
    assert unsupported_hint({"3DSOLID"}, True) == HINT_SOLIDS_FOOTPRINT
    assert unsupported_hint({"3DSOLID", "REGION"}, True) == HINT_SOLIDS_FOOTPRINT
    assert unsupported_hint({"3DSOLID"}, False) == HINT_OMIT_UNSUPPORTED
    assert unsupported_hint({"REGION"}, True) == HINT_OMIT_UNSUPPORTED
    assert "--omit-unsupported" in HINT_OMIT_UNSUPPORTED and "--solids footprint" in (
        HINT_SOLIDS_FOOTPRINT
    )
