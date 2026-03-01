from __future__ import annotations

from easyeda2fusion.builders.library_builder import LibraryBuilder
from easyeda2fusion.model import Component, Package, Pad, Point


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
