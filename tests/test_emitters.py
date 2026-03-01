from __future__ import annotations

import xml.etree.ElementTree as ET

from easyeda2fusion.builders.library_builder import GeneratedLibraryPart
from easyeda2fusion.builders.schematic_reconstruction import SchematicReconstructionBuilder
from easyeda2fusion.emitters.generated_library_emitter import _guess_prefix, emit_generated_library
from easyeda2fusion.matchers.library_matcher import MatchContext
from easyeda2fusion.builders.board_reconstruction import BoardReconstructionBuilder
from easyeda2fusion.model import (
    Board,
    Component,
    Device,
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

    assert "ADD 'R-US_R0603@C:/libs/rcl.lbr' 'R1' (20.0000 20.0000) R0;" in lines
    assert "VALUE 'R1' '10k';" in lines
    assert all(not line.startswith("#") for line in lines)


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

    assert "ADD 'R-US_R0603@C:/libs/rcl.lbr' 'R35' (20.0000 20.0000) R0;" in lines
    assert "ADD 'R-US_R0603@C:/libs/rcl.lbr' R35 (20.0000 20.0000) R0;" not in lines


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
    assert "ROTATE =R0 R1;" in lines
    assert "ROTATE =R0 R2;" in lines
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
    assert "LAYER 16;" in lines


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
    assert "TEXT 'TOPTXT' (10.0000 10.0000) R90;" in lines
    assert "TEXT 'BOTTXT' (20.0000 20.0000) MR180;" in lines


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
    assert len(bus_lines) == 7

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
    assert len(net_lines) == 1
    # External symbol pin "A" is at +7.62 mm from the instance origin.
    combined = " ".join(net_lines)
    assert "(27.6200 20.0000)" in combined
    assert "(52.6200 20.0000)" in combined


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
    assert "CHANGE XREF ON;" in lines
    assert "CHANGE SIZE 1.27;" in lines
    assert any(line.startswith("LABEL (") for line in lines)
    assert any(line.count("(") >= 2 and line.startswith("LABEL (") for line in lines)


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
    assert "CHANGE SIZE 1.27;" in lines
    assert any(line.startswith("LABEL (") for line in lines)


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
    assert add_line.endswith(" R0;")


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


def test_board_builder_canonicalizes_two_pin_rotation_for_resistors() -> None:
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
    assert "ROTATE =R90 R9;" in lines


def test_board_builder_uses_same_rotation_rule_for_resistor_and_capacitor() -> None:
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
    assert "ROTATE =R90 R1;" in lines
    assert "ROTATE =R90 C1;" in lines


def test_board_builder_does_not_apply_resistor_specific_orientation_correction() -> None:
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
    assert "ROTATE =R180 R40;" in lines


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
