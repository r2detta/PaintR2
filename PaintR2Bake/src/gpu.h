// SPDX-License-Identifier: GPL-3.0-or-later
//
// Just enough OpenCL to run one kernel.
//
// tinybvh ships a helper for this and it was not used: it reaches for GLFW to
// support sharing buffers with an OpenGL context, and this program has no
// window, no context and no reason to grow either. What is left after taking
// that away is small enough to own -- a device, a program, some buffers.
//
// Every entry point reports failure rather than throwing, because the caller's
// answer to "no OpenCL here" is not to abort. It is to trace on the CPU, which
// still works and is what a machine without a usable driver gets.

#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace gpu {

// Opaque so no OpenCL header leaks into the rest of the program; only gpu.cpp
// includes CL/cl.h.
struct Impl;

class Device {
public:
    Device();
    ~Device();
    Device(const Device&) = delete;
    Device& operator=(const Device&) = delete;

    // True when a device was found and the program compiled. `error` says why
    // not otherwise. Never a hard failure: see the file comment.
    bool open(const char* source, const char* options, std::string& error);

    // What we ended up on. Worth reporting -- an OpenCL run that silently
    // landed on a CPU device would look like a GPU win that is not one.
    const std::string& name() const { return name_; }
    uint64_t memory() const { return memory_; }
    bool is_gpu() const { return is_gpu_; }

    // Upload `bytes` from `host`, or allocate uninitialised when `host` is
    // null. Returns a slot index for `arg`, or -1 on failure.
    int upload(const void* host, size_t bytes, std::string& error);
    int allocate(size_t bytes, std::string& error);
    bool download(int slot, void* host, size_t bytes, std::string& error);

    bool arg_buffer(unsigned index, int slot);
    bool arg_uint(unsigned index, uint32_t value);
    bool arg_float(unsigned index, float value);
    // A kernel `float3` argument is sixteen bytes on the device, not twelve.
    // Passing three floats and letting this pad them is the difference between
    // a correct bounding box and one shifted by a component.
    bool arg_float3(unsigned index, float x, float y, float z);

    // Bind the kernel to set arguments on, then launch it. Two calls rather
    // than one because the arguments go in between, and binding by name once
    // keeps every `arg_*` free of a lookup.
    bool prepare(const char* entry, std::string& error);
    bool launch(size_t count, std::string& error);

    void release();

private:
    Impl* impl_;
    std::string name_;
    uint64_t memory_ = 0;
    bool is_gpu_ = false;
};

}  // namespace gpu
