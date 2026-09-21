# -*- mode: python ; coding: utf-8 -*-
"""Windows one-file, windowed build. No runtime user data is bundled."""
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata


ROOT = Path(SPEC).resolve().parent
WEB = ROOT / "web"
ICON = ROOT / "assets" / "JumperManager.ico"
VERSION_INFO = ROOT / "assets" / "version_info.txt"

if not ICON.is_file():
    raise RuntimeError("Run build_windows.py first to generate assets/JumperManager.ico.")

# Use an explicit application-data allowlist. Never add the project root: it
# contains mapping definitions, SSH logs, runtime records and build caches.
datas = [(str(ICON), "assets")]
web_suffixes = {".html", ".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2", ".webp", ".json"}
for path in sorted(WEB.rglob("*")):
    if path.is_file() and path.suffix.lower() in web_suffixes:
        if not path.resolve().is_relative_to(WEB.resolve()):
            raise RuntimeError(f"Web asset escapes the web directory: {path}")
        destination = Path("web") / path.relative_to(WEB).parent
        datas.append((str(path), destination.as_posix()))

# Preserve dependency metadata and license files; these contain no user data.
for distribution in ("pystray", "Pillow", "six"):
    datas += copy_metadata(distribution)

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "jumper_manager.tray",
        "pystray._win32",
        "pystray._util.win32",
        "PIL.Image",
        "PIL.ImageDraw",
        "PIL.IcoImagePlugin",
        "PIL.PngImagePlugin",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pystray._appindicator", "pystray._darwin", "pystray._gtk", "pystray._xorg",
        "pystray._util.gtk", "pystray._util.xorg",
        "PIL.ImageQt", "tkinter", "IPython", "matplotlib", "numpy", "pytest",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# Supplying binaries/datas directly to EXE, with no COLLECT, is one-file mode.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="JumperManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    runtime_tmpdir=None,
    icon=str(ICON),
    version=str(VERSION_INFO),
    uac_admin=False,
    uac_uiaccess=False,
)
