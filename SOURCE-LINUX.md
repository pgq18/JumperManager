# Corresponding source for the Linux release

JumperManager v1.2.1 is licensed under **AGPL-3.0-only**. The Linux standalone
executable is built from the same application source as the Windows release,
including the Linux CLI, user-service integration, and build support. The
matching source tag is
[v1.2.1](https://github.com/pgq18/JumperManager/tree/v1.2.1).

## Matching binary and source archives

The [v1.2.1 release](https://github.com/pgq18/JumperManager/releases/tag/v1.2.1)
provides:

- [JumperManager-Linux-x86_64.tar.gz](https://github.com/pgq18/JumperManager/releases/download/v1.2.1/JumperManager-Linux-x86_64.tar.gz):
  the standalone executable, build record,
  checksum, and accompanying notices and license texts.
- [JumperManager-Linux-Source-v1.2.1.tar.gz](https://github.com/pgq18/JumperManager/releases/download/v1.2.1/JumperManager-Linux-Source-v1.2.1.tar.gz):
  the matching application source snapshot,
  WebUI assets, tests, build scripts, pinned Linux build requirements, and
  license notices. It also contains the PyInstaller 6.22.3 source archive under
  `third-party-source/`, including its loader and bootloader sources.
- `SHA256SUMS.txt`: checksums of the release archives.

The complete `JumperManager-Source-v1.2.1.zip` described in
[SOURCE.md](SOURCE.md) contains the same application snapshot and is also
suitable for rebuilding Linux. Use the source archive matching the binary's
release version. The
`jumper-manager.build.json` record in the binary package identifies the
executable by SHA-256 and records the hashes of the application and build inputs.
`jumper-manager.sha256` provides the executable checksum.

Third-party components retain their own licenses. The Linux package includes
the notices for its bundled Ubuntu Python and native-library components under
`licenses/linux/`, together with the applicable PyInstaller license and
bootloader exception. The Linux section of `THIRD_PARTY_NOTICES.md` identifies
the bundled components separately from the Windows inventory.

## Rebuilding the standalone executable

Extract `JumperManager-Linux-Source-v1.2.1.tar.gz` and work from its project root.
Use **Ubuntu 24.04, x86_64, glibc 2.39, and CPython 3.12** with a separate Linux
build environment. This is the tested release baseline; compatibility with
older glibc versions or other distributions is not established by this build.

```sh
python3.12 -m venv .venv-build-linux
.venv-build-linux/bin/python -m pip install -r requirements-build-linux.txt
.venv-build-linux/bin/python build_linux.py --check
.venv-build-linux/bin/python build_linux.py
```

The script checks the build platform and pinned dependencies. It produces:

```text
dist/linux/jumper-manager
dist/linux/jumper-manager.sha256
dist/linux/jumper-manager.build.json
```

The root-level `jumper-manager` file in the source archive is a shell launcher
for source execution; it is not the standalone ELF executable. The build writes
the ELF under `dist/linux/` and leaves that source launcher intact.

The build record gives the precise interpreter version, libc baseline, and
Python package versions used for the delivered binary. The build includes the
WebUI and Python runtime; it does not include user mapping data, SSH settings,
or keys. OpenSSH is invoked from the user's system. The optional login-start
integration uses the system's `systemd --user` service manager.

To use a modified PyInstaller, extract the included PyInstaller source archive,
make the changes, and install that source into the same build environment.
If its package version changes, update the corresponding requirement pin before
running `build_linux.py`. Application and build sources are supplied in editable
form; a rebuilt program does not require a signing key or activation step.

## Running from source

Source execution requires Python 3.10 or newer and OpenSSH. The Linux runtime
uses only the Python standard library and does not require a graphical desktop
or third-party Python packages. From the extracted source directory:

```sh
chmod +x jumper-manager
./jumper-manager start
./jumper-manager list
./jumper-manager stop
```

The source launcher uses `python3`. This interpreter requirement applies only
to source execution; the standalone Linux program includes its Python runtime.
Remote SSH endpoints do not need Python. Port checks use native SSH
forwarding. Optional remote Linux process snapshots use `sh`, `od`,
`stat -L -c`, and `/proc`; `getent` is used only when the target service
address is a hostname. Missing tools or insufficient process visibility
produce an unknown snapshot and do not prevent tunnel startup.

## Keeping the delivery together

Keep `LICENSE`, `NOTICE`, `SOURCE-LINUX.md`, `THIRD_PARTY_NOTICES.md`, and the
applicable `licenses/` files with the Linux executable, and provide access to
the matching `JumperManager-Linux-Source-v1.2.1.tar.gz` alongside it. If rebuilding with
different libraries, retain their notices and update the Linux component
inventory for that new binary.

Linux installation and command examples are documented in `docs/LINUX.md`.
