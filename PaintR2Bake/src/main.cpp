// SPDX-License-Identifier: GPL-3.0-or-later
//
// paintr2_bake -- mesh maps, outside Blender.
//
//   paintr2_bake input.pr2b output.pr2m
//
// Seven maps: ambient occlusion, thickness, position, normal, height,
// curvature and bent normal. PaintR2 falls back to Cycles when this binary is
// not present, so the whole reason for it is speed -- if it does not beat
// Cycles by a wide margin it has no reason to exist.
//
// The shape that buys that margin:
//
//   read -> build one BVH -> rasterize the low-poly's UVs once -> trace
//
// Every later map reuses that gbuffer and that BVH. In Cycles each map pays
// for its own scene sync and its own BVH; here they are paid once no matter
// how many maps are asked for.

// tinybvh is vendored as-is and warns under -Wall -Wextra. Silencing it here
// rather than dropping the flags keeps our own warnings visible, which is the
// only reason the flags are on.
#define TINYBVH_IMPLEMENTATION
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-variable"
#pragma GCC diagnostic ignored "-Wuninitialized"
#pragma GCC diagnostic ignored "-Warray-bounds"
#include "../third_party/tiny_bvh.h"
#pragma GCC diagnostic pop

#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

#include "bake_cl.h"
#include "format.h"
#include "gpu.h"

using tinybvh::BVH;
using tinybvh::BVH8_CPU;
using tinybvh::BVH_GPU;
using tinybvh::bvhvec3;
using tinybvh::bvhvec4;
using tinybvh::Ray;

namespace {

// --- small vector helpers ----------------------------------------------------

struct Vec3 {
    float x = 0, y = 0, z = 0;
};

inline Vec3 operator+(Vec3 a, Vec3 b) { return {a.x + b.x, a.y + b.y, a.z + b.z}; }
inline Vec3 operator-(Vec3 a, Vec3 b) { return {a.x - b.x, a.y - b.y, a.z - b.z}; }
inline Vec3 operator*(Vec3 a, float s) { return {a.x * s, a.y * s, a.z * s}; }
inline float dot(Vec3 a, Vec3 b) { return a.x * b.x + a.y * b.y + a.z * b.z; }

inline Vec3 cross(Vec3 a, Vec3 b) {
    return {a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x};
}

// Negation that cannot hand back a negative zero.
//
// **Measured, and it cost most of a day: a BVH traversal misses every ray
// whose direction has a `-0.0` component.** The reciprocal of `-0.0` is
// negative infinity, the slab test then evaluates `inf - inf`, and the NaN
// that comes out compares false against every bound -- so the ray reports a
// clean miss rather than an error. With `+0.0` in the same component the same
// ray hits. Reproduced on tinybvh's own CPU traversal and on ours:
//
//     direction (+0, +0, -1)  ->  t = 0.2011
//     direction (-0, -0, -1)  ->  miss
//
// Which would be a curiosity, except that the cage ray is fired straight down
// a surface normal: a flat face has a normal of exactly `(0, 0, 1)`, plain
// negation gives exactly `(-0, -0, -1)`, and **every** texel of every flat
// surface misses. That is what it did -- 0 of 65536 -- while still producing a
// full set of plausible maps.
inline Vec3 flip(Vec3 v) {
    return {v.x == 0.0f ? 0.0f : -v.x,
            v.y == 0.0f ? 0.0f : -v.y,
            v.z == 0.0f ? 0.0f : -v.z};
}

// The same guard for a direction that was computed rather than negated. A
// hemisphere sample can land exactly on an axis, and that is enough.
inline Vec3 safe_direction(Vec3 v) {
    return {v.x == 0.0f ? 0.0f : v.x,
            v.y == 0.0f ? 0.0f : v.y,
            v.z == 0.0f ? 0.0f : v.z};
}

inline Vec3 normalize(Vec3 v) {
    const float length = std::sqrt(dot(v, v));
    return length > 1e-20f ? v * (1.0f / length) : Vec3{0, 0, 1};
}

// --- input -------------------------------------------------------------------

struct Scene {
    pr2::Header header{};
    std::vector<float> low;        // low_tris * 3 corners * 8 floats
    std::vector<float> high_pos;   // high_verts * 3
    std::vector<float> high_nrm;   // high_verts * 3
    std::vector<uint32_t> high_idx;
    std::vector<float> occ_pos;    // occluder_verts * 3
    std::vector<uint32_t> occ_idx; // occluder_tris * 3

    bool has_high() const { return header.high_verts > 0 && header.high_tris > 0; }
    bool has_occluders() const {
        return header.occluder_verts > 0 && header.occluder_tris > 0;
    }
};

bool read_all(std::FILE* file, void* into, size_t bytes) {
    return bytes == 0 || std::fread(into, 1, bytes, file) == bytes;
}

bool load(const char* path, Scene& scene, std::string& error) {
    std::FILE* file = std::fopen(path, "rb");
    if (!file) {
        error = std::string("cannot open ") + path;
        return false;
    }

    bool ok = read_all(file, &scene.header, sizeof(scene.header));
    if (ok && std::memcmp(scene.header.magic, pr2::kInputMagic, 8) != 0) {
        error = "not a PaintR2 bake input, or written by a different version";
        ok = false;
    }
    if (ok && scene.header.version != pr2::kVersion) {
        error = "input version " + std::to_string(scene.header.version) +
                ", this build speaks " + std::to_string(pr2::kVersion);
        ok = false;
    }
    if (ok && scene.header.low_tris == 0) {
        error = "no low-poly triangles to bake into";
        ok = false;
    }

    if (ok) {
        const size_t low_floats =
            size_t(scene.header.low_tris) * 3 * pr2::kLowCornerFloats;
        scene.low.resize(low_floats);
        ok = read_all(file, scene.low.data(), low_floats * sizeof(float));
        if (!ok) error = "input ends inside the low-poly";
    }

    if (ok && scene.has_high()) {
        const size_t verts = scene.header.high_verts;
        const size_t tris = scene.header.high_tris;
        scene.high_pos.resize(verts * 3);
        scene.high_nrm.resize(verts * 3);
        scene.high_idx.resize(tris * 3);
        ok = read_all(file, scene.high_pos.data(), verts * 3 * sizeof(float)) &&
             read_all(file, scene.high_nrm.data(), verts * 3 * sizeof(float)) &&
             read_all(file, scene.high_idx.data(), tris * 3 * sizeof(uint32_t));
        if (!ok) error = "input ends inside the high-poly";
    }

    if (ok && scene.has_occluders()) {
        const size_t verts = scene.header.occluder_verts;
        const size_t tris = scene.header.occluder_tris;
        scene.occ_pos.resize(verts * 3);
        scene.occ_idx.resize(tris * 3);
        ok = read_all(file, scene.occ_pos.data(), verts * 3 * sizeof(float)) &&
             read_all(file, scene.occ_idx.data(), tris * 3 * sizeof(uint32_t));
        if (!ok) error = "input ends inside the occluders";
        for (size_t i = 0; ok && i < scene.occ_idx.size(); ++i) {
            if (scene.occ_idx[i] >= verts) {
                error = "occluder index out of range";
                ok = false;
            }
        }
    }

    std::fclose(file);
    return ok;
}

// --- the gbuffer -------------------------------------------------------------
//
// What the low-poly's UV layout says about each texel: where on the surface it
// is and which way that surface faces. Rasterized once, then every map reads
// it. `valid` is also the coverage mask the output's alpha carries, which is
// what tells Blender's dilate.fill where the islands end.

struct GBuffer {
    uint32_t size = 0;
    std::vector<Vec3> position;
    std::vector<Vec3> normal;
    std::vector<Vec3> tangent;
    std::vector<float> sign;      // bitangent handedness, from MikkTSpace
    std::vector<uint8_t> valid;   // 0 empty, 1 conservative, 2 texel centre
    std::vector<float> claim;     // how far the conservative claimer reached

    void reset(uint32_t edge) {
        size = edge;
        const size_t count = size_t(edge) * edge;
        position.assign(count, Vec3{});
        normal.assign(count, Vec3{});
        tangent.assign(count, Vec3{});
        sign.assign(count, 1.0f);
        valid.assign(count, 0);
        claim.assign(count, 1e30f);
    }
};

// The closest point on a 2D triangle to `p`, as barycentric weights.
// Returns true when `p` was actually inside the triangle.
//
// **The return value is the whole point and it was missing.** Inside, the
// weights are `p`'s own barycentric. Outside, they are the barycentric of the
// nearest point on the nearest edge -- and those are *also* all non-negative
// and *also* sum to one, because a point on an edge is a legitimate point of
// the triangle. So there is no way to tell the two apart by looking at the
// weights afterwards, and the caller has to be told.
//
// It was not, and the caller inferred "inside" from the weights. Every
// triangle then claimed its whole padded bounding box as an exact hit, first
// come first served, and a texel genuinely inside one triangle kept a
// neighbour's edge value instead. On a flat two-triangle test plane that is
// invisible -- every texel holds the same surface anyway -- and on a real
// model it is faceting that follows the low-poly's triangles, with black
// patches where a wrong position landed inside the mesh.
bool clamped_barycentric(float px, float py,
                         float x0, float y0, float x1, float y1,
                         float x2, float y2, float inv_area,
                         float& w0, float& w1, float& w2) {
    w1 = ((px - x0) * (y2 - y0) - (x2 - x0) * (py - y0)) * inv_area;
    w2 = ((x1 - x0) * (py - y0) - (px - x0) * (y1 - y0)) * inv_area;
    w0 = 1.0f - w1 - w2;
    if (w0 >= 0.0f && w1 >= 0.0f && w2 >= 0.0f) return true;

    const float xs[3] = {x0, x1, x2};
    const float ys[3] = {y0, y1, y2};
    float best = 1e30f;
    float best_w[3] = {1.0f, 0.0f, 0.0f};

    for (int e = 0; e < 3; ++e) {
        const int a = e, b = (e + 1) % 3;
        const float ex = xs[b] - xs[a], ey = ys[b] - ys[a];
        const float len2 = ex * ex + ey * ey;
        float t = len2 > 1e-20f
                      ? ((px - xs[a]) * ex + (py - ys[a]) * ey) / len2
                      : 0.0f;
        t = t < 0.0f ? 0.0f : (t > 1.0f ? 1.0f : t);
        const float cx = xs[a] + ex * t, cy = ys[a] + ey * t;
        const float dx = px - cx, dy = py - cy;
        const float distance = dx * dx + dy * dy;
        if (distance < best) {
            best = distance;
            best_w[0] = best_w[1] = best_w[2] = 0.0f;
            best_w[a] = 1.0f - t;
            best_w[b] = t;
        }
    }
    w0 = best_w[0];
    w1 = best_w[1];
    w2 = best_w[2];
    return false;
}

// Rasterize the low-poly in UV space into the gbuffer.
//
// **Conservative, not centre-only.** A centre-only rule drops any triangle
// thinner than a texel and leaves a ragged edge on every island, and both
// failures look like the map is broken rather than like the rasterizer is.
// So a texel is claimed when the triangle merely *reaches* it -- within half a
// texel diagonal of its centre -- and takes the surface value at the nearest
// point of that triangle.
//
// Two priorities keep that honest. A texel whose centre a triangle actually
// covers is exact and can never be overwritten by a neighbour that only
// reaches it; conservative claims only ever fill what nothing covers.
void rasterize(const Scene& scene, GBuffer& gbuffer) {
    const uint32_t size = scene.header.size;
    gbuffer.reset(size);

    // Half a texel diagonal. A triangle whose nearest point is within this of
    // the centre overlaps the texel square somewhere.
    const float reach = 0.70710678f;
    const float reach2 = reach * reach;

    // How many texel centres more than one triangle covered. Not an error --
    // overlapping UV islands are a legitimate thing to author -- but a number
    // worth seeing, because when a bake disagrees with another baker inside an
    // island this is the first thing to rule out.
    size_t contested = 0;
    size_t degenerates = 0;

    const float* corner = scene.low.data();
    for (uint32_t tri = 0; tri < scene.header.low_tris; ++tri) {
        const float* c0 = corner + (size_t(tri) * 3 + 0) * pr2::kLowCornerFloats;
        const float* c1 = corner + (size_t(tri) * 3 + 1) * pr2::kLowCornerFloats;
        const float* c2 = corner + (size_t(tri) * 3 + 2) * pr2::kLowCornerFloats;

        const float x0 = c0[6] * size, y0 = c0[7] * size;
        const float x1 = c1[6] * size, y1 = c1[7] * size;
        const float x2 = c2[6] * size, y2 = c2[7] * size;

        const float area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0);
        if (std::fabs(area) < 1e-20f) continue;
        const float inv_area = 1.0f / area;

        // A triangle far smaller than a texel has a barycentric basis that is
        // numerically worthless: `inv_area` is enormous and the weights it
        // produces are noise, which can pass the inside test and claim texels
        // with an interpolation that means nothing. Below this it is rasterized
        // through the edge-clamped path only, which never divides by the area.
        const bool degenerate = std::fabs(area) < 1e-4f;
        if (degenerate) ++degenerates;

        // The bbox is grown by one texel, which is what gives the conservative
        // rule somewhere to act.
        int min_x = int(std::floor(std::fmin(x0, std::fmin(x1, x2)) - 1.0f));
        int max_x = int(std::ceil(std::fmax(x0, std::fmax(x1, x2)) + 1.0f));
        int min_y = int(std::floor(std::fmin(y0, std::fmin(y1, y2)) - 1.0f));
        int max_y = int(std::ceil(std::fmax(y0, std::fmax(y1, y2)) + 1.0f));
        if (min_x < 0) min_x = 0;
        if (min_y < 0) min_y = 0;
        if (max_x > int(size)) max_x = int(size);
        if (max_y > int(size)) max_y = int(size);

        for (int y = min_y; y < max_y; ++y) {
            for (int x = min_x; x < max_x; ++x) {
                const size_t texel = size_t(y) * size + size_t(x);
                const float px = float(x) + 0.5f;
                const float py = float(y) + 0.5f;

                float w0, w1, w2;
                bool inside = clamped_barycentric(
                    px, py, x0, y0, x1, y1, x2, y2, inv_area, w0, w1, w2);
                if (degenerate) inside = false;

                uint8_t priority;
                float distance = 0.0f;
                if (inside) {
                    priority = 2;
                } else {
                    const float cx = x0 * w0 + x1 * w1 + x2 * w2;
                    const float cy = y0 * w0 + y1 * w1 + y2 * w2;
                    const float dx = px - cx, dy = py - cy;
                    distance = dx * dx + dy * dy;
                    if (distance > reach2) continue;
                    priority = 1;
                }

                // An exact claim is final: a texel whose centre a triangle
                // covers belongs to that triangle, and a neighbour reaching
                // across cannot take it. Between two conservative claims the
                // nearer triangle wins -- "whichever came first in the mesh"
                // is not a tie-break, it is a coin toss that shows up as a
                // seam.
                if (gbuffer.valid[texel] == 2) {
                    if (inside) ++contested;
                    continue;
                }
                if (gbuffer.valid[texel] == 1) {
                    if (priority == 1 && distance >= gbuffer.claim[texel]) continue;
                }
                gbuffer.claim[texel] = distance;

                gbuffer.position[texel] = {
                    c0[0] * w0 + c1[0] * w1 + c2[0] * w2,
                    c0[1] * w0 + c1[1] * w1 + c2[1] * w2,
                    c0[2] * w0 + c1[2] * w1 + c2[2] * w2,
                };
                gbuffer.normal[texel] = normalize({
                    c0[3] * w0 + c1[3] * w1 + c2[3] * w2,
                    c0[4] * w0 + c1[4] * w1 + c2[4] * w2,
                    c0[5] * w0 + c1[5] * w1 + c2[5] * w2,
                });
                gbuffer.tangent[texel] = normalize({
                    c0[8] * w0 + c1[8] * w1 + c2[8] * w2,
                    c0[9] * w0 + c1[9] * w1 + c2[9] * w2,
                    c0[10] * w0 + c1[10] * w1 + c2[10] * w2,
                });
                // The sign is handedness, not a quantity: interpolating it
                // across a triangle whose corners disagree would produce a
                // basis that is neither. Nearest corner instead.
                const float best = std::fmax(w0, std::fmax(w1, w2));
                gbuffer.sign[texel] =
                    best == w0 ? c0[11] : (best == w1 ? c1[11] : c2[11]);
                gbuffer.valid[texel] = priority;
            }
        }
    }

    size_t covered = 0;
    for (uint8_t flag : gbuffer.valid) {
        if (flag) ++covered;
    }
    std::printf("STATS raster covered=%zu contested=%zu degenerate=%zu/%u\n",
                covered, contested, degenerates, scene.header.low_tris);
    std::fflush(stdout);
}

// --- sampling ----------------------------------------------------------------
//
// Cosine-weighted directions in the hemisphere around `n`, drawn from a
// Hammersley sequence rather than from white noise. Same ray count, visibly
// less grain: white noise clumps, and a clump is exactly what reads as a
// speckle in an AO map.
//
// The sequence is scrambled per texel, because an unscrambled one is the same
// pattern in every texel and the pattern itself becomes the artefact.

inline float radical_inverse(uint32_t bits, uint32_t scramble) {
    bits = (bits << 16u) | (bits >> 16u);
    bits = ((bits & 0x55555555u) << 1u) | ((bits & 0xAAAAAAAAu) >> 1u);
    bits = ((bits & 0x33333333u) << 2u) | ((bits & 0xCCCCCCCCu) >> 2u);
    bits = ((bits & 0x0F0F0F0Fu) << 4u) | ((bits & 0xF0F0F0F0u) >> 4u);
    bits = ((bits & 0x00FF00FFu) << 8u) | ((bits & 0xFF00FF00u) >> 8u);
    return float(bits ^ scramble) * 2.3283064365386963e-10f;
}

inline uint32_t hash(uint32_t x) {
    x ^= x >> 16; x *= 0x7feb352du;
    x ^= x >> 15; x *= 0x846ca68bu;
    x ^= x >> 16;
    return x;
}

// An orthonormal basis around `n` without a branch on a degenerate axis --
// Duff et al. Picking a fixed "up" vector instead breaks exactly when the
// normal is parallel to it, which on a real model is a whole flat face going
// wrong at once.
inline void basis(Vec3 n, Vec3& t, Vec3& b) {
    const float sign = std::copysign(1.0f, n.z);
    const float a = -1.0f / (sign + n.z);
    const float d = n.x * n.y * a;
    t = {1.0f + sign * n.x * n.x * a, sign * d, -sign * n.x};
    b = {d, sign + n.y * n.y * a, -n.y};
}

inline Vec3 cosine_direction(Vec3 n, float u1, float u2) {
    const float r = std::sqrt(u1);
    const float phi = 6.2831853071795864f * u2;
    const float x = r * std::cos(phi);
    const float y = r * std::sin(phi);
    const float z = std::sqrt(std::fmax(0.0f, 1.0f - u1));
    Vec3 t, b;
    basis(n, t, b);
    return t * x + b * y + n * z;
}

// The GPU kernel's `cone_direction`, kept identical on purpose: the two
// backends have to agree to the last bit, and the suite compares them.
inline Vec3 cone_direction(Vec3 n, float u1, float u2, float cos_max,
                           bool uniform) {
    const float phi = 6.2831853071795864f * u2;

    float r, z;
    if (uniform) {
        z = 1.0f - u1 * (1.0f - cos_max);
        r = std::sqrt(std::fmax(0.0f, 1.0f - z * z));
    } else {
        const float sin_max = std::sqrt(std::fmax(0.0f, 1.0f - cos_max * cos_max));
        r = std::sqrt(u1) * sin_max;
        z = std::sqrt(std::fmax(0.0f, 1.0f - r * r));
    }

    Vec3 t, b;
    basis(n, t, b);
    return t * (r * std::cos(phi)) + b * (r * std::sin(phi)) + n * z;
}

inline float attenuate(float t, float reach, uint32_t mode) {
    if (mode == pr2::kAttenuationNone || reach <= 0.0f) return 1.0f;
    const float x = std::fmin(1.0f, std::fmax(0.0f, 1.0f - t / reach));
    if (mode == pr2::kAttenuationLinear) return x;
    return x * x * (3.0f - 2.0f * x);
}

// --- parallel ----------------------------------------------------------------

template <typename Body>
void parallel_rows(uint32_t rows, const Body& body) {
    unsigned count = std::thread::hardware_concurrency();
    if (count == 0) count = 4;
    if (count > rows) count = rows ? rows : 1;

    std::atomic<uint32_t> next{0};
    std::atomic<uint32_t> done{0};
    std::vector<std::thread> workers;
    workers.reserve(count);

    for (unsigned i = 0; i < count; ++i) {
        workers.emplace_back([&] {
            for (;;) {
                const uint32_t row = next.fetch_add(1);
                if (row >= rows) break;
                body(row);
                const uint32_t finished = done.fetch_add(1) + 1;
                // One line per percent. Any more and the pipe becomes the
                // bottleneck on a fast bake.
                if (rows >= 100 && finished % (rows / 100) == 0) {
                    std::printf("PROGRESS %.3f\n", float(finished) / float(rows));
                    std::fflush(stdout);
                }
            }
        });
    }
    for (auto& worker : workers) worker.join();
}

// --- output ------------------------------------------------------------------

bool write_maps(const char* path, uint32_t size,
                const std::vector<std::pair<uint32_t, std::vector<float>>>& maps,
                std::string& error) {
    std::FILE* file = std::fopen(path, "wb");
    if (!file) {
        error = std::string("cannot write ") + path;
        return false;
    }

    pr2::OutputHeader header{};
    std::memcpy(header.magic, pr2::kOutputMagic, 8);
    header.version = pr2::kVersion;
    header.size = size;
    header.count = uint32_t(maps.size());

    bool ok = std::fwrite(&header, sizeof(header), 1, file) == 1;
    for (const auto& entry : maps) {
        if (!ok) break;
        ok = std::fwrite(&entry.first, sizeof(uint32_t), 1, file) == 1 &&
             std::fwrite(entry.second.data(), sizeof(float), entry.second.size(),
                         file) == entry.second.size();
    }
    if (!ok) error = "writing the output failed";
    std::fclose(file);
    return ok;
}

// --- what one run of the tracer produces ------------------------------------

struct Maps {
    std::vector<float> ao, thickness, bent, position, normal, height, curvature;

    void reset(size_t texels, uint32_t want) {
        const size_t floats = texels * 4;
        if (want & pr2::kMapAO) ao.assign(floats, 0.0f);
        if (want & pr2::kMapThickness) thickness.assign(floats, 0.0f);
        if (want & pr2::kMapBentNormal) bent.assign(floats, 0.0f);
        if (want & pr2::kMapPosition) position.assign(floats, 0.0f);
        if (want & pr2::kMapNormal) normal.assign(floats, 0.0f);
        if (want & pr2::kMapHeight) height.assign(floats, 0.0f);
        if (want & pr2::kMapCurvature) curvature.assign(floats, 0.0f);
    }
};

// Where each texel's shading actually happens: the high-poly surface under it,
// or the low-poly itself when there is no high-poly. Filled by `project`.
struct Projection {
    std::vector<Vec3> position;
    std::vector<Vec3> normal;
    std::vector<uint8_t> landed;

    void reset(size_t texels) {
        position.assign(texels, Vec3{});
        normal.assign(texels, Vec3{});
        landed.assign(texels, 0);
    }
};

struct Reach {
    float epsilon = 0.0f;
    float ao = 0.0f;
    float thickness = 0.0f;
};

// The ambient occlusion options that are not a distance. Grouped rather than
// passed one by one: they travel together through both backends, and the
// alternative was four more arguments on functions that already take twelve.
struct AOOptions {
    // Cosine of the cone's half-angle. 0 is the whole hemisphere, which is
    // what every default bake uses and what the old code did unconditionally.
    float cos_max = 0.0f;
    float min_distance = 0.0f;
    uint32_t attenuation = pr2::kAttenuationNone;
    bool uniform = false;
    bool ignore_backface = false;

    uint32_t flags() const {
        return (uniform ? 1u : 0u) | (ignore_backface ? 2u : 0u);
    }

    // True when the fast any-hit path still answers everything asked.
    bool plain() const {
        return attenuation == pr2::kAttenuationNone && min_distance <= 0.0f &&
               !ignore_backface;
    }
};

// The curvature range mapped onto 0..1. Outside it the map clamps.
struct CurvatureRange {
    float low = -1.0f;
    float high = 1.0f;
};

// Interpolated high-poly normal at a hit, and flipped to face the ray that
// found it. A sculpt is not reliably wound outward, so a hit can be the back
// of a face; shading from an inward normal starts the AO underground.
Vec3 hit_normal(const Scene& scene, const std::vector<uint32_t>& indices,
                uint32_t triangle, float u, float v, Vec3 direction) {
    const uint32_t tri = triangle * 3;
    const float w = 1.0f - u - v;
    const float* n = scene.high_nrm.data();
    const uint32_t a = indices[tri + 0], b = indices[tri + 1], c = indices[tri + 2];
    Vec3 normal = normalize({
        n[a * 3 + 0] * w + n[b * 3 + 0] * u + n[c * 3 + 0] * v,
        n[a * 3 + 1] * w + n[b * 3 + 1] * u + n[c * 3 + 1] * v,
        n[a * 3 + 2] * w + n[b * 3 + 2] * u + n[c * 3 + 2] * v,
    });
    if (dot(normal, direction) > 0.0f) normal = normal * -1.0f;
    return normal;
}

// --- projection (CPU) ---------------------------------------------------------
//
// See the `project` kernel for why this exists at all: with a high-poly, the
// low-poly's surface is not where the shading happens, and tracing from it
// produces a plausible image with every cavity displaced.

void project_cpu(const BVH8_CPU& bvh, const Scene& scene,
                 const std::vector<uint32_t>& indices, const GBuffer& gbuffer,
                 float extrusion, float ray_distance, Projection& out) {
    const uint32_t size = gbuffer.size;
    const bool has_high = scene.has_high();

    parallel_rows(size, [&](uint32_t y) {
        for (uint32_t x = 0; x < size; ++x) {
            const size_t texel = size_t(y) * size + x;
            if (!gbuffer.valid[texel]) continue;

            if (!has_high) {
                out.position[texel] = gbuffer.position[texel];
                out.normal[texel] = gbuffer.normal[texel];
                out.landed[texel] = 1;
                continue;
            }

            const Vec3 point = gbuffer.position[texel];
            const Vec3 normal = gbuffer.normal[texel];
            const Vec3 start = point + normal * extrusion;
            const Vec3 direction = flip(normal);

            Ray ray(bvhvec3(start.x, start.y, start.z),
                    bvhvec3(direction.x, direction.y, direction.z), ray_distance);
            bvh.Intersect(ray);

            if (ray.hit.t >= ray_distance) {
                // Nothing under this texel. Recorded as a miss rather than
                // guessed: each map decides what a miss means for itself.
                out.position[texel] = point;
                out.normal[texel] = normal;
                out.landed[texel] = 0;
                continue;
            }

            out.position[texel] = start + direction * ray.hit.t;
            out.normal[texel] =
                hit_normal(scene, indices, ray.hit.prim, ray.hit.u, ray.hit.v,
                           direction);
            out.landed[texel] = 1;
        }
    });
}

// --- surface maps (CPU) -------------------------------------------------------

void surface_cpu(const GBuffer& gbuffer, const Projection& projection,
                 uint32_t want, bool flip_green, Vec3 bbox_lo, Vec3 bbox_span,
                 float height_range, Maps& maps) {
    const uint32_t size = gbuffer.size;

    parallel_rows(size, [&](uint32_t y) {
        for (uint32_t x = 0; x < size; ++x) {
            const size_t texel = size_t(y) * size + x;
            if (!gbuffer.valid[texel]) continue;

            const Vec3 low = gbuffer.position[texel];
            const Vec3 n_low = gbuffer.normal[texel];
            const Vec3 high = projection.position[texel];
            const bool landed = projection.landed[texel] != 0;

            if (want & pr2::kMapPosition) {
                float* pixel = &maps.position[texel * 4];
                pixel[0] = std::fmin(1.0f, std::fmax(0.0f, (high.x - bbox_lo.x) / bbox_span.x));
                pixel[1] = std::fmin(1.0f, std::fmax(0.0f, (high.y - bbox_lo.y) / bbox_span.y));
                pixel[2] = std::fmin(1.0f, std::fmax(0.0f, (high.z - bbox_lo.z) / bbox_span.z));
                pixel[3] = 1.0f;
            }

            if (want & pr2::kMapNormal) {
                // Gram-Schmidt: the interpolated tangent drifts out of
                // perpendicular across a triangle, and MikkTSpace is defined
                // on the orthogonalised one. This is what makes our basis the
                // basis the shader decodes with.
                const Vec3 raw = gbuffer.tangent[texel];
                Vec3 t = raw - n_low * dot(n_low, raw);
                const float length = std::sqrt(dot(t, t));
                if (length > 1e-8f) t = t * (1.0f / length);
                const Vec3 b = cross(n_low, t) * gbuffer.sign[texel];

                const Vec3 n = landed ? projection.normal[texel] : n_low;
                Vec3 ts{dot(n, t), dot(n, b), dot(n, n_low)};
                ts = normalize(ts);
                if (flip_green) ts.y = -ts.y;

                float* pixel = &maps.normal[texel * 4];
                pixel[0] = ts.x * 0.5f + 0.5f;
                pixel[1] = ts.y * 0.5f + 0.5f;
                pixel[2] = ts.z * 0.5f + 0.5f;
                pixel[3] = 1.0f;
            }

            if (want & pr2::kMapHeight) {
                const float h = landed ? dot(high - low, n_low) : 0.0f;
                float value = 0.5f + h / (2.0f * height_range);
                value = std::fmin(1.0f, std::fmax(0.0f, value));
                float* pixel = &maps.height[texel * 4];
                pixel[0] = pixel[1] = pixel[2] = value;
                pixel[3] = 1.0f;
            }
        }
    });
}

// --- curvature (CPU) ----------------------------------------------------------
//
// See the `curvature` kernel for what this measures and why it is per texel
// rather than per vertex. In short: a vertex-based curvature can only be as
// detailed as the mesh is dense, and this one is bounded by the map instead.

void curvature_cpu(const GBuffer& gbuffer, const Projection& projection,
                   float scale, const CurvatureRange& range, Maps& maps) {
    const uint32_t size = gbuffer.size;

    parallel_rows(size, [&](uint32_t y) {
        for (uint32_t x = 0; x < size; ++x) {
            const size_t texel = size_t(y) * size + x;
            if (!gbuffer.valid[texel]) continue;

            float* pixel = &maps.curvature[texel * 4];
            pixel[0] = pixel[1] = pixel[2] = 0.5f;
            pixel[3] = 1.0f;

            if (x == 0 || y == 0 || x + 1 >= size || y + 1 >= size) continue;

            const size_t left = texel - 1, right = texel + 1;
            const size_t down = texel - size, up = texel + size;
            if (!gbuffer.valid[left] || !gbuffer.valid[right] ||
                !gbuffer.valid[down] || !gbuffer.valid[up]) {
                continue;
            }

            const Vec3 du = projection.position[right] - projection.position[left];
            const Vec3 dv = projection.position[up] - projection.position[down];
            const float lu = std::sqrt(dot(du, du));
            const float lv = std::sqrt(dot(dv, dv));
            const float step = std::fmin(lu, lv);

            // A UV seam is adjacent in the map and metres away on the model.
            if (lu < 1e-9f || lv < 1e-9f || std::fmax(lu, lv) > step * 8.0f) {
                continue;
            }

            const Vec3 dn_u = projection.normal[right] - projection.normal[left];
            const Vec3 dn_v = projection.normal[up] - projection.normal[down];
            const float ku = dot(dn_u, du * (1.0f / lu)) / lu;
            const float kv = dot(dn_v, dv * (1.0f / lv)) / lv;

            const float mean = (ku + kv) * 0.5f * scale;
            const float span = range.high - range.low;
            const float encoded =
                span > 1e-6f
                    ? std::fmin(1.0f, std::fmax(0.0f, (mean - range.low) / span))
                    : 0.5f;
            pixel[0] = pixel[1] = pixel[2] = encoded;
        }
    });
}

// --- CPU backend -------------------------------------------------------------
//
// Kept, and kept correct, rather than deleted once the GPU path worked. It is
// what runs on a machine with no usable OpenCL, and -- more useful day to day
// -- it is the only thing the GPU path can be checked against. A GPU result
// with nothing to compare it to is not verifiable.

// The CPU twin of the kernel's `ao_occlusion`. Same rules, same order, same
// four continuations -- see the comment there for why the backface test is a
// re-trace rather than a filter inside traversal.
float ao_occlusion_cpu(const BVH8_CPU& bvh, const std::vector<uint32_t>& indices,
                       const bvhvec4* vertices, const Vec3& origin,
                       const Vec3& d, float reach, const AOOptions& ao) {
    if (ao.plain()) {
        Ray ray(bvhvec3(origin.x, origin.y, origin.z),
                bvhvec3(d.x, d.y, d.z), reach);
        return bvh.IsOccluded(ray) ? 1.0f : 0.0f;
    }

    float travelled = 0.0f;
    Vec3 from = origin;

    for (int step = 0; step < 4; ++step) {
        const float remaining = reach - travelled;
        if (remaining <= 0.0f) return 0.0f;

        Ray ray(bvhvec3(from.x, from.y, from.z), bvhvec3(d.x, d.y, d.z),
                remaining);
        bvh.Intersect(ray);
        if (ray.hit.t >= remaining) return 0.0f;

        const float t = travelled + ray.hit.t;
        if (t < ao.min_distance) {
            travelled = t + 1e-4f;
            from = origin + d * travelled;
            continue;
        }

        if (ao.ignore_backface) {
            const uint32_t tri = ray.hit.prim * 3;
            const bvhvec4 a = vertices[indices[tri + 0]];
            const bvhvec4 b = vertices[indices[tri + 1]];
            const bvhvec4 c = vertices[indices[tri + 2]];
            const Vec3 e1{b.x - a.x, b.y - a.y, b.z - a.z};
            const Vec3 e2{c.x - a.x, c.y - a.y, c.z - a.z};
            const Vec3 face = cross(e1, e2);
            if (dot(face, d) > 0.0f) {
                travelled = t + 1e-4f;
                from = origin + d * travelled;
                continue;
            }
        }

        return attenuate(t, reach, ao.attenuation);
    }
    return 0.0f;
}

void trace_cpu(const BVH8_CPU& bvh, const GBuffer& gbuffer,
               const Projection& projection, uint32_t samples, uint32_t want,
               const Reach& reach, const AOOptions& ao,
               const std::vector<uint32_t>& indices, const bvhvec4* vertices,
               Maps& maps) {
    const uint32_t size = gbuffer.size;

    parallel_rows(size, [&](uint32_t y) {
        for (uint32_t x = 0; x < size; ++x) {
            const size_t texel = size_t(y) * size + x;
            if (!gbuffer.valid[texel]) continue;

            // The projected point, not the low-poly one -- see project_cpu.
            const Vec3 point = projection.position[texel];
            const Vec3 normal = projection.normal[texel];
            const uint32_t scramble = hash(uint32_t(texel) * 0x9e3779b9u);

            const bvhvec3 origin(point.x + normal.x * reach.epsilon,
                                 point.y + normal.y * reach.epsilon,
                                 point.z + normal.z * reach.epsilon);
            const bvhvec3 inner(point.x - normal.x * reach.epsilon,
                                point.y - normal.y * reach.epsilon,
                                point.z - normal.z * reach.epsilon);
            const Vec3 flipped = flip(normal);

            float open = 0.0f;
            uint32_t buried = 0;
            Vec3 bent{0, 0, 0};

            for (uint32_t s = 0; s < samples; ++s) {
                const float u1 = (float(s) + 0.5f) / float(samples);
                const float u2 = radical_inverse(s, scramble);

                if (want & (pr2::kMapAO | pr2::kMapBentNormal)) {
                    const Vec3 d = safe_direction(
                        cone_direction(normal, u1, u2, ao.cos_max, ao.uniform));
                    const Vec3 start{origin.x, origin.y, origin.z};
                    const float shadow = ao_occlusion_cpu(
                        bvh, indices, vertices, start, d, reach.ao, ao);
                    open += 1.0f - shadow;
                    bent = bent + d * (1.0f - shadow);
                }
                if (want & pr2::kMapThickness) {
                    const Vec3 d = safe_direction(cosine_direction(flipped, u1, u2));
                    Ray probe(inner, bvhvec3(d.x, d.y, d.z), reach.thickness);
                    if (bvh.IsOccluded(probe)) ++buried;
                }
            }

            const float inv = 1.0f / float(samples);

            if (want & pr2::kMapAO) {
                const float visibility = open * inv;
                float* pixel = &maps.ao[texel * 4];
                pixel[0] = pixel[1] = pixel[2] = visibility;
                pixel[3] = 1.0f;
            }
            if (want & pr2::kMapThickness) {
                const float depth = 1.0f - float(buried) * inv;
                float* pixel = &maps.thickness[texel * 4];
                pixel[0] = pixel[1] = pixel[2] = depth;
                pixel[3] = 1.0f;
            }
            if (want & pr2::kMapBentNormal) {
                const float length = std::sqrt(dot(bent, bent));
                const Vec3 direction = length > 1e-6f ? bent * (1.0f / length) : normal;
                float* pixel = &maps.bent[texel * 4];
                pixel[0] = direction.x * 0.5f + 0.5f;
                pixel[1] = direction.y * 0.5f + 0.5f;
                pixel[2] = direction.z * 0.5f + 0.5f;
                pixel[3] = 1.0f;
            }
        }
    });
}

// --- GPU backend -------------------------------------------------------------
//
// Three kernels over one upload: project, then trace, then surface. The
// uploads are the point -- the BVH and the mesh go to the device once and
// every map is read off them, where Cycles pays a scene sync per map.

bool trace_gpu(gpu::Device& device, const BVH_GPU& bvh, const Scene& scene,
               const std::vector<bvhvec4>& vertices,
               const std::vector<uint32_t>& indices, const GBuffer& gbuffer,
               uint32_t samples, uint32_t want, bool flip_green,
               const Reach& reach, const AOOptions& ao,
               const CurvatureRange& curvature_range, Vec3 bbox_lo,
               Vec3 bbox_span, float extrusion, float ray_distance,
               float height_range, float curvature_scale,
               const BVH_GPU* occlusion,
               const std::vector<bvhvec4>& occ_vertices,
               const std::vector<uint32_t>& occ_indices, Maps& maps,
               std::string& error) {
    const size_t texels = size_t(gbuffer.size) * gbuffer.size;
    const size_t out_bytes = texels * 4 * sizeof(float);

    // float3 is 12 bytes on the host and 16 on the device. Repacking rather
    // than memcpy-ing the host's arrays is not fussiness: the silent version
    // of this mistake is a map that is sheared by a third.
    std::vector<float> positions(texels * 4, 0.0f);
    std::vector<float> normals(texels * 4, 0.0f);
    std::vector<float> tangents(texels * 4, 0.0f);
    for (size_t i = 0; i < texels; ++i) {
        positions[i * 4 + 0] = gbuffer.position[i].x;
        positions[i * 4 + 1] = gbuffer.position[i].y;
        positions[i * 4 + 2] = gbuffer.position[i].z;
        positions[i * 4 + 3] = gbuffer.valid[i] ? 1.0f : 0.0f;
        normals[i * 4 + 0] = gbuffer.normal[i].x;
        normals[i * 4 + 1] = gbuffer.normal[i].y;
        normals[i * 4 + 2] = gbuffer.normal[i].z;
        tangents[i * 4 + 0] = gbuffer.tangent[i].x;
        tangents[i * 4 + 1] = gbuffer.tangent[i].y;
        tangents[i * 4 + 2] = gbuffer.tangent[i].z;
        tangents[i * 4 + 3] = gbuffer.sign[i];
    }

    // High-poly normals, padded to float4 for the same reason.
    std::vector<float> high_normals;
    if (scene.has_high()) {
        high_normals.assign(size_t(scene.header.high_verts) * 4, 0.0f);
        for (size_t v = 0; v < scene.header.high_verts; ++v) {
            high_normals[v * 4 + 0] = scene.high_nrm[v * 3 + 0];
            high_normals[v * 4 + 1] = scene.high_nrm[v * 3 + 1];
            high_normals[v * 4 + 2] = scene.high_nrm[v * 3 + 2];
        }
    } else {
        high_normals.assign(4, 0.0f);
    }

    const int slot_nodes =
        device.upload(bvh.bvhNode, size_t(bvh.usedNodes) * sizeof(BVH_GPU::BVHNode),
                      error);
    const int slot_order = device.upload(
        bvh.bvh.primIdx, size_t(bvh.bvh.idxCount) * sizeof(uint32_t), error);
    const int slot_tris =
        device.upload(indices.data(), indices.size() * sizeof(uint32_t), error);
    const int slot_verts =
        device.upload(vertices.data(), vertices.size() * sizeof(bvhvec4), error);
    const int slot_hnrm = device.upload(
        high_normals.data(), high_normals.size() * sizeof(float), error);
    const int slot_pos =
        device.upload(positions.data(), positions.size() * sizeof(float), error);
    const int slot_nrm =
        device.upload(normals.data(), normals.size() * sizeof(float), error);
    const int slot_tan =
        device.upload(tangents.data(), tangents.size() * sizeof(float), error);

    const int slot_proj_pos = device.allocate(out_bytes, error);
    const int slot_proj_nrm = device.allocate(out_bytes, error);

    // An unwanted output still needs a bound buffer -- OpenCL has no null
    // argument -- so it gets a one-texel stub the kernel never writes.
    auto out_slot = [&](uint32_t bit) {
        return device.allocate((want & bit) ? out_bytes : 16, error);
    };
    const int slot_ao = out_slot(pr2::kMapAO);
    const int slot_thick = out_slot(pr2::kMapThickness);
    const int slot_bent = out_slot(pr2::kMapBentNormal);
    const int slot_mpos = out_slot(pr2::kMapPosition);
    const int slot_mnrm = out_slot(pr2::kMapNormal);
    const int slot_mh = out_slot(pr2::kMapHeight);
    const int slot_mc = out_slot(pr2::kMapCurvature);

    const int slots[] = {slot_nodes, slot_order, slot_tris, slot_verts,
                         slot_hnrm, slot_pos, slot_nrm, slot_tan,
                         slot_proj_pos, slot_proj_nrm, slot_ao, slot_thick,
                         slot_bent, slot_mpos, slot_mnrm, slot_mh, slot_mc};
    for (int slot : slots) {
        if (slot < 0) return false;
    }

    // Bands, not one launch per kernel. A kernel that runs for seconds is
    // killed by the display driver's watchdog on a GPU that is also driving a
    // monitor, and that failure reads as a crash rather than a timeout. Bands
    // also give the host something to report progress from.
    const size_t band = 65536;

    // --- 1. project ---
    if (!device.prepare("project", error)) return false;
    device.arg_buffer(0, slot_nodes);
    device.arg_buffer(1, slot_order);
    device.arg_buffer(2, slot_tris);
    device.arg_buffer(3, slot_verts);
    device.arg_buffer(4, slot_hnrm);
    device.arg_buffer(5, slot_pos);
    device.arg_buffer(6, slot_nrm);
    device.arg_buffer(7, slot_proj_pos);
    device.arg_buffer(8, slot_proj_nrm);
    device.arg_uint(10, uint32_t(texels));
    device.arg_uint(11, scene.has_high() ? 1u : 0u);
    device.arg_float(12, extrusion);
    device.arg_float(13, ray_distance);
    for (size_t first = 0; first < texels; first += band) {
        device.arg_uint(9, uint32_t(first));
        if (!device.launch(std::min(band, texels - first), error)) return false;
    }

    // Projection hit rate, reported rather than assumed. A cage bake whose
    // rays mostly miss still produces every map -- flat normals, 0.5 height,
    // occlusion measured from the wrong surface -- and reports nothing. This
    // line is what turns that from a mystery into a number.
    {
        std::vector<float> landed(texels * 4);
        if (device.download(slot_proj_pos, landed.data(), out_bytes, error)) {
            size_t covered = 0, hits = 0;
            for (size_t i = 0; i < texels; ++i) {
                if (positions[i * 4 + 3] > 0.5f) {
                    ++covered;
                    if (landed[i * 4 + 3] > 0.5f) ++hits;
                }
            }
            std::printf("STATS projected %zu/%zu\n", hits, covered);
            std::fflush(stdout);
        }
    }

    // --- 2. trace ---
    const uint32_t traced = want & (pr2::kMapAO | pr2::kMapThickness |
                                    pr2::kMapBentNormal);
    if (traced) {
        uint32_t kernel_want = 0;
        if (want & pr2::kMapAO) kernel_want |= 1u;
        if (want & pr2::kMapThickness) kernel_want |= 2u;
        if (want & pr2::kMapBentNormal) kernel_want |= 4u;

        // One launch per mesh the rays have to see. Thickness is a fact about
        // this model alone; AO and the bent normal, with occluders, are traced
        // against this model *and* its neighbours -- a second BVH, uploaded
        // only when there is one, so a bake without neighbours pays nothing.
        struct Pass {
            uint32_t want;
            int nodes, order, tris, verts;
        };
        std::vector<Pass> passes;
        if (occlusion != nullptr && (kernel_want & 5u)) {
            if (kernel_want & 2u) {
                passes.push_back({2u, slot_nodes, slot_order, slot_tris, slot_verts});
            }
            const int occ_nodes = device.upload(
                occlusion->bvhNode,
                size_t(occlusion->usedNodes) * sizeof(BVH_GPU::BVHNode), error);
            const int occ_order = device.upload(
                occlusion->bvh.primIdx,
                size_t(occlusion->bvh.idxCount) * sizeof(uint32_t), error);
            const int occ_tris = device.upload(
                occ_indices.data(), occ_indices.size() * sizeof(uint32_t), error);
            const int occ_verts = device.upload(
                occ_vertices.data(), occ_vertices.size() * sizeof(bvhvec4), error);
            if (occ_nodes < 0 || occ_order < 0 || occ_tris < 0 || occ_verts < 0) {
                return false;
            }
            passes.push_back({kernel_want & 5u, occ_nodes, occ_order, occ_tris,
                              occ_verts});
        } else {
            passes.push_back({kernel_want, slot_nodes, slot_order, slot_tris,
                              slot_verts});
        }

        for (size_t p = 0; p < passes.size(); ++p) {
            const Pass& pass = passes[p];
            if (!device.prepare("trace", error)) return false;
            device.arg_buffer(0, pass.nodes);
            device.arg_buffer(1, pass.order);
            device.arg_buffer(2, pass.tris);
            device.arg_buffer(3, pass.verts);
            device.arg_buffer(4, slot_pos);
            device.arg_buffer(5, slot_proj_pos);
            device.arg_buffer(6, slot_proj_nrm);
            device.arg_buffer(7, slot_ao);
            device.arg_buffer(8, slot_thick);
            device.arg_buffer(9, slot_bent);
            device.arg_uint(11, uint32_t(texels));
            device.arg_uint(12, samples);
            device.arg_uint(13, pass.want);
            device.arg_float(14, reach.epsilon);
            device.arg_float(15, reach.ao);
            device.arg_float(16, reach.thickness);
            device.arg_float(17, ao.cos_max);
            device.arg_float(18, ao.min_distance);
            device.arg_uint(19, ao.attenuation);
            device.arg_uint(20, ao.flags());
            for (size_t first = 0; first < texels; first += band) {
                device.arg_uint(10, uint32_t(first));
                if (!device.launch(std::min(band, texels - first), error)) return false;
                const float done = (float(p) + float(first + band) / float(texels)) /
                                   float(passes.size());
                std::printf("PROGRESS %.3f\n", std::fmin(done, 1.0f) * 0.95f);
                std::fflush(stdout);
            }
        }
    }

    // --- 3. surface ---
    const uint32_t surfaced =
        want & (pr2::kMapPosition | pr2::kMapNormal | pr2::kMapHeight);
    if (surfaced) {
        uint32_t kernel_want = 0;
        if (want & pr2::kMapPosition) kernel_want |= 1u;
        if (want & pr2::kMapNormal) kernel_want |= 2u;
        if (want & pr2::kMapHeight) kernel_want |= 4u;

        if (!device.prepare("surface", error)) return false;
        device.arg_buffer(0, slot_pos);
        device.arg_buffer(1, slot_nrm);
        device.arg_buffer(2, slot_tan);
        device.arg_buffer(3, slot_proj_pos);
        device.arg_buffer(4, slot_proj_nrm);
        device.arg_buffer(5, slot_mpos);
        device.arg_buffer(6, slot_mnrm);
        device.arg_buffer(7, slot_mh);
        device.arg_uint(9, uint32_t(texels));
        device.arg_uint(10, kernel_want);
        device.arg_uint(11, flip_green ? 1u : 0u);
        device.arg_float3(12, bbox_lo.x, bbox_lo.y, bbox_lo.z);
        device.arg_float3(13, bbox_span.x, bbox_span.y, bbox_span.z);
        device.arg_float(14, height_range);
        for (size_t first = 0; first < texels; first += band) {
            device.arg_uint(8, uint32_t(first));
            if (!device.launch(std::min(band, texels - first), error)) return false;
        }
    }

    // --- 4. curvature ---
    if (want & pr2::kMapCurvature) {
        if (!device.prepare("curvature", error)) return false;
        device.arg_buffer(0, slot_pos);
        device.arg_buffer(1, slot_proj_pos);
        device.arg_buffer(2, slot_proj_nrm);
        device.arg_buffer(3, slot_mc);
        device.arg_uint(5, uint32_t(texels));
        device.arg_uint(6, gbuffer.size);
        device.arg_float(7, curvature_scale);
        device.arg_float(8, curvature_range.low);
        device.arg_float(9, curvature_range.high);
        for (size_t first = 0; first < texels; first += band) {
            device.arg_uint(4, uint32_t(first));
            if (!device.launch(std::min(band, texels - first), error)) return false;
        }
    }

    // --- 5. read back ---
    struct Wanted {
        uint32_t bit;
        int slot;
        std::vector<float>* into;
    };
    const Wanted outputs[] = {
        {pr2::kMapAO, slot_ao, &maps.ao},
        {pr2::kMapThickness, slot_thick, &maps.thickness},
        {pr2::kMapBentNormal, slot_bent, &maps.bent},
        {pr2::kMapPosition, slot_mpos, &maps.position},
        {pr2::kMapNormal, slot_mnrm, &maps.normal},
        {pr2::kMapHeight, slot_mh, &maps.height},
        {pr2::kMapCurvature, slot_mc, &maps.curvature},
    };
    for (const Wanted& entry : outputs) {
        if (!(want & entry.bit)) continue;
        if (!device.download(entry.slot, entry.into->data(), out_bytes, error)) {
            return false;
        }
    }
    return true;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 3) {
        std::fprintf(stderr,
                     "usage: paintr2_bake input.pr2b output.pr2m [--cpu]\n");
        return 2;
    }

    // `--cpu` exists for one reason: comparing the two backends. A GPU result
    // that cannot be checked against anything is not a verified result.
    bool force_cpu = false;
    for (int i = 3; i < argc; ++i) {
        if (std::strcmp(argv[i], "--cpu") == 0) force_cpu = true;
    }

    std::string error;
    Scene scene;
    if (!load(argv[1], scene, error)) {
        std::printf("ERROR %s\n", error.c_str());
        return 1;
    }

    const uint32_t size = scene.header.size;
    const uint32_t samples = scene.header.samples ? scene.header.samples : 64;
    const uint32_t want = scene.header.maps;

    // --- the mesh the rays are traced against ---
    //
    // The high-poly when there is one, the low-poly when there is not. Same
    // rule as Blender's selected-to-active and as meshmaps.py: the low-poly
    // only ever owns the UVs.
    //
    // Both backends read the same indexed form, and that is a memory decision.
    // Expanding to a triangle soup costs three vertices per triangle: at
    // sixteen million triangles, 774 MB for a mesh whose vertices are 128 MB.
    // The self-bake case gets a trivial 0,1,2,... index array so there is one
    // code path rather than two.
    std::vector<bvhvec4> vertices;
    std::vector<uint32_t> indices;

    if (scene.has_high()) {
        vertices.resize(scene.header.high_verts);
        for (size_t v = 0; v < vertices.size(); ++v) {
            vertices[v] = bvhvec4(scene.high_pos[v * 3 + 0],
                                  scene.high_pos[v * 3 + 1],
                                  scene.high_pos[v * 3 + 2], 0.0f);
        }
        std::vector<float>().swap(scene.high_pos);
        indices = std::move(scene.high_idx);
    } else {
        const size_t corners = size_t(scene.header.low_tris) * 3;
        vertices.resize(corners);
        indices.resize(corners);
        for (size_t i = 0; i < corners; ++i) {
            const float* c = scene.low.data() + i * pr2::kLowCornerFloats;
            vertices[i] = bvhvec4(c[0], c[1], c[2], 0.0f);
            indices[i] = uint32_t(i);
        }
    }

    const uint32_t triangles = uint32_t(indices.size() / 3);

    // --- the mesh an AO ray is stopped by ---
    //
    // This model's own traced mesh plus its neighbours, as one indexed mesh
    // with the neighbours' indices shifted past this model's vertices. Built
    // only when there are neighbours *and* a map that reads them; every other
    // map keeps tracing the mesh above and nothing else.
    const bool occluded_by_others =
        scene.has_occluders() && (want & (pr2::kMapAO | pr2::kMapBentNormal));
    std::vector<bvhvec4> occ_vertices;
    std::vector<uint32_t> occ_indices;
    if (occluded_by_others) {
        const uint32_t base = uint32_t(vertices.size());
        occ_vertices = vertices;
        occ_vertices.reserve(vertices.size() + scene.header.occluder_verts);
        for (size_t v = 0; v < scene.header.occluder_verts; ++v) {
            occ_vertices.emplace_back(scene.occ_pos[v * 3 + 0],
                                      scene.occ_pos[v * 3 + 1],
                                      scene.occ_pos[v * 3 + 2], 0.0f);
        }
        occ_indices = indices;
        occ_indices.reserve(indices.size() + scene.occ_idx.size());
        for (uint32_t index : scene.occ_idx) occ_indices.push_back(base + index);
    }
    std::vector<float>().swap(scene.occ_pos);
    std::vector<uint32_t>().swap(scene.occ_idx);
    const uint32_t occ_triangles = uint32_t(occ_indices.size() / 3);
    if (occluded_by_others) {
        std::printf("STATS occluders %u\n", scene.header.occluder_tris);
        std::fflush(stdout);
    }

    GBuffer gbuffer;
    rasterize(scene, gbuffer);
    // Read into the gbuffer and not needed again. Freeing it here is what
    // keeps the peak down at the moment the BVH and the maps coexist.
    std::vector<float>().swap(scene.low);

    Maps maps;
    maps.reset(size_t(size) * size, want);

    // --- pick a backend ---
    gpu::Device device;
    bool on_gpu = false;
    if (!force_cpu) {
        std::string reason;
        // No -cl-fast-relaxed-math, for the same reason the host is not built
        // with -ffast-math: the traversal's slab test needs IEEE infinities,
        // and relaxed math makes every axis-aligned ray miss. See build.sh.
        if (device.open(kBakeKernelSource, "-cl-mad-enable", reason)) {
            on_gpu = true;
            std::printf("DEVICE gpu %s\n", device.name().c_str());
        } else {
            // Not an error. A machine without a usable OpenCL driver still
            // bakes; it just bakes on its processor.
            std::printf("DEVICE cpu (%s)\n", reason.c_str());
        }
    } else {
        std::printf("DEVICE cpu (forced)\n");
    }
    std::fflush(stdout);

    Reach reach;
    Vec3 bbox_lo{}, bbox_span{1, 1, 1};
    const bool flip_green = (scene.header.flags & pr2::kFlagFlipGreen) != 0;
    const float height_range = scene.header.params[pr2::kParamHeightRange] > 0.0f
                                   ? scene.header.params[pr2::kParamHeightRange]
                                   : 0.05f;

    // Shared by both backends: the two distances that cannot be absolute, and
    // the box the Position map normalises into. The box is the *traced* mesh's
    // -- the high-poly's when there is one, since those are the points being
    // normalised, and the low-poly's box would clip whatever sticks out.
    const auto derive = [&](bvhvec3 lo, bvhvec3 hi) {
        const float diagonal = std::sqrt((hi.x - lo.x) * (hi.x - lo.x) +
                                         (hi.y - lo.y) * (hi.y - lo.y) +
                                         (hi.z - lo.z) * (hi.z - lo.z));
        reach.epsilon = std::fmax(diagonal * 1e-4f, 1e-6f);
        reach.ao = scene.header.params[pr2::kParamRayDistance] > 0.0f
                       ? scene.header.params[pr2::kParamRayDistance]
                       : diagonal;
        reach.thickness = scene.header.params[pr2::kParamThicknessReach] > 0.0f
                              ? scene.header.params[pr2::kParamThicknessReach]
                              : diagonal * 0.2f;
        bbox_lo = {lo.x, lo.y, lo.z};
        bbox_span = {std::fmax(hi.x - lo.x, 1e-6f), std::fmax(hi.y - lo.y, 1e-6f),
                     std::fmax(hi.z - lo.z, 1e-6f)};
        return diagonal;
    };

    // The AO options, read once and handed to whichever backend runs. A
    // spread of pi/2 -- the whole hemisphere -- gives cos_max = 0, which is
    // exactly what the code did before any of this existed.
    AOOptions ao;
    {
        const float spread = scene.header.params[pr2::kParamAOSpread];
        const float half = spread > 0.0f ? std::fmin(spread, 1.57079633f)
                                         : 1.57079633f;
        ao.cos_max = std::cos(half);
        if (ao.cos_max < 1e-6f) ao.cos_max = 0.0f;
        ao.min_distance = std::fmax(0.0f,
                                    scene.header.params[pr2::kParamAOMinDistance]);
        ao.attenuation =
            uint32_t(scene.header.params[pr2::kParamAOAttenuation] + 0.5f);
        ao.uniform = (scene.header.flags & pr2::kFlagAOUniform) != 0;
        ao.ignore_backface =
            (scene.header.flags & pr2::kFlagAOIgnoreBackface) != 0;
    }

    CurvatureRange curvature_range;
    {
        const float low = scene.header.params[pr2::kParamCurvatureLow];
        const float high = scene.header.params[pr2::kParamCurvatureHigh];
        if (high - low > 1e-6f) {
            curvature_range.low = low;
            curvature_range.high = high;
        }
    }

    const float extrusion = scene.header.params[pr2::kParamExtrusion];
    // Curvature is "how fast the normal turns per metre", so its raw value
    // scales with the model and has to be brought into 0..1 by something tied
    // to the model's size. The bounding box diagonal is that something.
    //
    // The 0.02 was measured rather than picked: on a dome of radius 0.5 the
    // true curvature is about 4 per metre, and a factor of 0.25 pushed the map
    // to a flat saturated 1.0 at the crown and 0.0 in the groove -- detail
    // clipped away at both ends. 0.02 lands the same dome near 0.61, which
    // leaves headroom for the sharper creases a real sculpt has.
    float curvature_scale = scene.header.params[pr2::kParamCurvatureScale];
    float ray_distance = scene.header.params[pr2::kParamRayDistance];

    if (on_gpu) {
        BVH_GPU bvh;
        bvh.Build(vertices.data(), indices.data(), triangles);
        const float diagonal = derive(bvh.aabbMin, bvh.aabbMax);
        if (curvature_scale <= 0.0f) curvature_scale = diagonal * 0.02f;
        // Zero means "no limit" on the Blender side. A ray that starts at the
        // extruded position has to cross the extrusion again before it reaches
        // anything, so the fallback has to clear it twice over.
        if (ray_distance <= 0.0f) {
            ray_distance = std::fmax(extrusion * 2.0f, diagonal * 0.05f);
        }

        BVH_GPU occ_bvh;
        if (occluded_by_others) {
            occ_bvh.Build(occ_vertices.data(), occ_indices.data(), occ_triangles);
        }

        if (!trace_gpu(device, bvh, scene, vertices, indices, gbuffer, samples,
                       want, flip_green, reach, ao, curvature_range, bbox_lo,
                       bbox_span, extrusion, ray_distance, height_range,
                       curvature_scale,
                       occluded_by_others ? &occ_bvh : nullptr, occ_vertices,
                       occ_indices, maps, error)) {
            std::printf("ERROR %s\n", error.c_str());
            return 1;
        }
    } else {
        // BVH8_CPU, not the plain BVH: the eight-wide AVX2 layout answers an
        // occlusion query two to three times faster, and that query is the
        // whole inner loop. It costs more memory -- 5.6 GB against 2.3 GB at
        // sixteen million triangles -- which is a trade worth revisiting if a
        // mesh ever does not fit.
        BVH8_CPU bvh;
        bvh.Build(vertices.data(), indices.data(), triangles);
        const float diagonal = derive(bvh.aabbMin, bvh.aabbMax);
        if (curvature_scale <= 0.0f) curvature_scale = diagonal * 0.02f;
        if (ray_distance <= 0.0f) {
            ray_distance = std::fmax(extrusion * 2.0f, diagonal * 0.05f);
        }

        Projection projection;
        projection.reset(size_t(size) * size);
        project_cpu(bvh, scene, indices, gbuffer, extrusion, ray_distance,
                    projection);
        {
            size_t covered = 0, hits = 0;
            for (size_t i = 0; i < projection.landed.size(); ++i) {
                if (gbuffer.valid[i]) {
                    ++covered;
                    if (projection.landed[i]) ++hits;
                }
            }
            std::printf("STATS projected %zu/%zu\n", hits, covered);
            std::fflush(stdout);
        }
        if (occluded_by_others) {
            // Thickness against this model alone, AO and bent normal against
            // it and its neighbours -- see `occluded_by_others`.
            const uint32_t own = want & pr2::kMapThickness;
            if (own) {
                trace_cpu(bvh, gbuffer, projection, samples, own, reach, ao,
                          indices, vertices.data(), maps);
            }
            BVH8_CPU occ_bvh;
            occ_bvh.Build(occ_vertices.data(), occ_indices.data(), occ_triangles);
            trace_cpu(occ_bvh, gbuffer, projection, samples,
                      want & (pr2::kMapAO | pr2::kMapBentNormal), reach, ao,
                      occ_indices, occ_vertices.data(), maps);
        } else {
            trace_cpu(bvh, gbuffer, projection, samples, want, reach, ao,
                      indices, vertices.data(), maps);
        }
        surface_cpu(gbuffer, projection, want, flip_green, bbox_lo, bbox_span,
                    height_range, maps);
        if (want & pr2::kMapCurvature) {
            curvature_cpu(gbuffer, projection, curvature_scale,
                          curvature_range, maps);
        }
    }

    std::vector<std::pair<uint32_t, std::vector<float>>> written;
    const auto keep = [&](uint32_t bit, std::vector<float>& data) {
        if (want & bit) written.emplace_back(bit, std::move(data));
    };
    keep(pr2::kMapAO, maps.ao);
    keep(pr2::kMapThickness, maps.thickness);
    keep(pr2::kMapBentNormal, maps.bent);
    keep(pr2::kMapPosition, maps.position);
    keep(pr2::kMapNormal, maps.normal);
    keep(pr2::kMapHeight, maps.height);
    keep(pr2::kMapCurvature, maps.curvature);

    if (written.empty()) {
        std::printf("ERROR no map requested that this build can produce\n");
        return 1;
    }

    if (!write_maps(argv[2], size, written, error)) {
        std::printf("ERROR %s\n", error.c_str());
        return 1;
    }

    std::printf("DONE %u\n", uint32_t(written.size()));
    return 0;
}
