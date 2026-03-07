from __future__ import annotations

from easyeda2fusion.builders.schematic_inference import infer_schematic_from_board
from easyeda2fusion.model import (
    Board,
    Component,
    Net,
    NetNode,
    Package,
    Pad,
    Point,
    Project,
    SourceFormat,
)


def test_inference_prunes_invalid_source_pin_nodes() -> None:
    project = Project(
        project_id="p_infer_prune_invalid",
        name="p_infer_prune_invalid",
        source_format=SourceFormat.EASYEDA_PRO,
        input_files=[],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="IC",
                package_id="PKG_U",
                at=Point(10.0, 10.0),
            ),
            Component(
                refdes="R1",
                value="",
                source_name="R",
                package_id="PKG_R",
                at=Point(20.0, 10.0),
            ),
        ],
        packages=[
            Package(
                package_id="PKG_U",
                name="PKG_U",
                pads=[
                    Pad(pad_number="1", at=Point(-1.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(1.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            ),
            Package(
                package_id="PKG_R",
                name="PKG_R",
                pads=[
                    Pad(pad_number="1", at=Point(-1.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(1.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            ),
        ],
        nets=[
            Net(
                name="VIN",
                nodes=[
                    NetNode(refdes="U1", pin="e36"),
                    NetNode(refdes="R1", pin="1"),
                ],
            )
        ],
        board=Board(),
    )

    report = infer_schematic_from_board(project, force=True)
    assert report.inferred is True
    assert all(node.pin != "e36" for net in project.nets for node in net.nodes)
    assert any("invalid source schematic pin-node references" in item for item in report.manual_review_items)


def test_inference_reprojects_local_package_pads_with_positive_component_rotation() -> None:
    project = Project(
        project_id="p_infer_rotation",
        name="p_infer_rotation",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(
                refdes="J1",
                value="",
                source_name="J",
                package_id="PKG_J",
                at=Point(10.0, 10.0),
                rotation_deg=270.0,
            )
        ],
        packages=[
            Package(
                package_id="PKG_J",
                name="PKG_J",
                pads=[
                    Pad(pad_number="1", at=Point(1.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(-1.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            )
        ],
        nets=[],
        board=Board(
            pads=[
                Pad(
                    pad_number="P1",
                    at=Point(10.0, 11.0),
                    shape="rect",
                    width_mm=1.0,
                    height_mm=1.0,
                    net="N1",
                ),
                Pad(
                    pad_number="P2",
                    at=Point(10.0, 9.0),
                    shape="rect",
                    width_mm=1.0,
                    height_mm=1.0,
                    net="N2",
                ),
            ]
        ),
    )

    infer_schematic_from_board(project, force=True)
    net_lookup = {net.name: {(node.refdes, node.pin) for node in net.nodes} for net in project.nets}
    assert ("J1", "1") in net_lookup.get("N2", set())
    assert ("J1", "2") in net_lookup.get("N1", set())


def test_inference_falls_back_to_mirrored_local_pad_frame_when_primary_projection_misses() -> None:
    project = Project(
        project_id="p_infer_mirror_fallback",
        name="p_infer_mirror_fallback",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(
                refdes="K1",
                value="",
                source_name="RELAY",
                package_id="PKG_K",
                at=Point(10.0, 10.0),
                rotation_deg=90.0,
            )
        ],
        packages=[
            Package(
                package_id="PKG_K",
                name="PKG_K",
                pads=[
                    Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    # Mirrored-x fallback is required to land this pad on N2.
                    Pad(pad_number="2", at=Point(-2.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            )
        ],
        nets=[],
        board=Board(
            pads=[
                Pad(
                    pad_number="1",
                    at=Point(10.0, 10.0),
                    shape="rect",
                    width_mm=1.0,
                    height_mm=1.0,
                    net="N1",
                ),
                Pad(
                    pad_number="2",
                    at=Point(10.0, 12.0),
                    shape="rect",
                    width_mm=1.0,
                    height_mm=1.0,
                    net="N2",
                ),
            ]
        ),
    )

    infer_schematic_from_board(project, force=True)
    net_lookup = {net.name: {(node.refdes, node.pin) for node in net.nodes} for net in project.nets}
    assert ("K1", "1") in net_lookup.get("N1", set())
    assert ("K1", "2") in net_lookup.get("N2", set())


def test_inference_skips_conflicting_pin_net_membership_instead_of_duplication() -> None:
    project = Project(
        project_id="p_infer_reassign",
        name="p_infer_reassign",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(
                refdes="J1",
                value="",
                source_name="J",
                package_id="PKG_J",
                at=Point(10.0, 10.0),
            )
        ],
        packages=[
            Package(
                package_id="PKG_J",
                name="PKG_J",
                pads=[
                    Pad(pad_number="1", at=Point(1.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                    Pad(pad_number="2", at=Point(-1.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0),
                ],
            )
        ],
        nets=[
            Net(name="N1", nodes=[NetNode(refdes="J1", pin="1")]),
            Net(name="N2", nodes=[NetNode(refdes="J1", pin="2")]),
        ],
        board=Board(
            pads=[
                Pad(
                    pad_number="P1",
                    at=Point(11.0, 10.0),
                    shape="rect",
                    width_mm=1.0,
                    height_mm=1.0,
                    net="N1",
                ),
                Pad(
                    pad_number="P2",
                    at=Point(9.0, 10.0),
                    shape="rect",
                    width_mm=1.0,
                    height_mm=1.0,
                    net="N1",
                ),
            ]
        ),
    )

    report = infer_schematic_from_board(project, force=True)
    net_lookup = {net.name: {(node.refdes, node.pin) for node in net.nodes} for net in project.nets}
    assert ("J1", "1") in net_lookup.get("N1", set())
    assert ("J1", "2") not in net_lookup.get("N1", set())
    assert ("J1", "2") in net_lookup.get("N2", set())
    assert any("Skipped 1 conflicting inferred pin-to-net nodes" in item for item in report.manual_review_items)
