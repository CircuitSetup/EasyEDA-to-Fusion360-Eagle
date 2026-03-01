from __future__ import annotations

from pathlib import Path

from easyeda2fusion.matchers.library_loader import load_library_entries


def test_load_lbr_library_entries(fixtures_dir):
    entries = load_library_entries(fixtures_dir / "simple_fusion.lbr")
    assert any(entry.add_token == "simple_fusion:R0603" for entry in entries)
    assert any(entry.package_name == "R0603" for entry in entries)


def test_load_lbr_mpn_entries(fixtures_dir):
    entries = load_library_entries(fixtures_dir / "simple_mpn_fusion.lbr")
    assert any((entry.mpn or "").upper() == "STM32F030K6T6" for entry in entries)
    assert any("STM32F030K6T6" in [alias.upper() for alias in entry.aliases] for entry in entries)


def test_auto_scan_includes_fusion_electron_library_dir(tmp_path, monkeypatch):
    user = tmp_path / "User"
    appdata = tmp_path / "AppData"
    localapp = tmp_path / "LocalAppData"
    electron_lbr = localapp / "Autodesk" / "Autodesk Fusion 360" / "Electron" / "lbr"
    electron_lbr.mkdir(parents=True, exist_ok=True)

    fixture = Path(__file__).parent / "fixtures" / "simple_fusion.lbr"
    (electron_lbr / "rcl.lbr").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setenv("USERPROFILE", str(user))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localapp))

    entries = load_library_entries(None)
    assert any((entry.library_name or "").lower() == "rcl" for entry in entries)
    assert any(entry.library_path and entry.library_path.endswith("rcl.lbr") for entry in entries)


def test_load_library_entries_accepts_passive_override_paths(fixtures_dir, tmp_path):
    fixture = fixtures_dir / "simple_fusion.lbr"
    resistor_lib = tmp_path / "resistors.lbr"
    capacitor_lib = tmp_path / "capacitors.lbr"
    resistor_lib.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    capacitor_lib.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")

    entries = load_library_entries(
        None,
        resistor_library_path=resistor_lib,
        capacitor_library_path=capacitor_lib,
    )

    paths = {str(item.library_path or "") for item in entries if item.library_path}
    assert str(resistor_lib.resolve()) in paths
    assert str(capacitor_lib.resolve()) in paths


def test_auto_scan_includes_default_dirs_even_with_explicit_library_path(tmp_path, monkeypatch):
    user = tmp_path / "User"
    appdata = tmp_path / "AppData"
    localapp = tmp_path / "LocalAppData"
    electron_lbr = localapp / "Autodesk" / "Autodesk Fusion 360" / "Electron" / "lbr"
    electron_lbr.mkdir(parents=True, exist_ok=True)

    fixture = Path(__file__).parent / "fixtures" / "simple_fusion.lbr"
    (electron_lbr / "default_scan.lbr").write_text(
        fixture.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    explicit_lib = tmp_path / "explicit.lbr"
    explicit_lib.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setenv("USERPROFILE", str(user))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localapp))

    entries = load_library_entries(explicit_lib)
    paths = {str(item.library_path or "") for item in entries if item.library_path}
    assert str(explicit_lib.resolve()) in paths
    assert any(path.endswith("default_scan.lbr") for path in paths)


def test_auto_scan_can_be_disabled(tmp_path, monkeypatch):
    user = tmp_path / "User"
    appdata = tmp_path / "AppData"
    localapp = tmp_path / "LocalAppData"
    electron_lbr = localapp / "Autodesk" / "Autodesk Fusion 360" / "Electron" / "lbr"
    electron_lbr.mkdir(parents=True, exist_ok=True)

    fixture = Path(__file__).parent / "fixtures" / "simple_fusion.lbr"
    (electron_lbr / "default_scan.lbr").write_text(
        fixture.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    monkeypatch.setenv("USERPROFILE", str(user))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localapp))

    entries = load_library_entries(None, use_default_fusion_libraries=False)
    assert not any((item.library_path or "").endswith("default_scan.lbr") for item in entries)
