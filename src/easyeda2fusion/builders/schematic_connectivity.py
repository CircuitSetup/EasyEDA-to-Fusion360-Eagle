from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BoardDerivedNetEndpoint:
    refdes: str
    pin: str
    x_mm: float
    y_mm: float
    outward_dx: float
    outward_dy: float


@dataclass(frozen=True)
class BoardDerivedNetConnection:
    net_name: str
    normalized_name: str
    net_kind: str
    endpoints: tuple[BoardDerivedNetEndpoint, ...]


@dataclass
class BoardDerivedNetConnectionMap:
    nets: tuple[BoardDerivedNetConnection, ...]

    def as_report_dict(self) -> dict[str, Any]:
        return {
            "net_count": len(self.nets),
            "nets": [
                {
                    "net_name": net.net_name,
                    "normalized_name": net.normalized_name,
                    "net_kind": net.net_kind,
                    "endpoint_count": len(net.endpoints),
                    "endpoints": [
                        {
                            "refdes": endpoint.refdes,
                            "pin": endpoint.pin,
                            "point_mm": {"x": endpoint.x_mm, "y": endpoint.y_mm},
                            "outward": {"dx": endpoint.outward_dx, "dy": endpoint.outward_dy},
                        }
                        for endpoint in net.endpoints
                    ],
                }
                for net in self.nets
            ],
        }


def build_board_derived_net_connection_map(connection_map: list[Any]) -> BoardDerivedNetConnectionMap:
    nets: list[BoardDerivedNetConnection] = []
    for connection in sorted(connection_map, key=lambda item: str(getattr(item, "net_name", "")).upper()):
        net_name = str(getattr(connection, "net_name", "") or "").strip()
        if not net_name:
            continue
        endpoints: list[BoardDerivedNetEndpoint] = []
        nodes = list(getattr(connection, "nodes", []) or [])
        for node in sorted(nodes, key=lambda item: (str(getattr(item, "refdes", "")), _pin_sort_key(str(getattr(item, "pin", ""))))):
            anchor = getattr(node, "anchor", None)
            if anchor is None:
                continue
            endpoints.append(
                BoardDerivedNetEndpoint(
                    refdes=str(getattr(node, "refdes", "")).strip(),
                    pin=str(getattr(node, "pin", "")).strip(),
                    x_mm=float(getattr(anchor, "x_mm", 0.0)),
                    y_mm=float(getattr(anchor, "y_mm", 0.0)),
                    outward_dx=float(getattr(anchor, "outward_dx", 0.0)),
                    outward_dy=float(getattr(anchor, "outward_dy", 0.0)),
                )
            )
        nets.append(
            BoardDerivedNetConnection(
                net_name=net_name,
                normalized_name=_normalize_net_name(net_name),
                net_kind=_classify_net_name(net_name),
                endpoints=tuple(endpoints),
            )
        )
    return BoardDerivedNetConnectionMap(nets=tuple(nets))


def _normalize_net_name(name: str) -> str:
    return "".join(ch for ch in str(name or "").upper() if ch.isalnum() or ch == "_")


def _classify_net_name(name: str) -> str:
    token = _normalize_net_name(name)
    if token in {
        "GND",
        "AGND",
        "DGND",
        "PGND",
        "EARTH",
        "CHASSIS",
        "VSS",
        "VSSA",
        "VSSD",
    }:
        return "ground"
    if token in {
        "3V3",
        "33V",
        "5V",
        "12V",
        "VCC",
        "VDD",
        "VBAT",
        "VIN",
        "AVDD",
        "DVDD",
    }:
        return "power"
    return "signal"


def _pin_sort_key(pin_id: str) -> tuple[int, str]:
    token = str(pin_id or "").strip()
    if token.isdigit():
        return (0, f"{int(token):09d}")
    return (1, token.upper())

