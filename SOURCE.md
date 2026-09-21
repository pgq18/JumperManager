# Corresponding source

JumperManager v1.1.4 is licensed under **AGPL-3.0-only**. The matching
application source is the `v1.1.4` tag at:

https://github.com/pgq18/JumperManager/tree/v1.1.4

The same release page as the Windows executable provides
**JumperManager-Source-v1.1.4.zip** at no charge. This archive includes the
application source, build scripts, pinned package requirements, license
notices, and the matching pystray 0.19.5 and PyInstaller 6.22.3 source archives
under `third-party-source/`. Original upstream sources are identified in
`THIRD_PARTY_NOTICES.md`. These libraries retain their original licenses.

## Rebuilding the Windows executable

Use 64-bit CPython 3.14 on Windows, with the pinned packages in
`requirements-build.txt`. The published build used CPython 3.14.7. From the
source directory, run:

```bat
python -m venv .venv-build
.venv-build\Scripts\python.exe -m pip install -r requirements-build.txt
.venv-build\Scripts\python.exe build_windows.py
```

The executable will be written to `dist/JumperManager.exe`. The build also
creates a checksum and a build record. For redistribution, include `LICENSE`,
`NOTICE`, `THIRD_PARTY_NOTICES.md`, `SOURCE.md`, and `licenses/` with the
executable, and provide access to the matching source.

To rebuild with a modified pystray library, extract its included source
archive, make your changes, and install that local directory into the build
environment before running `build_windows.py`. If you change its package
version, update the corresponding requirement pin to match. The application
source and scripts are supplied in editable form; no signing key or activation
step is needed to run a rebuilt executable.

The interpreter, Windows runtime and compiler/system libraries use their own
licenses. Build on a supported Windows toolchain. Review the bundled native
libraries and update their license notices when using a different interpreter
or dependency version.
