"""La ayuda de la CLI se muestra también en una consola de Windows (cp1252)."""

import io
import sys

import pytest

from cde_exporter.cli import main


@pytest.mark.parametrize("command", [["cad", "extract-block"], ["cad", "extract"]])
def test_help_prints_on_a_cp1252_console(monkeypatch, command) -> None:
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", console)

    with pytest.raises(SystemExit) as exit_info:
        main([*command, "--help"])

    console.flush()
    assert exit_info.value.code == 0
    text = raw.getvalue().decode("utf-8")
    assert "--font-substitutions" in text
    assert "→" in text
