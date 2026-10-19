#!/usr/bin/env bash
# Build paintr2_bake into bin/.
#
#   ./build.sh            release
#   ./build.sh debug      with symbols and sanitizers
#
# One translation unit, one header dependency, no build system. That is the
# point. This is the Linux build used day to day; Windows and
# macOS build from CMakeLists.txt, in CI.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${ROOT_DIR}/bin"
mkdir -p "${OUT_DIR}"

CXX="${CXX:-g++}"

# x86-64-v3 is the AVX2 baseline (Haswell, 2013). `-march=native` would be
# faster here and unrunnable on anyone else's machine, which for something we
# intend to ship is the wrong trade.
COMMON=(-std=c++17 -Wall -Wextra -pthread -march=x86-64-v3)

if [[ "${1:-release}" == "debug" ]]; then
    FLAGS=("${COMMON[@]}" -O0 -g -fsanitize=address,undefined)
    OUT="${OUT_DIR}/paintr2_bake_debug"
else
    # **No -ffast-math, and this was measured rather than assumed.** A BVH slab
    # test is built on IEEE infinity semantics: an axis-aligned ray has a
    # reciprocal direction of infinity, and the traversal relies on inf-inf
    # producing NaN and on NaN comparing false. Relaxed math breaks exactly
    # that, and the failure is not a crash -- every axis-aligned ray silently
    # misses. Ours are cage rays, fired straight down a flat surface's normal,
    # so with -ffast-math the projection missed 65408 of 65536 texels and every
    # map downstream was quietly wrong.
    FLAGS=("${COMMON[@]}" -O3)
    OUT="${OUT_DIR}/paintr2_bake"
fi

# The OpenCL kernel is compiled at runtime by the driver, so its source is
# embedded in the binary -- see embed_kernel.py, which CMakeLists.txt runs too.
echo "==> embedding src/bake.cl"
python3 "${ROOT_DIR}/embed_kernel.py" "${ROOT_DIR}/src/bake.cl" "${ROOT_DIR}/src/bake_cl.h"

echo "==> ${CXX} ${FLAGS[*]}"
"${CXX}" "${FLAGS[@]}" "${ROOT_DIR}/src/main.cpp" "${ROOT_DIR}/src/gpu.cpp" \
    -o "${OUT}" -lOpenCL

echo "==> ${OUT}"
ls -lh "${OUT}"

# --- ship it -----------------------------------------------------------------
#
# The binary lives inside the extension, next to the Python that calls it. The
# alternative -- leaving it here and having the add-on hunt for a source tree
# -- works on this machine and on no other.
if [[ "${1:-release}" != "debug" ]]; then
    # One folder per platform: the add-on only ever looks in its own machine's
    # (`external.platform_tag`).
    SHIP_DIR="$(cd "${ROOT_DIR}/.." && pwd)/PaintR2/bin/linux-x64"
    mkdir -p "${SHIP_DIR}"
    cp "${OUT}" "${SHIP_DIR}/"
    chmod +x "${SHIP_DIR}/$(basename "${OUT}")"
    echo "==> ${SHIP_DIR}/$(basename "${OUT}")"
fi
