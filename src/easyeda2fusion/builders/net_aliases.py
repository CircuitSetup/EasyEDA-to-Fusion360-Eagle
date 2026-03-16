from __future__ import annotations

from easyeda2fusion.builders.board_layers import is_copper_layer_num, layer_number
from easyeda2fusion.model import Project


def canonical_net_name(name: str | None, net_alias: dict[str, str]) -> str:
    raw = str(name or "").strip()
    if not raw:
        return ""
    return net_alias.get(raw, raw)


def build_track_net_aliases(tracks, vias=None) -> dict[str, str]:
    candidate_tracks = [
        track
        for track in tracks
        if str(track.net or "").strip()
        and is_copper_layer_num(layer_number(track.layer))
    ]
    if len(candidate_tracks) < 2:
        return {}

    uf = _UnionFind()
    for track in candidate_tracks:
        uf.add(str(track.net).strip())

    for idx in range(len(candidate_tracks)):
        left = candidate_tracks[idx]
        left_net = str(left.net or "").strip()
        left_layer = layer_number(left.layer)
        if not left_net:
            continue
        left_seg = ((float(left.start.x_mm), float(left.start.y_mm)), (float(left.end.x_mm), float(left.end.y_mm)))
        for jdx in range(idx + 1, len(candidate_tracks)):
            right = candidate_tracks[jdx]
            right_net = str(right.net or "").strip()
            if not right_net or right_net == left_net:
                continue
            if layer_number(right.layer) != left_layer:
                continue
            right_seg = ((float(right.start.x_mm), float(right.start.y_mm)), (float(right.end.x_mm), float(right.end.y_mm)))
            if segments_touch_or_overlap(left_seg, right_seg):
                uf.union(left_net, right_net)

    for via in vias or []:
        via_net = str(getattr(via, "net", "") or "").strip()
        touching: set[str] = set()
        if via_net:
            uf.add(via_net)
            touching.add(via_net)

        vx = float(via.at.x_mm)
        vy = float(via.at.y_mm)
        for track in candidate_tracks:
            track_net = str(track.net or "").strip()
            if not track_net:
                continue
            segment = (
                (float(track.start.x_mm), float(track.start.y_mm)),
                (float(track.end.x_mm), float(track.end.y_mm)),
            )
            if point_on_segment((vx, vy), segment):
                touching.add(track_net)

        if len(touching) >= 2:
            touching_list = sorted(touching)
            base = touching_list[0]
            for other in touching_list[1:]:
                uf.union(base, other)

    groups = uf.groups()
    aliases: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        canonical = pick_canonical_net_name(members)
        for member in members:
            aliases[member] = canonical
    return aliases


def project_track_net_aliases(project: Project) -> dict[str, str]:
    board = project.board
    if board is None:
        return {}

    metadata = getattr(project, "metadata", None)
    if isinstance(metadata, dict):
        cached = metadata.get("_track_net_aliases")
        if isinstance(cached, dict):
            return {
                str(key): str(value)
                for key, value in cached.items()
            }

    aliases = build_track_net_aliases(board.tracks, board.vias)
    if isinstance(metadata, dict):
        metadata["_track_net_aliases"] = dict(aliases)
    return aliases


def pick_canonical_net_name(names: list[str]) -> str:
    cleaned = [str(name or "").strip() for name in names if str(name or "").strip()]
    if not cleaned:
        return ""

    def sort_key(value: str) -> tuple[int, int, str]:
        upper = value.upper()
        anonymous = 1 if upper.startswith("N$") else 0
        return (anonymous, len(value), upper)

    return sorted(cleaned, key=sort_key)[0]


def segments_touch_or_overlap(
    seg_a: tuple[tuple[float, float], tuple[float, float]],
    seg_b: tuple[tuple[float, float], tuple[float, float]],
) -> bool:
    if _segments_share_endpoint(seg_a, seg_b):
        return True

    return line_segments_intersect(
        seg_a[0],
        seg_a[1],
        seg_b[0],
        seg_b[1],
    )


def point_on_segment(
    point: tuple[float, float],
    segment: tuple[tuple[float, float], tuple[float, float]],
) -> bool:
    (x, y) = point
    (x1, y1), (x2, y2) = segment
    cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
    if abs(cross) > 1e-6:
        return False
    dot = (x - x1) * (x2 - x1) + (y - y1) * (y2 - y1)
    if dot < -1e-6:
        return False
    sq_len = (x2 - x1) ** 2 + (y2 - y1) ** 2
    if dot - sq_len > 1e-6:
        return False
    return True


def line_segments_intersect(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> bool:
    def _orientation(p, q, r) -> int:
        val = (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])
        if abs(val) < 1e-9:
            return 0
        return 1 if val > 0 else 2

    o1 = _orientation(a1, a2, b1)
    o2 = _orientation(a1, a2, b2)
    o3 = _orientation(b1, b2, a1)
    o4 = _orientation(b1, b2, a2)

    if o1 != o2 and o3 != o4:
        return True

    if o1 == 0 and point_on_segment(b1, (a1, a2)):
        return True
    if o2 == 0 and point_on_segment(b2, (a1, a2)):
        return True
    if o3 == 0 and point_on_segment(a1, (b1, b2)):
        return True
    if o4 == 0 and point_on_segment(a2, (b1, b2)):
        return True
    return False


def _segments_share_endpoint(
    seg_a: tuple[tuple[float, float], tuple[float, float]],
    seg_b: tuple[tuple[float, float], tuple[float, float]],
) -> bool:
    return any(
        abs(ax - bx) < 1e-6 and abs(ay - by) < 1e-6
        for ax, ay in seg_a
        for bx, by in seg_b
    )


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        if item not in self._parent:
            self._parent[item] = item

    def find(self, item: str) -> str:
        parent = self._parent.get(item, item)
        if parent != item:
            parent = self.find(parent)
            self._parent[item] = parent
        else:
            self._parent.setdefault(item, item)
        return parent

    def union(self, left: str, right: str) -> None:
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left != root_right:
            self._parent[root_right] = root_left

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for item in list(self._parent):
            root = self.find(item)
            out.setdefault(root, []).append(item)
        return {
            root: sorted(members)
            for root, members in out.items()
        }
