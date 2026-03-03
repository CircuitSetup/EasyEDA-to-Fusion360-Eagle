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
