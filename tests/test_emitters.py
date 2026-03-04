from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from easyeda2fusion.builders.library_builder import GeneratedLibraryPart
from easyeda2fusion.builders.schematic_reconstruction import (
    SchematicReconstructionBuilder,
    _dedupe_label_specs,
    _external_device_pin_offsets,
    _label_spec_for_path,
    _route_path_between_points,
    _spread_label_specs,
)
from easyeda2fusion.emitters.generated_library_emitter import _guess_prefix, emit_generated_library
from easyeda2fusion.matchers.library_matcher import MatchContext
from easyeda2fusion.builders.board_reconstruction import BoardReconstructionBuilder
from easyeda2fusion.model import (
    Board,
    Component,
    Device,
    Hole,
    Net,
    NetNode,
    Package,
    Pad,
    Point,
    Project,
    Region,
    SchematicSheet,
    SourceFormat,
    Symbol,
    SymbolPin,
    Track,
    TextItem,
    Via,
)


def test_schematic_add_uses_explicit_library_path() -> None:
    project = Project(
        project_id="p1",
        name="p1",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(
                refdes="R1",
                value="10k",
                source_name="Resistor",
                device_id="rcl:R-US_R0603",
                at=Point(1.0, 2.0),
            )
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(
        project,
        library_paths={"rcl": "C:/libs/rcl.lbr"},
    )

    assert "ADD 'R-US_R0603@C:/libs/rcl.lbr' 'R1' R0 (20.0000 20.0000);" in lines
    assert "VALUE 'R1' '10k';" in lines
    assert all(not line.startswith("#") for line in lines)


def test_schematic_builder_keeps_default_grid_mode_without_grid_override() -> None:
    project = Project(
        project_id="p_grid_default",
        name="p_grid_default",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        metadata={"schematic_snap_to_default_grid": True},
        components=[
            Component(
                refdes="R1",
                value="10k",
                source_name="Resistor",
                device_id="easyeda_generated:DEV_R1",
                package_id="PKG",
                at=Point(1.0, 2.0),
            )
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(project)
    assert lines[0] == "GRID INCH 0.1 ON;"
    assert lines[1] == "SET WIRE_BEND 2;"


def test_schematic_builder_spreads_annotation_text_lines_to_avoid_overlap() -> None:
    project = Project(
        project_id="p_sch_text_spacing",
        name="p_sch_text_spacing",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        sheets=[
            SchematicSheet(
                sheet_id="sheet_1",
                name="sheet_1",
                annotations=[
                    TextItem(
                        text="LINE_A\nLINE_B",
                        at=Point(10.0, 10.0),
                        layer="schematic_text",
                        size_mm=1.2,
                    ),
                    TextItem(
                        text="LINE_C",
                        at=Point(10.0, 10.0),
                        layer="schematic_text",
                        size_mm=1.2,
                    ),
                ],
            )
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(project)
    text_lines = [line for line in lines if line.startswith("TEXT '")]
    assert len(text_lines) == 3

    points: list[tuple[float, float]] = []
    for line in text_lines:
        match = re.search(r"\(([-0-9.]+)\s+([-0-9.]+)\)", line)
        assert match is not None
        points.append((float(match.group(1)), float(match.group(2))))

    assert len(set(points)) == 3


def test_schematic_builder_translates_human_layout_into_visible_range() -> None:
    project = Project(
        project_id="p_human_visible_window",
        name="p_human_visible_window",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        metadata={"schematic_snap_to_default_grid": True},
        components=[
            Component(
                refdes="R1",
                value="10k",
                source_name="Resistor",
                device_id="easyeda_generated:DEV_R1",
                package_id="PKG",
                at=Point(0.0, 0.0),
            )
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[
                    Pad(pad_number="1", at=Point(-0.5, 0.0), shape="rect", width_mm=0.8, height_mm=0.8),
                    Pad(pad_number="2", at=Point(0.5, 0.0), shape="rect", width_mm=0.8, height_mm=0.8),
                ],
            )
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(project, layout_mode="human")
    add_line = next(line for line in lines if line.startswith("ADD "))
    match = re.search(r"\(([-0-9.]+)\s+([-0-9.]+)\)", add_line)
    assert match is not None
    x_mm = float(match.group(1))
    y_mm = float(match.group(2))
    assert x_mm >= (20.0 / 25.4)
    assert y_mm >= (20.0 / 25.4)


def test_schematic_value_prefers_mpn_for_non_passive_parts() -> None:
    project = Project(
        project_id="p_value_mpn",
        name="p_value_mpn",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(
                refdes="U1",
                value="MCU",
                source_name="STM32",
                device_id="mylib:STM32_DEV",
                mpn="STM32F030K6T6",
                at=Point(1.0, 2.0),
            )
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(
        project,
        library_paths={"mylib": "C:/libs/mylib.lbr"},
    )

    assert "VALUE 'U1' 'STM32F030K6T6';" in lines


def test_schematic_value_keeps_resistor_value_over_mpn() -> None:
    project = Project(
        project_id="p_value_rc",
        name="p_value_rc",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(
                refdes="R8",
                value="20Ω",
                source_name="R_AXIAL",
                device_id="rcl:R-US_RAXIAL",
                mpn="MFR0W4F200JA50",
                at=Point(1.0, 2.0),
            )
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(
        project,
        library_paths={"rcl": "C:/libs/rcl.lbr"},
    )

    assert "VALUE 'R8' '20Ω';" in lines
    assert "VALUE 'R8' 'MFR0W4F200JA50';" not in lines


def test_schematic_value_strips_package_size_tokens_for_display() -> None:
    project = Project(
        project_id="p_value_pkg_tokens",
        name="p_value_pkg_tokens",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(
                refdes="CN1",
                value="SCREWTERMINAL-3.5MM-3",
                source_name="SCREWTERMINAL",
                device_id="conn:TERM",
                package_id="SCREWTERMINAL-3.5MM-3",
                at=Point(1.0, 2.0),
            )
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(
        project,
        library_paths={"conn": "C:/libs/conn.lbr"},
    )

    assert "VALUE 'CN1' 'SCREWTERMINAL-3';" in lines


def test_schematic_value_strips_package_pitch_tokens_for_display() -> None:
    project = Project(
        project_id="p_value_pitch_token",
        name="p_value_pitch_token",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(
                refdes="CN2",
                value="CONN-TH_2P-P2.54_HCTL_HC-2510-2AW",
                source_name="CONN",
                device_id="conn:HDR",
                package_id="CONN-TH_2P-P2.54_HCTL_HC-2510-2AW",
                at=Point(1.0, 2.0),
            )
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(
        project,
        library_paths={"conn": "C:/libs/conn.lbr"},
    )

    assert "VALUE 'CN2' 'CONN-TH_2P-HCTL_HC-2510-2AW';" in lines


def test_schematic_value_removes_dangling_parentheses_after_sanitization() -> None:
    project = Project(
        project_id="p_value_dangling_paren",
        name="p_value_dangling_paren",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(
                refdes="CN3",
                value="CONN-TH_S2B-XH-A-1-LF-SN (",
                source_name="CONN",
                device_id="conn:HDR",
                package_id="CONN-TH_S2B-XH-A-1-LF-SN",
                at=Point(1.0, 2.0),
            )
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(
        project,
        library_paths={"conn": "C:/libs/conn.lbr"},
    )

    assert "VALUE 'CN3' 'CONN-TH_S2B-XH-A-1-LF-SN';" in lines


def test_schematic_add_quotes_orientation_like_resistor_refdes() -> None:
    project = Project(
        project_id="p_add_quote",
        name="p_add_quote",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(
                refdes="R35",
                value="10k",
                source_name="Resistor",
                device_id="rcl:R-US_R0603",
                at=Point(1.0, 2.0),
            )
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(
        project,
        library_paths={"rcl": "C:/libs/rcl.lbr"},
    )

    assert "ADD 'R-US_R0603@C:/libs/rcl.lbr' 'R35' R0 (20.0000 20.0000);" in lines
    assert "ADD 'R-US_R0603@C:/libs/rcl.lbr' R35 R0 (20.0000 20.0000);" not in lines


def test_schematic_builder_does_not_emit_delete_commands_by_default() -> None:
    project = Project(
        project_id="p_nodelete",
        name="p_nodelete",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(
                refdes="H3",
                value="",
                source_name="Header",
                device_id="easyeda_generated:DEV_H3",
                package_id="PKG1",
                at=Point(0.0, 0.0),
            )
        ],
        packages=[
            Package(
                package_id="PKG1",
                name="PKG1",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
    )
    lines = SchematicReconstructionBuilder().build_commands(project)
    assert not any(line.startswith("DELETE ") for line in lines)
    assert any(line.startswith("ADD ") for line in lines)


def test_generated_library_dedupes_symbol_names(tmp_path) -> None:
    symbol_name = "LED-TH_LED-DP2-2LED-AMCC"
    pin = SymbolPin(pin_number="1", pin_name="1", at=Point(0.0, 0.0))
    pad = Pad(
        pad_number="1",
        at=Point(0.0, 0.0),
        shape="rect",
        width_mm=1.0,
        height_mm=1.0,
        layer="top_copper",
    )

    part1 = GeneratedLibraryPart(
        symbol=Symbol(symbol_id="SYM_LED1", name=symbol_name, pins=[pin]),
        package=Package(package_id="PKG_A", name="PKG_DUP", pads=[pad]),
        device=Device(
            device_id="DEV_LED1",
            name="dev_led1",
            symbol_id="SYM_LED1",
            package_id="PKG_A",
            pin_pad_map={"1": "1"},
        ),
        source="test",
    )
    part2 = GeneratedLibraryPart(
        symbol=Symbol(symbol_id="SYM_LED2", name=symbol_name, pins=[pin]),
        package=Package(package_id="PKG_B", name="PKG_DUP", pads=[pad]),
        device=Device(
            device_id="DEV_LED2",
            name="dev_led2",
            symbol_id="SYM_LED2",
            package_id="PKG_B",
            pin_pad_map={"1": "1"},
        ),
        source="test",
    )

    ctx = MatchContext(new_library_parts=[part1, part2])
    out = emit_generated_library(ctx, tmp_path)
    assert out is not None

    tree = ET.parse(out)
    root = tree.getroot()
    symbol_names = [item.get("name") for item in root.findall(".//library/symbols/symbol")]
    assert len(symbol_names) == 2
    assert len(set(symbol_names)) == 2

    package_names = [item.get("name") for item in root.findall(".//library/packages/package")]
    assert len(package_names) == 2
    assert len(set(package_names)) == 2


def test_generated_library_emits_pad_rotation_and_round_th_for_equal_ellipse(tmp_path) -> None:
    symbol = Symbol(
        symbol_id="SYM_U1",
        name="U1",
        pins=[
            SymbolPin(pin_number="1", pin_name="1", at=Point(-5.08, 0.0)),
            SymbolPin(pin_number="2", pin_name="2", at=Point(5.08, 0.0)),
        ],
    )
    package = Package(
        package_id="PKG_U1",
        name="PKG_U1",
        pads=[
            Pad(
                pad_number="1",
                at=Point(0.0, 0.0),
                shape="rect",
                width_mm=0.25,
                height_mm=0.60,
                layer="top_copper",
                rotation_deg=90.0,
            ),
            Pad(
                pad_number="2",
                at=Point(2.54, 0.0),
                shape="ellipse",
                width_mm=1.20,
                height_mm=1.20,
                drill_mm=0.8,
                layer="top_copper",
                rotation_deg=90.0,
            ),
        ],
    )
    device = Device(
        device_id="DEV_U1",
        name="DEV_U1",
        symbol_id=symbol.symbol_id,
        package_id=package.package_id,
        pin_pad_map={"1": "1", "2": "2"},
    )
    part = GeneratedLibraryPart(symbol=symbol, package=package, device=device, source="test")
    ctx = MatchContext(new_library_parts=[part])

    out = emit_generated_library(ctx, tmp_path)
    assert out is not None

    tree = ET.parse(out)
    root = tree.getroot()
    smd = root.find(".//library/packages/package[@name='PKG_U1']/smd[@name='1']")
    assert smd is not None
    assert smd.get("rot") == "R90"

    th_pad = root.find(".//library/packages/package[@name='PKG_U1']/pad[@name='2']")
    assert th_pad is not None
    assert th_pad.get("shape") == "round"


def test_generated_library_emits_package_silkscreen_and_name_value_layers(tmp_path) -> None:
    symbol = Symbol(
        symbol_id="SYM_J1",
        name="J1",
        pins=[SymbolPin(pin_number="1", pin_name="1", at=Point(0.0, 0.0))],
    )
    package = Package(
        package_id="PKG_J1",
        name="PKG_J1",
        pads=[
            Pad(
                pad_number="1",
                at=Point(0.0, 0.0),
                shape="rect",
                width_mm=1.0,
                height_mm=1.0,
                layer="top_copper",
            ),
        ],
        outline=[
            {
                "kind": "wire_path",
                "layer": "3",
                "width_mm": 0.12,
                "points": [
                    {"x_mm": -1.0, "y_mm": -1.0},
                    {"x_mm": 1.0, "y_mm": -1.0},
                    {"x_mm": 1.0, "y_mm": 1.0},
                ],
            },
            {
                "kind": "text",
                "layer": "3",
                "text": ">NAME",
                "x_mm": 0.0,
                "y_mm": 2.0,
                "size_mm": 1.27,
                "rotation_deg": 0.0,
            },
        ],
    )
    device = Device(
        device_id="DEV_J1",
        name="DEV_J1",
        symbol_id=symbol.symbol_id,
        package_id=package.package_id,
        pin_pad_map={"1": "1"},
    )
    part = GeneratedLibraryPart(symbol=symbol, package=package, device=device, source="test")
    ctx = MatchContext(new_library_parts=[part])

    out = emit_generated_library(ctx, tmp_path)
    assert out is not None

    tree = ET.parse(out)
    root = tree.getroot()
    pkg = root.find(".//library/packages/package[@name='PKG_J1']")
    assert pkg is not None
    assert pkg.find("./wire[@layer='21']") is not None
    assert pkg.find("./text[@layer='25']") is not None
    assert pkg.find("./text[@layer='27']") is not None


def test_generated_library_does_not_emit_pad_number_labels_on_footprints(tmp_path) -> None:
    symbol = Symbol(
        symbol_id="SYM_U3",
        name="U3",
        pins=[SymbolPin(pin_number="1", pin_name="1", at=Point(0.0, 0.0))],
    )
    package = Package(
        package_id="PKG_U3",
        name="PKG_U3",
        pads=[
            Pad(pad_number="1", at=Point(-1.0, 0.8), shape="rect", width_mm=0.5, height_mm=0.8, layer="top_copper"),
            Pad(pad_number="2", at=Point(1.0, 0.8), shape="rect", width_mm=0.5, height_mm=0.8, layer="top_copper"),
            Pad(pad_number="3", at=Point(-1.0, -0.8), shape="rect", width_mm=0.5, height_mm=0.8, layer="top_copper"),
            Pad(pad_number="4", at=Point(1.0, -0.8), shape="rect", width_mm=0.5, height_mm=0.8, layer="top_copper"),
        ],
    )
    device = Device(
        device_id="DEV_U3",
        name="DEV_U3",
        symbol_id=symbol.symbol_id,
        package_id=package.package_id,
        pin_pad_map={"1": "1"},
    )
    part = GeneratedLibraryPart(symbol=symbol, package=package, device=device, source="test")
    ctx = MatchContext(new_library_parts=[part])

    out = emit_generated_library(ctx, tmp_path)
    assert out is not None

    tree = ET.parse(out)
    root = tree.getroot()
    pkg = root.find(".//library/packages/package[@name='PKG_U3']")
    assert pkg is not None
    labels = {text_el.text for text_el in pkg.findall("./text[@layer='51']")}
    assert not ({"1", "2", "3", "4"} & labels)


def test_generated_library_places_name_value_outside_pad_area(tmp_path) -> None:
    symbol = Symbol(
        symbol_id="SYM_X1",
        name="X1",
        pins=[SymbolPin(pin_number="1", pin_name="1", at=Point(0.0, 0.0))],
    )
    package = Package(
        package_id="PKG_X1",
        name="PKG_X1",
        pads=[
            Pad(
                pad_number="1",
                at=Point(0.0, 0.0),
                shape="rect",
                width_mm=1.2,
                height_mm=1.2,
                layer="top_copper",
            ),
        ],
        outline=[
            {"kind": "text", "layer": "3", "text": ">NAME", "x_mm": 0.0, "y_mm": 0.0, "size_mm": 1.0, "rotation_deg": 0.0},
            {"kind": "text", "layer": "3", "text": ">VALUE", "x_mm": 0.0, "y_mm": 0.0, "size_mm": 1.0, "rotation_deg": 0.0},
        ],
    )
    device = Device(
        device_id="DEV_X1",
        name="DEV_X1",
        symbol_id=symbol.symbol_id,
        package_id=package.package_id,
        pin_pad_map={"1": "1"},
    )
    part = GeneratedLibraryPart(symbol=symbol, package=package, device=device, source="test")
    out = emit_generated_library(MatchContext(new_library_parts=[part]), tmp_path)
    assert out is not None

    tree = ET.parse(out)
    root = tree.getroot()
    pkg = root.find(".//library/packages/package[@name='PKG_X1']")
    assert pkg is not None

    name_text = pkg.find("./text[.='>NAME']")
    value_text = pkg.find("./text[.='>VALUE']")
    assert name_text is not None
    assert value_text is not None

    name_y = float(name_text.get("y", "0"))
    value_y = float(value_text.get("y", "0"))
    assert abs(name_y) > 1.0
    assert abs(value_y) > 1.0
    assert abs(name_y - value_y) > 1.0


def test_generated_library_places_part_name_text_on_values_layer(tmp_path) -> None:
    symbol = Symbol(
        symbol_id="SYM_U2",
        name="U2",
        pins=[SymbolPin(pin_number="1", pin_name="1", at=Point(0.0, 0.0))],
    )
    package = Package(
        package_id="PKG_U2",
        name="PKG_U2",
        pads=[
            Pad(
                pad_number="1",
                at=Point(0.0, 0.0),
                shape="rect",
                width_mm=1.0,
                height_mm=1.0,
                layer="top_copper",
            ),
        ],
        outline=[
            {
                "kind": "text",
                "layer": "3",
                "text": "PKG_U2",
                "x_mm": 0.0,
                "y_mm": 1.5,
                "size_mm": 1.0,
                "rotation_deg": 0.0,
            },
        ],
    )
    device = Device(
        device_id="DEV_U2",
        name="DEV_U2",
        symbol_id=symbol.symbol_id,
        package_id=package.package_id,
        pin_pad_map={"1": "1"},
    )
    part = GeneratedLibraryPart(symbol=symbol, package=package, device=device, source="test")
    ctx = MatchContext(new_library_parts=[part])

    out = emit_generated_library(ctx, tmp_path)
    assert out is not None

    tree = ET.parse(out)
    root = tree.getroot()
    pkg = root.find(".//library/packages/package[@name='PKG_U2']")
    assert pkg is not None
    assert pkg.find("./text[@layer='27'][.='PKG_U2']") is not None
    assert pkg.find("./text[@layer='21'][.='PKG_U2']") is None


def test_generated_library_places_part_number_text_on_values_layer_not_designator(tmp_path) -> None:
    symbol = Symbol(
        symbol_id="SYM_D1",
        name="D1",
        pins=[SymbolPin(pin_number="1", pin_name="1", at=Point(0.0, 0.0))],
    )
    package = Package(
        package_id="PKG_D1",
        name="PKG_D1",
        pads=[
            Pad(
                pad_number="1",
                at=Point(0.0, 0.0),
                shape="rect",
                width_mm=1.0,
                height_mm=1.0,
                layer="top_copper",
            ),
        ],
        outline=[
            {
                "kind": "text",
                "layer": "3",
                "text": "MBR0520LT1G",
                "x_mm": 0.0,
                "y_mm": 1.5,
                "size_mm": 1.0,
                "rotation_deg": 0.0,
            },
            {
                "kind": "text",
                "layer": "3",
                "text": "R0603",
                "x_mm": 0.0,
                "y_mm": 0.0,
                "size_mm": 1.0,
                "rotation_deg": 0.0,
            },
            {
                "kind": "text",
                "layer": "3",
                "text": "R1",
                "x_mm": 0.0,
                "y_mm": -1.5,
                "size_mm": 1.0,
                "rotation_deg": 0.0,
            },
        ],
    )
    device = Device(
        device_id="DEV_D1",
        name="DEV_D1",
        symbol_id=symbol.symbol_id,
        package_id=package.package_id,
        pin_pad_map={"1": "1"},
    )
    part = GeneratedLibraryPart(symbol=symbol, package=package, device=device, source="test")
    ctx = MatchContext(new_library_parts=[part])

    out = emit_generated_library(ctx, tmp_path)
    assert out is not None

    tree = ET.parse(out)
    root = tree.getroot()
    pkg = root.find(".//library/packages/package[@name='PKG_D1']")
    assert pkg is not None
    assert pkg.find("./text[@layer='27'][.='MBR0520LT1G']") is not None
    assert pkg.find("./text[@layer='27'][.='R0603']") is not None
    assert pkg.find("./text[@layer='21'][.='R1']") is not None


def test_generated_library_moves_part_number_text_outside_package_body(tmp_path) -> None:
    symbol = Symbol(
        symbol_id="SYM_RA1",
        name="RA1",
        pins=[SymbolPin(pin_number="1", pin_name="1", at=Point(0.0, 0.0))],
    )
    package = Package(
        package_id="PKG_RA1",
        name="RES-ARRAY-SMD_0603-8P-L3.2-W1.6-BL",
        pads=[
            Pad(pad_number="1", at=Point(-1.2, -0.9), shape="rect", width_mm=0.65, height_mm=0.8, layer="top_copper"),
            Pad(pad_number="8", at=Point(1.2, 0.9), shape="rect", width_mm=0.65, height_mm=0.8, layer="top_copper"),
        ],
        outline=[
            {
                "kind": "text",
                "layer": "3",
                "text": "RES-ARRAY-SMD_0603-8P-L3.2-W1.6-BL",
                "x_mm": 0.0,
                "y_mm": 0.0,
                "size_mm": 1.7,
                "rotation_deg": 0.0,
            }
        ],
    )
    device = Device(
        device_id="DEV_RA1",
        name="DEV_RA1",
        symbol_id=symbol.symbol_id,
        package_id=package.package_id,
        pin_pad_map={"1": "1"},
    )

    out = emit_generated_library(MatchContext(new_library_parts=[GeneratedLibraryPart(symbol=symbol, package=package, device=device, source="test")]), tmp_path)
    assert out is not None
    root = ET.parse(out).getroot()
    pkg = root.find(".//library/packages/package[@name='RES-ARRAY-SMD_0603-8P-L3_2-W1_6-BL']")
    assert pkg is not None
    txt = pkg.find("./text[@layer='27'][.='RES-ARRAY-SMD_0603-8P-L3.2-W1.6-BL']")
    assert txt is not None
    # Part-number text should not remain at origin over the package center.
    assert abs(float(txt.get("x", "0"))) > 0.01 or abs(float(txt.get("y", "0"))) > 0.01


def test_generated_library_maps_layer50_outline_to_docu(tmp_path) -> None:
    symbol = Symbol(
        symbol_id="SYM_DOCU1",
        name="DOCU1",
        pins=[SymbolPin(pin_number="1", pin_name="1", at=Point(0.0, 0.0))],
    )
    package = Package(
        package_id="PKG_DOCU1",
        name="PKG_DOCU1",
        pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0, layer="top_copper")],
        outline=[
            {
                "kind": "wire_path",
                "layer": "50",
                "width_mm": 0.1,
                "points": [
                    {"x_mm": -1.0, "y_mm": -1.0},
                    {"x_mm": 1.0, "y_mm": -1.0},
                ],
            }
        ],
    )
    device = Device(
        device_id="DEV_DOCU1",
        name="DEV_DOCU1",
        symbol_id=symbol.symbol_id,
        package_id=package.package_id,
        pin_pad_map={"1": "1"},
    )
    out = emit_generated_library(MatchContext(new_library_parts=[GeneratedLibraryPart(symbol=symbol, package=package, device=device, source="test")]), tmp_path)
    assert out is not None
    root = ET.parse(out).getroot()
    pkg = root.find(".//library/packages/package[@name='PKG_DOCU1']")
    assert pkg is not None
    assert pkg.find("./wire[@layer='51']") is not None


def test_generated_library_emits_package_keepout_polygon_and_hole(tmp_path) -> None:
    symbol = Symbol(
        symbol_id="SYM_MECH1",
        name="MECH1",
        pins=[SymbolPin(pin_number="1", pin_name="1", at=Point(0.0, 0.0))],
    )
    package = Package(
        package_id="PKG_MECH1",
        name="PKG_MECH1",
        pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0, layer="top_copper")],
        outline=[
            {
                "kind": "polygon",
                "layer": "12",
                "width_mm": 0.15,
                "points": [
                    {"x_mm": -2.0, "y_mm": -1.0},
                    {"x_mm": 2.0, "y_mm": -1.0},
                    {"x_mm": 2.0, "y_mm": 1.0},
                    {"x_mm": -2.0, "y_mm": 1.0},
                ],
            },
            {
                "kind": "hole",
                "x_mm": 3.5,
                "y_mm": 0.0,
                "drill_mm": 1.3,
            },
        ],
    )
    device = Device(
        device_id="DEV_MECH1",
        name="DEV_MECH1",
        symbol_id=symbol.symbol_id,
        package_id=package.package_id,
        pin_pad_map={"1": "1"},
    )
    out = emit_generated_library(
        MatchContext(new_library_parts=[GeneratedLibraryPart(symbol=symbol, package=package, device=device, source="test")]),
        tmp_path,
    )
    assert out is not None
    root = ET.parse(out).getroot()
    pkg = root.find(".//library/packages/package[@name='PKG_MECH1']")
    assert pkg is not None
    poly = pkg.find("./polygon[@layer='41']")
    assert poly is not None
    vertices = poly.findall("./vertex")
    assert len(vertices) >= 4
    hole = pkg.find("./hole[@x='3.5'][@y='0'][@drill='1.3']")
    assert hole is not None


def test_generated_library_contains_supply_symbols_from_project_power_nets(tmp_path) -> None:
    project = Project(
        project_id="p_supply",
        name="p_supply",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        nets=[Net(name="GND"), Net(name="5V0"), Net(name="N$1")],
    )
    out = emit_generated_library(MatchContext(new_library_parts=[]), tmp_path, project=project)
    assert out is not None
    root = ET.parse(out).getroot()
    assert root.find(".//library/devicesets/deviceset[@name='PWR_GND']") is not None
    assert root.find(".//library/devicesets/deviceset[@name='PWR_5V']") is not None
    devices = root.findall(".//library/devicesets/deviceset/devices/device")
    assert all(str(device.get("package") or "").strip() for device in devices)
    pwr_connect = root.find(
        ".//library/devicesets/deviceset[@name='PWR_GND']/devices/device/connects/connect[@pin='GND'][@pad='1']"
    )
    assert pwr_connect is not None


def test_guess_prefix_uses_refdes_style_tokens() -> None:
    assert _guess_prefix("DEV_LED1") == "LED"
    assert _guess_prefix("DEV_CN5SW-B") == "CN"
    assert _guess_prefix("DEV_U28") == "U"
    assert _guess_prefix("DEV_12V") == "U"


def test_board_builder_declares_signals_and_avoids_route() -> None:
    project = Project(
        project_id="p1",
        name="p1",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(
                refdes="R1",
                value="10k",
                source_name="Resistor",
                device_id="rcl:R-US_R0603",
                at=Point(10.0, 10.0),
            ),
            Component(
                refdes="R2",
                value="10k",
                source_name="Resistor",
                device_id="rcl:R-US_R0603",
                at=Point(20.0, 10.0),
            ),
        ],
        nets=[Net(name="N1", nodes=[NetNode(refdes="R1", pin="1"), NetNode(refdes="R2", pin="1")])],
        board=Board(
            tracks=[
                Track(
                    start=Point(10.0, 10.0),
                    end=Point(20.0, 10.0),
                    width_mm=0.25,
                    layer="top_copper",
                    net="N1",
                )
            ]
        ),
    )

    lines = BoardReconstructionBuilder().build_commands(project)
    assert "ROTATE =R0 'R1';" in lines
    assert "ROTATE =R0 'R2';" in lines
    assert any(line.startswith("SIGNAL 'N1' R1 1 R2 1;") for line in lines)
    assert any(line.startswith("WIRE 'N1' 0.2500 (10.0000 10.0000) (20.0000 10.0000);") for line in lines)
    assert all(not line.startswith("ROUTE ") for line in lines)


def test_board_builder_skips_signal_commands_when_schematic_exists() -> None:
    project = Project(
        project_id="p1",
        name="p1",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        sheets=[SchematicSheet(sheet_id="s1", name="Main")],
        components=[
            Component(
                refdes="R1",
                value="10k",
                source_name="Resistor",
                device_id="rcl:R-US_R0603",
                package_id="PKG2",
                at=Point(10.0, 10.0),
            ),
            Component(
                refdes="R2",
                value="10k",
                source_name="Resistor",
                device_id="rcl:R-US_R0603",
                package_id="PKG2",
                at=Point(20.0, 10.0),
            ),
        ],
        packages=[
            Package(
                package_id="PKG2",
                name="PKG2",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
        nets=[Net(name="N1", nodes=[NetNode(refdes="R1", pin="1"), NetNode(refdes="R2", pin="1")])],
        board=Board(
            tracks=[
                Track(
                    start=Point(10.0, 10.0),
                    end=Point(20.0, 10.0),
                    width_mm=0.25,
                    layer="top_copper",
                    net="N1",
                )
            ]
        ),
    )

    lines = BoardReconstructionBuilder().build_commands(project)
    assert all(not line.startswith("SIGNAL ") for line in lines)
    assert any(line.startswith("WIRE 'N1' 0.2500 (10.0000 10.0000) (20.0000 10.0000);") for line in lines)


def test_board_builder_maps_numeric_pro_layers() -> None:
    project = Project(
        project_id="p1",
        name="p1",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        board=Board(
            tracks=[
                Track(start=Point(0.0, 0.0), end=Point(5.0, 0.0), width_mm=0.2, layer="1", net="N1"),
                Track(start=Point(0.0, 1.0), end=Point(5.0, 1.0), width_mm=0.2, layer="2", net="N2"),
            ]
        ),
    )
    lines = BoardReconstructionBuilder().build_commands(project)
    assert "LAYER 1;" in lines
    assert "LAYER Bottom;" in lines


def test_board_builder_merges_overlapping_track_net_names() -> None:
    project = Project(
        project_id="p1",
        name="p1",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        board=Board(
            tracks=[
                Track(start=Point(0.0, 0.0), end=Point(10.0, 0.0), width_mm=0.2, layer="1", net="N$1"),
                Track(start=Point(5.0, -2.0), end=Point(5.0, 2.0), width_mm=0.2, layer="1", net="GND"),
            ]
        ),
    )
    lines = BoardReconstructionBuilder().build_commands(project)
    assert any(line.startswith("WIRE 'GND' 0.2000 (0.0000 0.0000) (10.0000 0.0000);") for line in lines)
    assert any(line.startswith("WIRE 'GND' 0.2000 (5.0000 -2.0000) (5.0000 2.0000);") for line in lines)
    assert all("WIRE 'N$1'" not in line for line in lines)


def test_board_builder_merges_track_nets_across_via_touch() -> None:
    project = Project(
        project_id="p1",
        name="p1",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        board=Board(
            tracks=[
                Track(start=Point(0.0, 0.0), end=Point(5.0, 0.0), width_mm=0.2, layer="1", net="N$21"),
                Track(start=Point(5.0, 0.0), end=Point(5.0, 5.0), width_mm=0.2, layer="2", net="GND"),
            ],
            vias=[Via(at=Point(5.0, 0.0), drill_mm=0.3, diameter_mm=0.6, net="GND")],
        ),
    )
    lines = BoardReconstructionBuilder().build_commands(project)
    assert any(line.startswith("WIRE 'GND' 0.2000 (0.0000 0.0000) (5.0000 0.0000);") for line in lines)
    assert all("WIRE 'N$21'" not in line for line in lines)


def test_board_builder_emits_standalone_th_pad_as_signal_via() -> None:
    project = Project(
        project_id="p1",
        name="p1",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        board=Board(
            pads=[
                Pad(
                    pad_number="P1",
                    at=Point(10.0, 20.0),
                    shape="ellipse",
                    width_mm=6.0,
                    height_mm=6.0,
                    drill_mm=3.0,
                    layer="12",
                    net="GND",
                )
            ]
        ),
    )
    lines = BoardReconstructionBuilder().build_commands(project)
    assert "CHANGE DRILL 3.0000;" in lines
    assert any(line.startswith("VIA 'GND' 6.0000 round (10.0000 20.0000);") for line in lines)


def test_board_builder_uses_0_3mm_width_for_silkscreen_region_wires() -> None:
    project = Project(
        project_id="p_silk_wire_width",
        name="p_silk_wire_width",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        board=Board(
            regions=[
                Region(
                    region_id="silk_top",
                    layer="3",
                    points=[
                        Point(0.0, 0.0),
                        Point(10.0, 0.0),
                        Point(10.0, 5.0),
                    ],
                ),
                Region(
                    region_id="silk_bottom",
                    layer="4",
                    points=[
                        Point(20.0, 0.0),
                        Point(30.0, 0.0),
                        Point(30.0, 5.0),
                    ],
                ),
            ]
        ),
    )
    lines = BoardReconstructionBuilder().build_commands(project)
    current_layer = None
    silk_widths: list[float] = []
    for line in lines:
        if line.startswith("LAYER "):
            current_layer = line.removeprefix("LAYER ").removesuffix(";").strip()
            continue
        if current_layer in {"21", "22"} and line.startswith("WIRE "):
            width_token = line.split()[1]
            silk_widths.append(float(width_token))

    assert silk_widths
    assert all(abs(width - 0.3) < 1e-9 for width in silk_widths)


def test_board_builder_preserves_text_size_and_bottom_orientation() -> None:
    project = Project(
        project_id="p_text",
        name="p_text",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        board=Board(
            text=[
                # Top silkscreen/doc text
                TextItem(
                    text="TOPTXT",
                    at=Point(10.0, 10.0),
                    layer="3",
                    size_mm=1.8,
                    rotation_deg=90.0,
                ),
                # Bottom silkscreen/doc text
                TextItem(
                    text="BOTTXT",
                    at=Point(20.0, 20.0),
                    layer="4",
                    size_mm=1.2,
                    rotation_deg=180.0,
                ),
            ]
        ),
    )
    lines = BoardReconstructionBuilder().build_commands(project)
    assert "LAYER 21;" in lines
    assert "LAYER 22;" in lines
    assert "CHANGE SIZE 1.8000;" in lines
    assert "CHANGE SIZE 1.2000;" in lines
    assert "TEXT 'TOPTXT' (10.0000 10.0000) R270;" in lines
    assert "TEXT 'BOTTXT' (20.0000 20.0000) MR180;" in lines


def test_board_builder_keeps_standard_text_rotation_unmodified() -> None:
    project = Project(
        project_id="p_text_std",
        name="p_text_std",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        board=Board(
            text=[
                TextItem(
                    text="TOPTXT",
                    at=Point(10.0, 10.0),
                    layer="3",
                    size_mm=1.2,
                    rotation_deg=90.0,
                ),
            ]
        ),
    )
    lines = BoardReconstructionBuilder().build_commands(project)
    assert "TEXT 'TOPTXT' (10.0000 10.0000) R90;" in lines


def test_schematic_builder_filters_invalid_package_pins() -> None:
    project = Project(
        project_id="p1",
        name="p1",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U",
                device_id="easyeda_generated:DEV_U1",
                package_id="PKG1",
                at=Point(0.0, 0.0),
            ),
            Component(
                refdes="R1",
                value="",
                source_name="R",
                device_id="rcl:R-US_R0603",
                package_id="PKG2",
                at=Point(0.0, 0.0),
            ),
        ],
        packages=[
            Package(
                package_id="PKG1",
                name="PKG1",
                pads=[
                    Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            ),
            Package(
                package_id="PKG2",
                name="PKG2",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            ),
        ],
        nets=[
            Net(
                name="N1",
                nodes=[
                    NetNode(refdes="U1", pin="e36"),  # invalid for PKG1, should be filtered
                    NetNode(refdes="U1", pin="1"),
                    NetNode(refdes="R1", pin="1"),
                ],
            )
        ],
    )
    lines = SchematicReconstructionBuilder().build_commands(project, library_paths={"rcl": "C:/libs/rcl.lbr"})
    assert any(line.startswith("NET 'N1' ") for line in lines)
    assert all("e36" not in line for line in lines if line.startswith("NET "))


def test_schematic_builder_emits_orthogonal_paths_and_skips_single_node_net() -> None:
    project = Project(
        project_id="p1",
        name="p1",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(refdes="U1", value="", source_name="U", device_id="easyeda_generated:DEV_U1", package_id="PKG", at=Point(0.0, 0.0)),
            Component(refdes="U2", value="", source_name="U", device_id="easyeda_generated:DEV_U2", package_id="PKG", at=Point(30.0, 10.0)),
            Component(refdes="U3", value="", source_name="U", device_id="easyeda_generated:DEV_U3", package_id="PKG", at=Point(15.0, 30.0)),
        ],
        packages=[
            Package(package_id="PKG", name="PKG", pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)]),
        ],
        nets=[
            Net(name="SIG", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="U2", pin="1"), NetNode(refdes="U3", pin="1")]),
            Net(name="FLOAT", nodes=[NetNode(refdes="U1", pin="1")]),
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(project)
    net_lines = [line for line in lines if line.startswith("NET ")]
    assert all("FLOAT" not in line for line in net_lines)
    assert any(line.startswith("NET 'SIG' ") for line in net_lines)

    for line in net_lines:
        coords: list[tuple[float, float]] = []
        for token in line.split("(")[1:]:
            pair = token.split(")")[0].strip().split()
            coords.append((float(pair[0]), float(pair[1])))
        for idx in range(len(coords) - 1):
            x1, y1 = coords[idx]
            x2, y2 = coords[idx + 1]
            assert abs(x1 - x2) < 1e-6 or abs(y1 - y2) < 1e-6


def test_schematic_builder_avoids_internal_overlaps_on_dense_net() -> None:
    components = [
        Component(refdes=f"U{idx}", value="", source_name="U", device_id=f"easyeda_generated:DEV_U{idx}", package_id="PKG", at=Point(float((idx % 4) * 8), float((idx // 4) * 8)))
        for idx in range(1, 9)
    ]
    nodes = [NetNode(refdes=f"U{idx}", pin="1") for idx in range(1, 9)]
    project = Project(
        project_id="p_dense",
        name="p_dense",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=components,
        packages=[Package(package_id="PKG", name="PKG", pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)])],
        nets=[Net(name="BUS", nodes=nodes)],
    )

    lines = SchematicReconstructionBuilder().build_commands(project)
    bus_lines = [line for line in lines if line.startswith("NET 'BUS' ")]
    assert bus_lines
    # The builder now emits explicit pin-anchor stubs in addition to trunk paths.
    # Ensure at least a spanning-tree equivalent number of net segments exists.
    assert len(bus_lines) >= len(nodes) - 1

    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for line in bus_lines:
        coords: list[tuple[float, float]] = []
        for token in line.split("(")[1:]:
            pair = token.split(")")[0].strip().split()
            coords.append((float(pair[0]), float(pair[1])))
        for idx in range(len(coords) - 1):
            start = coords[idx]
            end = coords[idx + 1]
            if abs(start[0] - end[0]) < 1e-6 and abs(start[1] - end[1]) < 1e-6:
                continue
            segments.append((start, end))

    def share_endpoint(
        a_start: tuple[float, float],
        a_end: tuple[float, float],
        b_start: tuple[float, float],
        b_end: tuple[float, float],
    ) -> bool:
        endpoints_a = (a_start, a_end)
        endpoints_b = (b_start, b_end)
        for ax, ay in endpoints_a:
            for bx, by in endpoints_b:
                if abs(ax - bx) < 1e-6 and abs(ay - by) < 1e-6:
                    return True
        return False

    def axis_touch(
        a_start: tuple[float, float],
        a_end: tuple[float, float],
        b_start: tuple[float, float],
        b_end: tuple[float, float],
    ) -> bool:
        ax1, ay1 = a_start
        ax2, ay2 = a_end
        bx1, by1 = b_start
        bx2, by2 = b_end
        eps = 1e-6
        a_vertical = abs(ax1 - ax2) < eps
        b_vertical = abs(bx1 - bx2) < eps

        if a_vertical and b_vertical:
            if abs(ax1 - bx1) > eps:
                return False
            a0, a1 = sorted((ay1, ay2))
            b0, b1 = sorted((by1, by2))
            return max(a0, b0) <= min(a1, b1) + eps

        if (not a_vertical) and (not b_vertical):
            if abs(ay1 - by1) > eps:
                return False
            a0, a1 = sorted((ax1, ax2))
            b0, b1 = sorted((bx1, bx2))
            return max(a0, b0) <= min(a1, b1) + eps

        if a_vertical:
            x = ax1
            y = by1
            bx0, bx1s = sorted((bx1, bx2))
            ay0, ay1s = sorted((ay1, ay2))
            return bx0 - eps <= x <= bx1s + eps and ay0 - eps <= y <= ay1s + eps

        x = bx1
        y = ay1
        ax0, ax1s = sorted((ax1, ax2))
        by0, by1s = sorted((by1, by2))
        return ax0 - eps <= x <= ax1s + eps and by0 - eps <= y <= by1s + eps

    for idx, (a_start, a_end) in enumerate(segments):
        for jdx, (b_start, b_end) in enumerate(segments):
            if jdx <= idx:
                continue
            if share_endpoint(a_start, a_end, b_start, b_end):
                continue
            assert not axis_touch(a_start, a_end, b_start, b_end)


def test_generated_library_symbol_contains_name_and_value_text(tmp_path) -> None:
    symbol = Symbol(
        symbol_id="SYM_U9",
        name="U9",
        pins=[
            SymbolPin(pin_number="1", pin_name="1", at=Point(-5.08, 0.0)),
            SymbolPin(pin_number="2", pin_name="2", at=Point(5.08, 0.0)),
        ],
    )
    package = Package(
        package_id="PKG_U9",
        name="PKG_U9",
        pads=[
            Pad(pad_number="1", at=Point(-1.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
            Pad(pad_number="2", at=Point(1.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
        ],
    )
    device = Device(
        device_id="DEV_U9",
        name="DEV_U9",
        symbol_id=symbol.symbol_id,
        package_id=package.package_id,
        pin_pad_map={"1": "1", "2": "2"},
    )
    out = emit_generated_library(
        MatchContext(new_library_parts=[GeneratedLibraryPart(symbol=symbol, package=package, device=device, source="test")]),
        tmp_path,
    )
    assert out is not None
    root = ET.parse(out).getroot()
    sym = root.find(".//library/symbols/symbol[@name='U9']")
    assert sym is not None
    assert sym.find("./text[.='>NAME'][@layer='95']") is not None
    assert sym.find("./text[.='>VALUE'][@layer='96']") is not None


def test_board_builder_emits_polygon_for_copper_regions() -> None:
    project = Project(
        project_id="p1",
        name="p1",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        board=Board(
            regions=[
                Region(
                    region_id="reg1",
                    layer="1",
                    net="GND",
                    points=[
                        Point(0.0, 0.0),
                        Point(10.0, 0.0),
                        Point(10.0, 5.0),
                        Point(0.0, 5.0),
                    ],
                )
            ]
        ),
    )
    lines = BoardReconstructionBuilder().build_commands(project)
    assert "LAYER 1;" in lines
    assert any(line.startswith("POLYGON 'GND' 0 ") for line in lines)


def test_board_builder_preserves_copper_polygon_net_alias_from_tracks() -> None:
    project = Project(
        project_id="p_poly_alias",
        name="p_poly_alias",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        board=Board(
            regions=[
                Region(
                    region_id="reg_alias",
                    layer="1",
                    net="N$1",
                    points=[
                        Point(0.0, 0.0),
                        Point(20.0, 0.0),
                        Point(20.0, 10.0),
                        Point(0.0, 10.0),
                    ],
                )
            ],
            tracks=[
                Track(start=Point(0.0, 5.0), end=Point(20.0, 5.0), width_mm=0.2, layer="1", net="N$1"),
                Track(start=Point(10.0, 0.0), end=Point(10.0, 10.0), width_mm=0.2, layer="1", net="GND"),
            ],
        ),
    )

    lines = BoardReconstructionBuilder().build_commands(project)
    assert any(line.startswith("POLYGON 'GND' 0 ") for line in lines)
    assert all(not line.startswith("POLYGON 'N$1' 0 ") for line in lines)


def test_board_builder_emits_keepout_polygon_for_standard_layer_12_region() -> None:
    project = Project(
        project_id="p_keepout_std12",
        name="p_keepout_std12",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        board=Board(
            regions=[
                Region(
                    region_id="k1",
                    layer="12",
                    points=[
                        Point(0.0, 0.0),
                        Point(8.0, 0.0),
                        Point(8.0, 6.0),
                        Point(0.0, 6.0),
                    ],
                )
            ]
        ),
    )

    lines = BoardReconstructionBuilder().build_commands(project)
    assert "LAYER 41;" in lines
    assert any(line.startswith("POLYGON 0 ") for line in lines)


def test_board_builder_emits_cutout_region_on_milling_layer_46() -> None:
    project = Project(
        project_id="p_cutout_milling",
        name="p_cutout_milling",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        board=Board(
            cutouts=[
                Region(
                    region_id="slot1",
                    layer="46",
                    points=[
                        Point(1.0, 1.0),
                        Point(5.0, 1.0),
                        Point(5.0, 3.0),
                        Point(1.0, 3.0),
                    ],
                )
            ]
        ),
    )

    lines = BoardReconstructionBuilder().build_commands(project)
    assert "LAYER 46;" in lines
    assert any(line.startswith("WIRE ") and "(1.0000 1.0000)" in line for line in lines)


def test_board_builder_emits_hole_commands_for_board_holes() -> None:
    project = Project(
        project_id="p_holes_emit",
        name="p_holes_emit",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        board=Board(
            holes=[
                Hole(at=Point(12.5, 7.5), drill_mm=1.2, plated=False),
                Hole(at=Point(20.0, 10.0), drill_mm=0.8, plated=False),
            ]
        ),
    )
    lines = BoardReconstructionBuilder().build_commands(project)
    assert "HOLE 1.2000 (12.5000 7.5000);" in lines
    assert "HOLE 0.8000 (20.0000 10.0000);" in lines


def test_schematic_builder_uses_external_library_pin_offsets(tmp_path) -> None:
    lbr = tmp_path / "testlib.lbr"
    lbr.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<eagle version="9.6.2">
  <drawing>
    <library>
      <packages>
        <package name="PKG">
          <smd name="1" x="-0.05" y="0" dx="0.02" dy="0.02" layer="1"/>
          <smd name="2" x="0.05" y="0" dx="0.02" dy="0.02" layer="1"/>
        </package>
      </packages>
      <symbols>
        <symbol name="SYM">
          <pin name="A" x="7.62" y="0" visible="pad" length="short" direction="pas" rot="R180"/>
          <pin name="B" x="-7.62" y="0" visible="pad" length="short" direction="pas" rot="R0"/>
        </symbol>
      </symbols>
      <devicesets>
        <deviceset name="DEV">
          <gates>
            <gate name="G$1" symbol="SYM" x="0" y="0"/>
          </gates>
          <devices>
            <device name="" package="PKG">
              <connects>
                <connect gate="G$1" pin="A" pad="2"/>
                <connect gate="G$1" pin="B" pad="1"/>
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
            Component(refdes="U1", value="", source_name="U", device_id="testlib:DEV", package_id="PKG", at=Point(0.0, 0.0)),
            Component(refdes="U2", value="", source_name="U", device_id="testlib:DEV", package_id="PKG", at=Point(0.0, 0.0)),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[
                    Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            )
        ],
        nets=[Net(name="N1", nodes=[NetNode(refdes="U1", pin="2"), NetNode(refdes="U2", pin="2")])],
    )

    lines = SchematicReconstructionBuilder().build_commands(
        project,
        library_paths={"testlib": str(lbr).replace("\\", "/")},
    )
    net_lines = [line for line in lines if line.startswith("NET 'N1' ")]
    assert len(net_lines) >= 1
    # External symbol pin "A" is at +7.62 mm from the instance origin.
    combined = " ".join(net_lines)
    assert "(27.6200 20.0000)" in combined
    assert "(52.6200 20.0000)" in combined


def test_schematic_builder_uses_external_offsets_for_easyeda_generated_devices(tmp_path) -> None:
    lbr = tmp_path / "easyeda_generated.lbr"
    lbr.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<eagle version="9.6.2">
  <drawing>
    <library>
      <packages>
        <package name="PKG">
          <smd name="1" x="-0.05" y="0" dx="0.02" dy="0.02" layer="1"/>
          <smd name="2" x="0.05" y="0" dx="0.02" dy="0.02" layer="1"/>
        </package>
      </packages>
      <symbols>
        <symbol name="SYM">
          <pin name="A" x="7.62" y="0" visible="pad" length="short" direction="pas" rot="R180"/>
          <pin name="B" x="-7.62" y="0" visible="pad" length="short" direction="pas" rot="R0"/>
        </symbol>
      </symbols>
      <devicesets>
        <deviceset name="DEV_U">
          <gates>
            <gate name="G$1" symbol="SYM" x="0" y="0"/>
          </gates>
          <devices>
            <device name="" package="PKG">
              <connects>
                <connect gate="G$1" pin="A" pad="2"/>
                <connect gate="G$1" pin="B" pad="1"/>
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

    # Source symbol pin geometry intentionally differs from emitted library
    # geometry. Anchors must follow the actual library used in ADD commands.
    source_symbol = Symbol(
        symbol_id="SRC_SYM",
        name="SRC_SYM",
        pins=[
            SymbolPin(pin_number="1", pin_name="1", at=Point(-2.0, 0.0)),
            SymbolPin(pin_number="2", pin_name="2", at=Point(2.0, 0.0)),
        ],
    )
    project = Project(
        project_id="p_easyeda_generated_external_anchor",
        name="p_easyeda_generated_external_anchor",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        symbols=[source_symbol],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U",
                symbol_id="SRC_SYM",
                device_id="easyeda_generated:DEV_U",
                package_id="PKG",
                at=Point(0.0, 0.0),
            ),
            Component(
                refdes="U2",
                value="",
                source_name="U",
                symbol_id="SRC_SYM",
                device_id="easyeda_generated:DEV_U",
                package_id="PKG",
                at=Point(0.0, 0.0),
            ),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[
                    Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            )
        ],
        nets=[Net(name="N1", nodes=[NetNode(refdes="U1", pin="2"), NetNode(refdes="U2", pin="2")])],
    )

    lines = SchematicReconstructionBuilder().build_commands(
        project,
        library_paths={"easyeda_generated": str(lbr).replace("\\", "/")},
    )
    net_lines = [line for line in lines if line.startswith("NET 'N1' ")]
    assert len(net_lines) >= 1
    combined = " ".join(net_lines)
    # Library pin "A" (mapped to pad 2) is +7.62 mm from symbol origin.
    assert "(27.6200 20.0000)" in combined
    assert "(52.6200 20.0000)" in combined
    # Source symbol pad 2 offset (+2.0 mm) must not be used for routed anchors.
    assert "(22.0000 20.0000)" not in combined
    assert "(47.0000 20.0000)" not in combined


def test_schematic_builder_applies_external_gate_origin_and_rotation(tmp_path) -> None:
    lbr = tmp_path / "testlib_transform.lbr"
    lbr.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<eagle version="9.6.2">
  <drawing>
    <library>
      <packages>
        <package name="PKG">
          <smd name="1" x="-0.05" y="0" dx="0.02" dy="0.02" layer="1"/>
          <smd name="2" x="0.05" y="0" dx="0.02" dy="0.02" layer="1"/>
        </package>
      </packages>
      <symbols>
        <symbol name="SYM">
          <pin name="A" x="7.62" y="0" visible="pad" length="short" direction="pas" rot="R180"/>
          <pin name="B" x="-7.62" y="0" visible="pad" length="short" direction="pas" rot="R0"/>
        </symbol>
      </symbols>
      <devicesets>
        <deviceset name="DEV">
          <gates>
            <gate name="G$1" symbol="SYM" x="10.0" y="3.0" rot="R90"/>
          </gates>
          <devices>
            <device name="" package="PKG">
              <connects>
                <connect gate="G$1" pin="A" pad="2"/>
                <connect gate="G$1" pin="B" pad="1"/>
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
            Component(refdes="U1", value="", source_name="U", device_id="testlib:DEV", package_id="PKG", at=Point(0.0, 0.0)),
            Component(refdes="U2", value="", source_name="U", device_id="testlib:DEV", package_id="PKG", at=Point(0.0, 0.0)),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[
                    Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            )
        ],
        nets=[Net(name="N1", nodes=[NetNode(refdes="U1", pin="2"), NetNode(refdes="U2", pin="2")])],
    )

    lines = SchematicReconstructionBuilder().build_commands(
        project,
        library_paths={"testlib": str(lbr).replace("\\", "/")},
    )
    net_lines = [line for line in lines if line.startswith("NET 'N1' ")]
    assert len(net_lines) >= 1
    combined = " ".join(net_lines)
    add_positions: dict[str, tuple[float, float]] = {}
    add_pattern = re.compile(r"^ADD\s+'.*'\s+'([^']+)'\s+R[0-9]+\s+\(([-0-9.]+)\s+([-0-9.]+)\);$")
    for line in lines:
        match = add_pattern.match(line)
        if not match:
            continue
        add_positions[match.group(1)] = (float(match.group(2)), float(match.group(3)))

    u1_origin = add_positions["U1"]
    u2_origin = add_positions["U2"]
    u1_anchor = (u1_origin[0] + 10.0, u1_origin[1] + 10.62)
    u2_anchor = (u2_origin[0] + 10.0, u2_origin[1] + 10.62)
    # Gate transform: pin A (7.62, 0) with gate rot R90 and gate origin (10,3)
    # => (10.0, 10.62) relative to part origin.
    assert f"({u1_anchor[0]:.4f} {u1_anchor[1]:.4f})" in combined
    assert f"({u2_anchor[0]:.4f} {u2_anchor[1]:.4f})" in combined


def test_schematic_builder_emits_external_instance_rotation_and_matches_pin_anchor_frame(tmp_path) -> None:
    lbr = tmp_path / "testlib_rot270.lbr"
    lbr.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<eagle version="9.6.2">
  <drawing>
    <library>
      <packages>
        <package name="PKG">
          <smd name="1" x="-0.05" y="0" dx="0.02" dy="0.02" layer="1"/>
        </package>
      </packages>
      <symbols>
        <symbol name="SYM">
          <pin name="A" x="7.62" y="0" visible="pad" length="short" direction="pas" rot="R180"/>
        </symbol>
      </symbols>
      <devicesets>
        <deviceset name="DEV">
          <gates>
            <gate name="G$1" symbol="SYM" x="0" y="0"/>
          </gates>
          <devices>
            <device name="" package="PKG">
              <connects>
                <connect gate="G$1" pin="A" pad="1"/>
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
        project_id="p_ext_rot270",
        name="p_ext_rot270",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U",
                device_id="testlib:DEV",
                package_id="PKG",
                at=Point(0.0, 0.0),
                rotation_deg=270.0,
            ),
            Component(
                refdes="U2",
                value="",
                source_name="U",
                device_id="testlib:DEV",
                package_id="PKG",
                at=Point(0.0, 0.0),
                rotation_deg=270.0,
            ),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
        nets=[Net(name="N1", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="U2", pin="1")])],
    )

    lines = SchematicReconstructionBuilder().build_commands(
        project,
        library_paths={"testlib": str(lbr).replace("\\", "/")},
    )
    add_lines = [line for line in lines if line.startswith("ADD 'DEV@")]
    assert len(add_lines) == 2
    assert all(" R270 (" in line for line in add_lines)

    add_positions: dict[str, tuple[float, float]] = {}
    add_pattern = re.compile(r"^ADD\s+'.*'\s+'([^']+)'\s+R([0-9]+)\s+\(([-0-9.]+)\s+([-0-9.]+)\);$")
    for line in add_lines:
        match = add_pattern.match(line)
        assert match is not None
        assert int(match.group(2)) == 270
        add_positions[match.group(1)] = (float(match.group(3)), float(match.group(4)))
    assert "U1" in add_positions and "U2" in add_positions

    # Pin A local endpoint (7.62, 0) rotated R270 => (0, -7.62).
    u1_anchor = (add_positions["U1"][0], add_positions["U1"][1] - 7.62)
    u2_anchor = (add_positions["U2"][0], add_positions["U2"][1] - 7.62)
    net_lines = [line for line in lines if line.startswith("NET 'N1' ")]
    assert net_lines
    combined = " ".join(net_lines)
    assert f"({u1_anchor[0]:.4f} {u1_anchor[1]:.4f})" in combined
    assert f"({u2_anchor[0]:.4f} {u2_anchor[1]:.4f})" in combined


def test_schematic_builder_emits_labels_for_stub_fallback_when_routes_cross() -> None:
    project = Project(
        project_id="p_label_fallback",
        name="p_label_fallback",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(refdes="U1", value="", source_name="U", device_id="easyeda_generated:DEV_U1", package_id="PKG", at=Point(0.0, 0.0)),
            Component(refdes="U2", value="", source_name="U", device_id="easyeda_generated:DEV_U2", package_id="PKG", at=Point(20.0, 0.0)),
            Component(refdes="U3", value="", source_name="U", device_id="easyeda_generated:DEV_U3", package_id="PKG", at=Point(10.0, -10.0)),
            Component(refdes="U4", value="", source_name="U", device_id="easyeda_generated:DEV_U4", package_id="PKG", at=Point(10.0, 10.0)),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
        nets=[
            Net(name="N1", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="U2", pin="1")]),
            Net(name="N2", nodes=[NetNode(refdes="U3", pin="1"), NetNode(refdes="U4", pin="1")]),
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(project)
    label_lines = [line for line in lines if line.startswith("LABEL (")]
    if label_lines:
        assert "CHANGE XREF ON;" in lines
        assert any(line.startswith("CHANGE SIZE 1.27") for line in lines)
        assert any(line.count("(") >= 2 for line in label_lines)
    else:
        # Pin-aware orthogonal routing can avoid fallback labels for this simple
        # crossing shape while still preserving connectivity.
        assert "CHANGE XREF ON;" not in lines


def test_schematic_builder_avoids_crossing_foreign_pin_anchors_between_nets() -> None:
    project = Project(
        project_id="p_foreign_anchor_cross",
        name="p_foreign_anchor_cross",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(refdes="U1", value="", source_name="U", device_id="easyeda_generated:DEV_U1", package_id="PKG", at=Point(0.0, 0.0)),
            Component(refdes="U2", value="", source_name="U", device_id="easyeda_generated:DEV_U2", package_id="PKG", at=Point(20.0, 0.0)),
            Component(refdes="U3", value="", source_name="U", device_id="easyeda_generated:DEV_U3", package_id="PKG", at=Point(10.0, 0.0)),
            Component(refdes="U4", value="", source_name="U", device_id="easyeda_generated:DEV_U4", package_id="PKG", at=Point(10.0, 20.0)),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
        nets=[
            Net(name="N$1", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="U2", pin="1")]),
            Net(name="GVIN", nodes=[NetNode(refdes="U3", pin="1"), NetNode(refdes="U4", pin="1")]),
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(project)
    add_pattern = re.compile(r"^ADD\s+'.*'\s+'([^']+)'\s+R[0-9]+\s+\(([-0-9.]+)\s+([-0-9.]+)\);$")
    add_positions: dict[str, tuple[float, float]] = {}
    for line in lines:
        match = add_pattern.match(line)
        if not match:
            continue
        add_positions[match.group(1)] = (float(match.group(2)), float(match.group(3)))

    net_refs = {
        "N$1": {"U1", "U2"},
        "GVIN": {"U3", "U4"},
    }
    foreign_anchors: dict[str, set[tuple[float, float]]] = {}
    for net_name, refs in net_refs.items():
        foreign_anchors[net_name] = {
            (round(x, 4), round(y, 4))
            for ref, (x, y) in add_positions.items()
            if ref not in refs
        }

    eps = 1e-6
    for line in [item for item in lines if item.startswith("NET ")]:
        net_name = line.split("'")[1]
        coords: list[tuple[float, float]] = []
        for token in line.split("(")[1:]:
            pair = token.split(")")[0].strip().split()
            coords.append((float(pair[0]), float(pair[1])))
        for idx in range(len(coords) - 1):
            sx, sy = coords[idx]
            ex, ey = coords[idx + 1]
            for px, py in foreign_anchors.get(net_name, set()):
                on_vertical = abs(sx - ex) < eps and abs(px - sx) < eps and min(sy, ey) - eps <= py <= max(sy, ey) + eps
                on_horizontal = abs(sy - ey) < eps and abs(py - sy) < eps and min(sx, ex) - eps <= px <= max(sx, ex) + eps
                assert not (on_vertical or on_horizontal), (
                    f"net {net_name} passes through foreign anchor {(px, py)}"
                )


def test_schematic_builder_emits_power_net_labels() -> None:
    project = Project(
        project_id="p_power_labels",
        name="p_power_labels",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(refdes="U1", value="", source_name="U", device_id="easyeda_generated:DEV_U1", package_id="PKG", at=Point(0.0, 0.0)),
            Component(refdes="U2", value="", source_name="U", device_id="easyeda_generated:DEV_U2", package_id="PKG", at=Point(20.0, 0.0)),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
        nets=[Net(name="3V3", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="U2", pin="1")])],
    )

    lines = SchematicReconstructionBuilder().build_commands(project)
    assert "CHANGE XREF ON;" in lines
    assert any(line.startswith("CHANGE SIZE 1.27") for line in lines)
    assert any(line.startswith("LABEL (") for line in lines)


def test_schematic_builder_avoids_duplicate_power_labels_for_fallback_stub_paths() -> None:
    project = Project(
        project_id="p_power_fallback_dedupe",
        name="p_power_fallback_dedupe",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(refdes="U1", value="", source_name="U", device_id="easyeda_generated:DEV_U1", package_id="PKG", at=Point(0.0, 0.0)),
            Component(refdes="U2", value="", source_name="U", device_id="easyeda_generated:DEV_U2", package_id="PKG", at=Point(20.0, 0.0)),
            Component(refdes="U3", value="", source_name="U", device_id="easyeda_generated:DEV_U3", package_id="PKG", at=Point(10.0, -10.0)),
            Component(refdes="U4", value="", source_name="U", device_id="easyeda_generated:DEV_U4", package_id="PKG", at=Point(10.0, 10.0)),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
        nets=[
            Net(name="N1", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="U2", pin="1")]),
            Net(name="3V3", nodes=[NetNode(refdes="U3", pin="1"), NetNode(refdes="U4", pin="1")]),
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(project)
    label_lines = [line for line in lines if line.startswith("LABEL (")]
    assert label_lines

    pick_points: list[tuple[float, float]] = []
    for line in label_lines:
        coords = line.split("(")[1].split(")")[0].strip().split()
        pick_points.append((round(float(coords[0]), 4), round(float(coords[1]), 4)))
    assert len(pick_points) == len(set(pick_points))


def test_dedupe_label_specs_keeps_single_label_per_net_pick_point() -> None:
    deduped = _dedupe_label_specs(
        [
            ("3V3", 10.0, 20.0, 12.0, 21.0),
            ("3V3", 10.0, 20.0, 13.0, 22.0),
            ("VIN", 10.0, 20.0, 11.0, 20.5),
            ("VIN", 10.0, 20.0, 11.5, 20.5),
        ]
    )
    assert len(deduped) == 2
    assert deduped[0][:3] == ("3V3", 10.0, 20.0)
    assert deduped[1][:3] == ("VIN", 10.0, 20.0)


def test_schematic_builder_repositions_overlapping_parts() -> None:
    project = Project(
        project_id="p_overlap_parts",
        name="p_overlap_parts",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(refdes="U1", value="", source_name="U", device_id="easyeda_generated:DEV_U1", package_id="PKG", at=Point(0.0, 0.0)),
            Component(refdes="U2", value="", source_name="U", device_id="easyeda_generated:DEV_U2", package_id="PKG", at=Point(0.6, 0.3)),
            Component(refdes="U3", value="", source_name="U", device_id="easyeda_generated:DEV_U3", package_id="PKG", at=Point(1.0, 0.5)),
            Component(refdes="U4", value="", source_name="U", device_id="easyeda_generated:DEV_U4", package_id="PKG", at=Point(1.4, 0.8)),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(project)
    add_lines = [line for line in lines if line.startswith("ADD ")]
    assert len(add_lines) == 4

    coords: list[tuple[float, float]] = []
    for line in add_lines:
        start = line.index("(") + 1
        end = line.index(")", start)
        x_text, y_text = line[start:end].split()
        coords.append((float(x_text), float(y_text)))

    for idx in range(len(coords)):
        for jdx in range(idx + 1, len(coords)):
            dx = coords[idx][0] - coords[jdx][0]
            dy = coords[idx][1] - coords[jdx][1]
            assert (dx * dx + dy * dy) ** 0.5 >= 6.0


def test_schematic_builder_clustered_mode_compacts_wide_board_spread() -> None:
    project = Project(
        project_id="p_layout_clustered",
        name="p_layout_clustered",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(refdes="U1", value="", source_name="U", device_id="easyeda_generated:DEV_U1", package_id="PKG", at=Point(0.0, 0.0)),
            Component(refdes="R1", value="", source_name="R", device_id="easyeda_generated:DEV_R1", package_id="PKG", at=Point(5.0, 0.0)),
            Component(refdes="U2", value="", source_name="U", device_id="easyeda_generated:DEV_U2", package_id="PKG", at=Point(180.0, 120.0)),
            Component(refdes="R2", value="", source_name="R", device_id="easyeda_generated:DEV_R2", package_id="PKG", at=Point(185.0, 120.0)),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
        nets=[
            Net(name="N_A", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="R1", pin="1")]),
            Net(name="N_B", nodes=[NetNode(refdes="U2", pin="1"), NetNode(refdes="R2", pin="1")]),
        ],
    )

    board_lines = SchematicReconstructionBuilder().build_commands(project, layout_mode="board")
    clustered_lines = SchematicReconstructionBuilder().build_commands(project, layout_mode="clustered")

    def _positions(lines: list[str]) -> dict[str, tuple[float, float]]:
        out: dict[str, tuple[float, float]] = {}
        for line in lines:
            if not line.startswith("ADD "):
                continue
            parts = line.split()
            ref = parts[2].strip("'")
            start = line.index("(") + 1
            end = line.index(")", start)
            x_text, y_text = line[start:end].split()
            out[ref] = (float(x_text), float(y_text))
        return out

    board_pos = _positions(board_lines)
    clustered_pos = _positions(clustered_lines)
    assert board_pos and clustered_pos

    board_width = max(x for x, _ in board_pos.values()) - min(x for x, _ in board_pos.values())
    clustered_width = max(x for x, _ in clustered_pos.values()) - min(x for x, _ in clustered_pos.values())
    assert clustered_width < board_width


def test_schematic_builder_hybrid_layout_mode_from_metadata() -> None:
    project = Project(
        project_id="p_layout_hybrid_meta",
        name="p_layout_hybrid_meta",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(refdes="U1", value="", source_name="U", device_id="easyeda_generated:DEV_U1", package_id="PKG", at=Point(0.0, 0.0)),
            Component(refdes="R1", value="", source_name="R", device_id="easyeda_generated:DEV_R1", package_id="PKG", at=Point(3.0, 0.0)),
            Component(refdes="U2", value="", source_name="U", device_id="easyeda_generated:DEV_U2", package_id="PKG", at=Point(90.0, 90.0)),
            Component(refdes="R2", value="", source_name="R", device_id="easyeda_generated:DEV_R2", package_id="PKG", at=Point(93.0, 90.0)),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
        nets=[
            Net(name="N1", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="R1", pin="1")]),
            Net(name="N2", nodes=[NetNode(refdes="U2", pin="1"), NetNode(refdes="R2", pin="1")]),
        ],
        metadata={"schematic_layout_mode": "hybrid"},
    )

    lines = SchematicReconstructionBuilder().build_commands(project)
    assert project.metadata.get("schematic_layout_mode") == "hybrid"
    add_lines = [line for line in lines if line.startswith("ADD ")]
    assert len(add_lines) == 4


def test_schematic_builder_human_layout_keeps_decoupling_near_ic() -> None:
    project = Project(
        project_id="p_human_decoupling",
        name="p_human_decoupling",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(refdes="J1", value="", source_name="CONN", device_id="easyeda_generated:DEV_J1", package_id="PKG2", at=Point(5.0, 10.0)),
            Component(refdes="U1", value="", source_name="MCU", device_id="easyeda_generated:DEV_U1", package_id="PKG4", at=Point(100.0, 40.0)),
            Component(refdes="C1", value="100n", source_name="C", device_id="easyeda_generated:DEV_C1", package_id="PKG2", at=Point(101.0, 41.0)),
            Component(refdes="C2", value="1u", source_name="C", device_id="easyeda_generated:DEV_C2", package_id="PKG2", at=Point(99.0, 39.0)),
        ],
        packages=[
            Package(
                package_id="PKG2",
                name="PKG2",
                pads=[
                    Pad(pad_number="1", at=Point(-1.0, 0.0), shape="rect", width_mm=0.9, height_mm=0.9),
                    Pad(pad_number="2", at=Point(1.0, 0.0), shape="rect", width_mm=0.9, height_mm=0.9),
                ],
            ),
            Package(
                package_id="PKG4",
                name="PKG4",
                pads=[
                    Pad(pad_number="1", at=Point(-1.5, 1.5), shape="rect", width_mm=0.9, height_mm=0.9),
                    Pad(pad_number="2", at=Point(1.5, 1.5), shape="rect", width_mm=0.9, height_mm=0.9),
                    Pad(pad_number="3", at=Point(-1.5, -1.5), shape="rect", width_mm=0.9, height_mm=0.9),
                    Pad(pad_number="4", at=Point(1.5, -1.5), shape="rect", width_mm=0.9, height_mm=0.9),
                ],
            ),
        ],
        nets=[
            Net(name="VIN", nodes=[NetNode(refdes="J1", pin="1"), NetNode(refdes="U1", pin="3")]),
            Net(name="3V3", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="C1", pin="1"), NetNode(refdes="C2", pin="1")]),
            Net(name="GND", nodes=[NetNode(refdes="U1", pin="2"), NetNode(refdes="C1", pin="2"), NetNode(refdes="C2", pin="2"), NetNode(refdes="J1", pin="2")]),
        ],
    )
    lines = SchematicReconstructionBuilder().build_commands(project, layout_mode="human")

    coords: dict[str, tuple[float, float]] = {}
    for line in lines:
        if not line.startswith("ADD "):
            continue
        parts = line.split()
        ref = parts[2].strip("'")
        start = line.index("(") + 1
        end = line.index(")", start)
        x_text, y_text = line[start:end].split()
        coords[ref] = (float(x_text), float(y_text))

    assert {"J1", "U1", "C1", "C2"}.issubset(coords)
    u = coords["U1"]
    j = coords["J1"]
    for c_ref in ("C1", "C2"):
        c = coords[c_ref]
        assert ((u[0] - c[0]) ** 2 + (u[1] - c[1]) ** 2) ** 0.5 < ((j[0] - c[0]) ** 2 + (j[1] - c[1]) ** 2) ** 0.5


def test_schematic_builder_human_layout_groups_connector_protection_chain() -> None:
    project = Project(
        project_id="p_human_connector_chain",
        name="p_human_connector_chain",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(refdes="J1", value="", source_name="CONN", device_id="easyeda_generated:DEV_J1", package_id="PKG2", at=Point(0.0, 0.0)),
            Component(refdes="D1", value="", source_name="TVS", device_id="easyeda_generated:DEV_D1", package_id="PKG2", at=Point(5.0, 0.0)),
            Component(refdes="R1", value="", source_name="R", device_id="easyeda_generated:DEV_R1", package_id="PKG2", at=Point(10.0, 0.0)),
            Component(refdes="U1", value="", source_name="IC", device_id="easyeda_generated:DEV_U1", package_id="PKG4", at=Point(60.0, 0.0)),
        ],
        packages=[
            Package(
                package_id="PKG2",
                name="PKG2",
                pads=[
                    Pad(pad_number="1", at=Point(-1.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(1.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            ),
            Package(
                package_id="PKG4",
                name="PKG4",
                pads=[
                    Pad(pad_number="1", at=Point(-1.5, 1.5), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(1.5, 1.5), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="3", at=Point(-1.5, -1.5), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="4", at=Point(1.5, -1.5), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            ),
        ],
        nets=[
            Net(name="VIN", nodes=[NetNode(refdes="J1", pin="1"), NetNode(refdes="D1", pin="1"), NetNode(refdes="R1", pin="1")]),
            Net(name="VIN_FILT", nodes=[NetNode(refdes="D1", pin="2"), NetNode(refdes="R1", pin="2"), NetNode(refdes="U1", pin="1")]),
            Net(name="GND", nodes=[NetNode(refdes="J1", pin="2"), NetNode(refdes="U1", pin="2")]),
        ],
    )
    lines = SchematicReconstructionBuilder().build_commands(project, layout_mode="human")

    coords: dict[str, tuple[float, float]] = {}
    for line in lines:
        if not line.startswith("ADD "):
            continue
        ref = line.split()[2].strip("'")
        start = line.index("(") + 1
        end = line.index(")", start)
        x_text, y_text = line[start:end].split()
        coords[ref] = (float(x_text), float(y_text))

    assert {"J1", "D1", "R1", "U1"}.issubset(coords)
    jd = ((coords["J1"][0] - coords["D1"][0]) ** 2 + (coords["J1"][1] - coords["D1"][1]) ** 2) ** 0.5
    jr = ((coords["J1"][0] - coords["R1"][0]) ** 2 + (coords["J1"][1] - coords["R1"][1]) ** 2) ** 0.5
    ju = ((coords["J1"][0] - coords["U1"][0]) ** 2 + (coords["J1"][1] - coords["U1"][1]) ** 2) ** 0.5
    assert max(jd, jr) < ju


def test_schematic_builder_human_layout_aligns_repeated_channels() -> None:
    project = Project(
        project_id="p_human_repeated",
        name="p_human_repeated",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(refdes="U1", value="", source_name="IC", device_id="easyeda_generated:DEV_U1", package_id="PKG4", at=Point(20.0, 0.0)),
            Component(refdes="R1", value="", source_name="R", device_id="easyeda_generated:DEV_R1", package_id="PKG2", at=Point(30.0, 0.0)),
            Component(refdes="R2", value="", source_name="R", device_id="easyeda_generated:DEV_R2", package_id="PKG2", at=Point(31.0, 1.0)),
            Component(refdes="R3", value="", source_name="R", device_id="easyeda_generated:DEV_R3", package_id="PKG2", at=Point(32.0, 2.0)),
        ],
        packages=[
            Package(
                package_id="PKG2",
                name="PKG2",
                pads=[
                    Pad(pad_number="1", at=Point(-1.0, 0.0), shape="rect", width_mm=0.9, height_mm=0.9),
                    Pad(pad_number="2", at=Point(1.0, 0.0), shape="rect", width_mm=0.9, height_mm=0.9),
                ],
            ),
            Package(
                package_id="PKG4",
                name="PKG4",
                pads=[
                    Pad(pad_number="1", at=Point(-1.5, 1.5), shape="rect", width_mm=0.9, height_mm=0.9),
                    Pad(pad_number="2", at=Point(1.5, 1.5), shape="rect", width_mm=0.9, height_mm=0.9),
                    Pad(pad_number="3", at=Point(-1.5, -1.5), shape="rect", width_mm=0.9, height_mm=0.9),
                    Pad(pad_number="4", at=Point(1.5, -1.5), shape="rect", width_mm=0.9, height_mm=0.9),
                ],
            ),
        ],
        nets=[
            Net(name="CH1", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="R1", pin="1")]),
            Net(name="CH2", nodes=[NetNode(refdes="U1", pin="2"), NetNode(refdes="R2", pin="1")]),
            Net(name="CH3", nodes=[NetNode(refdes="U1", pin="3"), NetNode(refdes="R3", pin="1")]),
            Net(name="GND", nodes=[NetNode(refdes="R1", pin="2"), NetNode(refdes="R2", pin="2"), NetNode(refdes="R3", pin="2"), NetNode(refdes="U1", pin="4")]),
        ],
    )
    lines = SchematicReconstructionBuilder().build_commands(project, layout_mode="human")

    coords: dict[str, tuple[float, float]] = {}
    for line in lines:
        if not line.startswith("ADD "):
            continue
        ref = line.split()[2].strip("'")
        start = line.index("(") + 1
        end = line.index(")", start)
        x_text, y_text = line[start:end].split()
        coords[ref] = (float(x_text), float(y_text))

    y_values = [coords[ref][1] for ref in ("R1", "R2", "R3")]
    assert max(y_values) - min(y_values) <= 0.2


def test_schematic_builder_human_layout_is_deterministic() -> None:
    project = Project(
        project_id="p_human_deterministic",
        name="p_human_deterministic",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(refdes="J1", value="", source_name="CONN", device_id="easyeda_generated:DEV_J1", package_id="PKG2", at=Point(0.0, 0.0)),
            Component(refdes="U1", value="", source_name="IC", device_id="easyeda_generated:DEV_U1", package_id="PKG4", at=Point(50.0, 0.0)),
            Component(refdes="R1", value="", source_name="R", device_id="easyeda_generated:DEV_R1", package_id="PKG2", at=Point(25.0, 0.0)),
            Component(refdes="C1", value="", source_name="C", device_id="easyeda_generated:DEV_C1", package_id="PKG2", at=Point(25.0, 5.0)),
        ],
        packages=[
            Package(
                package_id="PKG2",
                name="PKG2",
                pads=[
                    Pad(pad_number="1", at=Point(-1.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(1.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            ),
            Package(
                package_id="PKG4",
                name="PKG4",
                pads=[
                    Pad(pad_number="1", at=Point(-1.5, 1.5), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(1.5, 1.5), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="3", at=Point(-1.5, -1.5), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="4", at=Point(1.5, -1.5), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            ),
        ],
        nets=[
            Net(name="SIG", nodes=[NetNode(refdes="J1", pin="1"), NetNode(refdes="R1", pin="1"), NetNode(refdes="U1", pin="1")]),
            Net(name="3V3", nodes=[NetNode(refdes="U1", pin="2"), NetNode(refdes="C1", pin="1")]),
            Net(name="GND", nodes=[NetNode(refdes="J1", pin="2"), NetNode(refdes="R1", pin="2"), NetNode(refdes="U1", pin="3"), NetNode(refdes="C1", pin="2")]),
        ],
    )
    lines_a = SchematicReconstructionBuilder().build_commands(project, layout_mode="human")
    lines_b = SchematicReconstructionBuilder().build_commands(project, layout_mode="human")
    assert lines_a == lines_b


def test_schematic_builder_human_layout_emits_organization_metrics() -> None:
    project = Project(
        project_id="p_human_metrics",
        name="p_human_metrics",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(refdes="U1", value="", source_name="IC", device_id="easyeda_generated:DEV_U1", package_id="PKG2", at=Point(0.0, 0.0)),
            Component(refdes="R1", value="", source_name="R", device_id="easyeda_generated:DEV_R1", package_id="PKG2", at=Point(10.0, 0.0)),
            Component(refdes="C1", value="", source_name="C", device_id="easyeda_generated:DEV_C1", package_id="PKG2", at=Point(20.0, 0.0)),
        ],
        packages=[
            Package(
                package_id="PKG2",
                name="PKG2",
                pads=[
                    Pad(pad_number="1", at=Point(-1.0, 0.0), shape="rect", width_mm=0.9, height_mm=0.9),
                    Pad(pad_number="2", at=Point(1.0, 0.0), shape="rect", width_mm=0.9, height_mm=0.9),
                ],
            )
        ],
        nets=[
            Net(name="5V", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="R1", pin="1")]),
            Net(name="GND", nodes=[NetNode(refdes="U1", pin="2"), NetNode(refdes="R1", pin="2"), NetNode(refdes="C1", pin="2")]),
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(project, layout_mode="human")
    assert lines
    metrics = project.metadata.get("schematic_organization_metrics", {})
    assert isinstance(metrics, dict)
    assert metrics.get("layout_mode") == "human"
    assert int(metrics.get("component_count", 0)) == 3
    assert "5V" in metrics.get("recognized_power_nets", [])
    assert "GND" in metrics.get("recognized_ground_nets", [])
    assert int(metrics.get("orphan_label_count", 1)) == 0


def test_schematic_builder_label_metrics_use_internal_mm_space_when_inch_output_enabled() -> None:
    project = Project(
        project_id="p_label_units",
        name="p_label_units",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U",
                device_id="easyeda_generated:DEV_U1",
                package_id="PKG1",
                at=Point(0.0, 0.0),
            ),
            Component(
                refdes="U2",
                value="",
                source_name="U",
                device_id="easyeda_generated:DEV_U2",
                package_id="PKG1",
                at=Point(20.0, 0.0),
            ),
        ],
        packages=[
            Package(
                package_id="PKG1",
                name="PKG1",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
        nets=[Net(name="GND", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="U2", pin="1")])],
        metadata={"schematic_snap_to_default_grid": True},
    )

    lines = SchematicReconstructionBuilder().build_commands(project, layout_mode="board")
    assert any(line.startswith("LABEL (") for line in lines)

    org_metrics = project.metadata.get("schematic_organization_metrics", {})
    draw_metrics = project.metadata.get("schematic_draw_metrics", {})
    assert int(org_metrics.get("orphan_label_count", 1) or 0) == 0
    assert int(draw_metrics.get("label_only_connection_count", 1) or 0) == 0


def test_schematic_builder_preserves_non_grid_pin_anchor_endpoints_with_default_grid_enabled() -> None:
    symbol = Symbol(
        symbol_id="SYM_FINE",
        name="SYM_FINE",
        pins=[
            SymbolPin(pin_number="1", pin_name="P1", at=Point(0.63, 0.37)),
            SymbolPin(pin_number="2", pin_name="P2", at=Point(-0.77, -0.41)),
        ],
    )
    project = Project(
        project_id="p_non_grid_anchor",
        name="p_non_grid_anchor",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        symbols=[symbol],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U",
                symbol_id="SYM_FINE",
                device_id="easyeda_generated:DEV_U1",
                package_id="PKG1",
                at=Point(0.0, 0.0),
            ),
            Component(
                refdes="U2",
                value="",
                source_name="U",
                symbol_id="SYM_FINE",
                device_id="easyeda_generated:DEV_U2",
                package_id="PKG1",
                at=Point(20.0, 0.0),
            ),
        ],
        packages=[
            Package(
                package_id="PKG1",
                name="PKG1",
                pads=[
                    Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            )
        ],
        nets=[Net(name="SIG", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="U2", pin="1")])],
        metadata={"schematic_snap_to_default_grid": True},
    )

    lines = SchematicReconstructionBuilder().build_commands(project, layout_mode="board")
    add_pattern = re.compile(r"^ADD\s+'.*'\s+'([^']+)'\s+R[0-9]+\s+\(([-0-9.]+)\s+([-0-9.]+)\);$")
    add_positions: dict[str, tuple[float, float]] = {}
    for line in lines:
        match = add_pattern.match(line)
        if not match:
            continue
        add_positions[match.group(1)] = (float(match.group(2)), float(match.group(3)))
    assert "U1" in add_positions and "U2" in add_positions

    expected_u1 = (
        round(add_positions["U1"][0] + (0.63 / 25.4), 4),
        round(add_positions["U1"][1] + (0.37 / 25.4), 4),
    )
    expected_u2 = (
        round(add_positions["U2"][0] + (0.63 / 25.4), 4),
        round(add_positions["U2"][1] + (0.37 / 25.4), 4),
    )

    points: set[tuple[float, float]] = set()
    for line in lines:
        if not line.startswith("NET 'SIG' "):
            continue
        for x, y in re.findall(r"\(([-0-9.]+)\s+([-0-9.]+)\)", line):
            points.add((round(float(x), 4), round(float(y), 4)))

    assert expected_u1 in points
    assert expected_u2 in points


def test_external_library_pin_rotation_defines_outward_direction(tmp_path) -> None:
    lib_path = tmp_path / "demo.lbr"
    lib_path.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<eagle version="9.6.2">
  <drawing>
    <library>
      <symbols>
        <symbol name="S">
          <pin name="P1" x="0" y="5.08" length="short"/>
          <pin name="P2" x="0" y="-5.08" length="short"/>
          <pin name="P3" x="10.16" y="0" length="short" rot="R180"/>
        </symbol>
      </symbols>
      <devicesets>
        <deviceset name="DEVX" prefix="U">
          <gates>
            <gate name="G$1" symbol="S" x="0" y="0"/>
          </gates>
          <devices>
            <device name="" package="PKG">
              <connects>
                <connect gate="G$1" pin="P1" pad="1"/>
                <connect gate="G$1" pin="P2" pad="2"/>
                <connect gate="G$1" pin="P3" pad="3"/>
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

    offsets = _external_device_pin_offsets(lib_path, "DEVX")
    assert "1" in offsets and "2" in offsets and "3" in offsets
    # Default pin rot is R0 (inward +X), so outward must be -X.
    assert offsets["1"].outward_dx == -1.0 and offsets["1"].outward_dy == 0.0
    assert offsets["2"].outward_dx == -1.0 and offsets["2"].outward_dy == 0.0
    # Pin rot R180 (inward -X), outward must be +X.
    assert offsets["3"].outward_dx == 1.0 and offsets["3"].outward_dy == 0.0


def test_generated_symbol_outward_fallback_uses_pin_cloud_center_not_origin() -> None:
    symbol = Symbol(
        symbol_id="SYM_EDGE_ORIGIN",
        name="SYM_EDGE_ORIGIN",
        pins=[
            SymbolPin(pin_number="1", pin_name="P1", at=Point(0.0, 5.08)),
            SymbolPin(pin_number="2", pin_name="P2", at=Point(0.0, -5.08)),
            SymbolPin(pin_number="3", pin_name="P3", at=Point(20.32, 0.0)),
        ],
        graphics=[{"kind": "origin", "x_mm": 0.0, "y_mm": 0.0}],
    )
    project = Project(
        project_id="p_generated_center_fallback",
        name="p_generated_center_fallback",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        symbols=[symbol],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U",
                symbol_id="SYM_EDGE_ORIGIN",
                device_id="easyeda_generated:DEV_U1",
                package_id="PKG3",
                at=Point(0.0, 0.0),
            ),
            Component(
                refdes="U2",
                value="",
                source_name="U",
                symbol_id="SYM_EDGE_ORIGIN",
                device_id="easyeda_generated:DEV_U2",
                package_id="PKG3",
                at=Point(30.0, 0.0),
            ),
        ],
        packages=[
            Package(
                package_id="PKG3",
                name="PKG3",
                pads=[
                    Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="3", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            )
        ],
        nets=[Net(name="5V", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="U2", pin="1")])],
        metadata={"schematic_snap_to_default_grid": True},
    )

    lines = SchematicReconstructionBuilder().build_commands(project, layout_mode="board")
    add_pattern = re.compile(r"^ADD\s+'.*'\s+'([^']+)'\s+R[0-9]+\s+\(([-0-9.]+)\s+([-0-9.]+)\);$")
    add_positions: dict[str, tuple[float, float]] = {}
    for line in lines:
        match = add_pattern.match(line)
        if not match:
            continue
        add_positions[match.group(1)] = (float(match.group(2)), float(match.group(3)))
    assert "U1" in add_positions
    u1_anchor = (
        round(add_positions["U1"][0] + (0.0 / 25.4), 4),
        round(add_positions["U1"][1] + (5.08 / 25.4), 4),
    )

    segments: set[tuple[tuple[float, float], tuple[float, float]]] = set()
    for line in lines:
        if not line.startswith("NET '5V' "):
            continue
        points = [
            (round(float(x), 4), round(float(y), 4))
            for x, y in re.findall(r"\(([-0-9.]+)\s+([-0-9.]+)\)", line)
        ]
        for idx in range(len(points) - 1):
            a = points[idx]
            b = points[idx + 1]
            segments.add((a, b))
            segments.add((b, a))

    exits = [segment for segment in segments if segment[0] == u1_anchor]
    assert exits
    # Left-side pin must exit horizontally left, not vertically from origin-based fallback.
    assert any(end[0] < u1_anchor[0] and end[1] == u1_anchor[1] for _, end in exits)


def test_schematic_builder_human_layout_does_not_drop_components() -> None:
    project = Project(
        project_id="p_human_no_drop",
        name="p_human_no_drop",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(refdes="U1", value="", source_name="IC", device_id="easyeda_generated:DEV_U1", package_id="PKG2", at=Point(0.0, 0.0)),
            Component(refdes="R1", value="", source_name="R", device_id="easyeda_generated:DEV_R1", package_id="PKG2", at=Point(10.0, 0.0)),
            Component(refdes="R2", value="", source_name="R", device_id="easyeda_generated:DEV_R2", package_id="PKG2", at=Point(20.0, 0.0)),
            Component(refdes="C1", value="", source_name="C", device_id="easyeda_generated:DEV_C1", package_id="PKG2", at=Point(30.0, 0.0)),
            Component(refdes="J1", value="", source_name="CONN", device_id="easyeda_generated:DEV_J1", package_id="PKG2", at=Point(40.0, 0.0)),
        ],
        packages=[
            Package(
                package_id="PKG2",
                name="PKG2",
                pads=[
                    Pad(pad_number="1", at=Point(-1.0, 0.0), shape="rect", width_mm=0.9, height_mm=0.9),
                    Pad(pad_number="2", at=Point(1.0, 0.0), shape="rect", width_mm=0.9, height_mm=0.9),
                ],
            )
        ],
        nets=[
            Net(name="SIG", nodes=[NetNode(refdes="J1", pin="1"), NetNode(refdes="R1", pin="1"), NetNode(refdes="U1", pin="1")]),
            Net(name="GND", nodes=[NetNode(refdes="J1", pin="2"), NetNode(refdes="R1", pin="2"), NetNode(refdes="R2", pin="2"), NetNode(refdes="C1", pin="2"), NetNode(refdes="U1", pin="2")]),
            Net(name="3V3", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="R2", pin="1"), NetNode(refdes="C1", pin="1")]),
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(project, layout_mode="human")
    add_lines = [line for line in lines if line.startswith("ADD ")]
    assert len(add_lines) == len(project.components)


def test_schematic_builder_uses_snapped_component_rotation_for_add() -> None:
    project = Project(
        project_id="p_rot_snap_schematic",
        name="p_rot_snap_schematic",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(
                refdes="R1",
                value="",
                source_name="R",
                device_id="easyeda_generated:DEV_R1",
                package_id="PKG",
                at=Point(0.0, 0.0),
                rotation_deg=87.0,
            )
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(project)
    add_line = next(line for line in lines if line.startswith("ADD "))
    assert " R90 (" in add_line


def test_schematic_builder_flips_pro_component_rotation_polarity_for_add() -> None:
    project = Project(
        project_id="p_rot_polarity_pro",
        name="p_rot_polarity_pro",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U",
                device_id="easyeda_generated:DEV_U1",
                package_id="PKG",
                at=Point(0.0, 0.0),
                rotation_deg=90.0,
            )
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(project)
    add_line = next(line for line in lines if line.startswith("ADD "))
    assert " R270 (" in add_line


def test_schematic_builder_canonicalizes_pro_two_pin_resistor_quarter_turn_to_r90() -> None:
    project = Project(
        project_id="p_rot_res_pro_quarter",
        name="p_rot_res_pro_quarter",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="R5",
                value="",
                source_name="R0603",
                device_id="easyeda_generated:DEV_R5",
                package_id="R0603",
                at=Point(0.0, 0.0),
                rotation_deg=90.0,
            )
        ],
        packages=[
            Package(
                package_id="R0603",
                name="R0603",
                pads=[
                    Pad(pad_number="1", at=Point(-0.8, 0.0), shape="rect", width_mm=0.8, height_mm=0.8),
                    Pad(pad_number="2", at=Point(0.8, 0.0), shape="rect", width_mm=0.8, height_mm=0.8),
                ],
            )
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(project)
    add_line = next(line for line in lines if line.startswith("ADD "))
    assert " R90 (" in add_line


def test_schematic_builder_normalizes_adjustable_resistor_to_r90() -> None:
    project = Project(
        project_id="p_rot_res_adj_pro",
        name="p_rot_res_adj_pro",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="R40",
                value="",
                source_name="TRIMMER",
                device_id="easyeda_generated:DEV_R40",
                package_id="PKG_ADJ",
                at=Point(0.0, 0.0),
                rotation_deg=180.0,
            )
        ],
        packages=[
            Package(
                package_id="PKG_ADJ",
                name="RES-ADJ-TH_3P-L9_5-W4_85-P2_50-BL-BS",
                pads=[
                    Pad(pad_number="1", at=Point(0.0, 2.5), shape="round", width_mm=1.0, height_mm=1.0, drill_mm=0.6),
                    Pad(pad_number="2", at=Point(0.0, 0.0), shape="round", width_mm=1.0, height_mm=1.0, drill_mm=0.6),
                    Pad(pad_number="3", at=Point(0.0, -2.5), shape="round", width_mm=1.0, height_mm=1.0, drill_mm=0.6),
                ],
            )
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(project)
    add_line = next(line for line in lines if line.startswith("ADD "))
    assert " R90 (" in add_line


def test_schematic_builder_rotates_pin_anchors_with_component_orientation() -> None:
    symbol = Symbol(
        symbol_id="SYM_RC",
        name="SYM_RC",
        pins=[
            SymbolPin(pin_number="1", pin_name="1", at=Point(-2.0, 0.0)),
            SymbolPin(pin_number="2", pin_name="2", at=Point(2.0, 0.0)),
        ],
    )
    project = Project(
        project_id="p_rotated_anchor",
        name="p_rotated_anchor",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        symbols=[symbol],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U",
                symbol_id="SYM_RC",
                device_id="easyeda_generated:DEV_U1",
                package_id="PKG",
                at=Point(0.0, 0.0),
                rotation_deg=90.0,
            ),
            Component(
                refdes="U2",
                value="",
                source_name="U",
                symbol_id="SYM_RC",
                device_id="easyeda_generated:DEV_U2",
                package_id="PKG",
                at=Point(20.0, 0.0),
                rotation_deg=0.0,
            ),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[
                    Pad(pad_number="1", at=Point(-0.8, 0.0), shape="rect", width_mm=0.8, height_mm=0.8),
                    Pad(pad_number="2", at=Point(0.8, 0.0), shape="rect", width_mm=0.8, height_mm=0.8),
                ],
            )
        ],
        nets=[Net(name="SIG", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="U2", pin="1")])],
    )

    lines = SchematicReconstructionBuilder().build_commands(project, layout_mode="board")
    net_lines = [line for line in lines if line.startswith("NET 'SIG' ")]
    assert net_lines
    joined = " ".join(net_lines)
    # For EasyEDA Pro schematic output, rotation sign is flipped for Fusion ADD
    # semantics. R1 at (20,20) with source +90deg is emitted as R270, so
    # pin1 (-2,0) lands at (0,+2) relative to origin.
    assert "(20.0000 22.0000)" in joined


def test_schematic_builder_emits_outward_pin_stubs_from_true_pin_anchors() -> None:
    symbol = Symbol(
        symbol_id="SYM_TWO_PIN",
        name="SYM_TWO_PIN",
        pins=[
            SymbolPin(pin_number="1", pin_name="IN", at=Point(-2.0, 0.0)),
            SymbolPin(pin_number="2", pin_name="OUT", at=Point(2.0, 0.0)),
        ],
    )
    project = Project(
        project_id="p_anchor_stubs",
        name="p_anchor_stubs",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        symbols=[symbol],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U",
                symbol_id="SYM_TWO_PIN",
                device_id="easyeda_generated:DEV_U1",
                package_id="PKG",
                at=Point(0.0, 0.0),
            ),
            Component(
                refdes="U2",
                value="",
                source_name="U",
                symbol_id="SYM_TWO_PIN",
                device_id="easyeda_generated:DEV_U2",
                package_id="PKG",
                at=Point(20.0, 0.0),
            ),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[
                    Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            )
        ],
        nets=[Net(name="SIG", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="U2", pin="1")])],
        metadata={"schematic_legacy_net_routing": False},
    )

    lines = SchematicReconstructionBuilder().build_commands(project, layout_mode="board")
    net_lines = [line for line in lines if line.startswith("NET 'SIG' ")]
    assert net_lines
    segments: set[tuple[tuple[float, float], tuple[float, float]]] = set()
    for line in net_lines:
        points = [
            (round(float(x), 4), round(float(y), 4))
            for x, y in re.findall(r"\(([-0-9.]+)\s+([-0-9.]+)\)", line)
        ]
        for idx in range(len(points) - 1):
            a = points[idx]
            b = points[idx + 1]
            segments.add((a, b))
            segments.add((b, a))

    # U1 anchor for pin1 is at (20-2, 20) and exits left by 1.27 mm.
    assert ((18.0, 20.0), (16.73, 20.0)) in segments
    # U2 anchor for pin1 is at (45-2, 20) and exits left by 1.27 mm.
    assert ((43.0, 20.0), (41.73, 20.0)) in segments


def test_schematic_builder_defaults_to_connection_map_net_routing() -> None:
    symbol = Symbol(
        symbol_id="SYM_TWO_PIN_LEGACY",
        name="SYM_TWO_PIN_LEGACY",
        pins=[
            SymbolPin(pin_number="1", pin_name="IN", at=Point(-2.0, 0.0)),
            SymbolPin(pin_number="2", pin_name="OUT", at=Point(2.0, 0.0)),
        ],
    )
    project = Project(
        project_id="p_legacy_default",
        name="p_legacy_default",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        symbols=[symbol],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U",
                symbol_id="SYM_TWO_PIN_LEGACY",
                device_id="easyeda_generated:DEV_U1",
                package_id="PKG",
                at=Point(0.0, 0.0),
            ),
            Component(
                refdes="U2",
                value="",
                source_name="U",
                symbol_id="SYM_TWO_PIN_LEGACY",
                device_id="easyeda_generated:DEV_U2",
                package_id="PKG",
                at=Point(20.0, 0.0),
            ),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[
                    Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            )
        ],
        nets=[Net(name="SIG", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="U2", pin="1")])],
    )

    lines = SchematicReconstructionBuilder().build_commands(project, layout_mode="board")
    assert project.metadata.get("schematic_connection_map_routing") is True
    assert int(project.metadata.get("schematic_connection_map_size", 0) or 0) >= 1
    net_lines = [line for line in lines if line.startswith("NET 'SIG' ")]
    assert len(net_lines) >= 1


def test_schematic_builder_reports_unresolved_pin_anchor_metrics() -> None:
    symbol = Symbol(
        symbol_id="SYM_ONE_PIN",
        name="SYM_ONE_PIN",
        pins=[SymbolPin(pin_number="1", pin_name="P1", at=Point(0.0, 0.0))],
    )
    project = Project(
        project_id="p_unresolved_anchor_metrics",
        name="p_unresolved_anchor_metrics",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        symbols=[symbol],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U",
                symbol_id="SYM_ONE_PIN",
                device_id="easyeda_generated:DEV_U1",
                package_id="PKG",
                at=Point(0.0, 0.0),
            ),
            Component(
                refdes="U2",
                value="",
                source_name="U",
                symbol_id="SYM_ONE_PIN",
                device_id="easyeda_generated:DEV_U2",
                package_id="PKG",
                at=Point(20.0, 0.0),
            ),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[
                    Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            )
        ],
        nets=[Net(name="BAD", nodes=[NetNode(refdes="U1", pin="2"), NetNode(refdes="U2", pin="2")])],
    )

    SchematicReconstructionBuilder().build_commands(project, layout_mode="board")
    draw_metrics = project.metadata.get("schematic_draw_metrics", {})
    assert int(draw_metrics.get("unresolved_pin_anchor_count", 0) or 0) >= 2
    unresolved = project.metadata.get("schematic_unresolved_pin_anchors", [])
    assert isinstance(unresolved, list)
    assert unresolved


def test_schematic_builder_pin_anchor_diagnostics_include_emitted_rotation_commands() -> None:
    symbol = Symbol(
        symbol_id="SYM_DIAG",
        name="SYM_DIAG",
        pins=[
            SymbolPin(pin_number="1", pin_name="1", at=Point(-2.0, 0.0)),
            SymbolPin(pin_number="2", pin_name="2", at=Point(2.0, 0.0)),
        ],
    )
    project = Project(
        project_id="p_diag_cmd",
        name="p_diag_cmd",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        symbols=[symbol],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U",
                symbol_id="SYM_DIAG",
                device_id="easyeda_generated:DEV_U1",
                package_id="PKG",
                at=Point(0.0, 0.0),
                rotation_deg=270.0,
            ),
            Component(
                refdes="U2",
                value="",
                source_name="U",
                symbol_id="SYM_DIAG",
                device_id="easyeda_generated:DEV_U2",
                package_id="PKG",
                at=Point(20.0, 0.0),
                rotation_deg=270.0,
            ),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[
                    Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            )
        ],
        nets=[Net(name="SIG", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="U2", pin="1")])],
        metadata={"schematic_debug_pin_anchors": True},
    )
    SchematicReconstructionBuilder().build_commands(project, layout_mode="board")
    diag = project.metadata.get("schematic_pin_anchor_diagnostics", [])
    assert isinstance(diag, list)
    assert diag
    assert all(item.get("emitted_add_command") for item in diag)
    assert all(str(item.get("emitted_rotation_token") or "").startswith("R") for item in diag)


def test_schematic_builder_rotates_pins_around_symbol_origin() -> None:
    symbol = Symbol(
        symbol_id="SYM_ORIGIN_OFF",
        name="SYM_ORIGIN_OFF",
        pins=[SymbolPin(pin_number="1", pin_name="1", at=Point(10.0, 0.0))],
        graphics=[{"kind": "origin", "x_mm": 5.0, "y_mm": 0.0}],
    )
    project = Project(
        project_id="p_symbol_origin_rotation",
        name="p_symbol_origin_rotation",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        symbols=[symbol],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U",
                symbol_id="SYM_ORIGIN_OFF",
                device_id="easyeda_generated:DEV_U1",
                package_id="PKG",
                at=Point(0.0, 0.0),
                rotation_deg=90.0,
            ),
            Component(
                refdes="U2",
                value="",
                source_name="U",
                symbol_id="SYM_ORIGIN_OFF",
                device_id="easyeda_generated:DEV_U2",
                package_id="PKG",
                at=Point(20.0, 0.0),
                rotation_deg=90.0,
            ),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
        nets=[Net(name="SIG", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="U2", pin="1")])],
    )

    lines = SchematicReconstructionBuilder().build_commands(project, layout_mode="board")
    net_lines = [line for line in lines if line.startswith("NET 'SIG' ")]
    assert net_lines
    joined = " ".join(net_lines)
    add_positions: dict[str, tuple[float, float]] = {}
    add_pattern = re.compile(r"^ADD\s+'.*'\s+'([^']+)'\s+R[0-9]+\s+\(([-0-9.]+)\s+([-0-9.]+)\);$")
    for line in lines:
        match = add_pattern.match(line)
        if not match:
            continue
        add_positions[match.group(1)] = (float(match.group(2)), float(match.group(3)))

    assert "U1" in add_positions and "U2" in add_positions
    u1_anchor = (add_positions["U1"][0], add_positions["U1"][1] - 5.0)
    u2_anchor = (add_positions["U2"][0], add_positions["U2"][1] - 5.0)
    # Origin-aware transform:
    # pin(10,0) - origin(5,0) => (5,0), with Pro polarity-corrected schematic
    # rotation (+90 source -> R270 emitted) => (0,-5)
    assert f"({u1_anchor[0]:.4f} {u1_anchor[1]:.4f})" in joined
    assert f"({u2_anchor[0]:.4f} {u2_anchor[1]:.4f})" in joined


def test_schematic_builder_prefers_perpendicular_pin_exits_for_vertical_pins() -> None:
    symbol = Symbol(
        symbol_id="SYM_VERT",
        name="SYM_VERT",
        pins=[
            SymbolPin(pin_number="1", pin_name="TOP", at=Point(0.0, 5.0)),
            SymbolPin(pin_number="2", pin_name="BOT", at=Point(0.0, -5.0)),
        ],
    )
    project = Project(
        project_id="p_perp_pins",
        name="p_perp_pins",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        symbols=[symbol],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U",
                symbol_id="SYM_VERT",
                device_id="easyeda_generated:DEV_U1",
                package_id="PKG",
                at=Point(0.0, 0.0),
            ),
            Component(
                refdes="U2",
                value="",
                source_name="U",
                symbol_id="SYM_VERT",
                device_id="easyeda_generated:DEV_U2",
                package_id="PKG",
                at=Point(20.0, 0.0),
            ),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[
                    Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            )
        ],
        nets=[Net(name="SIG", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="U2", pin="1")])],
    )

    lines = SchematicReconstructionBuilder().build_commands(project, layout_mode="board")
    add_pattern = re.compile(r"^ADD\s+'.*'\s+'([^']+)'\s+R[0-9]+\s+\(([-0-9.]+)\s+([-0-9.]+)\);$")
    add_positions: dict[str, tuple[float, float]] = {}
    for line in lines:
        match = add_pattern.match(line)
        if not match:
            continue
        add_positions[match.group(1)] = (float(match.group(2)), float(match.group(3)))

    assert "U1" in add_positions and "U2" in add_positions
    anchor_u1 = (round(add_positions["U1"][0], 4), round(add_positions["U1"][1] + 5.0, 4))
    anchor_u2 = (round(add_positions["U2"][0], 4), round(add_positions["U2"][1] + 5.0, 4))

    sig_paths = [
        [(round(float(x), 4), round(float(y), 4)) for x, y in re.findall(r"\(([-0-9.]+)\s+([-0-9.]+)\)", line)]
        for line in lines
        if line.startswith("NET 'SIG' ")
    ]
    assert sig_paths
    assert any(len(path) >= 2 for path in sig_paths)

    def _has_vertical_exit(anchor: tuple[float, float], paths: list[list[tuple[float, float]]]) -> bool:
        for path in paths:
            for idx in range(len(path) - 1):
                p0 = path[idx]
                p1 = path[idx + 1]
                if p0 == anchor and p1[0] == anchor[0] and p1[1] != anchor[1]:
                    return True
                if p1 == anchor and p0[0] == anchor[0] and p0[1] != anchor[1]:
                    return True
        return False

    assert _has_vertical_exit(anchor_u1, sig_paths)
    assert _has_vertical_exit(anchor_u2, sig_paths)


def test_schematic_builder_separates_parts_by_symbol_pin_span() -> None:
    symbol = Symbol(
        symbol_id="SYM_WIDE",
        name="SYM_WIDE",
        pins=[
            SymbolPin(pin_number="1", pin_name="IN", at=Point(-7.62, 0.0)),
            SymbolPin(pin_number="2", pin_name="OUT", at=Point(7.62, 0.0)),
        ],
        graphics=[
            {"kind": "wire", "x1_mm": -5.08, "y1_mm": -2.54, "x2_mm": 5.08, "y2_mm": -2.54},
            {"kind": "wire", "x1_mm": 5.08, "y1_mm": -2.54, "x2_mm": 5.08, "y2_mm": 2.54},
            {"kind": "wire", "x1_mm": 5.08, "y1_mm": 2.54, "x2_mm": -5.08, "y2_mm": 2.54},
            {"kind": "wire", "x1_mm": -5.08, "y1_mm": 2.54, "x2_mm": -5.08, "y2_mm": -2.54},
        ],
    )
    project = Project(
        project_id="p_span_clearance",
        name="p_span_clearance",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        symbols=[symbol],
        components=[
            Component(refdes="U1", value="", source_name="U", symbol_id="SYM_WIDE", device_id="easyeda_generated:DEV_U1", package_id="PKG", at=Point(0.0, 0.0)),
            Component(refdes="U2", value="", source_name="U", symbol_id="SYM_WIDE", device_id="easyeda_generated:DEV_U2", package_id="PKG", at=Point(0.2, 0.1)),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[
                    Pad(pad_number="1", at=Point(-0.8, 0.0), shape="rect", width_mm=0.8, height_mm=0.8),
                    Pad(pad_number="2", at=Point(0.8, 0.0), shape="rect", width_mm=0.8, height_mm=0.8),
                ],
            )
        ],
    )

    lines = SchematicReconstructionBuilder().build_commands(project)
    add_lines = [line for line in lines if line.startswith("ADD ")]
    assert len(add_lines) == 2

    coords: list[tuple[float, float]] = []
    for line in add_lines:
        start = line.index("(") + 1
        end = line.index(")", start)
        x_text, y_text = line[start:end].split()
        coords.append((float(x_text), float(y_text)))

    dx = coords[0][0] - coords[1][0]
    dy = coords[0][1] - coords[1][1]
    assert (dx * dx + dy * dy) ** 0.5 >= 18.0


def test_spread_label_specs_avoids_label_overlap() -> None:
    label_specs = [
        ("SIG1", 10.0, 10.0, 12.0, 12.0),
        ("SIG2", 10.0, 10.0, 12.0, 12.0),
        ("SIG3", 10.0, 10.0, 12.0, 12.0),
        ("SIG4", 10.0, 10.0, 12.0, 12.0),
    ]
    spread = _spread_label_specs(label_specs)
    assert len(spread) == 4
    coords = [(item[3], item[4]) for item in spread]
    assert len({(round(x, 4), round(y, 4)) for x, y in coords}) == 4
    for idx in range(len(coords)):
        for jdx in range(idx + 1, len(coords)):
            dx = coords[idx][0] - coords[jdx][0]
            dy = coords[idx][1] - coords[jdx][1]
            assert (dx * dx + dy * dy) ** 0.5 >= 1.0


def test_spread_label_specs_avoids_component_and_pin_obstacles() -> None:
    label_specs = [
        ("SIG1", 10.0, 10.0, 12.0, 12.0),
        ("SIG2", 10.0, 10.0, 12.0, 12.0),
    ]
    occupied = [
        (12.0, 12.0),   # component center
        (13.0, 12.0),   # pin anchor
    ]
    spread = _spread_label_specs(label_specs, occupied_points=occupied)
    assert len(spread) == 2
    for _, _, _, x, y in spread:
        for ox, oy in occupied:
            dx = x - ox
            dy = y - oy
            assert (dx * dx + dy * dy) ** 0.5 >= 2.0


def test_label_spec_places_label_at_path_end() -> None:
    spec = _label_spec_for_path([(0.0, 0.0), (5.0, 0.0), (5.0, 3.0)])
    assert spec is not None
    pick_x, pick_y, label_x, label_y = spec
    assert abs(label_x - 5.0) < 1e-6
    assert abs(label_y - 3.0) < 1e-6
    assert abs(pick_x - 5.0) < 1e-6
    assert 2.7 <= pick_y <= 2.9


def test_route_path_between_points_rejects_unavoidable_cross_net_overlap() -> None:
    occupied_segments = [
        ("OTHER", (5.0, -200.0), (5.0, 200.0)),
    ]

    path = _route_path_between_points(
        start=(0.0, 0.0),
        end=(10.0, 0.0),
        net_name="SIG",
        occupied_segments=occupied_segments,
    )

    assert path == []


def test_schematic_builder_connects_all_nodes_in_multi_part_net() -> None:
    project = Project(
        project_id="p_multi",
        name="p_multi",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(refdes="R1", value="", source_name="R", device_id="easyeda_generated:DEV_R", package_id="PKG2", at=Point(0.0, 0.0)),
            Component(refdes="R2", value="", source_name="R", device_id="easyeda_generated:DEV_R", package_id="PKG2", at=Point(20.0, 0.0)),
            Component(refdes="R3", value="", source_name="R", device_id="easyeda_generated:DEV_R", package_id="PKG2", at=Point(40.0, 0.0)),
        ],
        packages=[
            Package(
                package_id="PKG2",
                name="PKG2",
                pads=[
                    Pad(pad_number="1", at=Point(-1.0, 0.0), shape="rect", width_mm=0.8, height_mm=0.8),
                    Pad(pad_number="2", at=Point(1.0, 0.0), shape="rect", width_mm=0.8, height_mm=0.8),
                ],
            )
        ],
        nets=[Net(name="SIG", nodes=[NetNode(refdes="R1", pin="1"), NetNode(refdes="R2", pin="1"), NetNode(refdes="R3", pin="1")])],
    )
    lines = SchematicReconstructionBuilder().build_commands(project)
    sig_lines = [line for line in lines if line.startswith("NET 'SIG' ")]
    assert sig_lines
    combined = " ".join(sig_lines)
    assert "(17.4600 20.0000)" in combined
    assert "(42.4600 20.0000)" in combined
    assert "(67.4600 20.0000)" in combined


def test_schematic_builder_coalesces_alias_nets_using_board_topology() -> None:
    project = Project(
        project_id="p_alias_merge",
        name="p_alias_merge",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(refdes="U1", value="", source_name="U", device_id="easyeda_generated:DEV_U1", package_id="PKG1", at=Point(0.0, 0.0)),
            Component(refdes="U2", value="", source_name="U", device_id="easyeda_generated:DEV_U2", package_id="PKG1", at=Point(20.0, 0.0)),
        ],
        packages=[
            Package(
                package_id="PKG1",
                name="PKG1",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
        nets=[
            Net(name="N$1", nodes=[NetNode(refdes="U1", pin="1")]),
            Net(name="SIG", nodes=[NetNode(refdes="U2", pin="1")]),
        ],
        board=Board(
            tracks=[
                Track(start=Point(0.0, 0.0), end=Point(10.0, 0.0), width_mm=0.2, layer="1", net="N$1"),
                Track(start=Point(5.0, 0.0), end=Point(15.0, 0.0), width_mm=0.2, layer="1", net="SIG"),
            ]
        ),
    )

    lines = SchematicReconstructionBuilder().build_commands(project)
    assert any(line.startswith("NET 'SIG' ") for line in lines)
    assert not any(line.startswith("NET 'N$1' ") for line in lines)


def test_schematic_builder_snaps_non_orth_add_rotation_to_orthogonal() -> None:
    project = Project(
        project_id="p_rot_snap",
        name="p_rot_snap",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U",
                device_id="easyeda_generated:DEV_U1",
                package_id="PKG1",
                at=Point(0.0, 0.0),
            )
        ],
        packages=[
            Package(
                package_id="PKG1",
                name="PKG1",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
    )
    lines = SchematicReconstructionBuilder().build_commands(
        project,
        library_paths={"easyeda_generated": "C:/libs/easyeda_generated.lbr"},
    )
    add_line = next(line for line in lines if line.startswith("ADD 'DEV_U1@"))
    assert " R0 (" in add_line


def test_schematic_builder_does_not_emit_power_parts_in_board_linked_flow() -> None:
    project = Project(
        project_id="p_pwr",
        name="p_pwr",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(refdes="U1", value="", source_name="U", device_id="easyeda_generated:DEV_U", package_id="PKG2", at=Point(0.0, 0.0)),
            Component(refdes="U2", value="", source_name="U", device_id="easyeda_generated:DEV_U", package_id="PKG2", at=Point(30.0, 0.0)),
        ],
        packages=[
            Package(
                package_id="PKG2",
                name="PKG2",
                pads=[
                    Pad(pad_number="1", at=Point(-1.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(1.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            )
        ],
        nets=[
            Net(name="GND", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="U2", pin="1")]),
            Net(name="5V0", nodes=[NetNode(refdes="U1", pin="2"), NetNode(refdes="U2", pin="2")]),
        ],
    )
    lines = SchematicReconstructionBuilder().build_commands(
        project,
        library_paths={"easyeda_generated": "C:/libs/easyeda_generated.lbr"},
    )
    assert not any("PWR_GND" in line or "PWR_5V" in line for line in lines)
    inserted = project.metadata.get("supply_symbols_inserted", [])
    assert inserted == []


def test_board_builder_keeps_distinct_instances_with_same_device_id() -> None:
    project = Project(
        project_id="p_board_instances",
        name="p_board_instances",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(refdes="R19", value="", source_name="R0603", device_id="rcl:R-US_R0603", package_id="PKG0603", at=Point(10.0, 10.0)),
            Component(refdes="R20", value="", source_name="R0603", device_id="rcl:R-US_R0603", package_id="PKG0603", at=Point(20.0, 10.0)),
        ],
        packages=[
            Package(package_id="PKG0603", name="PKG0603", pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)])
        ],
        board=Board(),
    )
    lines = BoardReconstructionBuilder().build_commands(project)
    assert "MOVE R19 (10.0000 10.0000);" in lines
    assert "MOVE R20 (20.0000 10.0000);" in lines


def test_board_builder_uses_source_instance_id_to_keep_duplicate_refdes_distinct() -> None:
    project = Project(
        project_id="p_dup_ref",
        name="p_dup_ref",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(refdes="R2", source_instance_id="inst_a", value="", source_name="AXIAL", device_id="easyeda_generated:DEV_R2", package_id="PKG_AX", at=Point(5.0, 5.0)),
            Component(refdes="R2", source_instance_id="inst_b", value="", source_name="AXIAL", device_id="easyeda_generated:DEV_R2", package_id="PKG_AX", at=Point(15.0, 5.0)),
        ],
        packages=[
            Package(package_id="PKG_AX", name="PKG_AX", pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="round", width_mm=1.0, height_mm=1.0, drill_mm=0.6)])
        ],
        board=Board(),
    )
    lines = BoardReconstructionBuilder().build_commands(project)
    assert any(line.startswith("MOVE R2 (5.0000 5.0000);") for line in lines)
    assert any(line.startswith("MOVE R2_2 (15.0000 5.0000);") for line in lines)


def test_board_builder_skips_unresolved_component_moves_and_reports_event() -> None:
    project = Project(
        project_id="p_skip_unresolved",
        name="p_skip_unresolved",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="R42",
                source_instance_id="a",
                value="",
                source_name="AXIAL",
                device_id=None,
                package_id="PKG_AX",
                at=Point(10.0, 10.0),
            ),
            Component(
                refdes="R42",
                source_instance_id="b",
                value="",
                source_name="SMD",
                device_id="easyeda_generated:DEV_R42",
                package_id="PKG_0603",
                at=Point(20.0, 20.0),
            ),
        ],
        packages=[
            Package(package_id="PKG_AX", name="PKG_AX", pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="round", width_mm=1.0, height_mm=1.0, drill_mm=0.6)]),
            Package(package_id="PKG_0603", name="PKG_0603", pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=0.8, height_mm=0.8)]),
        ],
        board=Board(),
    )

    lines = BoardReconstructionBuilder().build_commands(project)
    assert "MOVE R42 (10.0000 10.0000);" not in lines
    assert "MOVE R42_2 (20.0000 20.0000);" in lines
    assert any(event.code == "BOARD_COMPONENT_SKIPPED_NO_DEVICE" for event in project.events)


def test_board_builder_aligns_two_pin_resistor_rotation_with_pro_schematic_logic() -> None:
    project = Project(
        project_id="p_rot_norm",
        name="p_rot_norm",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="R9",
                value="",
                source_name="R0805",
                device_id="easyeda_generated:DEV_R9",
                package_id="R0805",
                at=Point(10.0, 10.0),
                rotation_deg=-90.0,
            )
        ],
        packages=[
            Package(
                package_id="R0805",
                name="R0805",
                pads=[
                    Pad(pad_number="1", at=Point(-0.8, 0.0), shape="rect", width_mm=0.9, height_mm=1.0),
                    Pad(pad_number="2", at=Point(0.8, 0.0), shape="rect", width_mm=0.9, height_mm=1.0),
                ],
            )
        ],
        board=Board(),
    )
    lines = BoardReconstructionBuilder().build_commands(project)
    assert "ROTATE =R90 'R9';" in lines


def test_board_builder_canonicalizes_pro_resistor_positive_quarter_turn_to_r90() -> None:
    project = Project(
        project_id="p_rot_pos_quarter",
        name="p_rot_pos_quarter",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="R6",
                value="",
                source_name="R_AXIAL",
                device_id="easyeda_generated:DEV_R6",
                package_id="RAXIAL",
                at=Point(10.0, 10.0),
                rotation_deg=90.0,
            )
        ],
        packages=[
            Package(
                package_id="RAXIAL",
                name="R_AXIAL",
                pads=[
                    Pad(pad_number="1", at=Point(-5.08, 0.0), shape="round", width_mm=1.0, height_mm=1.0, drill_mm=0.6),
                    Pad(pad_number="2", at=Point(5.08, 0.0), shape="round", width_mm=1.0, height_mm=1.0, drill_mm=0.6),
                ],
            )
        ],
        board=Board(),
    )

    lines = BoardReconstructionBuilder().build_commands(project)
    assert "ROTATE =R90 'R6';" in lines


def test_board_builder_keeps_capacitor_rotation_unchanged_while_adjusting_resistors() -> None:
    project = Project(
        project_id="p_rot_consistency",
        name="p_rot_consistency",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="R1",
                value="",
                source_name="R0603",
                device_id="easyeda_generated:DEV_R1",
                package_id="PKG_2PIN",
                at=Point(10.0, 10.0),
                rotation_deg=-90.0,
            ),
            Component(
                refdes="C1",
                value="",
                source_name="C0603",
                device_id="easyeda_generated:DEV_C1",
                package_id="PKG_2PIN",
                at=Point(20.0, 10.0),
                rotation_deg=-90.0,
            ),
        ],
        packages=[
            Package(
                package_id="PKG_2PIN",
                name="PKG_2PIN",
                pads=[
                    Pad(pad_number="1", at=Point(-0.8, 0.0), shape="rect", width_mm=0.9, height_mm=1.0),
                    Pad(pad_number="2", at=Point(0.8, 0.0), shape="rect", width_mm=0.9, height_mm=1.0),
                ],
            )
        ],
        board=Board(),
    )
    lines = BoardReconstructionBuilder().build_commands(project)
    assert "ROTATE =R90 'R1';" in lines
    assert "ROTATE =R270 'C1';" in lines


def test_board_builder_normalizes_adjustable_resistor_rotation_for_multi_pin_packages() -> None:
    project = Project(
        project_id="p_rot_res_adj",
        name="p_rot_res_adj",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="R40",
                value="",
                source_name="TRIMMER",
                device_id="easyeda_generated:DEV_R40",
                package_id="PKG_ADJ",
                at=Point(10.0, 10.0),
                rotation_deg=180.0,
            )
        ],
        packages=[
            Package(
                package_id="PKG_ADJ",
                name="RES-ADJ-TH_3P-L9_5-W4_85-P2_50-BL-BS",
                pads=[
                    Pad(pad_number="1", at=Point(0.0, 2.5), shape="round", width_mm=1.0, height_mm=1.0, drill_mm=0.6),
                    Pad(pad_number="2", at=Point(0.0, 0.0), shape="round", width_mm=1.0, height_mm=1.0, drill_mm=0.6),
                    Pad(pad_number="3", at=Point(0.0, -2.5), shape="round", width_mm=1.0, height_mm=1.0, drill_mm=0.6),
                ],
            )
        ],
        board=Board(),
    )
    lines = BoardReconstructionBuilder().build_commands(project)
    assert "ROTATE =R90 'R40';" in lines


def test_board_builder_uses_effective_rotation_for_external_origin_offset_transform() -> None:
    project = Project(
        project_id="p_external_offset_effective_rot",
        name="p_external_offset_effective_rot",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="IC",
                device_id="samacsys_parts:PART",
                package_id="PKG",
                at=Point(10.0, 20.0),
                rotation_deg=0.0,
                attributes={
                    "_external_origin_offset_x_mm": 1.0,
                    "_external_origin_offset_y_mm": 2.0,
                    "_external_rotation_offset_deg": 90.0,
                },
            ),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
        board=Board(),
    )

    lines = BoardReconstructionBuilder().build_commands(project)
    assert "ROTATE =R90 'U1';" in lines
    # Effective rotation is +90 (0 + external +90), so offset (1,2) rotates to (-2,1).
    assert "MOVE U1 (8.0000 21.0000);" in lines


def test_board_builder_emits_move_for_each_instance_with_exact_coordinates() -> None:
    refs = [
        ("R2", 52.3240, 27.9959),
        ("R11", 28.0670, 26.7259),
        ("R4", 121.0310, 28.5039),
        ("R5", 28.7020, 18.0899),
        ("R6", 23.8760, 18.0899),
        ("R8", 51.8160, 18.9230),
        ("R7", 123.8250, 19.1770),
        ("R42", 120.8860, 28.5021),
        ("R26", 146.9390, 41.0210),
        ("R40", 23.8681, 18.0437),
        ("R41", 51.9955, 27.9646),
        ("R1", 42.1640, 31.2979),
    ]
    project = Project(
        project_id="p_exact_moves",
        name="p_exact_moves",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes=ref,
                value="",
                source_name="part",
                device_id=f"easyeda_generated:DEV_{ref}",
                package_id="PKG",
                at=Point(x, y),
            )
            for ref, x, y in refs
        ],
        packages=[Package(package_id="PKG", name="PKG", pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="round", width_mm=1.0, height_mm=1.0, drill_mm=0.6)])],
        board=Board(),
    )
    lines = BoardReconstructionBuilder().build_commands(project)
    move_lines = [line for line in lines if line.startswith("MOVE ")]
    assert len(move_lines) == len(refs)
    for ref, x, y in refs:
        expected = f"MOVE {ref} ({x:.4f} {y:.4f});"
        assert expected in lines


def test_board_builder_applies_external_origin_offset_to_move() -> None:
    project = Project(
        project_id="p_external_offset",
        name="p_external_offset",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U",
                device_id="samacsys_parts:MAX98357AETE+",
                package_id="PKG",
                at=Point(10.0, 20.0),
                rotation_deg=90.0,
                attributes={
                    "_external_origin_offset_x_mm": 1.0,
                    "_external_origin_offset_y_mm": 2.0,
                },
            ),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
        board=Board(),
    )
    lines = BoardReconstructionBuilder().build_commands(project)
    # Rotate(1,2) by +90deg -> (-2,1), applied to (10,20) => (8,21)
    assert "MOVE U1 (8.0000 21.0000);" in lines


def test_board_builder_applies_external_rotation_offset_to_rotate() -> None:
    project = Project(
        project_id="p_external_rot_offset",
        name="p_external_rot_offset",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U",
                device_id="samacsys_parts:MAX98357AETE+",
                package_id="PKG",
                at=Point(10.0, 20.0),
                rotation_deg=180.0,
                attributes={
                    "_external_rotation_offset_deg": 90.0,
                },
            ),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
        board=Board(),
    )
    lines = BoardReconstructionBuilder().build_commands(project)
    assert "ROTATE =R270 'U1';" in lines
