# SPDX-License-Identifier: GPL-3.0-or-later
"""Keeping painted layers alive across saves.

The problem: a "generated" Blender image exists only in memory. Paint into it,
save the .blend, reopen -- and the paint is gone, because Blender never wrote
the pixels anywhere. Something has to give the image a home.

The chosen approach is a hybrid: by default each layer is written as a PNG
into a folder next to the .blend, and "Pack Layers" can pull them inside the
.blend when a single self-contained file is wanted.

    my_project.blend
    my_project_paintr2/
        Suzanne_paint.png
        Suzanne_paint_Paint_2.png

Measured on 5.2: `image.pack()` fails on a GENERATED image that has never been
written, so packing saves to disk first and then packs.
"""

import os

import bpy


FOLDER_SUFFIX = "_paintr2"
FILE_FORMAT = 'PNG'
EXTENSION = ".png"


def external_directory():
    """Folder next to the current .blend, or None if it was never saved."""
    blend_path = bpy.data.filepath
    if not blend_path:
        return None
    return os.path.splitext(blend_path)[0] + FOLDER_SUFFIX


def layer_images():
    """Every image a PaintR2 layer refers to, without duplicates.

    Masks included. They were not, and that was a hole: a mask created after
    the .blend was first saved got written once by `adopt_new_image` and then
    never again, so every stroke painted into it after that was lost on
    reload. A mask is painted work like any other.
    """
    seen = set()
    images = []

    for material in bpy.data.materials:
        document = getattr(material, "paintr2", None)
        if document is None:
            continue

        from . import effects as effects_mod

        for layer in document.layers:
            # Paint effects too: an image the brush can write into is painted
            # work, wherever in the layer it sits.
            for image in (layer.image, layer.mask_image,
                          *effects_mod.paint_images(layer)):
                if image is None:
                    continue
                key = image.as_pointer()
                if key in seen:
                    continue
                seen.add(key)
                images.append(image)

    return images


def needs_saving(image):
    """True when losing this image would lose work."""
    if image.packed_file is not None:
        return False
    if image.is_dirty:
        return True
    # Never written anywhere: even unpainted, it only exists in memory.
    return image.source == 'GENERATED' and not image.filepath_raw


def unsaved_images():
    return [image for image in layer_images() if needs_saving(image)]


# --- writing ----------------------------------------------------------------


def save_image(image, directory):
    """Write `image` into `directory` as PNG. Returns the path."""
    os.makedirs(directory, exist_ok=True)

    path = os.path.join(directory, bpy.path.clean_name(image.name) + EXTENSION)

    image.filepath_raw = path
    image.file_format = FILE_FORMAT
    image.save()

    return path


def save_all(only_needed=True):
    """Write every layer image to the folder beside the .blend.

    Returns (saved_paths, error_message).
    """
    directory = external_directory()
    if directory is None:
        return [], "Save the .blend file first - layer images need a folder"

    saved = []
    failures = []

    for image in layer_images():
        if image.packed_file is not None:
            continue  # packed images carry their own pixels
        if only_needed and not needs_saving(image):
            continue

        try:
            saved.append(save_image(image, directory))
        except (RuntimeError, OSError) as error:
            failures.append(f"{image.name}: {error}")

    if failures:
        return saved, "; ".join(failures)
    return saved, None


def _pack_via_temp(image):
    """Pack an image that has no file to pack from. True when it worked.

    **MEASURED on 5.2: `Image.pack()` on an image with an empty `filepath_raw`
    does nothing and raises nothing** -- it returns, `packed_file` stays None,
    and the only way to find out is to look. So the image is written to a
    throwaway file first, packed from it, and the filepath cleared again; the
    temporary file is removed either way.

    This is what makes Pack work on a .blend that has never been saved, which
    is exactly when someone reaches for it: with no folder beside the file,
    packing is the *only* place the pixels can go.
    """
    import tempfile

    folder = tempfile.mkdtemp(prefix="paintr2_pack_")
    path = os.path.join(folder, f"{bpy.path.clean_name(image.name)}.png")
    previous_format = image.file_format
    previous_path = image.filepath_raw

    try:
        image.file_format = 'PNG'
        image.filepath_raw = path
        image.save()
        image.pack()
    finally:
        image.filepath_raw = "" if image.packed_file is not None else previous_path
        try:
            image.file_format = previous_format
        except (RuntimeError, TypeError):
            pass
        try:
            os.remove(path)
            os.rmdir(folder)
        except OSError:
            pass

    return image.packed_file is not None


def pack_all():
    """Embed every layer image in the .blend.

    A GENERATED image cannot be packed directly. With a folder beside the
    .blend it is written there first; without one it goes through a temporary
    file, so packing works on an unsaved .blend too.
    """
    directory = external_directory()
    packed = []
    failures = []

    for image in layer_images():
        if image.packed_file is not None:
            continue

        try:
            if directory is not None and (
                image.source == 'GENERATED' or not image.filepath_raw
            ):
                save_image(image, directory)

            if image.filepath_raw:
                image.pack()

            # Never trust the call: `pack()` is silent about doing nothing.
            if image.packed_file is None:
                _pack_via_temp(image)

            if image.packed_file is None:
                failures.append(f"{image.name}: Blender would not pack it")
                continue

            packed.append(image.name)
        except (RuntimeError, OSError) as error:
            failures.append(f"{image.name}: {error}")

    return packed, ("; ".join(failures) if failures else None)


def packed_images():
    """Every layer image currently embedded in the .blend."""
    return [image for image in layer_images() if image.packed_file is not None]


def unpack_all():
    """Move packed layer images back out to the folder beside the .blend."""
    directory = external_directory()
    if directory is None:
        return [], "Save the .blend file first"

    os.makedirs(directory, exist_ok=True)
    unpacked = []
    failures = []

    for image in layer_images():
        if image.packed_file is None:
            continue
        try:
            image.unpack(method='WRITE_LOCAL')
            unpacked.append(image.name)
        except (RuntimeError, OSError) as error:
            failures.append(f"{image.name}: {error}")

    return unpacked, ("; ".join(failures) if failures else None)


def adopt_new_image(image):
    """Give a freshly created layer image a home on disk straight away.

    This is the main line of defence. An image written at creation time is
    already a FILE when the .blend is saved, so the .blend records a link to
    it instead of a GENERATED placeholder whose pixels are thrown away.

    Returns the path, or None when the .blend has no folder yet.
    """
    directory = external_directory()
    if directory is None:
        return None

    try:
        return save_image(image, directory)
    except (RuntimeError, OSError) as error:
        print(f"[PaintR2] could not write {image.name}: {error}")
        return None


# --- save handlers ----------------------------------------------------------
#
# Timing measured on 5.2, saving an unsaved file with "Save As":
#
#     save_pre :  bpy.data.filepath == ''                <- nowhere to write
#     save_post:  bpy.data.filepath == '/path/p.blend'   <- .blend already out
#
# So on a first save the images only get a filepath *after* the .blend has
# been written, and the file on disk still describes them as GENERATED --
# which means their pixels are gone on reload. `_relink_pending` catches that
# case and saves once more, now that the links exist.

_relink_pending = False
_resaving = False


def on_save_pre(*_args):
    global _relink_pending

    if external_directory() is None:
        _relink_pending = True
        return

    _flush("save")


def on_save_post(*_args):
    global _relink_pending, _resaving

    if _resaving or external_directory() is None:
        return

    saved = _flush("save")

    if not _relink_pending:
        return

    _relink_pending = False
    if not saved:
        return

    # The .blend was written before these images had filepaths. Write it once
    # more so it points at them.
    _resaving = True
    try:
        bpy.ops.wm.save_mainfile()
        print("[PaintR2] re-saved the .blend so it links the layer images")
    except RuntimeError as error:
        print(
            f"[PaintR2] layer images were written but the .blend still calls "
            f"them generated. Save again to link them. ({error})"
        )
    finally:
        _resaving = False


def _flush(reason):
    saved, error = save_all(only_needed=True)
    if error:
        print(f"[PaintR2] some layer images could not be saved on {reason}: {error}")
    elif saved:
        print(f"[PaintR2] wrote {len(saved)} layer image(s)")
    return saved
