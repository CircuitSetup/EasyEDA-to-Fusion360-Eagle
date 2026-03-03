from __future__ import annotations

from typing import Callable

from easyeda2fusion.builders.schematic_netplan import NetAttachmentPlan


Point = tuple[float, float]


def emit_net_attachment_lines(
    plans: tuple[NetAttachmentPlan, ...],
    use_inch_output: bool,
    quote_token: Callable[[str], str],
    coord_for_output: Callable[[float, bool], float],
) -> list[str]:
    lines: list[str] = []
    for plan in plans:
        for path in plan.paths:
            points = list(path.points)
            if len(points) < 2:
                continue
            coords = " ".join(
                f"({coord_for_output(x_mm, use_inch_output):.4f} {coord_for_output(y_mm, use_inch_output):.4f})"
                for x_mm, y_mm in points
            )
            lines.append(f"NET {quote_token(plan.net_name)} {coords};")
    return lines
