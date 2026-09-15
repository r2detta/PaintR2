// SPDX-License-Identifier: GPL-3.0-or-later

#include "gpu.h"

#define CL_TARGET_OPENCL_VERSION 120
#include <CL/cl.h>

#include <cstring>
#include <map>

namespace gpu {

struct Impl {
    cl_platform_id platform = nullptr;
    cl_device_id device = nullptr;
    cl_context context = nullptr;
    cl_command_queue queue = nullptr;
    cl_program program = nullptr;
    std::map<std::string, cl_kernel> kernels;
    std::vector<cl_mem> buffers;
    cl_kernel current = nullptr;
};

namespace {

std::string device_string(cl_device_id device, cl_device_info info) {
    size_t bytes = 0;
    if (clGetDeviceInfo(device, info, 0, nullptr, &bytes) != CL_SUCCESS || bytes == 0) {
        return {};
    }
    std::string value(bytes, '\0');
    clGetDeviceInfo(device, info, bytes, value.data(), nullptr);
    while (!value.empty() && (value.back() == '\0' || value.back() == ' ')) {
        value.pop_back();
    }
    return value;
}

// Prefer a real GPU, and say so rather than accepting whatever came first. An
// OpenCL run that quietly landed on the CPU device would measure as a GPU
// backend that is not one, which is the kind of wrong number that survives for
// months.
bool pick(cl_platform_id& out_platform, cl_device_id& out_device, bool& is_gpu) {
    cl_uint platform_count = 0;
    if (clGetPlatformIDs(0, nullptr, &platform_count) != CL_SUCCESS || !platform_count) {
        return false;
    }
    std::vector<cl_platform_id> platforms(platform_count);
    clGetPlatformIDs(platform_count, platforms.data(), nullptr);

    const cl_device_type order[2] = {cl_device_type(CL_DEVICE_TYPE_GPU),
                                     cl_device_type(CL_DEVICE_TYPE_ALL)};
    for (cl_device_type wanted : order) {
        for (cl_platform_id platform : platforms) {
            cl_uint count = 0;
            if (clGetDeviceIDs(platform, wanted, 0, nullptr, &count) != CL_SUCCESS ||
                !count) {
                continue;
            }
            std::vector<cl_device_id> devices(count);
            clGetDeviceIDs(platform, wanted, count, devices.data(), nullptr);
            out_platform = platform;
            out_device = devices[0];
            is_gpu = (wanted == CL_DEVICE_TYPE_GPU);
            return true;
        }
    }
    return false;
}

}  // namespace

Device::Device() : impl_(new Impl) {}

Device::~Device() {
    release();
    delete impl_;
}

bool Device::open(const char* source, const char* options, std::string& error) {
    if (!pick(impl_->platform, impl_->device, is_gpu_)) {
        error = "no OpenCL device";
        return false;
    }

    name_ = device_string(impl_->device, CL_DEVICE_NAME);
    cl_ulong memory = 0;
    clGetDeviceInfo(impl_->device, CL_DEVICE_GLOBAL_MEM_SIZE, sizeof(memory),
                    &memory, nullptr);
    memory_ = uint64_t(memory);

    cl_int status = CL_SUCCESS;
    impl_->context =
        clCreateContext(nullptr, 1, &impl_->device, nullptr, nullptr, &status);
    if (status != CL_SUCCESS) {
        error = "clCreateContext failed (" + std::to_string(status) + ")";
        return false;
    }

    impl_->queue = clCreateCommandQueue(impl_->context, impl_->device, 0, &status);
    if (status != CL_SUCCESS) {
        error = "clCreateCommandQueue failed (" + std::to_string(status) + ")";
        return false;
    }

    const size_t length = std::strlen(source);
    impl_->program =
        clCreateProgramWithSource(impl_->context, 1, &source, &length, &status);
    if (status != CL_SUCCESS) {
        error = "clCreateProgramWithSource failed (" + std::to_string(status) + ")";
        return false;
    }

    status = clBuildProgram(impl_->program, 1, &impl_->device, options, nullptr,
                            nullptr);
    if (status != CL_SUCCESS) {
        // The build log is the only thing that says *what* is wrong with a
        // kernel, so it is carried out rather than summarised.
        size_t bytes = 0;
        clGetProgramBuildInfo(impl_->program, impl_->device, CL_PROGRAM_BUILD_LOG,
                              0, nullptr, &bytes);
        std::string log(bytes ? bytes : 1, '\0');
        if (bytes) {
            clGetProgramBuildInfo(impl_->program, impl_->device,
                                  CL_PROGRAM_BUILD_LOG, bytes, log.data(), nullptr);
        }
        error = "kernel build failed: " + log;
        return false;
    }

    return true;
}

int Device::upload(const void* host, size_t bytes, std::string& error) {
    if (bytes == 0) bytes = 1;
    cl_int status = CL_SUCCESS;
    cl_mem_flags flags = CL_MEM_READ_WRITE;
    if (host) flags |= CL_MEM_COPY_HOST_PTR;
    cl_mem buffer = clCreateBuffer(impl_->context, flags, bytes,
                                   const_cast<void*>(host), &status);
    if (status != CL_SUCCESS) {
        error = "device allocation of " + std::to_string(bytes / (1024 * 1024)) +
                " MB failed (" + std::to_string(status) + ")";
        return -1;
    }
    impl_->buffers.push_back(buffer);
    return int(impl_->buffers.size()) - 1;
}

int Device::allocate(size_t bytes, std::string& error) {
    return upload(nullptr, bytes, error);
}

bool Device::download(int slot, void* host, size_t bytes, std::string& error) {
    if (slot < 0 || size_t(slot) >= impl_->buffers.size()) {
        error = "bad buffer slot";
        return false;
    }
    const cl_int status = clEnqueueReadBuffer(impl_->queue, impl_->buffers[slot],
                                              CL_TRUE, 0, bytes, host, 0, nullptr,
                                              nullptr);
    if (status != CL_SUCCESS) {
        error = "reading back failed (" + std::to_string(status) + ")";
        return false;
    }
    return true;
}

bool Device::arg_buffer(unsigned index, int slot) {
    if (!impl_->current || slot < 0 || size_t(slot) >= impl_->buffers.size()) {
        return false;
    }
    return clSetKernelArg(impl_->current, index, sizeof(cl_mem),
                          &impl_->buffers[slot]) == CL_SUCCESS;
}

bool Device::arg_uint(unsigned index, uint32_t value) {
    return impl_->current &&
           clSetKernelArg(impl_->current, index, sizeof(uint32_t), &value) ==
               CL_SUCCESS;
}

bool Device::arg_float(unsigned index, float value) {
    return impl_->current &&
           clSetKernelArg(impl_->current, index, sizeof(float), &value) == CL_SUCCESS;
}

bool Device::arg_float3(unsigned index, float x, float y, float z) {
    const float padded[4] = {x, y, z, 0.0f};
    return impl_->current &&
           clSetKernelArg(impl_->current, index, sizeof(padded), padded) ==
               CL_SUCCESS;
}

bool Device::prepare(const char* entry, std::string& error) {
    auto found = impl_->kernels.find(entry);
    if (found == impl_->kernels.end()) {
        cl_int status = CL_SUCCESS;
        cl_kernel kernel = clCreateKernel(impl_->program, entry, &status);
        if (status != CL_SUCCESS) {
            error = std::string("no kernel named ") + entry;
            return false;
        }
        found = impl_->kernels.emplace(entry, kernel).first;
    }
    impl_->current = found->second;
    return true;
}

bool Device::launch(size_t count, std::string& error) {
    if (!impl_->current) {
        error = "launch without prepare";
        return false;
    }

    // Round the global size up to a whole number of work groups. The kernel
    // guards on `texel >= texel_count`, so the spill does nothing -- and a
    // global size that is not a multiple of the local size is an error on some
    // drivers rather than a rounding.
    size_t local = 64;
    size_t max_group = 0;
    if (clGetKernelWorkGroupInfo(impl_->current, impl_->device,
                                 CL_KERNEL_WORK_GROUP_SIZE, sizeof(max_group),
                                 &max_group, nullptr) == CL_SUCCESS &&
        max_group > 0 && local > max_group) {
        local = max_group;
    }
    const size_t global = ((count + local - 1) / local) * local;

    cl_int status = clEnqueueNDRangeKernel(impl_->queue, impl_->current, 1, nullptr,
                                           &global, &local, 0, nullptr, nullptr);
    if (status != CL_SUCCESS) {
        error = "launch failed (" + std::to_string(status) + ")";
        return false;
    }
    status = clFinish(impl_->queue);
    if (status != CL_SUCCESS) {
        error = "kernel did not finish (" + std::to_string(status) + ")";
        return false;
    }
    return true;
}

void Device::release() {
    for (auto& entry : impl_->kernels) clReleaseKernel(entry.second);
    impl_->kernels.clear();
    for (cl_mem buffer : impl_->buffers) clReleaseMemObject(buffer);
    impl_->buffers.clear();
    if (impl_->program) clReleaseProgram(impl_->program);
    if (impl_->queue) clReleaseCommandQueue(impl_->queue);
    if (impl_->context) clReleaseContext(impl_->context);
    impl_->program = nullptr;
    impl_->queue = nullptr;
    impl_->context = nullptr;
    impl_->current = nullptr;
}

}  // namespace gpu
