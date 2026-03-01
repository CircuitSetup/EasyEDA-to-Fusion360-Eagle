from __future__ import annotations

from pathlib import Path
from typing import Optional

from easyeda2fusion.model import ConversionMode, MatchMode


def run_gui_flow() -> Optional[tuple[list[Path], Path, ConversionMode, MatchMode]]:
    try:
        import tkinter as tk
        from tkinter import filedialog, simpledialog
    except Exception:
        return None

    root = tk.Tk()
    root.withdraw()

    selected_files = filedialog.askopenfilenames(
        title="Select EasyEDA input file(s)",
        filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
    )
    if not selected_files:
        root.destroy()
        return None

    output_dir = filedialog.askdirectory(title="Select output directory")
    if not output_dir:
        root.destroy()
        return None

    mode_answer = simpledialog.askstring(
        "Conversion Mode",
        "Mode (full, schematic, board, board-infer-schematic):",
        initialvalue=ConversionMode.FULL.value,
    )
    if not mode_answer:
        root.destroy()
        return None

    match_answer = simpledialog.askstring(
        "Match Mode",
        "Match mode (auto, prompt, strict, package-first):",
        initialvalue=MatchMode.AUTO.value,
    )
    if not match_answer:
        root.destroy()
        return None

    try:
        mode = ConversionMode(mode_answer.strip())
        match_mode = MatchMode(match_answer.strip())
    except ValueError:
        root.destroy()
        return None

    root.destroy()
    return ([Path(p).resolve() for p in selected_files], Path(output_dir).resolve(), mode, match_mode)
