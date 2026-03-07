from __future__ import annotations

import json

import pytest

from easyeda2fusion.builders.normalizer import Normalizer
from easyeda2fusion.converter import ConversionConfig, Converter
from easyeda2fusion.matchers.library_loader import _entries_from_lbr_file
from easyeda2fusion.matchers.library_matcher import LibraryEntry, LibraryMatcher
from easyeda2fusion.model import ConversionMode, MatchMode
from easyeda2fusion.model import (
    Board,
    Component,
    Package,
    Pad,
    ParsedDocument,
    ParsedSource,
    Point,
    Project,
    SourceFormat,
    Track,
)
from easyeda2fusion.parsers import parse_easyeda_files


def test_full_project_conversion(fixtures_dir, tmp_path):
    output = tmp_path / "full_output"
    config = ConversionConfig(
        input_files=[fixtures_dir / "std_full_project.json"],
        output_dir=output,
        mode=ConversionMode.FULL,
        match_mode=MatchMode.AUTO,
    )

    result = Converter().run(config)
    assert result.summary["detected_source_type"] == "easyeda_std"
    assert result.summary["schematic_status"] == "converted"
    assert result.summary["board_status"] == "converted"

    assert (output / "manifests" / "normalized_project.json").exists()
    assert (output / "scripts" / "rebuild_schematic.scr").exists()
    assert (output / "scripts" / "rebuild_board.scr").exists()
    assert not (output / "scripts" / "rebuild_project.scr").exists()
    assert (output / "reports" / "validation_report.json").exists()
    assert (output / "conversion.log").exists()
    assert (output / "artifacts" / "eagle.epf").exists()
    assert (output / "artifacts" / "StdFullProject.sch").exists()
    assert (output / "artifacts" / "StdFullProject.brd").exists()


def test_converter_emits_progress_updates(fixtures_dir, tmp_path):
    output = tmp_path / "progress_output"
    empty_library_dir = tmp_path / "empty_library"
    empty_library_dir.mkdir(parents=True, exist_ok=True)
    config = ConversionConfig(
        input_files=[fixtures_dir / "std_board.json"],
        output_dir=output,
        mode=ConversionMode.BOARD_INFER_SCHEMATIC,
        match_mode=MatchMode.AUTO,
        library_path=empty_library_dir,
        use_default_fusion_libraries=False,
    )

    updates: list[tuple[int, str]] = []

    def _capture(percent: int, message: str) -> None:
        updates.append((int(percent), str(message)))

    Converter().run(config, progress=_capture)

    assert updates
    assert updates[0][0] == 0
    assert updates[-1][0] == 100
    assert all(updates[idx][0] <= updates[idx + 1][0] for idx in range(len(updates) - 1))
    assert any("Parsing EasyEDA input" in message for _, message in updates)
    assert any("Matching parts to libraries" in message for _, message in updates)
    assert any("Conversion complete" in message for _, message in updates)


def test_board_only_inferred_schematic_reconstruction(fixtures_dir, tmp_path):
    output = tmp_path / "board_infer_output"
    config = ConversionConfig(
        input_files=[fixtures_dir / "std_board.json"],
        output_dir=output,
        mode=ConversionMode.BOARD_INFER_SCHEMATIC,
        match_mode=MatchMode.AUTO,
    )

    result = Converter().run(config)
    assert result.summary["schematic_status"] == "inferred"

    inference_manifest = output / "manifests" / "inferred_schematic_manifest.json"
    assert inference_manifest.exists()
    payload = json.loads(inference_manifest.read_text(encoding="utf-8"))
    assert payload["inferred"] is True


def test_converter_defaults_schematic_layout_mode_to_board(fixtures_dir, tmp_path):
    output = tmp_path / "layout_mode_output"
    config = ConversionConfig(
        input_files=[fixtures_dir / "std_board.json"],
        output_dir=output,
        mode=ConversionMode.BOARD_INFER_SCHEMATIC,
        match_mode=MatchMode.AUTO,
    )

    Converter().run(config)
    manifest = output / "manifests" / "normalized_project.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload.get("metadata", {}).get("schematic_layout_mode") == "board"


def test_converter_validation_includes_schematic_organization_metrics(fixtures_dir, tmp_path):
    output = tmp_path / "layout_metrics_output"
    config = ConversionConfig(
        input_files=[fixtures_dir / "std_board.json"],
        output_dir=output,
        mode=ConversionMode.BOARD_INFER_SCHEMATIC,
        match_mode=MatchMode.AUTO,
        schematic_layout_mode="human",
    )

    Converter().run(config)
    payload = json.loads((output / "reports" / "validation_report.json").read_text(encoding="utf-8"))
    metrics = payload.get("metrics", {})
    assert "schematic_organization_block_count" in metrics
    assert "schematic_organization_crossing_risk_score" in metrics
    assert "schematic_organization_overlap_count" in metrics
    assert "schematic_organization_orphan_label_count" in metrics


def test_ambiguous_part_matching(fixtures_dir):
    parsed = parse_easyeda_files([fixtures_dir / "ambiguous_part.json"])
    project = Normalizer().normalize(parsed).project

    entries = [
        LibraryEntry(
            device_name="GENERIC_R_0603_A",
            package_name="0603",
            symbol_name="R",
            component_class="resistor",
        ),
        LibraryEntry(
            device_name="GENERIC_R_0603_B",
            package_name="0603",
            symbol_name="R",
            component_class="resistor",
        ),
    ]

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.AUTO)
    assert ctx.summary.ambiguous == 1
    assert ctx.summary.unresolved == 0
    assert ctx.summary.created_new_parts == 1
    assert project.library_matches[0].matched is True
    assert project.library_matches[0].created_new_part is True


def test_missing_library_generation(fixtures_dir, tmp_path):
    output = tmp_path / "missing_lib_output"
    empty_library_dir = tmp_path / "empty_library"
    empty_library_dir.mkdir(parents=True, exist_ok=True)
    config = ConversionConfig(
        input_files=[fixtures_dir / "missing_library_generation.json"],
        output_dir=output,
        mode=ConversionMode.FULL,
        match_mode=MatchMode.AUTO,
        library_path=empty_library_dir,
        use_default_fusion_libraries=False,
    )

    result = Converter().run(config)
    assert result.summary["new_library_parts_created"] >= 1

    lib_manifest = output / "library" / "library_manifest.json"
    payload = json.loads(lib_manifest.read_text(encoding="utf-8"))
    assert len(payload["generated_parts"]) >= 1


def test_unresolved_part_reporting(fixtures_dir, tmp_path):
    output = tmp_path / "unresolved_output"
    config = ConversionConfig(
        input_files=[fixtures_dir / "unresolved_part.json"],
        output_dir=output,
        mode=ConversionMode.FULL,
        match_mode=MatchMode.STRICT,
    )

    result = Converter().run(config)
    assert result.summary["unresolved_parts"] >= 1

    unresolved_csv = output / "reports" / "unresolved_parts.csv"
    content = unresolved_csv.read_text(encoding="utf-8")
    assert "U1" in content


def test_multilayer_board_layer_mapping(fixtures_dir):
    parsed = parse_easyeda_files([fixtures_dir / "pro_multilayer_board.json"])
    normalization = Normalizer().normalize(parsed)

    mapped = {layer.mapped_name for layer in normalization.project.layers}
    assert "inner1_copper" in mapped
    assert "inner2_copper" in mapped
    assert any(layer.lossy for layer in normalization.project.layers)

    text = normalization.layer_report.as_text()
    assert "LOSSY" in text


def test_input_directory_without_json_errors(tmp_path):
    output = tmp_path / "out"
    empty_dir = tmp_path / "empty_input_dir"
    empty_dir.mkdir()

    config = ConversionConfig(
        input_files=[empty_dir],
        output_dir=output,
        mode=ConversionMode.FULL,
        match_mode=MatchMode.AUTO,
    )

    with pytest.raises(ValueError, match="no .json files"):
        Converter().run(config)


def test_passive_prefers_external_library_when_package_matches(fixtures_dir):
    parsed = parse_easyeda_files([fixtures_dir / "ambiguous_part.json"])
    project = Normalizer().normalize(parsed).project

    entries = [
        LibraryEntry(
            device_name="GENERIC_R_0603",
            package_name="0603",
            symbol_name="R",
            component_class="resistor",
        ),
        LibraryEntry(
            device_name="R0603",
            package_name="R0603",
            symbol_name="R",
            component_class="resistor",
            library_name="rcl",
            add_token="rcl:R0603",
            library_path=r"C:\libs\rcl.lbr",
        ),
    ]

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.PACKAGE_FIRST)
    assert ctx.summary.auto_matched == 1
    assert ctx.summary.unresolved == 0
    assert project.components[0].device_id == "rcl:R0603"
    assert r"C:\libs\rcl.lbr" in ctx.used_external_library_paths


def test_passive_prefers_user_selected_library_path_when_candidates_tie() -> None:
    project = Project(
        project_id="p_pref_lib",
        name="p_pref_lib",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="R1",
                value="10k",
                source_name="R0603",
                package_id="0603",
                at=Point(0.0, 0.0),
            )
        ],
    )

    entries = [
        LibraryEntry(
            device_name="R-US_R0603",
            package_name="0603",
            symbol_name="R",
            component_class="resistor",
            library_name="rcl_a",
            add_token="rcl_a:R-US_R0603",
            library_path=r"C:\libs\rcl_a.lbr",
        ),
        LibraryEntry(
            device_name="R-US_R0603",
            package_name="0603",
            symbol_name="R",
            component_class="resistor",
            library_name="rcl_b",
            add_token="rcl_b:R-US_R0603",
            library_path=r"C:\libs\rcl_b.lbr",
        ),
    ]

    ctx = LibraryMatcher().match(
        project,
        entries,
        match_mode=MatchMode.PACKAGE_FIRST,
        preferred_library_paths_by_class={"resistor": {r"C:\libs\rcl_b.lbr"}},
    )
    assert ctx.summary.auto_matched == 1
    assert project.components[0].device_id == "rcl_b:R-US_R0603"


def test_normalizer_does_not_merge_refdes_when_identity_conflicts() -> None:
    parsed = ParsedSource(
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=["fixture.json"],
        documents=[
            ParsedDocument(
                doc_type="board",
                name="pcb",
                raw_objects=[
                    {
                        "type": "component",
                        "id": "inst_board",
                        "refdes": "R5",
                        "source_name": "AXIAL",
                        "package": "R_AXIAL",
                        "x": 10.0,
                        "y": 10.0,
                    }
                ],
            ),
            ParsedDocument(
                doc_type="schematic",
                name="sch",
                raw_objects=[
                    {
                        "type": "component",
                        # Deliberately missing source_instance_id to simulate partial source data.
                        "refdes": "R5",
                        "source_name": "R0805",
                        "package": "R0805",
                        "x": 20.0,
                        "y": 20.0,
                    }
                ],
            ),
        ],
        metadata={"unit": "mm"},
    )

    project = Normalizer().normalize(parsed).project
    refs = [component.refdes for component in project.components]
    assert refs.count("R5") == 2


def test_normalizer_preserves_symbol_origin_metadata_for_anchor_rotation() -> None:
    parsed = ParsedSource(
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=["fixture.json"],
        documents=[
            ParsedDocument(
                doc_type="schematic",
                name="sch",
                raw_objects=[
                    {
                        "type": "symbol",
                        "id": "SYM1",
                        "name": "SYM1",
                        "origin_x": 10.0,
                        "origin_y": -5.0,
                        "pins": [
                            {"number": "1", "name": "P1", "x": 20.0, "y": 0.0},
                        ],
                    }
                ],
            )
        ],
        metadata={"unit": "mm"},
    )

    project = Normalizer().normalize(parsed).project
    symbol = next(item for item in project.symbols if item.symbol_id == "SYM1")
    origin_markers = [
        graphic
        for graphic in symbol.graphics
        if isinstance(graphic, dict) and str(graphic.get("kind", "")).lower() == "origin"
    ]
    assert len(origin_markers) == 1
    assert float(origin_markers[0].get("x_mm", 0.0)) == 10.0
    assert float(origin_markers[0].get("y_mm", 0.0)) == -5.0


def test_non_passive_part_number_requires_package_match(fixtures_dir):
    parsed = parse_easyeda_files([fixtures_dir / "std_full_project.json"])
    project = Normalizer().normalize(parsed).project
    target = next(component for component in project.components if component.refdes == "U1")
    target.mpn = None
    target.source_name = "STM32F030K6T6"
    target.package_id = "LQFP-48"

    entries = [
        LibraryEntry(
            device_name="STM32F030K6T6",
            package_name="QFN-32",
            symbol_name="U",
            component_class="ic",
            add_token="st:STM32F030K6T6",
        )
    ]

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.PACKAGE_FIRST)
    assert ctx.summary.auto_matched == 0
    match = next(item for item in project.library_matches if item.refdes == "U1")
    assert match.stage == "stage5_unresolved"
    assert match.reason == "insufficient_package_geometry"


def test_non_passive_part_number_match_splits_combined_part_and_package_token() -> None:
    project = Project(
        project_id="p_part_pkg_combo_match",
        name="p_part_pkg_combo_match",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="STM32F030K6T6LQFP48",
                package_id=None,
                at=Point(0.0, 0.0),
            )
        ],
    )

    entries = [
        LibraryEntry(
            device_name="STM32F030K6T6",
            package_name="LQFP-48",
            symbol_name="U",
            component_class="ic",
            add_token="st:STM32F030K6T6",
        )
    ]

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.PACKAGE_FIRST)
    assert ctx.summary.auto_matched == 1
    assert project.components[0].device_id == "st:STM32F030K6T6"
    assert project.components[0].package_id == "LQFP-48"


def test_non_passive_part_number_combined_field_still_requires_package_match() -> None:
    project = Project(
        project_id="p_part_pkg_combo_mismatch",
        name="p_part_pkg_combo_mismatch",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="STM32F030K6T6LQFP48",
                package_id=None,
                at=Point(0.0, 0.0),
            )
        ],
    )

    entries = [
        LibraryEntry(
            device_name="STM32F030K6T6",
            package_name="QFN-32",
            symbol_name="U",
            component_class="ic",
            add_token="st:STM32F030K6T6",
        )
    ]

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.PACKAGE_FIRST)
    assert ctx.summary.auto_matched == 0
    assert ctx.summary.unresolved == 1
    match = next(item for item in project.library_matches if item.refdes == "U1")
    assert match.stage == "stage5_unresolved"


def test_non_passive_part_number_partial_match_max98357_with_package_guard() -> None:
    project = Project(
        project_id="p_part_partial_max98357",
        name="p_part_partial_max98357",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="MAX98357AETE+T",
                package_id=None,
                attributes={"Footprint": "TQFN-16_EP-4.0X4.0X0.8"},
                at=Point(0.0, 0.0),
            )
        ],
    )

    entries = [
        LibraryEntry(
            device_name="MAX98357AETE+",
            package_name="TQFN-16",
            symbol_name="AMP",
            component_class="ic",
            add_token="maxim:MAX98357AETE+",
        )
    ]

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.PACKAGE_FIRST)
    assert ctx.summary.auto_matched == 1
    assert project.components[0].device_id == "maxim:MAX98357AETE+"


def test_non_passive_part_number_partial_match_lmr14020_with_hsop_package() -> None:
    project = Project(
        project_id="p_part_partial_lmr14020",
        name="p_part_partial_lmr14020",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="U2",
                value="",
                source_name="LMR14020SDDAR",
                package_id=None,
                attributes={"Footprint": "HSOP-8_LMR14020"},
                at=Point(0.0, 0.0),
            )
        ],
    )

    entries = [
        LibraryEntry(
            device_name="LMR14020",
            package_name="HSOP-8",
            symbol_name="REGULATOR",
            component_class="ic",
            add_token="ti:LMR14020",
        )
    ]

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.PACKAGE_FIRST)
    assert ctx.summary.auto_matched == 1
    assert project.components[0].device_id == "ti:LMR14020"


def test_non_passive_part_number_partial_match_s8050_with_package_guard() -> None:
    project = Project(
        project_id="p_part_partial_s8050",
        name="p_part_partial_s8050",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="Q1",
                value="",
                source_name="S8050M-D",
                package_id=None,
                attributes={"Footprint": "SOT-23"},
                at=Point(0.0, 0.0),
            )
        ],
    )

    entries = [
        LibraryEntry(
            device_name="S8050",
            package_name="SOT-23",
            symbol_name="NPN",
            component_class="transistor",
            add_token="transistor-bjt:S8050",
        )
    ]

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.PACKAGE_FIRST)
    assert ctx.summary.auto_matched == 1
    assert project.components[0].device_id == "transistor-bjt:S8050"


def test_non_passive_partial_part_number_still_requires_package_match() -> None:
    project = Project(
        project_id="p_part_partial_pkg_guard",
        name="p_part_partial_pkg_guard",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="U3",
                value="",
                source_name="LMR14020SDDAR",
                package_id=None,
                attributes={"Footprint": "HSOP-8_LMR14020"},
                at=Point(0.0, 0.0),
            )
        ],
    )

    entries = [
        LibraryEntry(
            device_name="LMR14020",
            package_name="SOT-23",
            symbol_name="REGULATOR",
            component_class="ic",
            add_token="ti:LMR14020_SOT23",
        )
    ]

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.PACKAGE_FIRST)
    assert ctx.summary.auto_matched == 0
    assert project.components[0].device_id != "ti:LMR14020_SOT23"


def test_non_passive_part_number_partial_match_accepts_encoded_qfn_package() -> None:
    project = Project(
        project_id="p_part_partial_max98357_encoded_pkg",
        name="p_part_partial_max98357_encoded_pkg",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="TQFN-16_L3.0-W3.0-P0.50-BL-EP1.5",
                mpn="MAX98357AETE+T",
                package_id=None,
                attributes={"Footprint": "TQFN-16_L3.0-W3.0-P0.50-BL-EP1.5"},
                at=Point(0.0, 0.0),
            )
        ],
    )
    entries = [
        LibraryEntry(
            device_name="MAX98357AETE+",
            package_name="QFN50P300X300X80-17N",
            symbol_name="MAX98357AETE+",
            component_class="ic",
            mpn="MAX98357AETE+",
            add_token="samacsys_parts:MAX98357AETE+",
        )
    ]
    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.PACKAGE_FIRST)
    assert ctx.summary.auto_matched == 1
    assert project.components[0].device_id == "samacsys_parts:MAX98357AETE+"


def test_non_passive_part_number_partial_match_accepts_encoded_soic_package() -> None:
    project = Project(
        project_id="p_part_partial_lmr_encoded_pkg",
        name="p_part_partial_lmr_encoded_pkg",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="U6",
                value="",
                source_name="HSOP-8_L5.0-W4.0-P1.27-LS6.2-BL-EP",
                mpn="LMR14020SDDAR",
                package_id=None,
                attributes={"Footprint": "HSOP-8_L5.0-W4.0-P1.27-LS6.2-BL-EP"},
                at=Point(0.0, 0.0),
            )
        ],
    )
    entries = [
        LibraryEntry(
            device_name="LMR14020SDDA",
            package_name="SOIC127P600X170-9N",
            symbol_name="LMR14020SDDA",
            component_class="ic",
            mpn="LMR14020SDDA",
            add_token="samacsys_parts:LMR14020SDDA",
        )
    ]
    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.PACKAGE_FIRST)
    assert ctx.summary.auto_matched == 1
    assert project.components[0].device_id == "samacsys_parts:LMR14020SDDA"


def test_non_passive_s8050_with_vendor_suffix_selects_external_candidate_without_prompt() -> None:
    project = Project(
        project_id="p_s8050_hy3d",
        name="p_s8050_hy3d",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="U23",
                value="SOT-23-3_L3.0-W1.7-P0.95-LS2.9-BR",
                source_name="SOT-23-3_L3.0-W1.7-P0.95-LS2.9-BR",
                mpn="S8050M-D HY3D",
                package_id=None,
                attributes={"Footprint": "SOT-23-3_L3.0-W1.7-P0.95-LS2.9-BR"},
                at=Point(0.0, 0.0),
            )
        ],
    )
    entries = [
        LibraryEntry(
            device_name="SMD-TRANSISTORS-NPN-25V-500MW-S8050(SOT-23)",
            package_name="SOT-23",
            symbol_name="S8050",
            component_class="transistor",
            mpn="S8050",
            add_token="opl:S8050_A",
        ),
        LibraryEntry(
            device_name="SMD-TRANSISTORS-NPN-25V-500MW-S8050(SOT-23)",
            package_name="SOT-23",
            symbol_name="S8050",
            component_class="transistor",
            mpn="S8050",
            add_token="opl:S8050_B",
        ),
    ]
    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.PACKAGE_FIRST)
    assert ctx.summary.auto_matched == 1
    assert project.components[0].device_id in {"opl:S8050_A", "opl:S8050_B"}


def test_non_passive_partial_match_with_large_noise_library_pool() -> None:
    project = Project(
        project_id="p_part_partial_large_noise_pool",
        name="p_part_partial_large_noise_pool",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="U6",
                value="",
                source_name="LMR14020SDDAR",
                package_id=None,
                attributes={"Footprint": "HSOP-8_L5.0-W4.0-P1.27-LS6.2-BL-EP"},
                at=Point(0.0, 0.0),
            )
        ],
    )

    entries: list[LibraryEntry] = []
    for idx in range(300):
        entries.append(
            LibraryEntry(
                device_name=f"DUMMYIC{idx:04d}QFN",
                package_name="QFN-32",
                symbol_name=f"DUMMY{idx:04d}",
                component_class="ic",
                add_token=f"dummy_lib:DUMMYIC{idx:04d}QFN",
            )
        )

    entries.append(
        LibraryEntry(
            device_name="LMR14020SDDA",
            package_name="SOIC127P600X170-9N",
            symbol_name="LMR14020SDDA",
            component_class="ic",
            mpn="LMR14020SDDA",
            add_token="samacsys_parts:LMR14020SDDA",
        )
    )

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.PACKAGE_FIRST)
    assert ctx.summary.auto_matched == 1
    assert project.components[0].device_id == "samacsys_parts:LMR14020SDDA"


def test_lbr_loader_parses_named_html_entities_in_old_eagle_files(tmp_path) -> None:
    lbr = tmp_path / "SamacSys_Parts.lbr"
    lbr.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE eagle SYSTEM "eagle.dtd">
<eagle version="7.7.0">
  <drawing>
    <library>
      <packages>
        <package name="QFN50P300X300X80-17N"/>
      </packages>
      <symbols>
        <symbol name="MAX98357AETE+"/>
      </symbols>
      <devicesets>
        <deviceset name="MAX98357AETE+" prefix="IC">
          <description>Size 3 &times; 3</description>
          <gates>
            <gate name="G$1" symbol="MAX98357AETE+" x="0" y="0"/>
          </gates>
          <devices>
            <device name="" package="QFN50P300X300X80-17N">
              <technologies>
                <technology name="">
                  <attribute name="MANUFACTURER_PART_NUMBER" value="MAX98357AETE+"/>
                </technology>
              </technologies>
            </device>
          </devices>
        </deviceset>
      </devicesets>
    </library>
  </drawing>
</eagle>
""",
        encoding="utf-8",
    )
    entries = _entries_from_lbr_file(lbr)
    assert entries
    assert any(item.device_name == "MAX98357AETE+" for item in entries)


def test_non_passive_stage2_package_class_uses_package_hints_not_only_package_id():
    project = Project(
        project_id="p_stage2_mosfet",
        name="p_stage2_mosfet",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="U26",
                value="",
                source_name="MOSFET",
                package_id="opaque_pkg_uuid",
                attributes={"package_name": "SOT-23"},
                at=Point(0.0, 0.0),
            )
        ],
        packages=[
            Package(
                package_id="opaque_pkg_uuid",
                name="SOT-23",
                pads=[
                    Pad(pad_number="1", at=Point(-0.95, 0.95), shape="rect", width_mm=0.5, height_mm=0.6),
                    Pad(pad_number="2", at=Point(-0.95, -0.95), shape="rect", width_mm=0.5, height_mm=0.6),
                    Pad(pad_number="3", at=Point(0.95, 0.0), shape="rect", width_mm=0.5, height_mm=0.6),
                ],
            )
        ],
    )

    entries = [
        LibraryEntry(
            device_name="MOSFET-N-SOT23",
            package_name="SOT-23",
            symbol_name="MOSFET-N",
            component_class="mosfet",
            add_token="transistor-fet:BSH105",
        )
    ]

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.PACKAGE_FIRST)
    assert ctx.summary.auto_matched == 1
    assert project.components[0].device_id == "transistor-fet:BSH105"


def test_external_match_falls_back_when_package_geometry_mismatches(tmp_path):
    external_lbr = tmp_path / "rcl_mismatch.lbr"
    external_lbr.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<eagle version="9.6.2">
  <drawing>
    <library>
      <packages>
        <package name="R0603">
          <smd name="1" x="-1.5000" y="0" dx="0.7" dy="0.9" layer="1"/>
          <smd name="2" x="1.5000" y="0" dx="0.7" dy="0.9" layer="1"/>
        </package>
      </packages>
      <symbols>
        <symbol name="R"><pin name="1" x="-2.54" y="0"/><pin name="2" x="2.54" y="0"/></symbol>
      </symbols>
      <devicesets>
        <deviceset name="R-US_R0603">
          <gates><gate name="G$1" symbol="R" x="0" y="0"/></gates>
          <devices>
            <device name="" package="R0603">
              <connects>
                <connect gate="G$1" pin="1" pad="1"/>
                <connect gate="G$1" pin="2" pad="2"/>
              </connects>
              <technologies><technology name=""/></technologies>
            </device>
          </devices>
        </deviceset>
      </devicesets>
    </library>
  </drawing>
</eagle>
""",
        encoding="utf-8",
    )

    project = Project(
        project_id="p1",
        name="p1",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(
                refdes="R1",
                value="10k",
                source_name="RES",
                package_id="R0603",
                at=Point(0.0, 0.0),
            )
        ],
        packages=[
            Package(
                package_id="R0603",
                name="R0603",
                pads=[
                    Pad(pad_number="1", at=Point(-0.75, 0.0), shape="rect", width_mm=0.8, height_mm=0.9, layer="top_copper"),
                    Pad(pad_number="2", at=Point(0.75, 0.0), shape="rect", width_mm=0.8, height_mm=0.9, layer="top_copper"),
                ],
            )
        ],
    )

    entries = [
        LibraryEntry(
            device_name="R-US_R0603",
            package_name="R0603",
            symbol_name="R",
            component_class="resistor",
            library_name="rcl",
            add_token="rcl:R-US_R0603",
            library_path=str(external_lbr),
        )
    ]

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.AUTO)
    assert ctx.summary.auto_matched == 0
    assert ctx.summary.created_new_parts == 1
    assert project.components[0].device_id is not None
    assert project.components[0].device_id.startswith("easyeda_generated:")


def test_passive_external_match_with_board_accepts_origin_only_mismatch(tmp_path):
    external_lbr = tmp_path / "rcl_relaxed.lbr"
    external_lbr.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<eagle version="9.6.2">
  <drawing>
    <library>
      <packages>
        <package name="R0603">
          <smd name="1" x="-0.85" y="0" dx="1.0" dy="1.1" layer="1"/>
          <smd name="2" x="0.85" y="0" dx="1.0" dy="1.1" layer="1"/>
        </package>
      </packages>
      <symbols>
        <symbol name="R"><pin name="1" x="-5.08" y="0"/><pin name="2" x="5.08" y="0"/></symbol>
      </symbols>
      <devicesets>
        <deviceset name="R-US_">
          <gates><gate name="G$1" symbol="R" x="0" y="0"/></gates>
          <devices>
            <device name="R0603" package="R0603">
              <connects>
                <connect gate="G$1" pin="1" pad="1"/>
                <connect gate="G$1" pin="2" pad="2"/>
              </connects>
              <technologies><technology name=""/></technologies>
            </device>
          </devices>
        </deviceset>
      </devicesets>
    </library>
  </drawing>
</eagle>
""",
        encoding="utf-8",
    )

    project = Project(
        project_id="p_board_fidelity",
        name="p_board_fidelity",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="R1",
                value="10k",
                source_name="R0603",
                package_id="R0603",
                at=Point(0.0, 0.0),
            )
        ],
        packages=[
            Package(
                package_id="R0603",
                name="R0603",
                pads=[
                    Pad(pad_number="1", at=Point(-0.753364, 0.0), shape="rect", width_mm=0.806, height_mm=0.864, layer="top_copper"),
                    Pad(pad_number="2", at=Point(0.753364, 0.0), shape="rect", width_mm=0.806, height_mm=0.864, layer="top_copper"),
                ],
            )
        ],
        board=Board(),
    )

    entries = [
        LibraryEntry(
            device_name="R-US_R0603",
            package_name="R0603",
            symbol_name="R",
            component_class="resistor",
            library_name="rcl",
            add_token="rcl:R-US_R0603",
            library_path=str(external_lbr),
        )
    ]

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.AUTO)
    assert ctx.summary.auto_matched == 1
    assert ctx.summary.created_new_parts == 0
    assert project.components[0].device_id == "rcl:R-US_R0603"
    relaxed_events = [
        event
        for event in project.events
        if event.code == "EXTERNAL_PACKAGE_GEOMETRY_RELAXED"
    ]
    assert len(relaxed_events) == 1
    assert "relaxed_origin_only:pad_origin_mismatch" in str(relaxed_events[0].context.get("reason", ""))


def test_passive_external_match_with_board_still_falls_back_on_pitch_mismatch(tmp_path):
    external_lbr = tmp_path / "rcl_bad_pitch.lbr"
    external_lbr.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<eagle version="9.6.2">
  <drawing>
    <library>
      <packages>
        <package name="R0603">
          <smd name="1" x="-1.5000" y="0" dx="0.7" dy="0.9" layer="1"/>
          <smd name="2" x="1.5000" y="0" dx="0.7" dy="0.9" layer="1"/>
        </package>
      </packages>
      <symbols>
        <symbol name="R"><pin name="1" x="-2.54" y="0"/><pin name="2" x="2.54" y="0"/></symbol>
      </symbols>
      <devicesets>
        <deviceset name="R-US_">
          <gates><gate name="G$1" symbol="R" x="0" y="0"/></gates>
          <devices>
            <device name="R0603" package="R0603">
              <connects>
                <connect gate="G$1" pin="1" pad="1"/>
                <connect gate="G$1" pin="2" pad="2"/>
              </connects>
              <technologies><technology name=""/></technologies>
            </device>
          </devices>
        </deviceset>
      </devicesets>
    </library>
  </drawing>
</eagle>
""",
        encoding="utf-8",
    )

    project = Project(
        project_id="p_board_bad_pitch",
        name="p_board_bad_pitch",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="R1",
                value="10k",
                source_name="R0603",
                package_id="R0603",
                at=Point(0.0, 0.0),
            )
        ],
        packages=[
            Package(
                package_id="R0603",
                name="R0603",
                pads=[
                    Pad(pad_number="1", at=Point(-0.753364, 0.0), shape="rect", width_mm=0.806, height_mm=0.864, layer="top_copper"),
                    Pad(pad_number="2", at=Point(0.753364, 0.0), shape="rect", width_mm=0.806, height_mm=0.864, layer="top_copper"),
                ],
            )
        ],
        board=Board(),
    )

    entries = [
        LibraryEntry(
            device_name="R-US_R0603",
            package_name="R0603",
            symbol_name="R",
            component_class="resistor",
            library_name="rcl",
            add_token="rcl:R-US_R0603",
            library_path=str(external_lbr),
        )
    ]

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.AUTO)
    assert ctx.summary.auto_matched == 0
    assert ctx.summary.created_new_parts == 1
    assert project.components[0].device_id is not None
    assert project.components[0].device_id.startswith("easyeda_generated:")


def test_external_match_with_board_accepts_rotated_origin_mismatch(tmp_path):
    external_lbr = tmp_path / "ic_rotated.lbr"
    external_lbr.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<eagle version="9.6.2">
  <drawing>
    <library>
      <packages>
        <package name="QFN4">
          <smd name="1" x="-1.0" y="1.0" dx="0.5" dy="0.5" layer="1"/>
          <smd name="2" x="-1.0" y="-1.0" dx="0.5" dy="0.5" layer="1"/>
          <smd name="3" x="1.0" y="-1.0" dx="0.5" dy="0.5" layer="1"/>
          <smd name="4" x="1.0" y="1.0" dx="0.5" dy="0.5" layer="1"/>
        </package>
      </packages>
      <symbols>
        <symbol name="U">
          <pin name="1" x="-2.54" y="2.54"/>
          <pin name="2" x="-2.54" y="-2.54"/>
          <pin name="3" x="2.54" y="-2.54"/>
          <pin name="4" x="2.54" y="2.54"/>
        </symbol>
      </symbols>
      <devicesets>
        <deviceset name="U4">
          <gates><gate name="G$1" symbol="U" x="0" y="0"/></gates>
          <devices>
            <device name="" package="QFN4">
              <connects>
                <connect gate="G$1" pin="1" pad="1"/>
                <connect gate="G$1" pin="2" pad="2"/>
                <connect gate="G$1" pin="3" pad="3"/>
                <connect gate="G$1" pin="4" pad="4"/>
              </connects>
              <technologies><technology name=""/></technologies>
            </device>
          </devices>
        </deviceset>
      </devicesets>
    </library>
  </drawing>
</eagle>
""",
        encoding="utf-8",
    )

    project = Project(
        project_id="p_board_rotated_origin",
        name="p_board_rotated_origin",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U4",
                package_id="QFN4",
                at=Point(0.0, 0.0),
            )
        ],
        packages=[
            Package(
                package_id="QFN4",
                name="QFN4",
                pads=[
                    Pad(pad_number="1", at=Point(-1.0, -1.0), shape="rect", width_mm=0.5, height_mm=0.5, layer="top_copper"),
                    Pad(pad_number="2", at=Point(1.0, -1.0), shape="rect", width_mm=0.5, height_mm=0.5, layer="top_copper"),
                    Pad(pad_number="3", at=Point(1.0, 1.0), shape="rect", width_mm=0.5, height_mm=0.5, layer="top_copper"),
                    Pad(pad_number="4", at=Point(-1.0, 1.0), shape="rect", width_mm=0.5, height_mm=0.5, layer="top_copper"),
                ],
            )
        ],
        board=Board(),
    )

    entries = [
        LibraryEntry(
            device_name="U4",
            package_name="QFN4",
            symbol_name="U",
            component_class="ic",
            library_name="iclib",
            add_token="iclib:U4",
            library_path=str(external_lbr),
        )
    ]

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.AUTO)
    assert ctx.summary.auto_matched == 1
    assert ctx.summary.created_new_parts == 0
    assert project.components[0].device_id == "iclib:U4"
    assert float(project.components[0].attributes.get("_external_rotation_offset_deg", 0.0)) in {90.0, 270.0}
    relaxed_events = [
        event
        for event in project.events
        if event.code == "EXTERNAL_PACKAGE_GEOMETRY_RELAXED"
    ]
    assert len(relaxed_events) == 1
    assert "relaxed_origin_only:pad_origin_mismatch" in str(relaxed_events[0].context.get("reason", ""))


def test_passive_external_match_with_board_accepts_origin_only_mismatch_for_std(tmp_path):
    external_lbr = tmp_path / "rcl_relaxed_std.lbr"
    external_lbr.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<eagle version="9.6.2">
  <drawing>
    <library>
      <packages>
        <package name="R0603">
          <smd name="1" x="-0.85" y="0" dx="1.0" dy="1.1" layer="1"/>
          <smd name="2" x="0.85" y="0" dx="1.0" dy="1.1" layer="1"/>
        </package>
      </packages>
      <symbols>
        <symbol name="R"><pin name="1" x="-5.08" y="0"/><pin name="2" x="5.08" y="0"/></symbol>
      </symbols>
      <devicesets>
        <deviceset name="R-US_">
          <gates><gate name="G$1" symbol="R" x="0" y="0"/></gates>
          <devices>
            <device name="R0603" package="R0603">
              <connects>
                <connect gate="G$1" pin="1" pad="1"/>
                <connect gate="G$1" pin="2" pad="2"/>
              </connects>
              <technologies><technology name=""/></technologies>
            </device>
          </devices>
        </deviceset>
      </devicesets>
    </library>
  </drawing>
</eagle>
""",
        encoding="utf-8",
    )

    project = Project(
        project_id="p_board_fidelity_std",
        name="p_board_fidelity_std",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(
                refdes="R1",
                value="10k",
                source_name="R0603",
                package_id="R0603",
                at=Point(0.0, 0.0),
            )
        ],
        packages=[
            Package(
                package_id="R0603",
                name="R0603",
                pads=[
                    Pad(pad_number="1", at=Point(-0.753364, 0.0), shape="rect", width_mm=0.806, height_mm=0.864, layer="top_copper"),
                    Pad(pad_number="2", at=Point(0.753364, 0.0), shape="rect", width_mm=0.806, height_mm=0.864, layer="top_copper"),
                ],
            )
        ],
        board=Board(),
    )

    entries = [
        LibraryEntry(
            device_name="R-US_R0603",
            package_name="R0603",
            symbol_name="R",
            component_class="resistor",
            library_name="rcl",
            add_token="rcl:R-US_R0603",
            library_path=str(external_lbr),
        )
    ]

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.AUTO)
    assert ctx.summary.auto_matched == 1
    assert ctx.summary.created_new_parts == 0
    assert project.components[0].device_id == "rcl:R-US_R0603"
    assert any(event.code == "EXTERNAL_PACKAGE_GEOMETRY_RELAXED" for event in project.events)


def test_screw_terminal_matching_prefers_pitch_and_pin_count() -> None:
    project = Project(
        project_id="p1",
        name="p1",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="CN1",
                value="",
                source_name="SCREWTERMINAL-3.5MM-3",
                package_id=None,
                at=Point(0.0, 0.0),
            )
        ],
    )

    entries = [
        LibraryEntry(
            device_name="SCREWTERMINAL-3.5MM-2",
            package_name="SCREWTERMINAL-3.5MM-2",
            symbol_name="CONN_2",
            component_class="connector",
            add_token="con-lstb:SCREWTERMINAL-3.5MM-2",
        ),
        LibraryEntry(
            device_name="SCREWTERMINAL-3.5MM-3",
            package_name="SCREWTERMINAL-3.5MM-3",
            symbol_name="CONN_3",
            component_class="connector",
            add_token="con-lstb:SCREWTERMINAL-3.5MM-3",
        ),
    ]

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.PACKAGE_FIRST)
    assert ctx.summary.auto_matched == 1
    assert project.components[0].device_id == "con-lstb:SCREWTERMINAL-3.5MM-3"


def test_screw_terminal_matching_collapses_equivalent_best_candidates() -> None:
    project = Project(
        project_id="p_screw_eq",
        name="p_screw_eq",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="CN2",
                value="",
                source_name="SCREWTERMINAL-3.5MM-2",
                package_id=None,
                at=Point(0.0, 0.0),
            )
        ],
    )

    entries = [
        LibraryEntry(
            device_name="CONN_023.5MM",
            package_name="SCREWTERMINAL-3.5MM-2",
            symbol_name="CONN_2",
            component_class="connector",
            library_name="lib_a",
            add_token="con-lstb:CONN_023.5MM",
        ),
        LibraryEntry(
            device_name="CONN_023.5MM",
            package_name="SCREWTERMINAL-3.5MM-2",
            symbol_name="CONN_2",
            component_class="connector",
            library_name="lib_b",
            add_token="con-lstb:CONN_023.5MM",
        ),
    ]

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.AUTO)
    assert ctx.summary.auto_matched == 1
    assert ctx.summary.created_new_parts == 0
    assert project.components[0].device_id == "con-lstb:CONN_023.5MM"


def test_screw_terminal_external_match_with_board_falls_back_when_only_rotated_origin_relaxation_fits(tmp_path) -> None:
    external_lbr = tmp_path / "screw_conn.lbr"
    external_lbr.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<eagle version="9.6.2">
  <drawing>
    <library>
      <packages>
        <package name="SCREWTERMINAL-3.5MM-3">
          <pad name="1" x="0" y="0" drill="1.2" diameter="2.6" shape="round"/>
          <pad name="2" x="-3.5" y="0" drill="1.2" diameter="2.6" shape="round"/>
          <pad name="3" x="-7.0" y="0" drill="1.2" diameter="2.6" shape="round"/>
        </package>
      </packages>
      <symbols>
        <symbol name="CONN_3">
          <pin name="1" x="-2.54" y="2.54"/>
          <pin name="2" x="-2.54" y="0"/>
          <pin name="3" x="-2.54" y="-2.54"/>
        </symbol>
      </symbols>
      <devicesets>
        <deviceset name="CONN_03SCREW">
          <gates><gate name="G$1" symbol="CONN_3" x="0" y="0"/></gates>
          <devices>
            <device name="" package="SCREWTERMINAL-3.5MM-3">
              <connects>
                <connect gate="G$1" pin="1" pad="1"/>
                <connect gate="G$1" pin="2" pad="2"/>
                <connect gate="G$1" pin="3" pad="3"/>
              </connects>
              <technologies><technology name=""/></technologies>
            </device>
          </devices>
        </deviceset>
      </devicesets>
    </library>
  </drawing>
</eagle>
""",
        encoding="utf-8",
    )

    project = Project(
        project_id="p_screw_transform",
        name="p_screw_transform",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(
                refdes="CN1",
                value="",
                source_name="SCREWTERMINAL-3.5MM-3",
                package_id="SCREWTERMINAL-3.5MM-3",
                at=Point(0.0, 0.0),
            )
        ],
        packages=[
            Package(
                package_id="SCREWTERMINAL-3.5MM-3",
                name="SCREWTERMINAL-3.5MM-3",
                pads=[
                    Pad(pad_number="1", at=Point(-3.5, 0.0), shape="round", width_mm=2.6, height_mm=2.6, drill_mm=1.2),
                    Pad(pad_number="2", at=Point(0.0, 0.0), shape="round", width_mm=2.6, height_mm=2.6, drill_mm=1.2),
                    Pad(pad_number="3", at=Point(3.5, 0.0), shape="round", width_mm=2.6, height_mm=2.6, drill_mm=1.2),
                ],
            )
        ],
        board=Board(),
    )

    entries = [
        LibraryEntry(
            device_name="CONN_03SCREW",
            package_name="SCREWTERMINAL-3.5MM-3",
            symbol_name="CONN_3",
            component_class="connector",
            library_name="SparkFun-Connectors",
            add_token="SparkFun-Connectors:CONN_03SCREW",
            library_path=str(external_lbr),
        )
    ]

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.AUTO)
    component = project.components[0]

    assert ctx.summary.auto_matched == 0
    assert ctx.summary.created_new_parts == 1
    assert component.device_id is not None
    assert component.device_id.startswith("easyeda_generated:")
    assert "_external_origin_offset_x_mm" not in component.attributes
    assert "_external_rotation_offset_deg" not in component.attributes
    mismatch_events = [
        event
        for event in project.events
        if event.code == "EXTERNAL_PACKAGE_GEOMETRY_MISMATCH"
    ]
    assert mismatch_events
    assert mismatch_events[0].context.get("reason") == "screw_terminal_origin_requires_rotation"


def test_generated_part_dedup_merges_identical_symbol_and_package() -> None:
    package = Package(
        package_id="PKG1",
        name="PKG1",
        pads=[
            Pad(pad_number="1", at=Point(-0.8, 0.0), shape="rect", width_mm=0.8, height_mm=0.9),
            Pad(pad_number="2", at=Point(0.8, 0.0), shape="rect", width_mm=0.8, height_mm=0.9),
        ],
    )
    project = Project(
        project_id="p1",
        name="p1",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(refdes="R1", value="10k", source_name="R", package_id="PKG1", at=Point(0.0, 0.0)),
            Component(refdes="R2", value="1k", source_name="R", package_id="PKG1", at=Point(10.0, 0.0)),
        ],
        packages=[package],
    )

    ctx = LibraryMatcher().match(project, library_entries=[], match_mode=MatchMode.AUTO)
    assert ctx.summary.created_new_parts == 1
    assert len(ctx.new_library_parts) == 1
    assert project.components[0].device_id == project.components[1].device_id


def test_generated_pin_labels_use_board_net_names_when_available() -> None:
    project = Project(
        project_id="p_hints",
        name="p_hints",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="Controller",
                package_id="PKG1",
                at=Point(10.0, 10.0),
            )
        ],
        packages=[
            Package(
                package_id="PKG1",
                name="PKG1",
                pads=[
                    Pad(pad_number="1", at=Point(-1.0, 0.0), shape="rect", width_mm=0.8, height_mm=0.8),
                    Pad(pad_number="2", at=Point(1.0, 0.0), shape="rect", width_mm=0.8, height_mm=0.8),
                ],
            )
        ],
        board=Board(
            pads=[
                Pad(pad_number="1", at=Point(9.0, 10.0), shape="rect", width_mm=0.8, height_mm=0.8, net="GND"),
                Pad(pad_number="2", at=Point(11.0, 10.0), shape="rect", width_mm=0.8, height_mm=0.8, net="SCL"),
            ],
            tracks=[
                Track(start=Point(9.0, 10.0), end=Point(11.0, 10.0), width_mm=0.2, layer="1", net="SCL"),
            ],
        ),
    )

    ctx = LibraryMatcher().match(project, library_entries=[], match_mode=MatchMode.AUTO)
    assert ctx.summary.created_new_parts == 1
    part = ctx.new_library_parts[0]
    names = {pin.pin_number: pin.pin_name for pin in part.symbol.pins}
    assert names["1"] == "GND"
    assert names["2"] == "SCL"
    assert part.device.pin_pad_map["GND"] == "1"
    assert part.device.pin_pad_map["SCL"] == "2"


def test_through_hole_passive_hints_do_not_match_external_smd() -> None:
    project = Project(
        project_id="p_th_hint",
        name="p_th_hint",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="R1",
                value="10k",
                source_name="R_AXIAL-0.4",
                package_id=None,
                at=Point(0.0, 0.0),
                attributes={"3D Model Title": "R_AXIAL-0.4"},
            )
        ],
    )

    entries = [
        LibraryEntry(
            device_name="R-US_R0603",
            package_name="R0603",
            symbol_name="R",
            component_class="resistor",
            library_name="rcl",
            add_token="rcl:R-US_R0603",
            library_path=r"C:\libs\rcl.lbr",
        ),
    ]

    ctx = LibraryMatcher().match(project, entries, match_mode=MatchMode.AUTO)
    assert ctx.summary.auto_matched == 0
    assert project.components[0].device_id != "rcl:R-US_R0603"
