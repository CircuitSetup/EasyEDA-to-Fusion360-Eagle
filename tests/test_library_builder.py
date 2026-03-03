from __future__ import annotations

import xml.etree.ElementTree as ET

from easyeda2fusion.builders.library_builder import LibraryBuilder
from easyeda2fusion.builders.library_builder import GeneratedLibraryPart
from easyeda2fusion.emitters.generated_library_emitter import emit_generated_library
from easyeda2fusion.matchers.library_matcher import MatchContext
from easyeda2fusion.model import Component, Device, Package, Pad, Point, Symbol, SymbolPin


def test_library_builder_uses_symbol_definition_pin_names() -> None:
    builder = LibraryBuilder()
    builder.configure(
        {
            "symbol_defs": {
                "sym_ic": {
                    "id": "sym_ic",
                    "name": "IC",
                    "pins": [
                        {"number": "1", "name": "GND", "pin_type": "Power"},
                        {"number": "2", "name": "VCC", "pin_type": "Power"},
                        {"number": "3", "name": "IO1", "pin_type": "InOut"},
                    ],
                }
            },
            "device_id_to_symbol_id": {"dev_uuid": "sym_ic"},
        }
    )

    component = Component(
        refdes="U1",
        value="",
        source_name="IC",
        package_id="PKG1",
        attributes={"Device": "dev_uuid"},
        at=Point(0.0, 0.0),
    )
    package = Package(
        package_id="PKG1",
        name="PKG1",
        pads=[
            Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
            Pad(pad_number="2", at=Point(2.54, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
            Pad(pad_number="3", at=Point(5.08, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
        ],
    )

    part, reason = builder.synthesize_missing_part(
        component,
        {"PKG1": package},
        pin_net_hints={"1": {"AGND"}, "2": {"5V0"}, "3": {"IO13"}},
    )
    assert reason is None
    assert part is not None

    names = {pin.pin_number: pin.pin_name for pin in part.symbol.pins}
    assert names["1"] == "GND"
    assert names["2"] == "VCC"
    assert names["3"] == "IO1"
    assert part.device.pin_pad_map["GND"] == "1"
    assert part.device.pin_pad_map["VCC"] == "2"
    assert part.device.pin_pad_map["IO1"] == "3"


def test_library_builder_falls_back_to_power_net_labels_when_symbol_unknown() -> None:
    builder = LibraryBuilder()
    builder.configure({})

    component = Component(
        refdes="U2",
        value="",
        source_name="Controller",
        package_id="PKG2",
        at=Point(0.0, 0.0),
    )
    package = Package(
        package_id="PKG2",
        name="PKG2",
        pads=[
            Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
            Pad(pad_number="2", at=Point(2.54, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
            Pad(pad_number="3", at=Point(5.08, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
        ],
    )

    part, reason = builder.synthesize_missing_part(
        component,
        {"PKG2": package},
        pin_net_hints={"1": {"GND"}, "2": {"3V3"}, "3": {"SCL"}},
    )
    assert reason is None
    assert part is not None

    names = {pin.pin_number: pin.pin_name for pin in part.symbol.pins}
    assert names["1"] == "GND"
    assert names["2"] == "3V3"


def test_library_builder_uses_single_trace_net_name_for_generic_pin_labels() -> None:
    builder = LibraryBuilder()
    builder.configure({})

    component = Component(
        refdes="U3",
        value="",
        source_name="Controller",
        package_id="PKG3",
        at=Point(0.0, 0.0),
    )
    package = Package(
        package_id="PKG3",
        name="PKG3",
        pads=[
            Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
            Pad(pad_number="2", at=Point(2.54, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
            Pad(pad_number="3", at=Point(5.08, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
        ],
    )

    part, reason = builder.synthesize_missing_part(
        component,
        {"PKG3": package},
        pin_net_hints={"1": {"SCL"}, "2": {"SDA"}, "3": {"N$44"}},
    )
    assert reason is None
    assert part is not None

    names = {pin.pin_number: pin.pin_name for pin in part.symbol.pins}
    assert names["1"] == "SCL"
    assert names["2"] == "SDA"
    assert names["3"] == "3"


def test_library_builder_labels_passive_pins_from_board_net_hints_and_preserves_pad_numbers() -> None:
    builder = LibraryBuilder()
    builder.configure({})

    component = Component(
        refdes="R1",
        value="10k",
        source_name="R0603",
        package_id="PKG_R",
        at=Point(0.0, 0.0),
    )
    package = Package(
        package_id="PKG_R",
        name="PKG_R",
        pads=[
            Pad(pad_number="1", at=Point(-0.8, 0.0), shape="rect", width_mm=0.8, height_mm=0.9),
            Pad(pad_number="2", at=Point(0.8, 0.0), shape="rect", width_mm=0.8, height_mm=0.9),
        ],
    )

    part, reason = builder.synthesize_missing_part(
        component,
        {"PKG_R": package},
        pin_net_hints={"1": {"GND"}, "2": {"VIN"}},
    )
    assert reason is None
    assert part is not None

    names = {pin.pin_number: pin.pin_name for pin in part.symbol.pins}
    assert names["1"] == "GND"
    assert names["2"] == "VIN"
    assert part.device.pin_pad_map["GND"] == "1"
    assert part.device.pin_pad_map["VIN"] == "2"


def test_library_builder_keeps_duplicate_net_labeled_pins_distinct() -> None:
    builder = LibraryBuilder()
    builder.configure({})

    component = Component(
        refdes="U9",
        value="",
        source_name="Driver",
        package_id="PKG_U9",
        at=Point(0.0, 0.0),
    )
    package = Package(
        package_id="PKG_U9",
        name="PKG_U9",
        pads=[
            Pad(pad_number="1", at=Point(-1.0, 2.0), shape="rect", width_mm=1.0, height_mm=1.0),
            Pad(pad_number="2", at=Point(-1.0, -2.0), shape="rect", width_mm=1.0, height_mm=1.0),
            Pad(pad_number="3", at=Point(1.0, 2.0), shape="rect", width_mm=1.0, height_mm=1.0),
            Pad(pad_number="4", at=Point(1.0, -2.0), shape="rect", width_mm=1.0, height_mm=1.0),
        ],
    )
    part, reason = builder.synthesize_missing_part(
        component,
        {"PKG_U9": package},
        pin_net_hints={"1": {"GND"}, "2": {"GND"}, "3": {"IO1"}, "4": {"IO2"}},
    )
    assert reason is None
    assert part is not None
    names = {pin.pin_number: pin.pin_name for pin in part.symbol.pins}
    assert names["1"] == "GND"
    assert names["2"] == "GND_2"
    assert part.device.pin_pad_map["GND"] == "1"
    assert part.device.pin_pad_map["GND_2"] == "2"


def test_library_builder_generates_resistor_array_symbol() -> None:
    builder = LibraryBuilder()
    builder.configure({})

    component = Component(
        refdes="R26",
        value="",
        source_name="RES-ARRAY-SMD_0603-8P-L3.2-W1.6-BL",
        package_id="PKG_RA",
        at=Point(0.0, 0.0),
    )
    package = Package(
        package_id="PKG_RA",
        name="PKG_RA",
        pads=[
            Pad(pad_number=str(idx), at=Point(float(idx), 0.0), shape="rect", width_mm=0.7, height_mm=0.8)
            for idx in range(1, 9)
        ],
    )
    part, reason = builder.synthesize_missing_part(
        component,
        {"PKG_RA": package},
        pin_net_hints={str(idx): {f"N{idx}"} for idx in range(1, 9)},
    )
    assert reason is None
    assert part is not None
    assert len(part.symbol.pins) == 8
    assert len(part.device.pin_pad_map) == 8
    # Resistor-array symbols include additional internal resistor graphics.
    assert len(part.symbol.graphics) > 6


def test_library_builder_generates_connector_symbol_with_single_side_pins() -> None:
    builder = LibraryBuilder()
    builder.configure({})

    component = Component(
        refdes="CN1",
        value="",
        source_name="SCREWTERMINAL-3.5MM-4",
        package_id="PKG_CONN",
        at=Point(0.0, 0.0),
    )
    package = Package(
        package_id="PKG_CONN",
        name="PKG_CONN",
        pads=[
            Pad(pad_number="1", at=Point(0.0, 0.0), shape="round", width_mm=1.0, height_mm=1.0, drill_mm=0.6),
            Pad(pad_number="2", at=Point(2.5, 0.0), shape="round", width_mm=1.0, height_mm=1.0, drill_mm=0.6),
            Pad(pad_number="3", at=Point(5.0, 0.0), shape="round", width_mm=1.0, height_mm=1.0, drill_mm=0.6),
            Pad(pad_number="4", at=Point(7.5, 0.0), shape="round", width_mm=1.0, height_mm=1.0, drill_mm=0.6),
        ],
    )
    part, reason = builder.synthesize_missing_part(
        component,
        {"PKG_CONN": package},
        pin_net_hints={"1": {"GND"}, "2": {"VIN"}, "3": {"SCL"}, "4": {"SDA"}},
    )
    assert reason is None
    assert part is not None
    assert len(part.symbol.graphics) == 4

    x_positions = {round(pin.at.x_mm, 6) for pin in part.symbol.pins}
    assert len(x_positions) == 1
    box_min_x = min(
        min(float(item.get("x1_mm", 0.0)), float(item.get("x2_mm", 0.0)))
        for item in part.symbol.graphics
        if item.get("kind") == "wire"
    )
    assert next(iter(x_positions)) < box_min_x
    names = {pin.pin_number: pin.pin_name for pin in part.symbol.pins}
    assert names["1"] == "GND"
    assert names["2"] == "VIN"


def test_library_builder_generates_two_side_layout_for_multi_pin_non_connector() -> None:
    builder = LibraryBuilder()
    builder.configure({})

    component = Component(
        refdes="U10",
        value="",
        source_name="Driver",
        package_id="PKG_U10",
        at=Point(0.0, 0.0),
    )
    package = Package(
        package_id="PKG_U10",
        name="PKG_U10",
        pads=[
            Pad(pad_number=str(idx), at=Point(float(idx), 0.0), shape="rect", width_mm=0.7, height_mm=0.8)
            for idx in range(1, 11)
        ],
    )

    part, reason = builder.synthesize_missing_part(
        component,
        {"PKG_U10": package},
        pin_net_hints={str(idx): {f"N{idx}"} for idx in range(1, 11)},
    )
    assert reason is None
    assert part is not None
    assert len(part.symbol.graphics) == 4
    assert len(part.symbol.pins) == 10

    box_min_x = min(
        min(float(item.get("x1_mm", 0.0)), float(item.get("x2_mm", 0.0)))
        for item in part.symbol.graphics
        if item.get("kind") == "wire"
    )
    box_max_x = max(
        max(float(item.get("x1_mm", 0.0)), float(item.get("x2_mm", 0.0)))
        for item in part.symbol.graphics
        if item.get("kind") == "wire"
    )
    xs = [round(pin.at.x_mm, 6) for pin in part.symbol.pins]
    ys = [round(pin.at.y_mm, 6) for pin in part.symbol.pins]
    left = min(xs)
    right = max(xs)
    assert left < round(box_min_x, 6)
    assert right > round(box_max_x, 6)
    assert any(round(pin.at.x_mm, 6) == left for pin in part.symbol.pins)
    assert any(round(pin.at.x_mm, 6) == right for pin in part.symbol.pins)
    # Non-connector multi-pin symbols should only use left/right sides.
    assert all(round(pin.at.x_mm, 6) in {left, right} for pin in part.symbol.pins)


def test_generated_library_orients_top_and_bottom_pins_for_perimeter_symbols(tmp_path) -> None:
    symbol = Symbol(
        symbol_id="SYM_U_PERIM",
        name="U_PERIM",
        pins=[
            SymbolPin(pin_number="1", pin_name="TOP", at=Point(0.0, 5.08)),
            SymbolPin(pin_number="2", pin_name="BOT", at=Point(0.0, -5.08)),
            SymbolPin(pin_number="3", pin_name="LEFT", at=Point(-5.08, 0.0)),
            SymbolPin(pin_number="4", pin_name="RIGHT", at=Point(5.08, 0.0)),
        ],
        graphics=[
            {"kind": "wire", "x1_mm": -3.0, "y1_mm": -3.0, "x2_mm": 3.0, "y2_mm": -3.0},
            {"kind": "wire", "x1_mm": 3.0, "y1_mm": -3.0, "x2_mm": 3.0, "y2_mm": 3.0},
            {"kind": "wire", "x1_mm": 3.0, "y1_mm": 3.0, "x2_mm": -3.0, "y2_mm": 3.0},
            {"kind": "wire", "x1_mm": -3.0, "y1_mm": 3.0, "x2_mm": -3.0, "y2_mm": -3.0},
        ],
    )
    package = Package(
        package_id="PKG_U_PERIM",
        name="PKG_U_PERIM",
        pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
    )
    part = GeneratedLibraryPart(
        symbol=symbol,
        package=package,
        device=Device(
            device_id="DEV_U_PERIM",
            name="DEV_U_PERIM",
            symbol_id=symbol.symbol_id,
            package_id=package.package_id,
            pin_pad_map={"TOP": "1"},
        ),
        source="test",
    )
    out = emit_generated_library(MatchContext(new_library_parts=[part]), tmp_path)
    assert out is not None

    tree = ET.parse(out)
    root = tree.getroot()
    pins = {
        str(item.get("name") or ""): str(item.get("rot") or "")
        for item in root.findall(".//library/symbols/symbol[@name='U_PERIM']/pin")
    }
    assert pins["TOP"] == "R270"
    assert pins["BOT"] == "R90"
    assert pins["LEFT"] == "R0"
    assert pins["RIGHT"] == "R180"


def test_generated_library_keeps_single_side_connector_pins_perpendicular(tmp_path) -> None:
    symbol = Symbol(
        symbol_id="SYM_J_CONN",
        name="J_CONN",
        pins=[
            SymbolPin(pin_number="1", pin_name="GND", at=Point(-7.62, 2.54)),
            SymbolPin(pin_number="2", pin_name="VIN", at=Point(-7.62, 0.0)),
            SymbolPin(pin_number="3", pin_name="SIG", at=Point(-7.62, -2.54)),
        ],
        graphics=[
            {"kind": "wire", "x1_mm": -5.08, "y1_mm": -3.81, "x2_mm": 5.08, "y2_mm": -3.81},
            {"kind": "wire", "x1_mm": 5.08, "y1_mm": -3.81, "x2_mm": 5.08, "y2_mm": 3.81},
            {"kind": "wire", "x1_mm": 5.08, "y1_mm": 3.81, "x2_mm": -5.08, "y2_mm": 3.81},
            {"kind": "wire", "x1_mm": -5.08, "y1_mm": 3.81, "x2_mm": -5.08, "y2_mm": -3.81},
        ],
    )
    package = Package(
        package_id="PKG_J_CONN",
        name="PKG_J_CONN",
        pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
    )
    part = GeneratedLibraryPart(
        symbol=symbol,
        package=package,
        device=Device(
            device_id="DEV_J_CONN",
            name="DEV_J_CONN",
            symbol_id=symbol.symbol_id,
            package_id=package.package_id,
            pin_pad_map={"GND": "1"},
        ),
        source="test",
    )
    out = emit_generated_library(MatchContext(new_library_parts=[part]), tmp_path)
    assert out is not None

    tree = ET.parse(out)
    root = tree.getroot()
    pins = {
        str(item.get("name") or ""): str(item.get("rot") or "")
        for item in root.findall(".//library/symbols/symbol[@name='J_CONN']/pin")
    }
    assert pins["GND"] == "R0"
    assert pins["VIN"] == "R0"
    assert pins["SIG"] == "R0"
