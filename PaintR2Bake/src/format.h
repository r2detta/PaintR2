// SPDX-License-Identifier: GPL-3.0-or-later
//
// The wire between Blender and the baker. Both sides read this file's layout,
// and the magic carries a version digit so a mismatched pair fails loudly at
// the first eight bytes rather than quietly producing a wrong map.

#pragma once

#include <cstdint>

namespace pr2 {

constexpr char kInputMagic[8] = {'P', 'R', '2', 'B', 'A', 'K', 'E', '1'};
constexpr char kOutputMagic[8] = {'P', 'R', '2', 'M', 'A', 'P', 'S', '1'};
constexpr uint32_t kVersion = 4;

// One bit per map, so a single run can produce several off one rasterization.
// That sharing is the point: in Cycles every map pays for its own bake.
enum MapBit : uint32_t {
    kMapAO = 1u << 0,
    kMapThickness = 1u << 1,
    kMapPosition = 1u << 2,
    kMapNormal = 1u << 3,
    kMapHeight = 1u << 4,
    kMapCurvature = 1u << 5,
    kMapBentNormal = 1u << 6,
};

enum Flag : uint32_t {
    kFlagFlipGreen = 1u << 0,
    // Ambient occlusion sampling. Cosine-weighted is the default because it
    // is what an unweighted hit fraction *means*; uniform is the option
    // Substance offers and it reads harder-edged.
    kFlagAOUniform = 1u << 1,
    // Skip occluders whose front is turned away from the ray. On open,
    // single-sided geometry -- a shell, a plane with thickness modelled in --
    // the inside surface would otherwise shadow the outside.
    kFlagAOIgnoreBackface = 1u << 2,
};

// How an occluder's distance weighs on the result. `kAttenuationNone` is the
// binary answer: hit or not. The other two fade an occluder out as it moves
// away, which is what makes a large search radius usable at all.
enum Attenuation : uint32_t {
    kAttenuationNone = 0,
    kAttenuationLinear = 1,
    kAttenuationSmooth = 2,
};

// Named indices into Header::params, so neither side counts slots by hand.
enum Param {
    kParamExtrusion = 0,
    kParamRayDistance = 1,      // AO max occluder distance, in world units
    kParamThicknessReach = 2,
    kParamHeightRange = 3,
    kParamCurvatureScale = 4,   // curvature sampling radius, in world units
    // Nearer than this and an occluder is ignored. For contact shadows that
    // should not appear, and for geometry that touches itself.
    kParamAOMinDistance = 5,
    // Half-angle of the cone rays are fired into, in radians. Pi/2 is the
    // whole hemisphere; narrower reads as a directional occlusion.
    kParamAOSpread = 6,
    kParamAOAttenuation = 7,    // an Attenuation value, as a float
    // The curvature range mapped onto 0..1. Outside it the map clamps.
    kParamCurvatureLow = 8,
    kParamCurvatureHigh = 9,
};

#pragma pack(push, 1)
struct Header {
    char magic[8];
    uint32_t version;
    uint32_t size;        // square map edge
    uint32_t samples;     // rays per texel
    uint32_t maps;        // MapBit mask
    uint32_t flags;       // Flag mask
    // Sixteen, not eight: the eight were full and a header that has to grow
    // for every option is a header that gets versioned to death.
    float params[16];
    uint32_t low_tris;    // low-poly triangle soup, 8 floats per corner
    uint32_t high_verts;  // 0 means "bake the low-poly against itself"
    uint32_t high_tris;
    // Other models, for ambient occlusion and the bent normal only. Positions
    // and indices, no normals: nothing is shaded off them, they only stop
    // rays. Kept out of the BVH the projection and thickness rays use -- a
    // cage ray that lands on the model next door writes the neighbour's
    // surface into this model's normal map, and a thickness probe that finds
    // it calls the model thick where it is not.
    uint32_t occluder_verts;
    uint32_t occluder_tris;
};

struct OutputHeader {
    char magic[8];
    uint32_t version;
    uint32_t size;
    uint32_t count;       // how many maps follow
};
#pragma pack(pop)

// Floats per low-poly corner: position, normal, uv, tangent, bitangent sign.
//
// The tangent is carried rather than derived. A tangent-space normal map is
// only correct against the exact basis the renderer will use to read it, and
// Blender's is MikkTSpace -- so the basis comes from Blender, computed by
// `mesh.calc_tangents()`, instead of being recomputed here from UVs and
// arriving subtly different. Getting this wrong does not fail; it lights every
// dent slightly askew.
constexpr uint32_t kLowCornerFloats = 12;

}  // namespace pr2
