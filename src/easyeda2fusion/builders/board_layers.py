from __future__ import annotations


def layer_number(layer_name: str) -> str:
    key = str(layer_name or "").strip().lower()
    if key in {"top_copper", "1", "top", "toplayer"}:
        return "1"
    if key in {"bottom_copper", "2", "bottom", "bottomlayer"}:
        return "16"
    if key in {"3", "top_silkscreen", "topsilkscreen", "topsilklayer", "topsilkscreenlayer"}:
        return "21"
    if key in {"4", "bottom_silkscreen", "bottomsilkscreen", "bottomsilklayer", "bottomsilkscreenlayer"}:
        return "22"
    if key in {"5", "top_mask", "topsoldermasklayer"}:
        return "29"
    if key in {"6", "bottom_mask", "bottomsoldermasklayer"}:
        return "30"
    if key in {"7", "top_paste", "toppastemasklayer", "topsolderpastelayer"}:
        return "31"
    if key in {"8", "bottom_paste", "bottompastemasklayer", "bottomsolderpastelayer"}:
        return "32"
    if key in {"11", "dimension", "outline", "board_outline", "boardoutlinelayer"}:
        return "20"
    if key in {"12", "39", "41", "keepout", "keepoutlayer", "tkeepout", "trestrict"}:
        return "41"
    if key in {"40", "42", "bkeepout", "brestrict"}:
        return "42"
    if key in {"46", "milling", "millinglayer", "route", "routelayer", "cutout", "slot"}:
        return "46"
    if key in {"47", "56", "drill", "hole", "holedrawing", "drilldrawinglayer"}:
        return "44"
    if key in {"13", "14", "documentation", "mechanical", "t_docu"}:
        return "51"
    if key.startswith("inner"):
        digits = "".join(ch for ch in key if ch.isdigit())
        if digits:
            inner_idx = max(1, int(digits))
            if inner_idx <= 14:
                return str(1 + inner_idx)
            return "51"
    if key.isdigit():
        idx = int(key)
        if 15 <= idx <= 28:
            return str(idx - 13)
    return "51"


def is_copper_layer_num(layer_num: str) -> bool:
    try:
        idx = int(layer_num)
    except Exception:
        return False
    return idx == 1 or idx == 16 or 2 <= idx <= 15


def is_keepout_layer_num(layer_num: str) -> bool:
    return str(layer_num) in {"39", "40", "41", "42", "43"}
