from __future__ import annotations

from easyeda2fusion.builders.normalizer import Normalizer
from easyeda2fusion.model import ParsedDocument, ParsedSource, SourceFormat
from easyeda2fusion.parsers.easyeda_pro import EasyEDAProParser
from easyeda2fusion.parsers.easyeda_std import _legacy_pad_drill
from easyeda2fusion.parsers import parse_easyeda_files


def test_standard_schematic_import(fixtures_dir):
    parsed = parse_easyeda_files([fixtures_dir / "std_schematic.json"])
    assert parsed.source_format == SourceFormat.EASYEDA_STD

    result = Normalizer().normalize(parsed)
    project = result.project
    assert len(project.sheets) == 1
    assert len(project.components) == 2
    assert project.board is None


def test_pro_schematic_import(fixtures_dir):
    parsed = parse_easyeda_files([fixtures_dir / "pro_schematic.json"])
    assert parsed.source_format == SourceFormat.EASYEDA_PRO

    result = Normalizer().normalize(parsed)
    project = result.project
    assert len(project.sheets) == 1
    assert len(project.components) == 2
    assert project.board is None


def test_standard_board_import(fixtures_dir):
    parsed = parse_easyeda_files([fixtures_dir / "std_board.json"])
    result = Normalizer().normalize(parsed)
    board = result.project.board
    assert board is not None
    assert len(board.outline) == 1
    assert len(board.tracks) == 1
    assert len(board.vias) == 1


def test_standard_legacy_shape_string_board_import(fixtures_dir):
    parsed = parse_easyeda_files([fixtures_dir / "std_legacy_board_shape_strings.json"])
    assert parsed.source_format == SourceFormat.EASYEDA_STD
    assert any(doc.doc_type == "board" for doc in parsed.documents)
    assert parsed.metadata.get("y_axis_inverted") is True
    assert parsed.metadata.get("origin_raw") == {"x": 100.0, "y": 100.0}

    result = Normalizer().normalize(parsed)
    project = result.project
    board = project.board
    assert board is not None
    assert len(board.tracks) == 2
    assert len(board.holes) == 1
    assert len(board.outline) == 1
    assert len(board.text) == 1
    assert abs(board.text[0].size_mm - 0.999998) < 1e-5
    assert any(component.refdes == "R1" for component in project.components)
    package = next(pkg for pkg in project.packages if pkg.package_id == "TEST-PKG")
    assert len(package.pads) == 2
    assert abs((package.pads[0].drill_mm or 0.0) - 1.016) < 1e-6
    assert any(item.get("kind") == "wire_path" for item in package.outline)
    assert all(not (item.get("kind") == "text" and item.get("text") == "R1") for item in package.outline)
    assert any(net.name == "N1" and any(node.refdes == "R1" and node.pin == "1" for node in net.nodes) for net in project.nets)


def test_standard_legacy_duplicate_refdes_are_renamed_and_nodes_follow(fixtures_dir):
    parsed = parse_easyeda_files([fixtures_dir / "std_legacy_duplicate_refdes.json"])
    result = Normalizer().normalize(parsed)
    project = result.project

    refs = sorted(component.refdes for component in project.components)
    assert refs == ["K1", "K1_2"]

    net_k1_3 = next(net for net in project.nets if net.name == "K1_3")
    assert any(node.refdes == "K1_2" and node.pin == "1" for node in net_k1_3.nodes)


def test_standard_legacy_copperarea_prefers_rich_path_payload(fixtures_dir):
    parsed = parse_easyeda_files([fixtures_dir / "std_legacy_copperarea_path_payload.json"])
    result = Normalizer().normalize(parsed)
    board = result.project.board
    assert board is not None
    copper_regions = [region for region in board.regions if region.net == "GND"]
    assert copper_regions
    assert len(copper_regions[0].points) == 5


def test_normalizer_mirrors_legacy_package_frame_when_component_pad_fit_requires_it():
    parsed = ParsedSource(
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        documents=[
            ParsedDocument(
                doc_type="board",
                name="legacy_board",
                raw_objects=[
                    {
                        "type": "component",
                        "refdes": "K1",
                        "package_id": "PKG_RELAY",
                        "x": 50.0,
                        "y": 50.0,
                        "rotation": 90.0,
                        "side": "top",
                    },
                    {
                        "type": "package",
                        "id": "PKG_RELAY",
                        "name": "PKG_RELAY",
                        "pads": [
                            {"number": "1", "x": 1.0, "y": 0.0, "width": 1.0, "height": 1.0, "shape": "round"},
                            {"number": "2", "x": -1.0, "y": 0.0, "width": 1.0, "height": 1.0, "shape": "round"},
                            {"number": "3", "x": 2.0, "y": 1.0, "width": 1.0, "height": 1.0, "shape": "round"},
                        ],
                    },
                    # Absolute board pads correspond to mirrored-local package frame.
                    {"type": "pad", "number": "1", "x": 50.0, "y": 49.0, "width": 1.0, "height": 1.0, "shape": "round"},
                    {"type": "pad", "number": "2", "x": 50.0, "y": 51.0, "width": 1.0, "height": 1.0, "shape": "round"},
                    {"type": "pad", "number": "3", "x": 49.0, "y": 48.0, "width": 1.0, "height": 1.0, "shape": "round"},
                ],
                metadata={},
            )
        ],
        layers=[],
        rules=[],
        metadata={
            "legacy_shape_string_mode": True,
            "coordinate_scale_to_mm": 1.0,
        },
        events=[],
    )

    result = Normalizer().normalize(parsed)
    project = result.project
    package = next(pkg for pkg in project.packages if pkg.package_id == "PKG_RELAY")
    x_coords = sorted(round(float(pad.at.x_mm), 6) for pad in package.pads)
    assert x_coords == [-2.0, -1.0, 1.0]
    assert any(event.code == "LEGACY_PACKAGE_LOCAL_FRAME_MIRRORED" for event in project.events)


def test_standard_legacy_pad_drill_recovers_diameter_from_primary_radius_value():
    parts = ["PAD", "ELLIPSE", "0", "0", "8", "8", "11", "", "1", "2.36", "", "", "", "0"]
    assert abs(_legacy_pad_drill(parts, width=8.0, height=8.0) - 4.72) < 1e-9


def test_pro_board_import(fixtures_dir):
    parsed = parse_easyeda_files([fixtures_dir / "pro_board.json"])
    result = Normalizer().normalize(parsed)
    board = result.project.board
    assert board is not None
    assert len(board.tracks) == 2
    assert len(board.vias) == 1


def test_pro_project_manifest_bundle_import(fixtures_dir):
    parsed = parse_easyeda_files([fixtures_dir / "pro_bundle" / "project.json"])
    assert parsed.source_format == SourceFormat.EASYEDA_PRO
    assert any(doc.doc_type == "board" for doc in parsed.documents)
    assert any(doc.doc_type == "schematic" for doc in parsed.documents)
    assert parsed.metadata.get("coordinate_scale_to_mm") == 0.0254

    result = Normalizer().normalize(parsed)
    project = result.project
    assert project.board is not None
    assert len(project.components) >= 1
    assert any(net.nodes for net in project.nets if net.name == "N1")


def test_pro_pad_geometry_prefers_drill_shape_for_through_hole():
    width, height, drill, shape = EasyEDAProParser._pad_geometry_from_shapes(
        drill_shape=["ROUND", 39.37, 39.37],
        copper_shape=["ELLIPSE", 66.929, 66.929],
    )
    assert width == 66.929
    assert height == 66.929
    assert drill == 39.37
    assert shape == "ellipse"


def test_pro_pad_geometry_extracts_poly_bounding_box():
    width, height, drill, shape = EasyEDAProParser._pad_geometry_from_shapes(
        drill_shape=[],
        copper_shape=["POLY", [0.0, 0.0, "L", 20.0, 0.0, 20.0, 8.0, 0.0, 8.0]],
    )
    assert shape == "poly"
    assert width == 20.0
    assert height == 8.0
    assert drill is None


def test_pro_pad_rotation_is_parsed_for_board_and_footprint_package():
    records = [
        ["PAD", "p1", 0, "N1", 1, "1", 10.0, 20.0, 270, None, ["RECT", 9.0, 23.0, 0], []],
    ]
    _, _, objects, _ = EasyEDAProParser._convert_epcb_records(records)
    pad_obj = next(item for item in objects if item.get("type") == "pad")
    assert pad_obj["rotation"] == 270

    package = EasyEDAProParser._convert_efoo_records_to_package("fp1", "PKG1", records)
    assert package is not None
    assert package["pads"][0]["rotation"] == 270


def test_pro_component_package_prefers_exact_device_id_mapping():
    records = [
        ["COMPONENT", "c1", 0, 1, 0.0, 0.0, 0.0, {"Designator": "R1", "Device": "dev_axial", "Name": "R0603"}, 0],
    ]
    _, _, objects, _ = EasyEDAProParser._convert_epcb_records(
        records,
        footprint_id_to_title={"fp_axial": "R_AXIAL-0.4"},
        device_id_to_footprint={"dev_axial": "R_AXIAL-0.4"},
        device_title_to_footprint={"R0603": "R0603"},
    )
    component = next(item for item in objects if item.get("type") == "component")
    assert component["package"] == "R_AXIAL-0.4"


def test_pro_point_stream_skips_arc_angle_values():
    stream = [0.0, 0.0, "ARC", 90, 1.0, 1.0, "L", 2.0, 1.0]
    points = EasyEDAProParser._points_from_command_stream(stream)
    assert points == [(0.0, 0.0), (1.0, 1.0), (2.0, 1.0)]


def test_pro_poured_uses_primary_ring_only_when_multiple_rings_present():
    poured = [
        [0.0, 0.0, "L", 10.0, 0.0, 10.0, 10.0, 0.0, 10.0],
        [2.0, 2.0, "L", 8.0, 2.0, 8.0, 8.0, 2.0, 8.0],
    ]
    points = EasyEDAProParser._extract_point_list(poured, primary_ring_only=True)
    assert points == [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]


def test_pro_poured_uses_parent_pour_net_and_skips_fragment_rows():
    records = [
        ["POUR", "p1", 0, "GND", 1, 0.2, "gge1", 0, [[0, 0, "L", 10, 0, 10, 10, 0, 10]], ["SOLID", 0], 0, 0],
        ["POURED", "r1", "p1", 0, True, [[0, 0, "L", 10, 0, 10, 10, 0, 10]]],
        ["POURED", "r2", "p1", 6.02, False, [[1, 1, "L", 1.1, 1.1]]],
    ]

    _, _, objects, _ = EasyEDAProParser._convert_epcb_records(records)
    regions = [item for item in objects if item.get("type") == "region"]

    assert any(item.get("id") == "r1" and item.get("net") == "GND" and item.get("layer") == "1" for item in regions)
    assert all(item.get("id") != "r2" for item in regions)
    assert all(item.get("net") != "6.02" for item in regions)


def test_pro_epcb_string_rotation_uses_rotation_field_index_13():
    records = [
        ["STRING", "t1", 0, 3, 100.0, 200.0, "R8", "default", 39.37, 8, 0, 0, 3, 90, 0, 0, 0, 0],
    ]
    _, _, objects, _ = EasyEDAProParser._convert_epcb_records(records)
    text_obj = next(item for item in objects if item.get("type") == "text")
    assert text_obj["rotation"] == 90


def test_pro_efoo_package_extracts_silkscreen_outline_and_name_text():
    records = [
        ["POLY", "e1", 0, "", 3, 10, [0, 0, "L", 100, 0, 100, 100], 0],
        ["ATTR", "e2", 0, "", 3, 10, 20, "Designator", "U?", 0, 0, "default", 67.5, 6, 0, 0, 3, 0, 0, 0, 0, 0],
        ["PAD", "p1", 0, "", 1, "1", 0, 0, 0, None, ["RECT", 20, 20, 0], []],
    ]
    package = EasyEDAProParser._convert_efoo_records_to_package("fp1", "PKG1", records)
    assert package is not None
    assert len(package["pads"]) == 1
    outline = package.get("outline", [])
    assert any(item.get("kind") == "wire_path" for item in outline)
    assert any(item.get("kind") == "text" and item.get("text") == ">NAME" for item in outline)


def test_pro_efoo_package_extracts_keepout_polygon_and_hole_primitives():
    records = [
        ["POLY", "k1", 0, "", 12, 10, [0, 0, "L", 100, 0, 100, 50, 0, 50], 0],
        ["HOLE", "h1", 0, 47, 25, 30, 12],
        ["PAD", "p1", 0, "", 1, "1", 0, 0, 0, None, ["RECT", 20, 20, 0], []],
    ]
    package = EasyEDAProParser._convert_efoo_records_to_package("fp2", "PKG2", records)
    assert package is not None
    outline = package.get("outline", [])
    assert any(item.get("kind") == "polygon" and item.get("layer") == "12" for item in outline)
    assert any(item.get("kind") == "hole" and abs(float(item.get("drill", 0.0)) - 12.0) < 1e-9 for item in outline)


def test_pro_efoo_string_rotation_uses_rotation_field_index_13():
    records = [
        ["STRING", "s1", 0, 3, 10.0, 20.0, "PKG-TXT", "default", 45.0, 7, 0, 0, 3, 180, 0, 0, 0, 0],
        ["PAD", "p1", 0, "", 1, "1", 0, 0, 0, None, ["RECT", 20, 20, 0], []],
    ]
    package = EasyEDAProParser._convert_efoo_records_to_package("fp1", "PKG1", records)
    assert package is not None
    text = next(item for item in package["outline"] if item.get("kind") == "text")
    assert text["rotation"] == 180


def test_pro_poured_skips_complex_multi_ring_geometry():
    records = [
        ["POUR", "p1", 0, "GND", 1, 0.2, "gge1", 0, [[0, 0, "L", 20, 0, 20, 20, 0, 20]], ["SOLID", 0], 0, 0],
        [
            "POURED",
            "r1",
            "p1",
            0,
            True,
            [
                [0, 0, "L", 20, 0, 20, 20, 0, 20],
                [5, 5, "L", 15, 5, 15, 15, 5, 15],
            ],
        ],
    ]

    _, _, objects, _ = EasyEDAProParser._convert_epcb_records(records)
    regions = [item for item in objects if item.get("id") == "r1"]
    assert not regions


def test_pro_esym_symbol_definition_extracts_pin_names_and_types():
    records = [
        ["HEAD", {"originX": 5.0, "originY": -3.0}],
        ["PIN", "p1", 1, None, -20, 0, 10, 0, None, 0, 0, 1],
        ["ATTR", "a1", "p1", "NAME", "GND", False, False, -5, -4, 0, "st3", 0],
        ["ATTR", "a2", "p1", "NUMBER", "1", False, False, -10, 0, 0, "st4", 0],
        ["ATTR", "a3", "p1", "Pin Type", "Power", False, False, -20, 0, 0, "st2", 0],
    ]

    symbol_def = EasyEDAProParser._convert_esym_records_to_symbol_def("sym1", "SYM1", records)
    assert symbol_def is not None
    assert symbol_def["id"] == "sym1"
    assert symbol_def["name"] == "SYM1"
    assert symbol_def["origin_x"] == 5.0
    assert symbol_def["origin_y"] == -3.0
    assert len(symbol_def["pins"]) == 1
    pin = symbol_def["pins"][0]
    assert pin["number"] == "1"
    assert pin["name"] == "GND"
    assert pin["pin_type"] == "Power"
