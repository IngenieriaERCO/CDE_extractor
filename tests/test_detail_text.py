import ast
from pathlib import Path

from cde_exporter.cad.detail_model import (
    Bounds3D,
    DetailSource,
    EntityCount,
    EntityDisposition,
    EntityStatus,
    ExtractedDetail,
    ExtractionResult,
    ExtractionWarning,
    FontUse,
    HatchDetail,
    HatchSummary,
    NestedBlockInfo,
    ResultStatus,
    ScaleInfo,
    SelectedReference,
    SelectionMethod,
    Severity,
    TransformationStep,
    UnitInfo,
    ValidationCheck,
)
from cde_exporter.cad.detail_text import format_detail_report


def _detail(**updates) -> ExtractedDetail:
    values = {
        "source": DetailSource("entrada.dxf", "Detalle"),
        "reference": SelectedReference(1, "A1", "0", (1, 2, 3), 0, (1, 1, 1)),
        "units": UnitInfo(4, "mm", "declaradas en el DXF y coherentes", 1.0, ()),
        "scale": None,
        "bounds": None,
        "inventory": (),
        "dispositions": (),
        "dependencies": (),
        "warnings": (),
    }
    values.update(updates)
    return ExtractedDetail(**values)


def _profile_detail() -> ExtractedDetail:
    fonts = (
        FontUse("Roboto", "arial.ttf", False, 41),
        FontUse("Vanilla Extract", "arial.ttf", False, 10),
        FontUse("Verdana", "arial.ttf", False, 4),
        FontUse("romans.shx", "arial.ttf", False, 27),
    )
    warnings = (
        ExtractionWarning(
            "ENTITIES_WITHOUT_BOUNDS",
            Severity.WARNING,
            "La extracción contiene entidades sin caja geométrica calculable.",
        ),
    )
    inventory = (
        EntityCount("LINE", "_000_Rótulo__General", EntityStatus.CONSERVED, 178),
        EntityCount("TEXT", "_000_Rótulo__General", EntityStatus.TRANSFORMED, 82),
        EntityCount("HATCH", "MATERIALES", EntityStatus.SIMPLIFIED, 1),
    )
    dispositions = (
        EntityDisposition(
            1,
            "B10",
            "HATCH",
            "MATERIALES",
            EntityStatus.SIMPLIFIED,
            "Simplificado por estimación de 522385 tramos.",
        ),
    )
    nested = (NestedBlockInfo("PC - PT", 2, 10),) + tuple(
        NestedBlockInfo(f"*U{index}", 1, 1) for index in range(1, 9)
    )
    result = ExtractionResult(
        "perfil_k0010.pdf",
        ResultStatus.COMPLETE_WITH_WARNINGS,
        (
            ValidationCheck("PDF legible", True, "El PDF existe y contiene páginas."),
            ValidationCheck("Una sola página", True, "El PDF contiene exactamente una página."),
            ValidationCheck(
                "Tamaño de página coincide con la escala",
                True,
                "Tamaño medido: 416.980 × 119.403 mm; tamaño esperado: 416.980 × 119.403 mm.",
            ),
        ),
    )
    return _detail(
        source=DetailSource("data/input/prueba_erco_perfil.dxf", "Perfil_K0010"),
        reference=SelectedReference(1, "9AE419", "_000_Rótulo__General", (0, 0, 0), 0, (1, 1, 1)),
        units=UnitInfo(6, "m", "declaradas en el DXF y coherentes", 1000.0, ()),
        scale=ScaleInfo(50, 20, (1, 1, 1)),
        bounds=Bounds3D((4924093.699, 2632176.190, 0), (4924114.048, 2632181.660, 0)),
        inventory=inventory,
        dispositions=dispositions,
        warnings=warnings,
        extracted_at="2026-10-23T12:00:00-05:00",
        exporter_version="0.1.0",
        block_definition_handle="9AE40E",
        selection_method=SelectionMethod.AUTOMATIC,
        detail_size=(20.349, 5.470),
        output_factor_mm_per_unit=20,
        content_size_mm=(406.980, 109.403),
        page_size_mm=(416.980, 119.403),
        margin_mm=5,
        physical_scale_guaranteed=True,
        transformations=(
            TransformationStep("insertion", "Inserción", (0, 0, 0)),
            TransformationStep("rotation", "Rotación", (0,)),
            TransformationStep("scale", "Escala uniforme", (1, 1, 1)),
        ),
        nested_blocks=nested,
        fonts=fonts,
        hatches=HatchSummary(
            21,
            1,
            (
                HatchDetail(
                    "MATERIALES",
                    "ANSI35",
                    0.005,
                    "simplified",
                    "Simplificado por estimación de 522385 tramos.",
                ),
            ),
        ),
        result=result,
    )


def test_complete_report_has_all_seven_sections_in_order() -> None:
    report = format_detail_report(_profile_detail())
    sections = [
        "A. IDENTIFICACIÓN DE LA EXTRACCIÓN",
        "B. IDENTIFICACIÓN DEL BLOQUE",
        "C. UNIDADES Y ESCALA",
        "D. TRANSFORMACIONES APLICADAS",
        "E. ENTIDADES CAD PROCESADAS",
        "F. FIDELIDAD GRÁFICA E INCIDENCIAS",
        "G. RESULTADO",
    ]

    assert [report.index(section) for section in sections] == sorted(
        report.index(section) for section in sections
    )


def test_minimal_old_model_is_safe_and_reports_unavailable_values() -> None:
    report = format_detail_report(_detail())

    assert "Escala nominal: no aplica" in report
    assert "Caja geométrica (mínimo a máximo): no disponible" in report
    assert "Resultado pendiente: el informe se generó antes de exportar" in report
    assert "no disponible" in report


def test_coordinates_use_three_fixed_decimals_without_scientific_notation() -> None:
    detail = _detail(
        bounds=Bounds3D((4921067.496522669, 0, 0), (4921068, 1, 1)),
    )

    report = format_detail_report(detail)

    assert "4921067.497" in report
    assert "4.921067e" not in report.lower()


def test_result_statuses_are_rendered_in_spanish() -> None:
    checks = (ValidationCheck("PDF legible", True, "Correcto."),)
    complete = _detail(result=ExtractionResult("a.pdf", ResultStatus.COMPLETE, checks))
    warned = _detail(result=ExtractionResult("a.pdf", ResultStatus.COMPLETE_WITH_WARNINGS, checks))

    assert "Estado final: COMPLETA" in format_detail_report(complete)
    assert "Estado final: COMPLETA CON ADVERTENCIAS" in format_detail_report(warned)


def test_unverified_physical_scale_includes_its_reason() -> None:
    detail = _detail(
        physical_scale_guaranteed=False,
        physical_scale_note="unidades sin verificar",
    )

    report = format_detail_report(detail)

    assert "Escala física: NO GARANTIZADA; motivo: unidades sin verificar" in report


def test_failed_validation_is_marked_as_failed() -> None:
    failed = ValidationCheck("Una sola página", False, "Se encontraron dos páginas.")
    report = format_detail_report(
        _detail(result=ExtractionResult("a.pdf", ResultStatus.COMPLETE_WITH_WARNINGS, (failed,)))
    )

    assert "[FALLÓ] Una sola página: Se encontraron dos páginas." in report


def test_large_nonconserved_inventory_is_truncated_after_thirty_groups() -> None:
    rows = tuple(
        EntityDisposition(index, None, f"TYPE{index:02}", "0", EntityStatus.TRANSFORMED, "motivo")
        for index in range(35)
    )
    report = format_detail_report(_detail(dispositions=rows))

    assert "… y 5 grupos más" in report
    assert "TYPE29" in report
    assert "TYPE30" not in report


def test_nested_anonymous_blocks_are_summarized_and_normal_name_is_listed() -> None:
    nested = (NestedBlockInfo("PC - PT", 2, 10),) + tuple(
        NestedBlockInfo(f"*U{index}", 1, 1) for index in range(1, 9)
    )
    report = format_detail_report(_detail(nested_blocks=nested))

    assert "Bloque anidado: PC - PT; profundidad 2; referencias 10" in report
    assert "Bloques anónimos: 8 (*U1, *U2, *U3, *U4, *U5, *U6, *U7, *U8)" in report


def test_report_is_deterministic_and_all_lines_fit_width() -> None:
    report = format_detail_report(_profile_detail())

    assert report == format_detail_report(_profile_detail())
    assert all(len(line) <= 100 for line in report.splitlines())


def test_windows_source_path_extracts_filename() -> None:
    detail = _detail(source=DetailSource(r"C:\\entrada\\perfil.dxf", "Detalle"))

    assert "Archivo DXF de origen: perfil.dxf" in format_detail_report(detail)


def test_detail_text_has_no_ezdxf_import() -> None:
    source = Path(__file__).parents[1] / "src" / "cde_exporter" / "cad" / "detail_text.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert "ezdxf" not in imported


def test_report_is_utf8_and_prints_k0010_profile(capsys) -> None:
    report = format_detail_report(_profile_detail())
    print(report)
    captured = capsys.readouterr().out

    assert "INFORME DE EXTRACCIÓN Y FIDELIDAD" in report
    report.encode("utf-8")
    assert "Perfil_K0010" in report
    assert "261" in report
    assert "522385 tramos" in report
    assert captured == report + "\n"


def test_nominal_scale_is_written_without_decimals() -> None:
    report = format_detail_report(_profile_detail())

    assert "Escala nominal: 1:50\n" in report
    assert "1:50.000" not in report


def test_each_section_has_blank_line_and_underline() -> None:
    report = format_detail_report(_profile_detail())
    title = "A. IDENTIFICACIÓN DE LA EXTRACCIÓN"

    assert f"\n\n{title}\n{'-' * len(title)}\n" in report


def test_rotation_is_written_in_degrees_and_units_in_words() -> None:
    report = format_detail_report(_profile_detail())

    assert "Rotación: 0.000°" in report
    assert "Unidades declaradas: metros (código 6)" in report


def test_known_warning_adds_advice_in_final_notes() -> None:
    report = " ".join(format_detail_report(_profile_detail()).split())

    assert "Revise que no falte contenido en los bordes del PDF." in report


def test_total_of_substituted_font_uses_is_reported() -> None:
    report = format_detail_report(_profile_detail())

    assert "Total de usos de fuentes sustituidas: 82" in report
