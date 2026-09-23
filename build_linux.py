"""Build the Linux x86_64 standalone CLI and WebUI on Ubuntu 24.04.

Use CPython 3.12 in .venv-build-linux with requirements-build-linux.txt.
Output is dist/linux/jumper-manager; the source shell launcher is untouched.
This script does not install dependencies, publish files, or remove directories.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import shlex
import struct
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "requirements-build-linux.txt"
SOURCE_DATE_EPOCH = str(int(datetime(2026, 9, 23, tzinfo=timezone.utc).timestamp()))
PUBLIC_WEB_SUFFIXES = {".html", ".css", ".js", ".svg", ".png", ".jpg", ".jpeg",
                       ".ico", ".woff", ".woff2", ".webp", ".json"}
PUBLIC_ASSET_SUFFIXES = {".svg", ".png", ".jpg", ".jpeg", ".ico", ".webp"}


def application_version() -> str:
    source = ROOT / "jumper_manager" / "__init__.py"
    for statement in ast.parse(source.read_text(encoding="utf-8")).body:
        if isinstance(statement, ast.Assign) and any(isinstance(name, ast.Name) and name.id == "__version__" for name in statement.targets):
            value = ast.literal_eval(statement.value)
            if isinstance(value, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", value):
                return value
    raise RuntimeError("jumper_manager/__init__.py must define a literal __version__.")


def pinned_requirements() -> dict[str, str]:
    result = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        value = line.partition("#")[0].strip()
        if not value:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)", value)
        if not match:
            raise RuntimeError(f"Build dependencies must be pinned exactly: {line}")
        result[match[1]] = match[2]
    return result


def check_toolchain() -> dict[str, str]:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Build on Linux; this script does not cross-compile from Windows.")
    if platform.python_implementation() != "CPython" or sys.version_info[:2] != (3, 12):
        raise RuntimeError("Use CPython 3.12 in the Linux build environment.")
    if struct.calcsize("P") != 8 or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("This build targets Linux x86_64 only.")
    distribution = platform.freedesktop_os_release()
    if distribution.get("ID") != "ubuntu" or distribution.get("VERSION_ID") != "24.04":
        raise RuntimeError("Use Ubuntu 24.04 for the supported Linux binary build baseline.")
    installed = {}
    mismatches = []
    for name, expected in pinned_requirements().items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            actual = None
        if actual != expected:
            mismatches.append(f"{name}: expected {expected}, installed {actual or 'missing'}")
        if actual is not None:
            installed[name] = actual
    if mismatches:
        command = shlex.join([sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)])
        raise RuntimeError("Build dependencies do not match:\n" + "\n".join(mismatches) + "\nRun: " + command)
    application_version()
    return installed


def public_assets() -> list[tuple[Path, str]]:
    """Explicit public directories only; never bundle root/data or SSH files."""
    result = []
    for directory, suffixes in ((ROOT / "web", PUBLIC_WEB_SUFFIXES), (ROOT / "assets", PUBLIC_ASSET_SUFFIXES)):
        if not directory.is_dir():
            if directory.name == "web":
                raise RuntimeError("The web directory is required for the Linux WebUI.")
            continue
        boundary = directory.resolve()
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix.lower() in suffixes:
                if not path.resolve().is_relative_to(boundary):
                    raise RuntimeError(f"Public asset escapes its directory: {path}")
                destination = Path(directory.name) / path.relative_to(directory).parent
                result.append((path, destination.as_posix()))
    if not (ROOT / "web" / "index.html").is_file():
        raise RuntimeError("web/index.html is missing.")
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_build_record(executable: Path, assets: list[tuple[Path, str]], environment: dict[str, str]) -> None:
    paths = {ROOT / "app.py", ROOT / "jumper-manager", ROOT / "build_linux.py", REQUIREMENTS}
    paths.update((ROOT / "jumper_manager").glob("*.py"))
    paths.update(path for path, _ in assets)
    packages = {distribution.metadata["Name"]: distribution.version for distribution in metadata.distributions()
                if distribution.metadata.get("Name")}
    libc_name, libc_version = platform.libc_ver()
    record = {
        "application": "JumperManager", "version": application_version(),
        "python": platform.python_version(), "architecture": platform.machine(),
        "distribution": platform.freedesktop_os_release().get("PRETTY_NAME"),
        "libc": {"name": libc_name, "version": libc_version},
        "python_hash_seed": environment["PYTHONHASHSEED"],
        "source_date_epoch": environment["SOURCE_DATE_EPOCH"],
        "packages": dict(sorted(packages.items(), key=lambda item: item[0].lower())),
        "sources": {path.relative_to(ROOT).as_posix(): sha256(path) for path in sorted(paths)},
        "executable": {"name": executable.name, "bytes": executable.stat().st_size, "sha256": sha256(executable)},
    }
    (executable.parent / "jumper-manager.build.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (executable.parent / "jumper-manager.sha256").write_text(f"{record['executable']['sha256']}  {executable.name}\n", encoding="ascii")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Validate the Linux build environment without building")
    args = parser.parse_args()
    installed = check_toolchain()
    print("Build toolchain: " + ", ".join(f"{name} {version}" for name, version in installed.items()))
    if args.check:
        return 0
    assets = public_assets()
    build_root = ROOT / "build" / "linux"
    work_path = build_root / "work"
    cache_path = build_root / "cache"
    dist_path = ROOT / "dist" / "linux"
    for directory in (build_root, work_path, cache_path, dist_path):
        directory.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = "1"
    environment["SOURCE_DATE_EPOCH"] = SOURCE_DATE_EPOCH
    environment["PYINSTALLER_CONFIG_DIR"] = str(cache_path)
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--console",
               "--name", "jumper-manager", "--distpath", str(dist_path), "--workpath", str(work_path),
               "--specpath", str(build_root), "--paths", str(ROOT)]
    for module in ("pystray", "PIL", "tkinter", "IPython", "matplotlib", "numpy", "pytest"):
        command += ["--exclude-module", module]
    for path, destination in assets:
        command += ["--add-data", f"{path}:{destination}"]
    command.append(str(ROOT / "app.py"))
    print("Building: " + shlex.join(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=environment, check=True)
    executable = dist_path / "jumper-manager"
    if not executable.is_file():
        raise RuntimeError("PyInstaller returned successfully but no Linux executable was created.")
    with executable.open("rb") as stream:
        header = stream.read(20)
    if len(header) < 20 or header[:6] != b"\x7fELF\x02\x01" or int.from_bytes(header[18:20], "little") != 62:
        raise RuntimeError("Build output is not a Linux x86_64 ELF executable.")
    if not os.access(executable, os.X_OK):
        raise RuntimeError("Build output is not executable.")
    write_build_record(executable, assets, environment)
    print(f"Built: {executable} ({executable.stat().st_size:,} bytes)")
    print(f"SHA256: {sha256(executable)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
