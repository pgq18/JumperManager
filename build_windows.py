r"""Build JumperManager.exe with the pinned Windows CPython 3.14 toolchain.

Usage:
    .venv-build\Scripts\python.exe -m pip install -r requirements-build.txt
    .venv-build\Scripts\python.exe build_windows.py

The build only replaces its named artifacts. Other dist/ files and all data/
contents are untouched. No dependency installation or recursive cleanup occurs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import struct
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "requirements-build.txt"
VERSION = "1.1.1"
RELEASE_EPOCH = str(int(datetime(2026, 9, 21, tzinfo=timezone.utc).timestamp()))


def pinned_requirements() -> dict[str, str]:
    result = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        value = line.partition("#")[0].strip()
        if not value:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)", value)
        if not match:
            raise RuntimeError(f"Build requirements must use exact versions: {line}")
        result[match[1]] = match[2]
    return result


def check_toolchain() -> dict[str, str]:
    if os.name != "nt":
        raise RuntimeError("This build targets Windows; run it on Windows.")
    if sys.version_info[:2] != (3, 14):
        raise RuntimeError("Use 64-bit CPython 3.14 for this reproducible build.")
    if platform.python_implementation() != "CPython" or struct.calcsize("P") != 8:
        raise RuntimeError("The Windows build requires 64-bit CPython.")
    installed = {}
    missing = []
    for name, expected in pinned_requirements().items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            actual = None
        if actual != expected:
            missing.append(f"{name}: expected {expected}, installed {actual or 'missing'}")
        if actual is not None:
            installed[name] = actual
    if missing:
        command = subprocess.list2cmdline([sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)])
        raise RuntimeError("Build dependencies do not match the pinned toolchain:\n" + "\n".join(missing) + "\nRun: " + command)
    return installed


def generate_icon() -> Path:
    from PIL import Image
    from jumper_manager.tray import create_icon

    target = ROOT / "assets" / "JumperManager.ico"
    target.parent.mkdir(parents=True, exist_ok=True)
    image = create_icon().convert("RGBA")
    if image.size != (256, 256):
        image = image.resize((256, 256), Image.Resampling.LANCZOS)
    temporary = target.with_name("JumperManager.build.ico")
    sizes = [(side, side) for side in (16, 20, 24, 32, 40, 48, 64, 128, 256)]
    image.save(temporary, format="ICO", sizes=sizes)
    with Image.open(temporary) as check:
        if check.format != "ICO" or not set(sizes).issubset(check.ico.sizes()):
            raise RuntimeError("Generated application icon is missing an expected size.")
    temporary.replace(target)
    return target


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_build_record(executable: Path, environment: dict[str, str]) -> None:
    source_paths = [ROOT / "app.py", ROOT / "build_windows.py", ROOT / "JumperManager.spec", REQUIREMENTS]
    source_paths.extend(sorted((ROOT / "jumper_manager").glob("*.py")))
    source_paths.extend(path for path in sorted((ROOT / "web").rglob("*")) if path.is_file())
    source_paths.extend([ROOT / "assets" / "JumperManager.ico", ROOT / "assets" / "version_info.txt"])
    packages = {}
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            packages[name] = distribution.version
    record = {
        "application": "JumperManager", "version": VERSION,
        "python": platform.python_version(), "architecture": platform.machine(),
        "platform": platform.platform(),
        "python_hash_seed": environment["PYTHONHASHSEED"],
        "source_date_epoch": environment["SOURCE_DATE_EPOCH"],
        "packages": dict(sorted(packages.items(), key=lambda item: item[0].lower())),
        "sources": {path.relative_to(ROOT).as_posix(): sha256(path) for path in source_paths},
        "executable": {"name": executable.name, "bytes": executable.stat().st_size, "sha256": sha256(executable)},
    }
    (executable.parent / "JumperManager.build.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (executable.parent / "JumperManager.exe.sha256").write_text(f"{record['executable']['sha256']}  {executable.name}\n", encoding="ascii")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Check pinned build dependencies without creating an executable")
    parser.add_argument("--icon-only", action="store_true", help="Generate and verify the multi-size Windows icon only")
    args = parser.parse_args()
    installed = check_toolchain()
    print("Build toolchain: " + ", ".join(f"{key} {value}" for key, value in installed.items()))
    if args.check:
        return 0
    icon = generate_icon()
    print(f"Application icon: {icon}")
    if args.icon_only:
        return 0

    build_root = ROOT / "build" / "windows"
    work_path = build_root / "work"
    cache_path = build_root / "cache"
    dist_path = ROOT / "dist"
    for directory in (work_path, cache_path, dist_path):
        directory.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = "1"
    environment["SOURCE_DATE_EPOCH"] = RELEASE_EPOCH
    environment["PYINSTALLER_CONFIG_DIR"] = str(cache_path)
    environment["PYSTRAY_BACKEND"] = "win32"
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", str(dist_path),
               "--workpath", str(work_path), str(ROOT / "JumperManager.spec")]
    print("Building: " + subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=environment, check=True)
    executable = dist_path / "JumperManager.exe"
    if not executable.is_file():
        raise RuntimeError("PyInstaller returned successfully but JumperManager.exe was not created.")
    write_build_record(executable, environment)
    print(f"Built: {executable} ({executable.stat().st_size:,} bytes)")
    print(f"SHA256: {sha256(executable)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
