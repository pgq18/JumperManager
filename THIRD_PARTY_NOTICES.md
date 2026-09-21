# Third-party notices

JumperManager's own source code is licensed under **AGPL-3.0-only**. The
third-party components below retain their own licenses and copyright notices.
This document describes the Windows executable built with CPython 3.14.7,
PyInstaller 6.22.3, pystray 0.19.5, Pillow 12.3.0, and six 1.17.0.

The accompanying `licenses/` directory contains the license texts. Keep this
document and that directory with the executable when redistributing it.

## Python packages and executable support

| Component | Version | License | Included license text | Official source |
| --- | --- | --- | --- | --- |
| CPython interpreter and standard library | 3.14.7 | PSF-2.0 and retained historical notices; incorporated components retain their own terms | `Python-LICENSE.txt` and `Python-Incorporated-Software-Notices.html` | [CPython v3.14.7](https://github.com/python/cpython/tree/v3.14.7); [Python license and incorporated-software notices](https://docs.python.org/release/3.14.7/license.html) |
| pystray | 0.19.5 | LGPL-3.0-or-later | `pystray-LGPL-3.0.txt` and `pystray-GPL-3.0.txt` | [pystray source](https://github.com/moses-palmer/pystray); [versioned source distribution](https://github.com/moses-palmer/pystray/tree/v0.19.5) |
| Pillow / Python Imaging Library | 12.3.0 | MIT-CMU; included native libraries have additional terms | `Pillow-and-bundled-libraries-LICENSE.txt` | [Pillow 12.3.0](https://github.com/python-pillow/Pillow/tree/12.3.0) |
| six | 1.17.0 | MIT | `six-LICENSE.txt` | [six source](https://github.com/benjaminp/six); [versioned source distribution](https://pypi.org/project/six/1.17.0/#files) |
| PyInstaller bootloader and loader | 6.22.3 | GPL-2.0-or-later with the PyInstaller bootloader exception | `PyInstaller-COPYING.txt` | [PyInstaller v6.22.3](https://github.com/pyinstaller/pyinstaller/tree/v6.22.3) |
| PyInstaller `pyi_rth_inspect` runtime hook | 6.22.3 | Apache-2.0 | Apache license text in `PyInstaller-COPYING.txt` | [Runtime hook source](https://github.com/pyinstaller/pyinstaller/blob/v6.22.3/PyInstaller/hooks/rthooks/pyi_rth_inspect.py) |

pystray is used for the Windows notification-area icon. Its library code and
use are covered by the GNU Lesser General Public License, version 3 or later.
Copyright (C) 2016–2022 Moses Palmér. The LGPL and GPL texts are both included.
The application's source and build instructions permit rebuilding the
executable with a modified, interface-compatible copy of pystray; retain access
to the matching pystray source distribution alongside a binary release.

The PyInstaller bootloader exception permits distribution of the combined
executable under the application's license. PyInstaller's embedded runtime
hook is separately covered by Apache-2.0. The PyInstaller license text is
included for both its exception and the runtime-hook terms. See the
[upstream licensing explanation](https://pyinstaller.org/en/stable/license.html).

## Native libraries included with Pillow

The complete Pillow wheel license file is preserved, including its additional
license sections. Do not replace it with the shorter top-level Pillow source
license. The wheel identifies the following component versions:

| Component | Version | Official source |
| --- | --- | --- |
| Brotli | 1.2.0 | [google/brotli](https://github.com/google/brotli) |
| FreeType | 2.14.3 | [FreeType](https://freetype.org/) |
| HarfBuzz | 14.2.1 | [harfbuzz/harfbuzz](https://github.com/harfbuzz/harfbuzz) |
| Little CMS | 2.19.1 | [mm2/Little-CMS](https://github.com/mm2/Little-CMS) |
| libavif | 1.4.2 | [AOMediaCodec/libavif](https://github.com/AOMediaCodec/libavif) |
| libjpeg-turbo | 3.1.4.1 | [libjpeg-turbo/libjpeg-turbo](https://github.com/libjpeg-turbo/libjpeg-turbo) |
| libpng | 1.6.58 | [pnggroup/libpng](https://github.com/pnggroup/libpng) |
| libwebp | 1.6.0 | [WebP source](https://chromium.googlesource.com/webm/libwebp/) |
| OpenJPEG | 2.5.4 | [uclouvain/openjpeg](https://github.com/uclouvain/openjpeg) |
| LibTIFF | 4.7.1 | [libtiff/libtiff](https://gitlab.com/libtiff/libtiff) |
| XZ / liblzma | 5.8.3 | [tukaani-project/xz](https://github.com/tukaani-project/xz/tree/v5.8.3) |
| zlib-ng | 2.3.3 | [zlib-ng/zlib-ng](https://github.com/zlib-ng/zlib-ng) |

This software is based in part on the work of the Independent JPEG Group.
This software is based in part on the work of the FreeType Team.

## Native libraries used by the Python runtime

| Component / binary | Version | License | License text | Official source |
| --- | --- | --- | --- | --- |
| OpenSSL (`libcrypto-3-x64.dll`, `libssl-3-x64.dll`) | 3.6.4 | Apache-2.0 | `OpenSSL-LICENSE.txt` | [OpenSSL 3.6.4](https://github.com/openssl/openssl/tree/openssl-3.6.4) |
| Zstandard (`zstd.dll`) | 1.5.7 | BSD-3-Clause option | `zstd-LICENSE.txt` | [Zstandard v1.5.7](https://github.com/facebook/zstd/tree/v1.5.7) |
| liblzma (`liblzma.dll`) | 5.8.3 | 0BSD | `XZ-0BSD.txt` and XZ 5.8.3 section of `Pillow-and-bundled-libraries-LICENSE.txt` | [XZ v5.8.3 licensing](https://github.com/tukaani-project/xz/blob/v5.8.3/COPYING) |
| bzip2 (`LIBBZ2.dll`) | 1.0.8 | bzip2-1.0.8 | `bzip2-LICENSE.txt` | [bzip2 1.0.8 source archive](https://sourceware.org/pub/bzip2/bzip2-1.0.8.tar.gz) |
| Expat (`libexpat.dll`) | 2.8.1 | MIT | `Expat-COPYING.txt` | [Expat R_2_8_1](https://github.com/libexpat/libexpat/tree/R_2_8_1) |
| libffi (`ffi.dll`) | 3.4.8 | MIT | `libffi-LICENSE.txt` | [libffi v3.4.8](https://github.com/libffi/libffi/tree/v3.4.8) |
| mpdecimal (`libmpdec-4.dll`) | 4.0.1 | BSD-2-Clause | `mpdecimal-LICENSE.txt` | [mpdecimal 4.0.1 source archive](https://www.bytereef.org/software/mpdecimal/releases/mpdecimal-4.0.1.tar.gz) |
| zlib (`zlib.dll`) | 1.3.2 | Zlib | `zlib-LICENSE.txt` | [zlib v1.3.2](https://github.com/madler/zlib/tree/v1.3.2) |

OpenSSL is copyright 1998–2026 The OpenSSL Authors. Zstandard is copyright
Meta Platforms, Inc. and affiliates. Complete copyright notices and warranty
disclaimers for these components are retained in their license texts.

## Microsoft runtime components

The executable also includes the Microsoft Visual C++ runtime
(`VCRUNTIME140.dll`, `VCRUNTIME140_1.dll`, version 14.44.35208.0), Universal C
Runtime and API-set forwarding libraries (version 10.0.22621.1), and three
additional API-set forwarding libraries (version 10.0.26100.4654). These
Microsoft components retain their separate Microsoft license terms and are
not relicensed under AGPL-3.0-only.

Copyright © Microsoft Corporation. All rights reserved.

See Microsoft's [Visual C++ runtime redistribution terms and distributable
files](https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution)
and [runtime redistribution guidance](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files).
The applicable Microsoft runtime/SDK distribution terms must accompany or
otherwise govern the supplied runtime components as required by their source
distribution. Merely including this notice does not grant redistribution rights.

## Rebuilding and source availability

Python's incorporated-software notices include the Unicode Character Database
(16.0.0 in this interpreter, Unicode License V3) and the pyzstd-derived
Zstandard bindings (BSD-3-Clause), as well as the other acknowledgements
preserved in the accompanying Python licensing document.

The matching pystray 0.19.5 and PyInstaller 6.22.3 source archives accompany
the release. The published JumperManager source includes `requirements-build.txt`,
`build_windows.py`, and `JumperManager.spec`. The Python-package versions used
for this executable are pinned there. The component source links above identify
upstream source distributions; their respective licenses remain in effect.

Build-only packages such as altgraph, packaging, pefile, setuptools,
pywin32-ctypes, and the standard hooks from pyinstaller-hooks-contrib are not
application runtime components in the examined executable. If future builds
include additional runtime files or native libraries, update this inventory
and retain their corresponding notices.

Matching application and dependency sources, together with rebuild instructions, are provided in `JumperManager-Source-v1.1.1.zip` on the [release page](https://github.com/pgq18/JumperManager/releases/tag/v1.1.1). See also [SOURCE.md](SOURCE.md).
