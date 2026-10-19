# SPDX-License-Identifier: GPL-3.0-or-later
#
# paintr2_bake for every platform, in CI. build.sh stays the Linux day-to-day
# build; this is the same one translation unit with each compiler's flags.
#
#   cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
#   cmake --build build --config Release
#
# The binary lands in build/ (build/Release/ with MSVC). Where it goes in the
# add-on is the caller's: PaintR2/bin/<platform>/.
#
# OpenCL:
#   Linux    libOpenCL.so (ocl-icd), headers from the distribution
#   Windows  Khronos OpenCL-ICD-Loader + OpenCL-Headers, built beforehand and
#            passed with -DCMAKE_PREFIX_PATH; OpenCL.dll itself comes with the
#            GPU driver, never with us
#   macOS    the system framework (OpenCL 1.2)

cmake_minimum_required(VERSION 3.16)
project(paintr2_bake LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release)
endif()

# The kernel source goes into the binary -- see embed_kernel.py.
find_package(Python3 REQUIRED COMPONENTS Interpreter)
set(KERNEL_SOURCE ${CMAKE_CURRENT_SOURCE_DIR}/src/bake.cl)
set(KERNEL_HEADER ${CMAKE_CURRENT_SOURCE_DIR}/src/bake_cl.h)
add_custom_command(
    OUTPUT ${KERNEL_HEADER}
    COMMAND Python3::Interpreter ${CMAKE_CURRENT_SOURCE_DIR}/embed_kernel.py
            ${KERNEL_SOURCE} ${KERNEL_HEADER}
    DEPENDS ${KERNEL_SOURCE} ${CMAKE_CURRENT_SOURCE_DIR}/embed_kernel.py
    COMMENT "Embedding src/bake.cl"
    VERBATIM
)

add_executable(paintr2_bake
    src/main.cpp
    src/gpu.cpp
    ${KERNEL_HEADER}
)

find_package(Threads REQUIRED)
target_link_libraries(paintr2_bake PRIVATE Threads::Threads)

if(APPLE)
    # No -march: arm64 only, and tiny_bvh takes its NEON path there.
    target_link_libraries(paintr2_bake PRIVATE "-framework OpenCL")
else()
    # The Khronos loader installs a CMake package; elsewhere FindOpenCL does.
    find_package(OpenCLICDLoader CONFIG QUIET)
    if(TARGET OpenCL::OpenCL)
        target_link_libraries(paintr2_bake PRIVATE OpenCL::OpenCL)
    else()
        find_package(OpenCL REQUIRED)
        target_link_libraries(paintr2_bake PRIVATE OpenCL::OpenCL)
    endif()
    find_package(OpenCLHeaders CONFIG QUIET)
    if(TARGET OpenCL::Headers)
        target_link_libraries(paintr2_bake PRIVATE OpenCL::Headers)
    endif()
endif()

# **No fast math anywhere** -- the BVH slab test relies on IEEE infinities;
# see build.sh for what relaxed math did to the cage rays.
if(MSVC)
    # /arch:AVX2 is the same Haswell baseline as -march=x86-64-v3. /fp:precise
    # is MSVC's default and is spelled out so nobody "optimises" it to fast.
    # /utf-8: the sources are UTF-8.
    target_compile_options(paintr2_bake PRIVATE
        /arch:AVX2 /fp:precise /utf-8 /W3 /EHsc
        /wd4068  # unknown pragma: the GCC diagnostic pragmas around tiny_bvh
    )
    # Not WIN32_LEAN_AND_MEAN: tiny_bvh defines it itself and would warn.
    target_compile_definitions(paintr2_bake PRIVATE
        _CRT_SECURE_NO_WARNINGS NOMINMAX)
    # One file to ship: the C runtime linked in, not a VC++ redistributable
    # the user may not have.
    set_property(TARGET paintr2_bake PROPERTY
        MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
    # Delay-loaded, so a machine with no OpenCL driver still starts the
    # baker and gets the CPU path rather than "OpenCL.dll was not found"
    # (gpu.cpp checks before the first call).
    target_link_options(paintr2_bake PRIVATE /DELAYLOAD:OpenCL.dll)
    target_link_libraries(paintr2_bake PRIVATE delayimp)
elseif(APPLE)
    target_compile_options(paintr2_bake PRIVATE -Wall -Wextra)
else()
    target_compile_options(paintr2_bake PRIVATE -Wall -Wextra -march=x86-64-v3)
    if(MINGW)
        # wmain instead of main: UTF-16 arguments (see main.cpp).
        target_compile_options(paintr2_bake PRIVATE -municode)
        target_link_options(paintr2_bake PRIVATE -municode -static)
    endif()
endif()
