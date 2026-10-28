import ezdxf

from cde_exporter.cad import block_commands
from cde_exporter.cli import main


def _source(path, count=1, units=ezdxf.units.InsertUnits.Millimeters, size=(10, 5)):
    document = ezdxf.new()
    document.units = units
    document.blocks.new("Detalle").add_line((0, 0), size)
    for index in range(count):
        document.modelspace().add_blockref("Detalle", (index * 100, 0))
    document.saveas(path)
    return path


def _arguments(source, prefix, *extra):
    return [
        "cad", "extract-block", str(source), "--block-name", "Detalle",
        "--output-prefix", str(prefix), *extra,
    ]


def _answers(monkeypatch, values):
    iterator = iter(values)
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return next(iterator)

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(block_commands, "_is_interactive", lambda: True)
    return prompts


def _names(folder):
    return sorted(item.name for item in folder.iterdir())


def test_prompt_sets_scale_and_is_recorded(tmp_path, monkeypatch):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)
    prompts = _answers(monkeypatch, ["1:50"])
    source = _source(tmp_path / "origen.dxf")
    out = tmp_path / "salida"

    assert main(_arguments(source, out / "detalle")) == 0

    assert len(prompts) == 1
    assert "1:N" in prompts[0]
    assert _names(out) == ["detalle.manifest.json", "detalle.pdf", "detalle.txt"]
    report = (out / "detalle.txt").read_text(encoding="utf-8")
    assert "Escala nominal: 1:50" in report


def test_invalid_answers_are_asked_again(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)
    prompts = _answers(monkeypatch, ["50", "1:0", "1:20"])
    source = _source(tmp_path / "origen.dxf")
    out = tmp_path / "salida"

    assert main(_arguments(source, out / "detalle")) == 0

    assert len(prompts) == 3
    printed = capsys.readouterr().out
    assert printed.count("Escala no válida. Escriba la escala como 1:N") == 2
    assert "usa 1:N" not in printed
    assert "El denominador" not in printed
    report = (out / "detalle.txt").read_text(encoding="utf-8")
    assert "Escala nominal: 1:20" in report


def test_scale_option_skips_the_prompt(tmp_path, monkeypatch):
    def forbidden(prompt=""):
        raise AssertionError("No debe preguntar la escala")

    monkeypatch.setattr("builtins.input", forbidden)
    monkeypatch.setattr(block_commands, "_is_interactive", lambda: True)
    source = _source(tmp_path / "origen.dxf")
    out = tmp_path / "salida"

    assert main(_arguments(source, out / "detalle", "--scale", "1:50")) == 0


def test_missing_answer_publishes_nothing(tmp_path, monkeypatch):
    def closed(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    monkeypatch.setattr(block_commands, "_is_interactive", lambda: True)
    source = _source(tmp_path / "origen.dxf")
    out = tmp_path / "salida"

    assert main(_arguments(source, out / "detalle")) == 1

    assert not out.exists() or _names(out) == []


def test_summary_is_shown_before_asking(tmp_path, monkeypatch, capsys):
    _answers(monkeypatch, ["1:50"])
    source = _source(tmp_path / "origen.dxf")

    assert main(_arguments(source, tmp_path / "salida" / "detalle")) == 0

    printed = capsys.readouterr().out
    assert "Referencia: ocurrencia 1 de 1" in printed
    assert "Unidades: mm" in printed
    assert "Tamaño del detalle: 10.000 × 5.000 unidades CAD" in printed
    assert "Medida física del detalle según la escala" in printed


def test_unknown_units_warn_before_asking(tmp_path, monkeypatch, capsys):
    _answers(monkeypatch, ["1:100"])
    source = _source(
        tmp_path / "origen.dxf", units=ezdxf.units.InsertUnits.Unitless, size=(1000, 500)
    )

    assert main(_arguments(source, tmp_path / "salida" / "detalle")) == 0

    assert "NO quedará garantizada" in capsys.readouterr().err


def test_all_references_share_one_question(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)
    prompts = _answers(monkeypatch, ["1:50"])
    source = _source(tmp_path / "origen.dxf", count=2)
    out = tmp_path / "salida"

    assert main(_arguments(source, out / "detalle", "--all")) == 0

    assert len(prompts) == 1
    assert "se aplicará a las 2 referencias" in capsys.readouterr().out
    assert _names(out) == [
        "detalle_001.manifest.json", "detalle_001.pdf", "detalle_001.txt",
        "detalle_002.manifest.json", "detalle_002.pdf", "detalle_002.txt",
    ]


def test_without_terminal_keeps_previous_behavior_with_warning(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CDE_KEEP_INTERMEDIATES", raising=False)
    monkeypatch.setattr(block_commands, "_is_interactive", lambda: False)
    source = _source(tmp_path / "origen.dxf")
    out = tmp_path / "salida"

    assert main(_arguments(source, out / "detalle")) == 0

    assert "no se indicó --scale" in capsys.readouterr().err
    assert _names(out) == ["detalle.manifest.json", "detalle.pdf", "detalle.txt"]
    report = (out / "detalle.txt").read_text(encoding="utf-8")
    assert "Escala nominal: no aplica" in report