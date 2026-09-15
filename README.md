# PaintR2

**Layered texture painting inside Blender** — and the result exported as real
texture files.

Blender's Texture Paint works on one image at a time; there are no layers. You
can build layer logic in the shader, but a shader mix is not a file you can
export. PaintR2 joins the two: paint in a stack of layers, take the result out
as PNG or EXR.

It is not trying to replace Substance Painter. The goal is to finish the jobs
simple enough that going to Painter for them was never worth the round trip.

---

> ### ⚠️ Under development
>
> PaintR2 is **still being built** and has not reached 1.0 (the current series
> is `0.10.x`). What it does today is **simple-level** texture painting: a layer
> stack, painting, fills, masks, and per-channel export. It is not a substitute
> for a full material pipeline.
>
> In practice that means:
>
> - **Don't rely on it alone for serious work.** Try it on a copy first, and
>   save often.
> - **The interface and behaviour can change** between versions.
> - **You will probably hit bugs.** If you do, please
>   [open an issue](https://github.com/r2detta/PaintR2/issues) — saying what you
>   were doing and what Blender's console printed helps enormously.
>
> What is deliberately left out is below: [Known limits](#known-limits).

---

## Requirements

- **Blender 5.2 LTS.** Only. It will not run on earlier versions.

## Installation

**From a release (recommended):** download `PaintR2-<version>.zip` from the
[Releases](https://github.com/r2detta/PaintR2/releases) page, then in Blender
pick `Edit → Preferences → Add-ons → ▾ → Install from Disk`.

The green **Code → Download ZIP** button gives you a file that installs the same
way — Blender takes the extension's identity from the manifest, not from the
folder name. The only difference is that this zip also carries the baker's C++
source, so the install takes a few hundred kilobytes more. The release package
has that stripped out.

**From source:** clone the repository and link it into your extensions folder:

```bash
git clone https://github.com/r2detta/PaintR2.git
ln -s "$PWD/PaintR2" ~/.config/blender/5.2/extensions/user_default/PaintR2
```

---

## Quick start

1. In the 3D viewport, select the mesh (or meshes) you want to paint.
2. Press `N` to open the sidebar and switch to the **PaintR2** tab.
3. Choose a resolution and a base colour, then press **Set Models**.
   An object with no material gets one called `<object>_Paint`; an object that
   already has one **keeps the name but has the material reset in place** — so
   whatever was in that material is gone, and only its name survives.
4. In the **Channels** panel, pick which channels this material carries
   (Base Color, Roughness, Metallic, Normal, Height).
5. Press **Open PaintR2**. Layers and painting open in a window of their own.
6. When you are done, **Bake & Export** from that window.

The sidebar is only for setting up; the actual work happens in the PaintR2
window.

---

## What's in it

**Layers**
- Add, delete, reorder, hide; drag and drop to move
- 19 blend modes and opacity
- **Groups** — nested, with the group's mask and opacity applied to the whole
  subtree
- Layer masks, and viewing a mask on its own
- Colour labels, multiple selection, copy and paste
- Paint, fill, procedural, adjustment and node layer types

**Painting**
- In the 3D viewport, through Blender's own brush engine (pressure and stroke
  smoothing included)
- Every brush, stroke, falloff, cursor and texture setting, inside the window
- Symmetry: X / Y / Z and a mirror plane

**Fill tools**
- **Polygon Fill** — triangle or polygon
- **Mesh Fill** — connected part or whole object
- **UV Island Fill** — one UV island
- Box and lasso selection, drag to fill, edge softening and bleed
- Its own undo ring (`Ctrl Z`)

**Procedural and adjustment**
- Gradient, Noise, Checker — as a layer or as a mask
- Use your own node group as a layer
- Adjustment layers: HSV, Levels, Bright/Contrast, Gamma, Invert
- A per-layer effect stack

**PBR and export**
- Five channels: Base Color, Roughness, Metallic, Normal, Height
- The right colour space and bit depth per channel; Normal and Height at 16 bit
- Live Principled preview, with lighting and shading modes
- Exported as a separate file per channel

**Mesh map baking**
- Ambient Occlusion, Curvature, Thickness, Position
- Normal, Height and Bent Normal from a high-poly

**Multiple objects and materials** — painted as one set, and exported together.

---

## Shortcuts

Press `F1` or `?` inside the window for the full list. The common ones:

| Key | What it does |
|---|---|
| `↑` `↓` | Select the layer above / below |
| `Ctrl ↑` `Ctrl ↓` | Move the layer |
| `M` | Turn the mask off / back on |
| `Shift M` | Show the mask on its own |
| `I` | Show the layer on its own |
| `X` | Delete the layer, its mask, or the selected filter |
| `Ctrl G` | Group the selected layers |
| `Ctrl C` / `Ctrl V` | Copy / paste layers |
| `Ctrl Z` | Undo a fill |

---

## About the mesh map baker

Mesh maps can go through an accelerated baker shipped with the add-on
(`bin/paintr2_bake`), currently built for **Linux x86-64** only.

On any other platform there is nothing for you to do: the add-on notices and
quietly falls back to **Cycles**. The maps are still produced, just more slowly.
The one exception is **Bent Normal**, which only the accelerated baker can
produce — on the Cycles path it is skipped, and the report says so.

The baker's source is under `PaintR2Bake/`, with its own `build.sh`. To build it
for your platform:

```bash
cd PaintR2Bake && ./build.sh
```

---

## Known limits

**Deliberate limits** — these are not missing features, they are decisions:

- **Resolution cannot be changed afterwards.** It is chosen in Set Models and
  stays. Rescaling would mean resampling every layer and every mask, which is an
  unrecoverable loss. If you started wrong, the way out is a new material.
- **One PaintR2 window at a time.**
- **No UDIM.** A single UV tile.
- No smart materials or smart masks.


---

## Licence

GPL-3.0-or-later. Full text in [LICENSE](LICENSE).

`PaintR2Bake/third_party/tiny_bvh.h` is MIT licensed; its copyright and
permission notice are in the file's own header.
