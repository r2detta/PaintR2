// SPDX-License-Identifier: GPL-3.0-or-later
//
// The tracing half of paintr2_bake, on the GPU.
//
// One work item per texel. It reads where the surface is from the gbuffer the
// host rasterized, fires the rays, and writes the maps. Nothing here knows
// about UVs or triangles-in-texture-space -- that was decided on the host, and
// this file only ever sees points and normals.
//
// The BVH is Aila & Laine's layout, which is what tinybvh's `BVH_GPU` builds:
// a node carries the bounds of *both children* rather than its own, so one
// node fetch tests two boxes and the traversal never has to load a child just
// to find out it missed. The struct below mirrors `BVH_GPU::BVHNode` field for
// field; the two have to agree exactly, and the host asserts the size.
//
// **Triangles are indexed here, unlike in tinybvh's own kernels.** Theirs read
// `verts + 3 * triIdx`, which needs a triangle soup: at sixteen million
// triangles that is 774 MB of vertices on the device for a mesh whose vertices
// are 128 MB. The indirection through `tris` costs one more fetch in the leaf
// and buys back most of that.

struct BVHNode {
    float4 lmin;  // xyz = left child min,  w = left child node index
    float4 lmax;  // xyz = left child max,  w = right child node index
    float4 rmin;  // xyz = right child min, w = triangle count (0 = interior)
    float4 rmax;  // xyz = right child max, w = first triangle
};

#define STACK_SIZE 32

// The reciprocal of a ray direction, kept finite.
//
// The slab test is `fma(bound, rD, rO)` with `rO = O * -rD`, and that is only
// well behaved while `rD` is finite. A direction component of zero makes it
// infinite, the two terms then evaluate `inf - inf`, and the NaN that comes
// out compares false against every bound -- so the ray reports a clean miss.
//
// **This is not a rare case.** A cage ray is fired straight down a surface
// normal, and a flat face's normal is exactly axis-aligned, so the component
// is exactly zero for every texel of every flat face. Measured: it missed all
// 65536 of them and still produced a full set of plausible maps.
//
// Clamping to 1e16 rather than special-casing keeps the inner loop
// branch-free, and it errs the safe way: a slab interval that comes out too
// wide costs a triangle test, one that comes out NaN costs the hit.
inline float3 safe_recip(const float3 d) {
    const float tiny = 1e-16f;
    const float x = fabs(d.x) < tiny ? (d.x < 0.0f ? -tiny : tiny) : d.x;
    const float y = fabs(d.y) < tiny ? (d.y < 0.0f ? -tiny : tiny) : d.y;
    const float z = fabs(d.z) < tiny ? (d.z < 0.0f ? -tiny : tiny) : d.z;
    return (float3)(1.0f / x, 1.0f / y, 1.0f / z);
}

// Möller-Trumbore, shared by both queries. `tmax` is carried in and out so the
// closest-hit form can shrink it as it goes.
inline bool hit_triangle(const float3 v0, const float3 v1, const float3 v2,
                         const float3 O, const float3 D,
                         float* t, float* bu, float* bv) {
    const float3 e1 = v1 - v0, e2 = v2 - v0;
    const float3 h = cross(D, e2);
    const float det = dot(e1, h);
    // Two-sided on purpose. A high-poly sculpt is not reliably wound outward,
    // and culling backfaces would punch holes in the occlusion exactly where
    // the artist flipped a face without noticing.
    if (fabs(det) < 1e-12f) return false;
    const float f = 1.0f / det;
    const float3 s = O - v0;
    const float u = f * dot(s, h);
    if (u < 0.0f || u > 1.0f) return false;
    const float3 q = cross(s, e1);
    const float v = f * dot(D, q);
    if (v < 0.0f || u + v > 1.0f) return false;
    const float d = f * dot(e2, q);
    if (d <= 0.0f) return false;
    *t = d; *bu = u; *bv = v;
    return true;
}

// Any-hit. The whole inner loop of AO and thickness: it does not matter what
// was hit or how far away, only that something was, so it returns on the first
// triangle and never sorts.
bool occluded(const global struct BVHNode* nodes, const global uint* order,
              const global uint* tris, const global float4* verts,
              const float3 O, const float3 D, const float3 rD, const float tmax) {
    const float3 rO = O * -rD;
    uint node = 0, stack[STACK_SIZE], depth = 0;

    while (true) {
        const float4 lmin = nodes[node].lmin, lmax = nodes[node].lmax;
        const float4 rmin = nodes[node].rmin, rmax = nodes[node].rmax;
        const uint count = as_uint(rmin.w);

        if (count > 0) {
            const uint first = as_uint(rmax.w);
            for (uint i = 0; i < count; i++) {
                const uint tri = order[first + i] * 3;
                float t, bu, bv;
                if (hit_triangle(verts[tris[tri + 0]].xyz,
                                 verts[tris[tri + 1]].xyz,
                                 verts[tris[tri + 2]].xyz,
                                 O, D, &t, &bu, &bv) && t < tmax) {
                    return true;
                }
            }
            if (depth == 0) break;
            node = stack[--depth];
            continue;
        }

        // Both children's slabs in one go. `fma(min, rD, rO)` is the slab test
        // with the reciprocal folded in -- no division in the hot loop.
        const float3 t1a = fma(lmin.xyz, rD, rO), t2a = fma(lmax.xyz, rD, rO);
        const float3 t1b = fma(rmin.xyz, rD, rO), t2b = fma(rmax.xyz, rD, rO);
        const float3 mina = fmin(t1a, t2a), maxa = fmax(t1a, t2a);
        const float3 minb = fmin(t1b, t2b), maxb = fmax(t1b, t2b);
        const float near_a = fmax(fmax(fmax(mina.x, mina.y), mina.z), 0.0f);
        const float far_a = fmin(fmin(fmin(maxa.x, maxa.y), maxa.z), tmax);
        const float near_b = fmax(fmax(fmax(minb.x, minb.y), minb.z), 0.0f);
        const float far_b = fmin(fmin(fmin(maxb.x, maxb.y), maxb.z), tmax);

        float da = near_a > far_a ? 1e30f : near_a;
        float db = near_b > far_b ? 1e30f : near_b;
        uint left = as_uint(lmin.w), right = as_uint(lmax.w);

        // Nearest child first, the other one stacked. On a miss this is what
        // makes an any-hit query cheap: the near side usually answers it.
        if (da > db) {
            const float d = da; da = db; db = d;
            const uint n = left; left = right; right = n;
        }
        if (da == 1e30f) {
            if (depth == 0) break;
            node = stack[--depth];
        } else {
            node = left;
            if (db != 1e30f && depth < STACK_SIZE - 1) stack[depth++] = right;
        }
    }
    return false;
}

// Closest hit. Used where *what* was hit matters -- the high-poly's surface
// under a low-poly texel, for normal, height and position.
// Returns (t, u, v, triangle) with t = tmax when nothing was hit.
float4 nearest(const global struct BVHNode* nodes, const global uint* order,
               const global uint* tris, const global float4* verts,
               const float3 O, const float3 D, const float3 rD, const float tmax) {
    const float3 rO = O * -rD;
    float4 best = (float4)(tmax, 0.0f, 0.0f, as_float(0xFFFFFFFFu));
    uint node = 0, stack[STACK_SIZE], depth = 0;

    while (true) {
        const float4 lmin = nodes[node].lmin, lmax = nodes[node].lmax;
        const float4 rmin = nodes[node].rmin, rmax = nodes[node].rmax;
        const uint count = as_uint(rmin.w);

        if (count > 0) {
            const uint first = as_uint(rmax.w);
            for (uint i = 0; i < count; i++) {
                const uint index = order[first + i];
                const uint tri = index * 3;
                float t, bu, bv;
                if (hit_triangle(verts[tris[tri + 0]].xyz,
                                 verts[tris[tri + 1]].xyz,
                                 verts[tris[tri + 2]].xyz,
                                 O, D, &t, &bu, &bv) && t < best.x) {
                    best = (float4)(t, bu, bv, as_float(index));
                }
            }
            if (depth == 0) break;
            node = stack[--depth];
            continue;
        }

        const float3 t1a = fma(lmin.xyz, rD, rO), t2a = fma(lmax.xyz, rD, rO);
        const float3 t1b = fma(rmin.xyz, rD, rO), t2b = fma(rmax.xyz, rD, rO);
        const float3 mina = fmin(t1a, t2a), maxa = fmax(t1a, t2a);
        const float3 minb = fmin(t1b, t2b), maxb = fmax(t1b, t2b);
        const float near_a = fmax(fmax(fmax(mina.x, mina.y), mina.z), 0.0f);
        const float far_a = fmin(fmin(fmin(maxa.x, maxa.y), maxa.z), best.x);
        const float near_b = fmax(fmax(fmax(minb.x, minb.y), minb.z), 0.0f);
        const float far_b = fmin(fmin(fmin(maxb.x, maxb.y), maxb.z), best.x);

        float da = near_a > far_a ? 1e30f : near_a;
        float db = near_b > far_b ? 1e30f : near_b;
        uint left = as_uint(lmin.w), right = as_uint(lmax.w);

        if (da > db) {
            const float d = da; da = db; db = d;
            const uint n = left; left = right; right = n;
        }
        if (da == 1e30f) {
            if (depth == 0) break;
            node = stack[--depth];
        } else {
            node = left;
            if (db != 1e30f && depth < STACK_SIZE - 1) stack[depth++] = right;
        }
    }
    return best;
}

// --- sampling ----------------------------------------------------------------
//
// Deliberately identical to the host's CPU path, down to the constants. The
// two backends have to produce the same image or the GPU one cannot be
// checked against anything, and "close enough" is not checkable.

inline float radical_inverse(uint bits, uint scramble) {
    bits = (bits << 16u) | (bits >> 16u);
    bits = ((bits & 0x55555555u) << 1u) | ((bits & 0xAAAAAAAAu) >> 1u);
    bits = ((bits & 0x33333333u) << 2u) | ((bits & 0xCCCCCCCCu) >> 2u);
    bits = ((bits & 0x0F0F0F0Fu) << 4u) | ((bits & 0xF0F0F0F0u) >> 4u);
    bits = ((bits & 0x00FF00FFu) << 8u) | ((bits & 0xFF00FF00u) >> 8u);
    return (float)(bits ^ scramble) * 2.3283064365386963e-10f;
}

inline uint hash_u32(uint x) {
    x ^= x >> 16; x *= 0x7feb352du;
    x ^= x >> 15; x *= 0x846ca68bu;
    x ^= x >> 16;
    return x;
}

// Duff et al.'s branchless orthonormal basis. Picking a fixed "up" instead
// fails exactly when the normal is parallel to it, which on a real model is a
// whole flat face going wrong at once rather than a stray texel.
inline void make_basis(const float3 n, float3* t, float3* b) {
    const float s = n.z >= 0.0f ? 1.0f : -1.0f;
    const float a = -1.0f / (s + n.z);
    const float d = n.x * n.y * a;
    *t = (float3)(1.0f + s * n.x * n.x * a, s * d, -s * n.x);
    *b = (float3)(d, s + n.y * n.y * a, -n.y);
}

// Turns any -0.0 component into +0.0. See `flip` in main.cpp for the full
// measurement; in short, the reciprocal of -0.0 is -inf, the slab test then
// evaluates inf - inf, and the resulting NaN reports a clean miss. A cage ray
// fired down a flat surface's normal is exactly (-0, -0, -1), so without this
// every texel of every flat face misses -- silently, with a full set of
// plausible maps to show for it.
inline float3 no_negative_zero(const float3 v) {
    return (float3)(v.x == 0.0f ? 0.0f : v.x,
                    v.y == 0.0f ? 0.0f : v.y,
                    v.z == 0.0f ? 0.0f : v.z);
}

inline float3 cosine_direction(const float3 n, const float u1, const float u2) {
    const float r = sqrt(u1);
    const float phi = 6.2831853071795864f * u2;
    float3 t, b;
    make_basis(n, &t, &b);
    return t * (r * cos(phi)) + b * (r * sin(phi)) + n * sqrt(fmax(0.0f, 1.0f - u1));
}

// A ray inside a cone of half-angle `acos(cos_max)` around `n`.
//
// `cos_max = 0` is the whole hemisphere and reproduces `cosine_direction`
// exactly, which is what keeps the default path unchanged. Two distributions:
// cosine-weighted, whose density already carries the cosine term so the AO
// estimate is a plain hit fraction; and uniform, which reads harder because grazing directions get the same vote.
inline float3 cone_direction(const float3 n, const float u1, const float u2,
                             const float cos_max, const int uniform) {
    const float phi = 6.2831853071795864f * u2;
    float3 t, b;
    make_basis(n, &t, &b);

    float r, z;
    if (uniform) {
        z = 1.0f - u1 * (1.0f - cos_max);
        r = sqrt(fmax(0.0f, 1.0f - z * z));
    } else {
        // Cosine-weighted, restricted to the cone: the disc the hemisphere
        // sampling projects from is scaled by sin(theta_max).
        const float sin_max = sqrt(fmax(0.0f, 1.0f - cos_max * cos_max));
        r = sqrt(u1) * sin_max;
        z = sqrt(fmax(0.0f, 1.0f - r * r));
    }
    return t * (r * cos(phi)) + b * (r * sin(phi)) + n * z;
}

// How much an occluder at `t` shadows, given the search radius and the mode.
inline float attenuate(const float t, const float reach, const uint mode) {
    if (mode == 0u || reach <= 0.0f) return 1.0f;
    const float x = clamp(1.0f - t / reach, 0.0f, 1.0f);
    if (mode == 1u) return x;
    return x * x * (3.0f - 2.0f * x);   // smoothstep
}

// --- projection ---------------------------------------------------------------
//
// With a high-poly, a texel's shading point is **not** where the low-poly is.
// It is where the high-poly is under it, and finding that is the whole of a
// cage bake: start out along the low-poly's normal by `extrusion`, walk back
// in, and take the first surface met.
//
// Getting this wrong is the quiet failure of the whole program. Tracing AO
// from the low-poly surface against high-poly geometry produces an image --
// a plausible one -- in which every cavity is displaced by the gap between
// the two meshes. Nothing reports it.

void kernel project(
    const global struct BVHNode* nodes,
    const global uint* order,
    const global uint* tris,
    const global float4* verts,
    const global float4* normals,            // per high-poly vertex
    const global float4* gbuffer_position,   // xyz world, w = covered
    const global float4* gbuffer_normal,
    global float4* out_position,             // xyz world, w = 1 when a ray landed
    global float4* out_normal,
    const uint texel_offset,
    const uint texel_count,
    const uint has_high,
    const float extrusion,
    const float ray_distance)
{
    const uint texel = texel_offset + get_global_id(0);
    if (texel >= texel_count) return;

    const float4 packed = gbuffer_position[texel];
    if (packed.w < 0.5f) {
        out_position[texel] = (float4)(0.0f, 0.0f, 0.0f, 0.0f);
        return;
    }

    const float3 low_point = packed.xyz;
    const float3 low_normal = gbuffer_normal[texel].xyz;

    if (!has_high) {
        // Self-bake: the low-poly *is* the surface. Copying rather than
        // branching everywhere downstream keeps one code path for both.
        out_position[texel] = (float4)(low_point, 1.0f);
        out_normal[texel] = (float4)(low_normal, 0.0f);
        return;
    }

    const float3 origin = low_point + low_normal * extrusion;
    const float3 direction = no_negative_zero(-low_normal);
    const float3 rD = safe_recip(direction);
    const float4 hit = nearest(nodes, order, tris, verts, origin, direction, rD,
                               ray_distance);

    if (as_uint(hit.w) == 0xFFFFFFFFu) {
        // No high-poly under this texel. Marked rather than guessed: the maps
        // decide for themselves what a miss means -- flat for a normal map,
        // mid-grey for height -- and a guess here would be the same wrong
        // answer for all of them.
        out_position[texel] = (float4)(low_point, 0.0f);
        out_normal[texel] = (float4)(low_normal, 0.0f);
        return;
    }

    const uint tri = as_uint(hit.w) * 3;
    const float u = hit.y, v = hit.z, w = 1.0f - u - v;
    const float3 n = normalize(normals[tris[tri + 0]].xyz * w +
                               normals[tris[tri + 1]].xyz * u +
                               normals[tris[tri + 2]].xyz * v);

    out_position[texel] = (float4)(origin + direction * hit.x, 1.0f);
    // Two-sided geometry means the hit can be the back of a face. The shading
    // normal has to face the ray that found it, or the AO traced from here
    // starts underground.
    out_normal[texel] = (float4)(dot(n, direction) > 0.0f ? -n : n, 0.0f);
}

// --- surface maps -------------------------------------------------------------
//
// Everything that is a property of the projected surface rather than of the
// light around it: no rays, one work item per texel, effectively free next to
// the tracing.

void kernel surface(
    const global float4* gbuffer_position,   // low: xyz world, w = covered
    const global float4* gbuffer_normal,
    const global float4* gbuffer_tangent,    // xyz world, w = bitangent sign
    const global float4* projected_position, // high: xyz world, w = hit
    const global float4* projected_normal,
    global float4* out_position,
    global float4* out_normal,
    global float4* out_height,
    const uint texel_offset,
    const uint texel_count,
    const uint want,                         // bit0 position, bit1 normal, bit2 height
    const uint flip_green,
    const float3 bbox_lo,
    const float3 bbox_span,
    const float height_range)
{
    const uint texel = texel_offset + get_global_id(0);
    if (texel >= texel_count) return;

    const float4 low = gbuffer_position[texel];
    if (low.w < 0.5f) return;

    const float4 high = projected_position[texel];
    const bool landed = high.w > 0.5f;

    if (want & 1u) {
        // Where on the model this texel is, as 0..1 inside its own box. The
        // box is whatever was traced -- the high-poly when there is one, since
        // those are the points being normalised.
        const float3 p = (high.xyz - bbox_lo) / bbox_span;
        out_position[texel] = (float4)(clamp(p, 0.0f, 1.0f), 1.0f);
    }

    if (want & 2u) {
        const float3 n_low = gbuffer_normal[texel].xyz;
        const float4 tangent = gbuffer_tangent[texel];

        // Gram-Schmidt. The interpolated tangent drifts out of perpendicular
        // across a triangle, and MikkTSpace is defined on the orthogonalised
        // one -- this is the step that makes our basis the same basis the
        // shader will decode with.
        float3 t = tangent.xyz - n_low * dot(n_low, tangent.xyz);
        const float length = fast_length(t);
        t = length > 1e-8f ? t / length : t;
        const float3 b = cross(n_low, t) * tangent.w;

        const float3 n = landed ? projected_normal[texel].xyz : n_low;
        float3 ts = (float3)(dot(n, t), dot(n, b), dot(n, n_low));
        ts = normalize(ts);
        if (flip_green) ts.y = -ts.y;

        out_normal[texel] = (float4)(ts * 0.5f + 0.5f, 1.0f);
    }

    if (want & 4u) {
        // Signed along the low-poly's normal: positive where the high-poly
        // stands proud, negative where it is cut in. 0.5 is flat, the same
        // convention every engine reads and the one the Height channel uses.
        const float h = landed ? dot(high.xyz - low.xyz, gbuffer_normal[texel].xyz)
                               : 0.0f;
        const float encoded = clamp(0.5f + h / (2.0f * height_range), 0.0f, 1.0f);
        out_height[texel] = (float4)(encoded, encoded, encoded, 1.0f);
    }
}

// --- curvature ----------------------------------------------------------------
//
// Curvature per *texel*, from how the surface normal turns across the map --
// not per vertex from mesh topology, which is what PaintR2's Cycles path does
// and what limits it. A vertex-based curvature can only be as detailed as the
// mesh is dense: a four-poly box has four edges to say anything with, and no
// amount of smoothing invents a fifth. This reads the projected high-poly
// normal, so its detail is bounded by the map's resolution instead.
//
// The measure is the divergence of the normal field along the surface: how far
// the normal leans between one texel and the next, per metre travelled. That
// is curvature in the textbook sense, positive where the surface bulges and
// negative where it folds in.
//
// Two things it has to refuse rather than smear:
//
//   * a neighbour outside the island -- there is no surface between them
//   * a neighbour across a UV seam, which is adjacent in the map and metres
//     away on the model. Caught by comparing the step in world space against
//     the step the texel grid should take; a seam is orders of magnitude out.

void kernel curvature(
    const global float4* gbuffer_position,
    const global float4* projected_position,
    const global float4* projected_normal,
    global float4* out_curvature,
    const uint texel_offset,
    const uint texel_count,
    const uint size,
    const float scale,
    const float low,               // curvature that maps to black
    const float high)              // and to white
{
    const uint texel = texel_offset + get_global_id(0);
    if (texel >= texel_count) return;
    if (gbuffer_position[texel].w < 0.5f) return;

    const uint x = texel % size, y = texel / size;
    if (x == 0 || y == 0 || x + 1 >= size || y + 1 >= size) {
        out_curvature[texel] = (float4)(0.5f, 0.5f, 0.5f, 1.0f);
        return;
    }

    const uint left = texel - 1, right = texel + 1;
    const uint down = texel - size, up = texel + size;
    if (gbuffer_position[left].w < 0.5f || gbuffer_position[right].w < 0.5f ||
        gbuffer_position[down].w < 0.5f || gbuffer_position[up].w < 0.5f) {
        out_curvature[texel] = (float4)(0.5f, 0.5f, 0.5f, 1.0f);
        return;
    }

    const float3 p = projected_position[texel].xyz;
    const float3 pl = projected_position[left].xyz, pr = projected_position[right].xyz;
    const float3 pd = projected_position[down].xyz, pu = projected_position[up].xyz;
    const float3 nl = projected_normal[left].xyz, nr = projected_normal[right].xyz;
    const float3 nd = projected_normal[down].xyz, nu = projected_normal[up].xyz;

    const float3 du = pr - pl;
    const float3 dv = pu - pd;
    const float lu = fast_length(du), lv = fast_length(dv);

    // A seam: adjacent in the map, far apart on the model. The reference is
    // the texel's own step, taken from the shorter of the two axes.
    const float step = fmin(lu, lv);
    if (lu < 1e-9f || lv < 1e-9f || fmax(lu, lv) > step * 8.0f) {
        out_curvature[texel] = (float4)(0.5f, 0.5f, 0.5f, 1.0f);
        return;
    }

    // Directional derivative of the normal along each axis, in units of
    // "radians per metre". The sign follows the surface: a bulge turns the
    // normal away from the direction of travel.
    const float ku = dot(nr - nl, du / lu) / lu;
    const float kv = dot(nu - nd, dv / lv) / lv;

    // Mean curvature, scaled by a world-size factor so the map does not change
    // meaning when the model is scaled.
    const float mean = (ku + kv) * 0.5f * scale;

    // Then remapped onto 0..1. The default -1..1 puts flat at 0.5, which is
    // what every consumer of a curvature map expects; narrowing the range is
    // how a map that came out nearly flat is made readable without baking it
    // again at a different radius.
    const float span = high - low;
    const float encoded = span > 1e-6f
                              ? clamp((mean - low) / span, 0.0f, 1.0f)
                              : 0.5f;
    out_curvature[texel] = (float4)(encoded, encoded, encoded, 1.0f);
    (void)p;
}

// --- the bake ----------------------------------------------------------------

// The AO answer for one ray: 0 when nothing shadows it, otherwise how much.
//
// The fast path is the old one -- an any-hit query, no distance, no facing.
// It is taken whenever nothing asks for more, and it is what every default
// bake still runs. The slow path exists for three options that all need to
// know *what* was hit: a minimum distance, attenuation, and ignoring
// occluders whose front is turned away.
//
// Backfaces are skipped by re-tracing from just past the hit rather than by
// filtering inside traversal: the traversal is shared with everything else
// and threading a predicate through it would cost every other query. Four
// continuations, because a ray that has passed through four surfaces has
// found a solid.
inline float ao_occlusion(const global struct BVHNode* nodes,
                          const global uint* order, const global uint* tris,
                          const global float4* verts,
                          const float3 origin, const float3 d, const float3 rd,
                          const float reach, const float min_distance,
                          const uint attenuation, const int ignore_backface) {
    if (attenuation == 0u && min_distance <= 0.0f && !ignore_backface) {
        return occluded(nodes, order, tris, verts, origin, d, rd, reach)
                   ? 1.0f : 0.0f;
    }

    float travelled = 0.0f;
    float3 from = origin;

    for (int step = 0; step < 4; step++) {
        const float remaining = reach - travelled;
        if (remaining <= 0.0f) return 0.0f;

        const float4 hit = nearest(nodes, order, tris, verts, from, d, rd,
                                   remaining);
        if (hit.x >= remaining) return 0.0f;

        const float t = travelled + hit.x;
        if (t < min_distance) {
            // Too close to count, but the ray carries on: something further
            // out may still shadow this point.
            travelled = t + 1e-4f;
            from = origin + d * travelled;
            continue;
        }

        if (ignore_backface) {
            const uint tri = as_uint(hit.w) * 3;
            const float3 v0 = verts[tris[tri + 0]].xyz;
            const float3 e1 = verts[tris[tri + 1]].xyz - v0;
            const float3 e2 = verts[tris[tri + 2]].xyz - v0;
            if (dot(cross(e1, e2), d) > 0.0f) {
                travelled = t + 1e-4f;
                from = origin + d * travelled;
                continue;
            }
        }

        return attenuate(t, reach, attenuation);
    }
    return 0.0f;
}

void kernel trace(
    const global struct BVHNode* nodes,
    const global uint* order,
    const global uint* tris,
    const global float4* verts,
    const global float4* gbuffer_position,   // low: xyz world, w = covered
    const global float4* shading_position,   // where to trace FROM (projected)
    const global float4* shading_normal,     // and which way that surface faces
    global float4* out_ao,
    global float4* out_thickness,
    global float4* out_bent,
    const uint texel_offset,                 // this band's first texel
    const uint texel_count,                  // one past this band's last
    const uint samples,
    const uint want,                         // bit0 ao, bit1 thickness, bit2 bent
    const float epsilon,
    const float ao_reach,
    const float thickness_reach,
    const float ao_cos_max,        // cone half-angle, as its cosine
    const float ao_min_distance,
    const uint ao_attenuation,
    const uint ao_flags)           // bit0 uniform, bit1 ignore backface
{
    // Bands rather than one launch over the whole map, for two reasons that
    // both matter: the host can report progress between them, and a kernel
    // that runs for seconds trips the display driver's watchdog and is killed
    // out from under us. A band is sized by the host to land near 50 ms.
    const uint texel = texel_offset + get_global_id(0);
    if (texel >= texel_count) return;

    if (gbuffer_position[texel].w < 0.5f) return;  // outside every UV island

    // The projected point, not the low-poly one. With no high-poly the two are
    // the same and `project` said so; with one, this is the difference between
    // occlusion that sits on the model and occlusion that floats above it.
    const float3 point = shading_position[texel].xyz;
    const float3 normal = shading_normal[texel].xyz;
    const uint scramble = hash_u32(texel * 0x9e3779b9u);

    const float3 origin = point + normal * epsilon;
    const float3 inner = point - normal * epsilon;
    const float3 flipped = no_negative_zero(-normal);

    float open = 0.0f;
    uint buried = 0;
    float3 bent = (float3)(0.0f, 0.0f, 0.0f);

    const int uniform = (ao_flags & 1u) != 0u;
    const int ignore_backface = (ao_flags & 2u) != 0u;

    for (uint s = 0; s < samples; s++) {
        const float u1 = ((float)s + 0.5f) / (float)samples;
        const float u2 = radical_inverse(s, scramble);

        if (want & 5u) {   // AO or bent normal -- both need the outward ray
            const float3 d = no_negative_zero(
                cone_direction(normal, u1, u2, ao_cos_max, uniform));
            const float3 rd = safe_recip(d);
            const float shadow = ao_occlusion(
                nodes, order, tris, verts, origin, d, rd, ao_reach,
                ao_min_distance, ao_attenuation, ignore_backface);
            open += 1.0f - shadow;
            // A bent normal is the average direction light actually arrives
            // from, so a ray votes by how much of it got out.
            bent += d * (1.0f - shadow);
        }
        if (want & 2u) {
            const float3 d = no_negative_zero(cosine_direction(flipped, u1, u2));
            const float3 rd = safe_recip(d);
            if (occluded(nodes, order, tris, verts, inner, d, rd, thickness_reach)) {
                buried++;
            }
        }
    }

    const float inv = 1.0f / (float)samples;

    if (want & 1u) {
        // Cosine-weighted directions already carry the cosine term, so the
        // estimate is the plain hit fraction -- no extra weight, no 1/pi.
        const float visibility = open * inv;
        out_ao[texel] = (float4)(visibility, visibility, visibility, 1.0f);
    }
    if (want & 2u) {
        // The complement: a texel whose inward rays all land is buried, and
        // reads dark.
        const float depth = 1.0f - (float)buried * inv;
        out_thickness[texel] = (float4)(depth, depth, depth, 1.0f);
    }
    if (want & 4u) {
        // Fully occluded leaves nothing to average; the geometric normal is
        // the only honest answer there.
        const float length = fast_length(bent);
        const float3 direction = length > 1e-6f ? bent / length : normal;
        out_bent[texel] = (float4)(direction * 0.5f + 0.5f, 1.0f);
    }
}
