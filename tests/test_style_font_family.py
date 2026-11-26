"""Estilos TrueType sin archivo de fuente: la familia va en el XDATA del estilo (AutoCAD).

Sin leerla, ezdxf no la sustituía y caía en su fuente por defecto, que en un equipo con
AutoCAD es ``AcadEref.ttf`` (de símbolos): a los textos les faltaban letras sueltas en el PDF y
en la vista previa («CAJA DE PASO» salía «CA  DA  A»). Ver ``font_config.style_font_request``.

La comprobación por texto (``drawn_texts``) dibuja el Model Space y mira, en cada texto que
llega a ``pipeline.draw_text``, que la fuente con la que se dibuja tenga todas sus letras.
Con ``CDE_REFERENCE_TESTS=1`` se pasa también sobre los DXF de referencia de ``data/input``.
"""

import io
import os
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import ezdxf
import pytest
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.recorder import Recorder
from ezdxf.fonts import fonts

from cde_exporter.cad.fidelity import inspect_fidelity
from cde_exporter.cad.font_config import (
    DEFAULT_FAMILY,
    active_substitutions,
    configure_fonts,
    style_font_request,
)
from cde_exporter.cad.preview_index import TEXT_SUBSTITUTIONS, Preparation

_ARIAL = Path("C:/Windows/Fonts/arial.ttf")
pytestmark = pytest.mark.skipif(not _ARIAL.is_file(), reason="arial.ttf no está instalado")
INVENTED = "Familia Inventada 987"
TEXT = "CAJA DE PASO 1.30m x 1.50m - SECCIÓN B-B"


def _source(path: Path, family: str | None, font: str = "") -> Path:
    """Un MTEXT en un estilo con archivo ``font`` o, si está vacío, con la familia ``family``
    en el XDATA del estilo (como guarda AutoCAD un estilo TrueType)."""
    document = ezdxf.new("R2018")
    style = document.styles.add("ARIAL", font=font)
    if family is not None:
        style.set_extended_font_data(family, italic=False, bold=False)
    document.modelspace().add_mtext(
        TEXT, dxfattribs={"style": "ARIAL", "char_height": 1, "insert": (0, 0)}
    )
    document.saveas(path)
    return path


class _TextsOnly(Frontend):
    """Solo dibuja lo que puede llevar texto (y los bloques que lo contienen): en un DXF de
    referencia, los rayados y lo demás no hacen falta y gastarían minutos y memoria. Una
    entidad con datos inválidos se salta (el exportador la omite con su aviso)."""

    WITH_TEXT = frozenset({"TEXT", "MTEXT", "ATTRIB", "INSERT", "DIMENSION", "MULTILEADER"})

    def draw_entity(self, entity, properties) -> None:
        if entity.dxftype() not in self.WITH_TEXT:
            return
        try:
            super().draw_entity(entity, properties)
        except ezdxf.DXFError:
            return


def drawn_texts(document) -> list[tuple[str, str, frozenset[str]]]:
    """Cada texto que se dibuja en el Model Space: (texto, archivo de la fuente con la que se
    dibuja, letras que esa fuente no tiene). Con las fuentes que estén instaladas al llamarla."""
    frontend = _TextsOnly(RenderContext(document), Recorder())
    engine = frontend.pipeline.text_engine
    found = []
    cmaps: dict[str, frozenset[int]] = {}
    draw_text = frontend.pipeline.draw_text

    def capture(text, transform, properties, cap_height, dxftype="TEXT") -> None:
        face = properties.font or frontend.pipeline.default_font_face
        name = Path(engine.get_font(face).name).name
        if name not in cmaps:
            try:
                cmaps[name] = frozenset(fonts.font_manager.get_ttf_font(name).getBestCmap())
            except Exception:  # noqa: BLE001 - SHX u otra fuente sin cmap: no se comprueba
                cmaps[name] = frozenset()
        missing = frozenset(
            char
            for char in text
            if cmaps[name]
            and not char.isspace()
            and ord(char) >= 32
            and ord(char) not in cmaps[name]
        )
        found.append((text, name, missing))
        draw_text(text, transform, properties, cap_height, dxftype)

    frontend.pipeline.draw_text = capture
    with redirect_stdout(io.StringIO()):
        frontend.draw_layout(document.modelspace())
    return found


def _configured(document):
    with redirect_stdout(io.StringIO()):
        return configure_fonts(str(TEXT_SUBSTITUTIONS), None, document, str(_ARIAL))


# ----------------------------------------------------------------- la familia del estilo


def test_the_style_request_is_its_file_or_the_family_of_its_xdata(tmp_path: Path) -> None:
    document = ezdxf.readfile(_source(tmp_path / "a.dxf", INVENTED))
    assert style_font_request(document.styles.get("ARIAL")) == INVENTED
    document = ezdxf.readfile(_source(tmp_path / "b.dxf", None, font="romans.shx"))
    assert style_font_request(document.styles.get("ARIAL")) == "romans.shx"
    document = ezdxf.readfile(_source(tmp_path / "c.dxf", None))
    assert style_font_request(document.styles.get("ARIAL")) == ""


def test_a_missing_style_family_is_substituted_and_reported(tmp_path: Path) -> None:
    source = _source(tmp_path / "inventada.dxf", INVENTED)
    document = ezdxf.readfile(source)
    restore = _configured(document)
    try:
        substitution = active_substitutions()[INVENTED.casefold()]
        assert substitution[1] == "arial.ttf" and substitution[2] == "respaldo automático"
        assert substitution[3] == 1  # un texto
        report = inspect_fidelity(source)
        texts = drawn_texts(document)
    finally:
        restore()
    # El TXT la declara como sustituida (FONT_SUBSTITUTED), sin letras que falten.
    assert [(family, found) for (_s, family, _r, found) in report.fonts] == [(INVENTED, False)]
    assert report.missing_glyphs == {}
    # Y se dibuja con Arial, con todas sus letras.
    assert [(name, missing) for _text, name, missing in texts] == [("arial.ttf", frozenset())]


def test_the_invented_family_draws_the_same_letters_as_arial(tmp_path: Path) -> None:
    """En la vista previa preparada (que instala las mismas fuentes que el PDF), el texto con la
    familia inventada tiene los mismos contornos que con el estilo en ``arial.ttf``."""

    def rings(path: Path) -> int:
        index = Preparation(
            ezdxf.readfile(path), boxes_target=SimpleNamespace(modelspace_boxes=None)
        ).build()
        assert index.texts == 1
        return int(index.span_start.size - 1)

    invented = rings(_source(tmp_path / "inventada.dxf", INVENTED))
    arial = rings(_source(tmp_path / "arial.dxf", None, font="arial.ttf"))
    assert invented == arial
    assert invented >= sum(1 for char in TEXT if not char.isspace())


def test_text_without_any_font_uses_the_fallback_not_any_installed_font(tmp_path: Path) -> None:
    """Red de seguridad: lo que no pide ninguna fuente (``FontFace()``, «sans-serif») se dibuja
    con la de respaldo, no con la instalada que mejor puntúe (``AcadEref.ttf`` con AutoCAD)."""
    document = ezdxf.readfile(_source(tmp_path / "sin_fuente.dxf", None))
    restore = _configured(document)
    try:
        assert fonts.find_best_match(family=DEFAULT_FAMILY).filename == "arial.ttf"
        texts = drawn_texts(document)
    finally:
        restore()
    assert [(name, missing) for _text, name, missing in texts] == [("arial.ttf", frozenset())]


# ----------------------------------------------------------------- DXF de referencia

_REFERENCES = [
    Path("data/input/referencia/prueba_erco_perfil.dxf"),
    Path("data/input/referencia/Plano_completo.dxf"),
    Path("data/input/referencia/VIA_2MA.dxf"),
    Path("C:/Users/LuisAngelNunezPadill/Downloads/PRGD-25-1365-CIV-PL-106_1_VA.dxf"),
]
# Letras que Arial no tiene y que sí son de esas fuentes (p. ej. el chino de «SimHei» en Perfil):
# salen con el aviso GLYPHS_MISSING. Lo que no puede pasar es que falten letras latinas.
_LATIN = frozenset(chr(code) for code in range(32, 0x250))


@pytest.mark.skipif(
    os.environ.get("CDE_REFERENCE_TESTS") != "1",
    reason="DXF grandes de data/input: solo con CDE_REFERENCE_TESTS=1",
)
@pytest.mark.parametrize("path", _REFERENCES, ids=lambda path: path.stem)
def test_every_text_of_the_references_has_its_letters(path: Path) -> None:
    if not path.is_file():
        pytest.skip(f"no está {path}")
    document = ezdxf.readfile(path)
    restore = _configured(document)
    try:
        texts = drawn_texts(document)
    finally:
        restore()
    assert texts
    assert not [text for text, name, _missing in texts if name.casefold() == "acaderef.ttf"]
    broken = [(text, name, sorted(missing)) for text, name, missing in texts if missing & _LATIN]
    assert broken == [], broken[:20]
