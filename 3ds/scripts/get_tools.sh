#!/bin/sh
# Fetches the CIA packaging tools into 3ds/.tools/ (no sudo, nothing system-wide):
#   makerom    - prebuilt release from github.com/3DSGuy/Project_CTR
#   bannertool - built from source (github.com/diasurgical/bannertool, maintained fork
#                of the archived Steveice10/bannertool). Its buildtools Makefile rejects
#                arm64 hosts, so we compile the handful of sources directly.
# Needs: curl, unzip, git, cc/c++ (Xcode command line tools).
set -eu

MAKEROM_VER="v0.19.0"
BANNERTOOL_REF="1.2.0"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOLS="$ROOT/.tools"
WORK="$TOOLS/src"
mkdir -p "$TOOLS" "$WORK"

# ---- makerom ----
if [ ! -x "$TOOLS/makerom" ]; then
    case "$(uname -s)-$(uname -m)" in
        Darwin-arm64)  asset="macos_arm64" ;;
        Darwin-x86_64) asset="macos_x86_64" ;;
        Linux-x86_64)  asset="ubuntu_x86_64" ;;
        *) echo "No prebuilt makerom for $(uname -s)-$(uname -m); build Project_CTR yourself." >&2; exit 1 ;;
    esac
    url="https://github.com/3DSGuy/Project_CTR/releases/download/makerom-$MAKEROM_VER/makerom-$MAKEROM_VER-$asset.zip"
    echo "Downloading $url"
    rm -rf "$WORK/makerom" && mkdir -p "$WORK/makerom"
    curl -fL -o "$WORK/makerom.zip" "$url"
    unzip -o -q "$WORK/makerom.zip" -d "$WORK/makerom"
    bin="$(find "$WORK/makerom" -type f -name makerom | head -n 1)"
    [ -n "$bin" ] || { echo "makerom binary not found in archive" >&2; exit 1; }
    cp "$bin" "$TOOLS/makerom"
    chmod +x "$TOOLS/makerom"
    # Downloaded binaries on macOS carry the quarantine flag.
    command -v xattr >/dev/null 2>&1 && xattr -d com.apple.quarantine "$TOOLS/makerom" 2>/dev/null || true
fi

# ---- bannertool ----
if [ ! -x "$TOOLS/bannertool" ]; then
    rm -rf "$WORK/bannertool"
    git clone --depth 1 --branch "$BANNERTOOL_REF" \
        https://github.com/diasurgical/bannertool.git "$WORK/bannertool"
    src="$WORK/bannertool/source"
    obj="$WORK/bannertool/obj"
    mkdir -p "$obj"
    for f in "$src"/pc/stb_image.c "$src"/pc/stb_vorbis.c; do
        cc -O2 -w -I"$src" -c "$f" -o "$obj/$(basename "$f").o"
    done
    for f in "$src"/*.cpp "$src"/pc/*.cpp "$src"/3ds/*.cpp; do
        c++ -O2 -std=c++11 -w -I"$src" -DVERSION_MAJOR=1 -DVERSION_MINOR=2 -DVERSION_MICRO=0 \
            -c "$f" -o "$obj/$(basename "$f").o"
    done
    c++ "$obj"/*.o -o "$TOOLS/bannertool"
fi

echo "Tools ready in $TOOLS:"
"$TOOLS/makerom" 2>&1 | head -n 1 || true
ls -l "$TOOLS/makerom" "$TOOLS/bannertool"
