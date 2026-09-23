# Corresponding source

JumperManager v1.2.1 is licensed under **AGPL-3.0-only**. The Windows and Linux
executables in this release are built from the same application source at the
`v1.2.1` tag:

https://github.com/pgq18/JumperManager/tree/v1.2.1

The [v1.2.1 release](https://github.com/pgq18/JumperManager/releases/tag/v1.2.1)
provides these corresponding-source archives at no charge:

- [JumperManager-Source-v1.2.1.zip](https://github.com/pgq18/JumperManager/releases/download/v1.2.1/JumperManager-Source-v1.2.1.zip):
  the complete application source for both platforms, WebUI assets, tests,
  build scripts, pinned package requirements, license notices, and the matching
  pystray 0.19.5 and PyInstaller 6.22.3 source archives under `third-party-source/`.
- [JumperManager-Linux-Source-v1.2.1.tar.gz](https://github.com/pgq18/JumperManager/releases/download/v1.2.1/JumperManager-Linux-Source-v1.2.1.tar.gz):
  the same application source snapshot and build files, with the matching
  PyInstaller source archive for the Linux build.

The release's `SHA256SUMS.txt` identifies the downloadable archives. Each
binary package also includes an executable checksum and a build record.
Original upstream sources and their licenses are identified in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Both standalone programs include their own Python runtime. Remote SSH
endpoints do not need Python: TCP checks use OpenSSH forwarding, and
optional Linux process snapshots use a read-only shell helper with system
tools and `/proc`. Nothing is installed on the remote endpoint.

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

## Rebuilding or running on Linux

See [SOURCE-LINUX.md](SOURCE-LINUX.md) for the Linux build environment,
standalone executable build, and optional source execution. Linux installation
and everyday commands are covered in [docs/LINUX.md](docs/LINUX.md).
