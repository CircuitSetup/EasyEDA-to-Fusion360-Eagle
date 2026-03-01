from __future__ import annotations

import argparse
import json
from pathlib import Path

from easyeda2fusion.converter import ConversionConfig, Converter
from easyeda2fusion.matchers.library_matcher import LibraryEntry
from easyeda2fusion.model import ConversionMode, MatchMode, SourceFormat


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()

    if args.gui:
        from easyeda2fusion.ui.gui import run_gui_flow

        gui_result = run_gui_flow()
        if gui_result is None:
            print("GUI flow canceled.")
            return 1
        input_files, output_dir, mode, match_mode = gui_result
        source_format = None
        library_path = None
        resistor_library_path = None
        capacitor_library_path = None
        use_default_fusion_libraries = True
        verbose = args.verbose
    else:
        input_files = _arg_input_paths(args.input) if args.input else _prompt_input_files()
        output_dir = Path(args.output).expanduser().resolve() if args.output else _prompt_output_dir()
        mode = ConversionMode(args.mode)
        match_mode = MatchMode(args.match_mode)
        source_format = SourceFormat(args.source_format) if args.source_format else None
        library_path = Path(args.library).expanduser().resolve() if args.library else None
        resistor_library_path = Path(args.resistor_library).expanduser().resolve() if args.resistor_library else None
        capacitor_library_path = (
            Path(args.capacitor_library).expanduser().resolve() if args.capacitor_library else None
        )
        use_default_fusion_libraries = not bool(args.no_default_fusion_libraries)
        verbose = args.verbose

    config = ConversionConfig(
        input_files=input_files,
        output_dir=output_dir,
        mode=mode,
        match_mode=match_mode,
        source_format=source_format,
        library_path=library_path,
        resistor_library_path=resistor_library_path,
        capacitor_library_path=capacitor_library_path,
        use_default_fusion_libraries=use_default_fusion_libraries,
        verbose=verbose,
    )

    resolver = _interactive_resolver if match_mode == MatchMode.PROMPT else None
    try:
        result = Converter().run(config, resolver=resolver)
    except Exception as exc:
        print(f"Conversion failed: {exc}")
        print(
            "Hint: use --input with EasyEDA JSON file path(s). If a path contains spaces, keep it quoted."
        )
        if verbose:
            raise
        return 2

    print("Conversion completed.")
    print(f"Output: {result.output_dir}")
    print(json.dumps(result.summary, indent=2))
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert EasyEDA Standard/Lite or Pro projects to Fusion 360 Electronics (EAGLE) reconstruction artifacts.",
    )
    parser.add_argument("-i", "--input", action="append", help="Input EasyEDA file (repeat for multiple files)")
    parser.add_argument("-o", "--output", help="Output directory")
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in ConversionMode],
        default=ConversionMode.FULL.value,
        help="Conversion mode",
    )
    parser.add_argument(
        "--match-mode",
        choices=[mode.value for mode in MatchMode],
        default=MatchMode.AUTO.value,
        help="Library matching mode",
    )
    parser.add_argument(
        "--source-format",
        choices=[SourceFormat.EASYEDA_STD.value, SourceFormat.EASYEDA_PRO.value],
        help="Force source format detection",
    )
    parser.add_argument("--library", help="Path to JSON file or folder containing target library entries")
    parser.add_argument(
        "--resistor-library",
        help="Path to preferred resistor library (.lbr) or folder containing resistor library files",
    )
    parser.add_argument(
        "--capacitor-library",
        help="Path to preferred capacitor library (.lbr) or folder containing capacitor library files",
    )
    parser.add_argument(
        "--no-default-fusion-libraries",
        action="store_true",
        help="Do not auto-scan local default Fusion/EAGLE .lbr folders",
    )
    parser.add_argument("--gui", action="store_true", help="Launch simple GUI picker flow")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser


def _prompt_input_files() -> list[Path]:
    raw = input("Enter EasyEDA input file path(s), comma-separated: ").strip()
    paths = [Path(item.strip()).expanduser().resolve() for item in raw.split(",") if item.strip()]
    if not paths:
        raise SystemExit("No input files provided")
    return paths


def _arg_input_paths(raw_inputs: list[str] | None) -> list[Path]:
    if not raw_inputs:
        return []
    cleaned = [item.strip() for item in raw_inputs if item and item.strip()]
    if not cleaned:
        return []
    return [Path(item).expanduser().resolve() for item in cleaned]


def _prompt_output_dir() -> Path:
    raw = input("Enter output directory path: ").strip()
    if not raw:
        raise SystemExit("No output directory provided")
    return Path(raw).expanduser().resolve()


def _interactive_resolver(component, candidates: list[LibraryEntry]) -> LibraryEntry | None:
    print()
    print("Ambiguous library match:")
    print(
        f"  refdes={component.refdes} source={component.source_name} package={component.package_id or '-'} value={component.value}"
    )
    for idx, candidate in enumerate(candidates, start=1):
        print(
            f"  {idx}. {candidate.device_name} pkg={candidate.package_name} class={candidate.component_class or '-'} mpn={candidate.mpn or '-'}"
        )
    print("  d. defer/unresolved")

    while True:
        answer = input("Choose candidate: ").strip().lower()
        if answer == "d":
            return None
        if answer.isdigit():
            choice = int(answer)
            if 1 <= choice <= len(candidates):
                return candidates[choice - 1]
        print("Invalid choice. Enter a number or 'd'.")


if __name__ == "__main__":
    raise SystemExit(main())
