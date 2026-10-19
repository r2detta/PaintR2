# Icon folder

You can replace PaintR2's interface icons with your own.

A PNG dropped in here takes the place of that icon. If the file is not there,
the add-on's own drawing is used instead — so you can **replace the set a piece
at a time**, and a missing file is not an error. A folder with two icons in it
is a working folder.

## Format

- **PNG, RGBA.** `.ico` will not work — Blender does not recognise that
  extension.
- **64x64**, one file per icon. No scale variants are kept.
- **White silhouette plus transparency.** The colour is applied at draw time, so
  the hover, dimmed and highlighted states all come out of the same file. If you
  put a coloured file in, that icon cannot be tinted and will always stay the
  colour it is.
- Leave a little margin: keep the mark roughly 4–8 pixels in from the edge of
  the 64-pixel square.

## File names

A file's name is the name of the icon it replaces. The full list of valid names:

```
adjustment     arrow_down     arrow_up       brush          check
chevron_down   chevron_left   chevron_right  cross          cube
effects        eye_closed     eye_open       fill_bucket    folder
gradient       mask_square    mesh_fill      mirror         nodes
plus           polygon_fill   pressure       select_box     select_lasso
stroke         swap           trash          uv_island_fill wireframe
layer_adjust   layer_delete   layer_fill     layer_filter   layer_group
layer_mask     layer_paint    layer_pattern
```

The `layer_*` names are the Layers panel's own set, kept apart from the brush
strip: replacing `layer_fill` changes the Layers toolbar and leaves the
strip's `fill_bucket` as it is.

A PNG matching none of these is ignored.

## Using your own folder

So your files are not wiped when you update the add-on, you can keep your icons
in a folder outside the package:

`Edit → Preferences → Add-ons → PaintR2 → Icon Folder`

Point that at your folder and icons are read from there first; for any name with
no file there, the packaged one is used, and failing that the drawing.

## After changing a file

Close and reopen the PaintR2 window. There is no need to restart Blender.

## What is in this folder today

```
brush.png   fill_bucket.png
```

Every other icon comes from the add-on's own drawings.
