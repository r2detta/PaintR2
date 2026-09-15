# SPDX-License-Identifier: GPL-3.0-or-later
"""The interface root: owns the widget tree, the layout and event dispatch.

One `UIRoot` exists per open PaintR2 window. The modal operator feeds it
events, the draw handler asks it to render. It never touches bpy operators
directly -- every callback is injected by window.py, so the toolkit stays
testable and free of circular imports.

Layout is a header, a settings strip, a tool row, the live viewport and a
layer panel on the right:

    +---------------------------------------------------------------+
    | PaintR2      Mirror X Y Z [<>]      Suzanne - Texture Paint [X]|
    +---------------------------------------------------------------+
    | [brush] Blend  [colour][2nd] Size ---o--- Strength ---o---     |
    +---------------------------------------------------------------+
    | [/][#][@][:]                                                   |
    +----------------------------------------------+----------------+
    |                                              | [+] [+] [ ]    |
    |            LIVE 3D VIEWPORT                  |  layer         |
    |            (drawn by Blender)                |  list          |
    |                                              | [^][v][x]      |
    +----------------------------------------------+----------------+

The settings strip is one band with two possible contents. The tool row picks
which: the brush's controls, or the three fill tools' -- a brush and a fill
have almost nothing in common to show, and stacking both strips would spend a
row of height on controls that are inert half the time.

The whole region is ours. None of Blender's own regions -- tool header, asset
shelf, sidebar, toolbar -- is left showing inside a PaintR2 window; every one
of them is hidden and everything they offered is drawn here instead, bound to
the same RNA properties. That is a project rule, and it exists because the
alternative was tried three times and produced a broken interface three
times.

Everything that acts on one layer lives *on that layer's row*: its blend mode,
its opacity, its colour, its name. The panel keeps only what acts on the stack
as a whole -- create at the top, order and delete at the bottom -- so there is
no footer of controls whose target you have to remember.
"""

import math
import time

from . import draw
from . import theme
from . import icons
from . import keymap
from .colorpicker import ColorPicker, ColorSwatch
from .dialog import Dialog
from .props import Popover, PropertyPanel
from .widget import Container
from .widgets import (
    Box,
    Button,
    Dropdown,
    Slider,
    IconButton,
    Label,
    ListView,
    Menu,
    Panel,
    Row,
    Spacer,
    TextField,
    ToggleButton,
    ViewportArea,
)



class UIRoot(Container):
    """Root container. Fills the whole region and lays out the main frame."""

    # Panel width in unscaled pixels, mirrored from PR2_UIState so that layout
    # never has to reach into RNA.
    # How far either side of a panel's inner edge counts as the splitter, and
    # how far the two may be dragged. The limits are the same numbers the RNA
    # properties carry; a test asserts they have not drifted apart, because a
    # drag that could go past what the property accepts would snap back on the
    # next redraw with nothing to explain it.
    SPLITTER_GRAB = 4
    LAYERS_WIDTH_RANGE = (200, 640)
    DISPLAY_WIDTH_RANGE = (200, 520)
    BRUSH_PANEL_WIDTH_RANGE = (240, 560)

    _layers_width = 340
    _display_width = 250
    _brush_panel_width = 300

    # Layer row metrics. The numbers live in theme.py at 1x like every other
    # measure; the row carries every per-layer control, laid out as:
    # visibility, content, mask, name, opacity, blend.
    ROW_HEIGHT = theme.LIST_ROW
    ROW_PADDING = theme.LIST_ROW_PAD
    ROW_GAP = theme.LIST_ROW_GAP
    # An eye, not a checkbox, so it wants the room an icon needs to read.
    TOGGLE_SIZE = 16
    CHIP_SIZE = 20
    OPACITY_WIDTH = 40
    BLEND_WIDTH = 78
    CONTROL_HEIGHT = 20
    # How little room the name may be left with before a right-hand control
    # is dropped instead. Blend goes first, opacity second: the blend mode is
    # the wider of the two and the rarer to be changed, and a layer whose name
    # is three elided letters is not a list any more.
    NAME_MIN = 46

    # --- the Objects / Materials panel -------------------------------------
    # Above the layer stack, in the same column, because the three answer one
    # question in order: which model, which of its materials, which layer of
    # that material. Splitting them across two edges of the window would mean
    # the eye crossing the canvas to follow its own decision.
    #
    # Its rows are shorter than a layer row: a layer row carries six controls
    # and these carry a name.
    TARGET_ROW = 22
    TARGET_LABEL = 16
    # How much of the right-hand column the panel may take before the layer
    # list starts losing rows to it. The layer list is what the window is for.
    TARGETS_SHARE = 0.45
    # And what it takes when there is nothing in it: enough for the two
    # headings and one empty row each, so the panel still says what it is.
    TARGETS_MIN_ROWS = 2

    # The properties section under the list: what the active layer's pattern
    # is edited in. Zero-height when there is nothing to edit, so the list
    # gets the whole panel back, and never more than this share of the panel
    # when it is open -- the list is what the panel is for. The section does
    # not scroll, so a spec taller than the share is clipped; every block in
    # it is collapsible, which is the way out.
    PROPERTIES_SHARE = 0.5

    # Which icon a layer row's chip shows when there is no image to show. A
    # procedural is looked up by its kind first, because "generated" is not
    # one thing any more -- a ramp and a group of hand-built nodes have
    # nothing in common to draw.
    TYPE_ICONS = {
        'PROCEDURAL': icons.gradient,
        'GROUP': icons.folder,
        'ADJUSTMENT': icons.adjustment,
    }
    KIND_ICONS = {
        'CUSTOM': icons.nodes,
    }
    # What an effect row draws in its chip. A filter is the fallback, because
    # it is the only one of the three that is not a thing of its own.
    EFFECT_ICONS = {
        'PATTERN': icons.gradient,
        'PAINT': icons.brush,
        'FILTER': icons.adjustment,
    }

    # One level of nesting, and the disclosure triangle that opens a group.
    # The triangle is only drawn on groups, so an ordinary row loses no width
    # to it; a child is indented by the same amount, which lines it up under
    # the group that owns it.
    INDENT = theme.LIST_INDENT
    TWISTY_SIZE = 10

    # How far the pointer travels for a full 0..1 sweep of an in-row value.
    VALUE_DRAG_RANGE = 160

    # Square side of the icon buttons, unscaled.
    ICON_BUTTON = 26

    # The header buttons: square, like the icon buttons.
    HEADER_BUTTON = 26

    # Wide enough for "Shader Graph" with room around it, at 1x.
    GRAPH_BUTTON = 96

    # "Display", beside it. Narrower because the word is.
    DISPLAY_BUTTON = 66

    # Mirroring axes, in the order Blender lists them.
    SYMMETRY_AXES = ('X', 'Y', 'Z')
    SYMMETRY_BUTTON = 28

    # The brush strip, unscaled. Blender's tool header is one widget tall and
    # so is this; the widths are what each control needs to stay readable.
    BRUSH_BAR_HEIGHT = 30
    BRUSH_PICKER_WIDTH = 132
    BRUSH_BLEND_WIDTH = 104
    BRUSH_SWATCH = 22
    BRUSH_SLIDER_WIDTH = 156
    BRUSH_PRESSURE_WIDTH = 22

    # The tool row, under the settings strip. Square icon buttons, left
    # aligned, in the order a texture gets built: the brush first, then the
    # fills from the smallest region a click can name to the largest.
    TOOL_BAR_HEIGHT = 30
    TOOL_BUTTON = 26
    # Kept apart from TOOLS: a bare function in a class body becomes a method,
    # and an icon is neither bound nor called with self.
    TOOL_ICONS = {
        'BRUSH': icons.brush,
        'POLY_FILL': icons.polygon_fill,
        'MESH_FILL': icons.mesh_fill,
        'UV_FILL': icons.uv_island_fill,
    }
    TOOLS = (
        ('BRUSH', "Brush", "Paint with the brush"),
        ('POLY_FILL', "Polygon Fill", "Fill the polygon under the pointer"),
        ('MESH_FILL', "Mesh Fill", "Fill the mesh part under the pointer"),
        ('UV_FILL', "UV Island Fill", "Fill the UV island under the pointer"),
    )

    # The fill strip. It stands in the same band as the brush strip -- only
    # one of the two is ever laid out -- so the metrics match.
    FILL_SCOPE_WIDTH = 104
    FILL_BLEND_WIDTH = 104
    FILL_SLIDER_WIDTH = 156
    FILL_MODE_WIDTH = 56
    FILL_OPTIONS_WIDTH = 74

    # The popovers Blender keeps in its tool header. Wide enough for a
    # one-word name plus its chevron; anything longer says so below.
    BRUSH_POPOVER_WIDTH = 74
    # A button wide enough for its own name. "Texture Mask" does not fit the
    # standard one, and a popover elided down to "Textur..." beside another
    # one called "Texture" names nothing.
    BRUSH_POPOVER_WIDTHS = {'MASK_TEXTURE': 104}
    # Wider than a popover: it has no chevron to leave room for, and
    # "Properties" is a longer word than any of them.
    BRUSH_PROPERTIES_WIDTH = 90
    BRUSH_POPOVERS = (
        ('BRUSH', "Brush"),
        # Blender's tool header puts the two texture popovers straight after
        # the brush: what a stroke stamps and what masks it belong beside
        # what it is. Same order here.
        ('TEXTURE', "Texture"),
        ('MASK_TEXTURE', "Texture Mask"),
        ('STROKE', "Stroke"),
        ('FALLOFF', "Falloff"),
        ('CURSOR', "Cursor"),
    )

    # Options is not one of them any more. Its own popover held six settings
    # and shut on the first click outside it; the button now opens the brush
    # properties panel, where those six are a section and every other popover
    # is one too.
    BRUSH_PROPERTIES_ID = 'OPTIONS'

    # How long the pointer must rest before a tooltip appears, in seconds.
    TOOLTIP_DELAY = 0.5

    # What each control inside a layer row is for. Rows are not widgets, so
    # they cannot carry a `tooltip` of their own.
    ROW_TOOLTIPS = {
        'TOGGLE': "Show or hide this layer",
        'MASK': "Select the mask - filters and the brush go here",
        'NAME': "Select the layer - double-click to rename",
        'OPACITY': "Opacity - drag sideways",
        'BLEND': "Blend mode",
    }

    def __init__(self, on_close=None):
        super().__init__("root")
        self.on_close = on_close
        self._ui_scale = 1.0
        self.needs_redraw = True
        self.debug = False

        # Widget currently holding the mouse (a drag in progress), and the
        # widget drawing an overlay on top of everything (an open dropdown).
        self._captured = None
        self._hovered = None
        # Which tool the row has selected. It decides which settings strip is
        # laid out, so it is read by `on_resize` rather than acted on directly.
        self.active_tool = 'BRUSH'
        # Overlays, bottom-first. Only the top one takes input; all of them
        # draw. See `set_overlay`.
        self._overlays = []
        self._last_mouse = (0, 0)
        self._last_event_repr = ""


        # Injected by window.py; the toolkit never touches bpy itself.
        self.on_layer_select = None
        self.on_layer_toggle = None
        self.on_layer_add = None
        self.on_layer_remove = None
        self.on_layer_group_selected = None
        self.on_layer_solo = None
        self.on_layer_tag = None
        # (layer_index, effect_index, on_mask) -- the effect stack.
        self.on_effect_toggle = None
        self.on_effect_select = None
        self.on_effect_blend = None
        self.on_effect_opacity = None
        self.on_effect_add = None
        self.on_effect_remove = None
        self.on_effect_move = None
        self.on_mask_bake = None
        self.on_fill_undo = None
        # (side, width) -- a panel was resized by dragging its edge.
        self.on_panel_width = None
        self.on_layer_move = None
        self.on_layer_reorder = None
        self.on_layer_rename = None
        self.on_brush_select = None
        self.on_brush_blend = None
        self.on_brush_color = None
        self.on_brush_secondary_color = None
        self.on_brush_swap = None
        self.on_brush_size = None
        self.on_brush_strength = None
        self.on_brush_pressure = None
        # (owner, key, value) and (action, *args) for the popovers, and for
        # the brush properties panel -- one pair of callbacks, because both
        # draw the same rows.
        self.on_brush_prop = None
        self.on_brush_action = None
        # (is_open,) -- the brush properties panel's own switch.
        self.on_brush_panel_toggle = None
        self.on_tool_change = None
        # (key, value) against scene.paintr2.fill
        self.on_fill_prop = None
        self.on_symmetry_toggle = None
        self.on_symmetry_plane_toggle = None
        self.on_wireframe_toggle = None
        # The per-layer ones take the row index first: they act on the row that
        # was clicked, which is not necessarily the active layer.
        self.on_blend_change = None
        self.on_opacity_change = None
        self.on_mask_add = None
        self.on_mask_remove = None
        self.on_mask_invert = None
        self.on_mask_toggle = None
        # True to give the active layer a procedural mask, False to take it
        # away. The kind is the caller's business, not the toolkit's.
        self.on_mask_procedural = None
        # (kind) -- add a procedural. Where it lands is not decided here: the
        # data side already knows whether a mask or a layer is the target.
        self.on_procedural_add = None
        # (kind) -- add an adjustment layer.
        self.on_adjustment_add = None
        # (open) -- show or hide the shader graph editor.
        self.on_shader_graph = None
        # (open) -- show or hide the Display panel down the left edge.
        self.on_display_toggle = None
        # (open) -- show or hide the Objects / Materials panel.
        self.on_targets_toggle = None
        # (owner, key, value) for the rows inside it. Same shape as every
        # other property panel, so the data side reuses one writer.
        self.on_display_prop = None
        self.on_display_action = None
        # () -- the export dialog's Export button. Separate from
        # `on_display_action` on purpose: every other action in that panel
        # fires the moment it is clicked, and this one is the answer to a
        # question the window asked first.
        self.on_export_run = None
        # Nothing selected: the click landed under the last row.
        self.on_layer_deselect = None
        # ([layer index], extend) -- a rubber band from the empty space
        # covered these layers.
        self.on_layer_box_select = None
        # (layer index) -- write that layer's painted mask out to a file.
        self.on_mask_export = None
        # (layer index) -- open or close a group.
        self.on_group_toggle = None
        # (owner, key, value) for the properties section under the list, where
        # `owner` names which block of the active layer is being edited.
        self.on_layer_prop = None
        self.on_layer_prop_action = None
        self.on_fill_color = None
        # (object name) and (slot index) -- the Objects / Materials panel.
        # Names and indices rather than datablocks, the same as everywhere
        # else in `gui/`: the toolkit is not allowed to hold an RNA wrapper.
        self.on_object_select = None
        self.on_material_select = None
        # Both answer whether they did anything, so an unclaimed Ctrl C or
        # Ctrl V goes on to Blender instead of being eaten here.
        self.on_layer_copy = None
        self.on_layer_paste = None

        # index -> (content texture, mask texture); refreshed every redraw.
        self._thumbnails = {}

        # What the Objects / Materials panel is showing, as plain dicts.
        self._object_rows = []
        self._material_rows = []
        self._targets_height = 0
        # How many layers are on the clipboard. Pushed in, not asked for: the
        # toolkit has no clipboard and is not allowed to reach for one.
        self._clipboard_count = 0

        # Mirror planes, projected to region pixels by window.py.
        self._symmetry_shapes = ()

        self._build()
        self.attach(self)

        # Overlays are not part of the layout, so they are attached by hand
        # rather than added as children -- they still need `root` to be set.
        for widget in (
            self.picker,
            self.fill_swatch,
            self.brush_color,
            self.brush_secondary,
            self.row_blend,
            self.name_field,
            self.menu,
            self.export_dialog,
        ):
            widget.attach(self, self)
        self.fill_swatch.picker = self.picker
        self.brush_color.picker = self.picker
        self.brush_secondary.picker = self.picker
        for popover in self.popovers.values():
            popover.panel.swatch.picker = self.picker
        self.fill_options.panel.swatch.picker = self.picker
        self.properties.swatch.picker = self.picker
        self.display_properties.swatch.picker = self.picker
        self.export_dialog.panel.swatch.picker = self.picker

    # --- construction -----------------------------------------------------

    def _build(self):
        self.clear()

        self.header = self.add(Box('BG_HEADER', "header"))
        self.title = self.add(Label("PaintR2", theme.FONT_SIZE_TITLE, name="title"))
        self.close_button = self.add(
            Button("Close", self._on_close_clicked, name="close")
        )
        self.close_button.tooltip = "Close the PaintR2 window"

        # The one piece of Blender's own interface this window ever shows, and
        # it is not shown *inside* our region -- it gets an editor of its own
        # beside us (the rule is about our region, not about the window). A shader
        # graph cannot be redrawn in our toolkit and should not be: it is where
        # a Custom pattern is built, and it has to be the real thing.
        self.graph_button = self.add(
            ToggleButton(
                "Shader Graph",
                on_toggle=self._on_shader_graph_toggled,
                tooltip="Open the shader graph beside the canvas",
                name="shader_graph",
            )
        )

        # Beside the graph toggle, and for the same reason it is there: it
        # changes what the *window* shows rather than what the document is.
        # Lighting and shader mode are both "how am I looking at this", and
        # neither belongs in the layer stack.
        self.display_button = self.add(
            ToggleButton(
                "Display",
                on_toggle=self._on_display_toggled,
                tooltip="Lighting and shader options",
                name="display_toggle",
            )
        )

        # Its own button, the same panel. Baking a mesh map is bake, look,
        # change a number, bake again -- and doing that from the sidebar means
        # leaving the window on every loop.
        self.bake_button = self.add(
            ToggleButton(
                "Bake",
                on_toggle=self._on_bake_toggled,
                tooltip="Mesh maps and export",
                name="bake_toggle",
            )
        )

        # The Objects / Materials panel's switch. An icon rather than a word:
        # the header already carries three labelled buttons and a centred
        # group, and a fourth word is what starts pushing them into each
        # other on a narrow window.
        self.targets_button = self.add(
            ToggleButton(
                on_toggle=self._on_targets_toggled,
                tooltip="Show the models and materials in the paint set",
                icon=icons.cube,
                name="targets_toggle",
            )
        )
        # Pressed to begin with, because the panel is shown to begin with.
        # A button whose look disagrees with what is on screen is worse than
        # no button: it says the panel is closed while it is open.
        self.targets_button.active = True

        # Between the viewport and the layer stack: the brush, described in
        # full. The strip above keeps its popovers -- open, change one thing,
        # close -- and this is the other way of working, left open while
        # painting. Both are fed the same rows.
        self.brush_panel = self.add(Panel("Brush", 'BG_PANEL', "brush_panel"))
        self.brush_panel.visible = False
        self.brush_panel_rows = PropertyPanel(name="brush_panel_rows")
        self.brush_panel_rows.background = "BOX_INNER"
        self.brush_panel_rows.on_change = self._on_brush_prop
        self.brush_panel_rows.on_action = self._on_brush_action
        self.brush_panel.add(self.brush_panel_rows, height=None)

        # Down the left edge, opposite the layer stack. A panel rather than a
        # popover because these are settings you leave open and keep turning
        # while you look at the model -- a popover that shuts on the first
        # click outside it would be closed for every stroke.
        self.display = self.add(Panel("Display", 'BG_PANEL', "display"))
        self.display.visible = False
        self.display_properties = PropertyPanel(name="display_properties")
        self.display_properties.background = "BOX_INNER"
        self.display_properties.on_change = self._on_display_prop
        self.display_properties.on_action = self._on_display_action
        self.display.add(self.display_properties, height=None)

        # The export dialog. Its rows are scene properties like any other, so
        # they write through the same channel the Display panel's do -- what
        # makes it a dialog is that nothing happens until Export is pressed.
        self.export_dialog = Dialog(name="export_dialog")
        self.export_dialog.title = "Export Textures"
        self.export_dialog.confirm_text = "Export"
        self.export_dialog.panel.background = "BOX_INNER"
        self.export_dialog.panel.on_change = self._on_display_prop
        self.export_dialog.panel.on_action = self._on_display_action
        self.export_dialog.on_confirm = self._on_export_confirm

        # Mirroring, centred in the header. The axes write the mesh's own
        # `use_mesh_mirror_*` -- the very property Blender's Symmetry panel
        # writes -- so the two always agree. The plane button is ours alone:
        # Blender has no equivalent for drawing the mirror planes on the model.
        self.symmetry_label = self.add(
            Label("Mirror", theme.FONT_SIZE_SMALL, 'WIDGET_TEXT_DIM',
                  align=Label.ALIGN_RIGHT, name="symmetry_label")
        )

        self.symmetry_row = self.add(Row(joined=True, name="symmetry_row"))
        self.symmetry_buttons = {}
        for axis in self.SYMMETRY_AXES:
            button = self.symmetry_row.add(
                ToggleButton(
                    axis,
                    on_toggle=self._make_symmetry_handler(axis),
                    tooltip=f"Mirror strokes across {axis}",
                    name=f"symmetry_{axis.lower()}",
                )
            )
            self.symmetry_buttons[axis] = button

        self.plane_button = self.add(
            ToggleButton(
                on_toggle=self._on_symmetry_plane_toggled,
                tooltip="Show the mirror planes on the model",
                icon=icons.mirror,
                name="symmetry_plane",
            )
        )

        # The viewport's own Wireframe overlay, next to the mirror controls
        # because it is the other thing in this header that draws on the model
        # rather than changing it. Bound to the same property Blender's
        # Overlays popover writes, so the two always agree.
        self.wire_button = self.add(
            ToggleButton(
                on_toggle=self._on_wireframe_toggled,
                tooltip="Show the mesh wireframe. Turned on by itself when a "
                        "fill tool is picked, and off again when it is not",
                icon=icons.wireframe,
                name="wireframe",
            )
        )

        # --- the brush strip ------------------------------------------------
        # Blender's tool header is hidden, so this is the whole brush
        # interface: the picker, the blend mode, the colours and the two values
        # a stroke is made of. Every control writes the property Blender writes
        # -- through `paint.brush_owner`, because unified paint settings
        # override the brush for colour and size.
        self.brush_bar = self.add(Box('BG_PANEL', "brush_bar"))

        self.brush_picker = self.add(
            Dropdown(on_change=self._on_brush_selected, name="brush_picker")
        )
        self.brush_picker.popup_width = self.BRUSH_PICKER_WIDTH
        self.brush_picker.tooltip = "Brush"

        self.brush_blend = self.add(
            Dropdown(on_change=self._on_brush_blend_selected, name="brush_blend")
        )
        self.brush_blend.popup_width = 130
        self.brush_blend.tooltip = "Blend mode"

        self.brush_color = self.add(
            ColorSwatch((0.8, 0.8, 0.8, 1.0), self._on_brush_color,
                        name="brush_color")
        )
        self.brush_color.tooltip = "Brush colour"
        self.brush_secondary = self.add(
            ColorSwatch((0.0, 0.0, 0.0, 1.0), self._on_brush_secondary,
                        name="brush_secondary")
        )
        self.brush_secondary.tooltip = "Secondary colour"
        self.brush_swap = self.add(
            IconButton(icons.swap, self._on_brush_swap,
                       "Swap the two colours  (X)", name="brush_swap")
        )

        self.brush_size = self.add(
            Slider("Size", 0.2, self._on_brush_size, name="brush_size")
        )
        self.brush_size.display = self._format_size
        self.brush_size_pressure = self.add(
            ToggleButton(
                on_toggle=lambda value: self._emit(
                    self.on_brush_pressure, 'SIZE', value),
                tooltip="Pen pressure controls size",
                icon=icons.pressure,
                name="brush_size_pressure",
            )
        )

        self.brush_strength = self.add(
            Slider("Strength", 1.0, self._on_brush_strength, name="brush_strength")
        )
        self.brush_strength.display = self._format_strength
        self.brush_strength_pressure = self.add(
            ToggleButton(
                on_toggle=lambda value: self._emit(
                    self.on_brush_pressure, 'STRENGTH', value),
                tooltip="Pen pressure controls strength",
                icon=icons.pressure,
                name="brush_strength_pressure",
            )
        )

        # Where the Options popover used to sit: the switch for the brush
        # properties panel. A toggle rather than a dropdown because the panel
        # is the kind of thing you leave open, and a button that opens
        # something which then stays open has to look pressed while it is.
        self.brush_panel_button = self.add(
            ToggleButton(
                "Properties",
                on_toggle=self._on_brush_panel_toggled,
                tooltip="Every brush setting, in a panel beside the layers",
                name="brush_panel_toggle",
            )
        )

        # Brush, Texture, Texture Mask, Stroke, Falloff and Cursor --
        # Blender's brush popovers, rebuilt from the same RNA properties.
        # Their contents are pushed in by `sync_brush_panels` on every
        # redraw.
        self.popovers = {}
        for identifier, label in self.BRUSH_POPOVERS:
            popover = self.add(
                Popover(label, name=f"popover_{identifier.lower()}")
            )
            popover.tooltip = f"{label} settings"
            popover.panel.on_change = self._on_brush_prop
            popover.panel.on_action = self._on_brush_action
            self.popovers[identifier] = popover

        # Largest brush size the slider can reach, in pixels. Blender's own
        # soft maximum; typing a bigger number elsewhere still shows correctly
        # because the slider clamps its *display*, not the value.
        self._brush_size_max = 500

        # --- the fill strip -------------------------------------------------
        # Substance Painter's polygon fill, in three tools. They share this
        # strip because at this level they differ in one thing only -- how far
        # a click reaches -- and that is the one control that changes with the
        # tool. The colour swatches are deliberately *not* rebuilt: a fill
        # paints with the brush's colour, so those three widgets are laid
        # out by both strips and never lose their value when the tool changes.
        self.fill_scope = self.add(
            Dropdown(on_change=self._on_fill_scope, name="fill_scope")
        )
        self.fill_scope.popup_width = self.FILL_SCOPE_WIDTH
        self.fill_scope.tooltip = "How far a click reaches"

        self.fill_blend = self.add(
            Dropdown(on_change=self._on_fill_blend, name="fill_blend")
        )
        self.fill_blend.popup_width = 130
        self.fill_blend.tooltip = "Blend mode"

        self.fill_alpha = self.add(
            Slider("Alpha", 1.0, self._on_fill_alpha, name="fill_alpha")
        )
        self.fill_alpha.display = self._format_strength

        # Fill / Erase, joined like the symmetry axes. Painter puts unassign on
        # the right mouse button, which in a Blender viewport already means
        # something else, so it is a visible mode here instead of a hidden one.
        self.fill_mode_row = self.add(Row(joined=True, name="fill_mode_row"))
        self.fill_mode_buttons = {}
        for identifier, label, tooltip in (
            ('ASSIGN', "Fill", "Lay the colour down"),
            ('ERASE', "Erase", "Clear the region instead of filling it"),
        ):
            button = self.fill_mode_row.add(
                ToggleButton(
                    label,
                    on_toggle=self._make_fill_mode_handler(identifier),
                    tooltip=tooltip,
                    name=f"fill_mode_{identifier.lower()}",
                )
            )
            self.fill_mode_buttons[identifier] = button
        self.fill_mode_buttons['ASSIGN'].active = True

        # Click, box or lasso. Joined for the same reason Fill/Erase is: it
        # is one control with three positions, not three switches.
        self.fill_select_row = self.add(Row(joined=True, name="fill_select_row"))
        self.fill_select_buttons = {}
        for identifier, icon, tooltip in (
            ('CLICK', icons.brush, "Click - one face, or a sweep of them"),
            ('RECT', icons.select_box, "Box - drag a rectangle over the model"),
            ('LASSO', icons.select_lasso, "Lasso - draw a shape over the model"),
        ):
            button = self.fill_select_row.add(
                ToggleButton(
                    on_toggle=self._make_fill_select_handler(identifier),
                    tooltip=tooltip,
                    icon=icon,
                    name=f"fill_select_{identifier.lower()}",
                )
            )
            self.fill_select_buttons[identifier] = button
        self.fill_select_buttons['CLICK'].active = True

        # Antialiasing and bleed. Rows rather than widgets, same as the brush
        # popovers -- the spec comes from window.py on every redraw.
        self.fill_options = self.add(
            Popover("Options", name="popover_fill_options")
        )
        self.fill_options.tooltip = "Fill options"
        self.fill_options.panel.on_change = self._on_brush_prop
        self.fill_options.panel.on_action = self._on_brush_action

        # --- the tool row ---------------------------------------------------
        # Under the settings strip, hard against the left edge. Selecting a
        # tool swaps the strip above it; nothing else in the window moves.
        self.tool_bar = self.add(Box('BG_PANEL', "tool_bar"))
        self.tool_row = self.add(Row(joined=True, name="tool_row"))
        self.tool_buttons = {}
        for identifier, label, tooltip in self.TOOLS:
            button = self.tool_row.add(
                ToggleButton(
                    on_toggle=self._make_tool_handler(identifier),
                    tooltip=f"{label} - {tooltip}",
                    icon=self.TOOL_ICONS[identifier],
                    name=f"tool_{identifier.lower()}",
                )
            )
            self.tool_buttons[identifier] = button
        self.tool_buttons['BRUSH'].active = True

        # Not a widget that draws content: a gap left for the live viewport.
        self.viewport = self.add(ViewportArea("viewport"))

        # --- Objects / Materials --------------------------------------------
        # Two lists in one panel because they are one decision read downwards:
        # the model, then the texture set on it. The material list only ever
        # shows the selected model's slots -- another model's materials are
        # not a thing you can act on from here, and listing them would be
        # offering a click that does nothing.
        self.targets = self.add(
            Panel("Objects / Materials", 'BG_PANEL', "targets")
        )

        self.object_label = Label(
            "Models", theme.FONT_SIZE_SMALL, 'WIDGET_TEXT_DIM',
            name="object_label",
        )
        self.targets.add(self.object_label, height=self.TARGET_LABEL)

        self.object_list = ListView(
            row_height=self.TARGET_ROW, name="object_list"
        )
        self.object_list.background = "BOX_INNER"
        self.object_list.empty_text = "No models - run Set Models"
        self.object_list.draw_row = self._draw_object_row
        self.object_list.on_activate = self._on_object_activate
        self.targets.add(self.object_list, height=None)
        self._object_list_index = len(self.targets.child_heights) - 1

        self.material_label = Label(
            "Materials", theme.FONT_SIZE_SMALL, 'WIDGET_TEXT_DIM',
            name="material_label",
        )
        self.targets.add(self.material_label, height=self.TARGET_LABEL)

        self.material_list = ListView(
            row_height=self.TARGET_ROW, name="material_list"
        )
        self.material_list.background = "BOX_INNER"
        self.material_list.empty_text = "No materials"
        self.material_list.draw_row = self._draw_material_row
        self.material_list.on_activate = self._on_material_activate
        self.targets.add(self.material_list, height=None)
        self._material_list_index = len(self.targets.child_heights) - 1

        self.layers = self.add(Panel("Layers", 'BG_PANEL', "layers"))

        # Creating things at the top, acting on the order at the bottom. Fixed
        # width, so a fourth button (a node layer, say) will not shrink the
        # ones already there.
        create_row = Row(item_width=self.ICON_BUTTON, name="create_buttons")
        create_row.add(
            IconButton(
                icons.fill_bucket,
                lambda _b: self._emit(self.on_layer_add, 'FILL'),
                "Add a fill layer",
                name="add_fill",
            )
        )
        create_row.add(
            IconButton(
                icons.brush,
                lambda _b: self._emit(self.on_layer_add, 'PAINT'),
                "Add a paint layer",
                name="add_paint",
            )
        )
        # Not a button that adds one thing: a procedural is a family, and
        # which member is being added is a choice, not a default with the rest
        # hidden behind a preference.
        self.procedural_button = create_row.add(
            IconButton(
                icons.gradient,
                self._on_procedural_clicked,
                "Add a procedural layer",
                name="add_procedural",
            )
        )
        self.adjustment_button = create_row.add(
            IconButton(
                icons.adjustment,
                self._on_adjustment_clicked,
                "Add an adjustment layer",
                name="add_adjustment",
            )
        )
        # With several layers selected the folder button folds them, the way
        # every layer stack does it. One button, because "make a group" and
        # "make a group out of these" are the same intent and the selection
        # already says which one is meant.
        self.group_button = create_row.add(
            IconButton(
                icons.folder,
                self._on_group_clicked,
                "Add a group",
                name="add_group",
            )
        )
        # Filters and patterns, stacked inside the active layer. Its own
        # button because it is not "add a layer" -- it changes the layer that
        # is already there, and a menu is the only honest shape for it: there
        # are seven filters and four patterns.
        self.effect_button = create_row.add(
            IconButton(
                icons.effects,
                self._on_effect_clicked,
                "Add a filter to this layer, or to its mask",
                name="add_effect",
            )
        )

        self.mask_button = create_row.add(
            IconButton(
                icons.mask_square,
                self._on_mask_clicked,
                "Mask options for the active layer",
                name="mask",
            )
        )

        # Deleting is not creating, so it does not sit with the create buttons
        # -- it sits at the other end of the same strip, where a destructive
        # action cannot be hit by a hand aiming at the one beside it.
        create_row.add(Spacer(name="create_gap"))
        self.delete_button = create_row.add(
            IconButton(
                icons.trash,
                self._on_delete_clicked,
                "Delete  (X)",
                name="delete_layer",
            )
        )
        self.create_row = create_row
        self.layers.add(create_row, height=self.ICON_BUTTON)

        self.layer_list = ListView(row_height=self.ROW_HEIGHT, name="layer_list")
        self.layer_list.empty_text = "No layers"
        self.layer_list.draw_row = self._draw_layer_row
        self.layer_list.on_activate = self._on_layer_activate
        self.layer_list.on_row_click = self._on_layer_row_click
        self.layer_list.on_row_drag = self._on_layer_row_drag
        self.layer_list.on_row_double_click = self._on_layer_row_double_click
        self.layer_list.on_reorder = self._on_layer_reorder
        self.layer_list.on_empty_click = self._on_layer_empty_click
        self.layer_list.on_box_select = self._on_layer_box_select
        self.layer_list.on_spring_open = self._on_layer_spring_open
        self.layer_list.drop_offset = self._drop_offset
        self.layer_list.drop_accepts = self._drop_accepts
        self.layer_list.drop_refuses = self._drop_refuses
        self.layer_list.on_row_context = self._on_layer_row_context
        self.layer_list.drag_rows = self._dragged_rows
        self.layers.add(self.layer_list, height=None)

        # Under the list, above the order buttons: the active layer's pattern,
        # drawn from a row spec the same way the brush popovers are. It
        # is the one place a procedural is edited after it has been added.
        self.properties = PropertyPanel(name="layer_properties")
        # The same box the list is drawn into, so the two read as one panel.
        self.properties.background = "BOX_INNER"
        self.properties.on_change = self._on_layer_prop
        self.properties.on_action = self._on_layer_prop_action
        self.layers.add(self.properties, height=0)
        self._properties_index = len(self.layers.child_heights) - 1
        self._properties_height = 0

        order_row = Row(item_width=self.ICON_BUTTON, name="order_buttons")
        order_row.add(
            IconButton(
                icons.arrow_up,
                lambda _b: self._emit(self.on_layer_move, 'UP'),
                "Move the active layer up",
                name="move_up",
            )
        )
        order_row.add(
            IconButton(
                icons.arrow_down,
                lambda _b: self._emit(self.on_layer_move, 'DOWN'),
                "Move the active layer down",
                name="move_down",
            )
        )
        self.order_row = order_row
        self.layers.add(order_row, height=self.ICON_BUTTON)

        # What the viewport is looking at. In the header, not along the
        # bottom: Blender's asset shelf owns the bottom edge, and a strip of
        # ours wedged above it was drawn over and left looking clipped.
        self.status_label = self.add(
            Label(
                "Ready",
                theme.FONT_SIZE_SMALL,
                'WIDGET_TEXT_DIM',
                name="status_text",
            )
        )

        self.picker = ColorPicker()

        # --- controls that live on a layer row, not in the layout ------------
        # None of these are children of anything: they are positioned over the
        # row that was clicked and driven as overlays. That is what lets a
        # 28-pixel row carry a nineteen-item menu and a text field.

        self.row_blend = Dropdown(
            on_change=self._on_row_blend_selected, name="row_blend"
        )
        self.row_blend.popup_width = 130
        self.row_blend.visible = False

        # An off-layout swatch the colour picker can anchor to; it stands in
        # for whichever fill layer's chip was clicked.
        self.fill_swatch = ColorSwatch(
            (0.8, 0.8, 0.8, 1.0), self._on_row_fill_color, name="fill_color"
        )
        self.fill_swatch.visible = False

        self.name_field = TextField(name="layer_name")
        self.name_field.on_commit = self._on_row_name_committed

        self.menu = Menu(name="menu")

        # Which row each of the above is currently acting on.
        self._blend_row = -1
        self._fill_row = -1
        self._rename_row = -1
        self._opacity_row = -1
        self._opacity_value = 1.0

        # [(identifier, label)] for the procedural menu. Pushed in rather than
        # read: `procedural.KINDS` is RNA's business, not the toolkit's.
        self._procedural_kinds = []
        self._adjustment_kinds = []
        # Which half of the active layer is selected: 'NONE', 'COLOR' or
        # 'MASK'. Mirrored here so the buttons can answer for themselves
        # without reaching into a row dict every time they are drawn.
        self._active_part = 'NONE'
        # Blend modes, and the subset of them that means anything to a single
        # number. Both pushed in from the data side for the same reason.
        self._blend_items = []
        self._scalar_blend = set()
        # What the procedural button is about to do, so the menu can say so.

        # Filled by sync_layers; the menus are built from these rather than
        # from RNA, which the toolkit is not allowed to touch.
        self._active_exists = False
        self._active_has_mask = False
        self._active_has_paint_mask = False
        self._active_has_proc_mask = False
        self._active_mask_on = False
        self._active_is_group = False
        self._active_has_children = False
        # More than one row selected. Nothing that edits a layer runs while it
        # is true; the buttons that would are disabled rather than silent.
        self._multi_select = False
        # The shortcut sheet: an overlay the F1 binding raises. Nothing
        # else in the window announces that any of these keys exist.
        self.shortcuts_visible = False
        self._key_actions = self._build_key_actions()
        self._selected_count = 0
        self._selection_has_group = False
        # Which row a context menu was opened over.
        self._context_row = -1
        # Which half of the effect menu is being offered.
        self._effect_on_mask = False
        # The panel edge being dragged, and the one merely hovered.
        self._splitter = None
        self._over_splitter = None
        # Which page the left panel is showing.
        self._left_page = 'DISPLAY'
        # Whether the Objects / Materials panel is switched on, mirrored from
        # RNA so the button can answer for itself.
        self._show_targets = True
        # The box or lasso being drawn over the model, or None.
        self._fill_shape = None
        self._fill_shape_filled = False

        # Tooltip state: what the pointer is resting on and since when.
        self._hover_since = 0.0
        self._tooltip_text = ""

    def _emit(self, callback, *args):
        """Call the owner's callback if it is wired. True when it was.

        The answer matters for keys: a binding whose callback is missing must
        not be reported as handled, or the window swallows the key and Blender
        never sees it.
        """
        if callback is None:
            return False
        callback(*args)
        return True

    def _on_close_clicked(self, _button):
        self._emit(self.on_close)

    def _on_mask_clicked(self, button):
        """Mask options for the active layer, as a menu.

        Deliberately not a toggle. A second click used to delete the mask,
        which is a lot of work to throw away by accident, and inverting is what
        you actually want when a mask is the wrong way round. Deleting lives
        in the X menu with the other destructive things.
        """
        if not self._active_exists:
            return

        items = []
        if not self._active_has_paint_mask:
            items.append(('WHITE', "Add White Mask"))
            items.append(('BLACK', "Add Black Mask"))
        else:
            items.append(('INVERT', "Invert Mask"))

        # A pattern *on* the mask is a Procedural effect now -- added from the
        # Effect menu, where every other pattern is added, and stacked with
        # the rest. So there is no "Add Gradient Mask" entry here any more.
        # The two entries that act on one already in the file stay: a document
        # saved before the effect stack existed still carries one, and it has
        # to be bakeable and removable.
        if self._active_has_proc_mask:
            # Baking is what a pattern is for once it is right: it stops being
            # editable and starts being paintable, which is the only way to
            # touch it up by hand.
            items.append(('PROC_BAKE', "Bake Pattern to Mask"))
            items.append(('PROC_REMOVE', "Remove Gradient Mask"))

        if self._active_has_mask:
            items.append(
                ('DISABLE' if self._active_mask_on else 'ENABLE',
                 "Disable Mask" if self._active_mask_on else "Enable Mask")
            )

        self.menu.open_at(
            button.x, button.y, items, self._on_mask_choice, title="Mask"
        )

    def _on_shader_graph_toggled(self, value):
        self._emit(self.on_shader_graph, bool(value))

    def _on_targets_toggled(self, value):
        self._emit(self.on_targets_toggle, value)

    def _on_display_toggled(self, value):
        # Pressing Display while the Bake page is up turns the page rather
        # than closing the panel -- the button is "show me this", not a
        # second switch for the same panel.
        if value or self._left_page != 'DISPLAY':
            self._emit(self.on_display_toggle, True, 'DISPLAY')
        else:
            self._emit(self.on_display_toggle, False, 'DISPLAY')

    def _on_bake_toggled(self, value):
        if value or self._left_page != 'BAKE':
            self._emit(self.on_display_toggle, True, 'BAKE')
        else:
            self._emit(self.on_display_toggle, False, 'BAKE')

    def _on_display_prop(self, owner, key, value):
        self._emit(self.on_display_prop, owner, key, value)

    def _on_display_action(self, action, *args):
        # The one action in this panel that asks before it acts. Everything a
        # bake is about -- the folder, the size, the format, the channels --
        # used to live in a collapsed sub-panel the button did not mention,
        # and pressing it wrote six files somewhere without saying so.
        if action == 'bake_export':
            self.open_export_dialog()
            return
        self._emit(self.on_display_action, action, *args)

    def open_export_dialog(self):
        self.export_dialog.open_popup()

    def _on_export_confirm(self):
        self._emit(self.on_export_run)

    def sync_export_dialog(self, rows, footnote="", can_export=True):
        """Push the dialog's rows in. Rebuilt from RNA every redraw, like the
        panels -- but only while it is up, because building them costs a walk
        of every paint model and nothing shows it the rest of the time."""
        dialog = self.export_dialog
        rows = list(rows or ())
        if rows != dialog.panel.rows:
            dialog.panel.set_rows(rows)
            self.needs_redraw = True
        if footnote != dialog.footnote:
            dialog.footnote = footnote
            self.needs_redraw = True
        if bool(can_export) != dialog.confirm_enabled:
            dialog.confirm_enabled = bool(can_export)
            self.needs_redraw = True

    @property
    def export_dialog_open(self):
        return self.export_dialog.open

    def sync_display(self, rows):
        """Push the Display panel's rows in. Rebuilt from RNA every redraw."""
        rows = list(rows or ())
        if rows != self.display_properties.rows:
            self.display_properties.set_rows(rows)
            self.needs_redraw = True

    def sync_shader_graph(self, is_open):
        """Mirror whether the shader graph editor is actually there.

        Read back rather than remembered: the editor is a Blender area and can
        be closed with Blender's own controls, and a toggle still lit after
        that would be lying about the state of the window.
        """
        if self.graph_button.active != bool(is_open):
            self.graph_button.active = bool(is_open)
            self.needs_redraw = True

    def set_procedural_kinds(self, kinds):
        """The pattern registry, as the menu the button opens."""
        kinds = list(kinds)
        if kinds != self._procedural_kinds:
            self._procedural_kinds = kinds
            self.procedural_button.tooltip = self._procedural_tooltip()
            self.needs_redraw = True

    def _on_procedural_clicked(self, button):
        """Which pattern. A menu, because a pattern is a family.

        This was briefly a plain "add one and pick it afterwards" button, on
        the reasoning that a choice belongs beside the parameters it
        changes. That reasoning holds for a *filter*, where the parameters are
        all that differ. It does not hold here: Gradient, Noise, Checker and
        Custom are four different things to make, not four settings of one,
        and the button that makes them has to say so before it makes one.
        """
        if not self._procedural_kinds:
            self._emit(self.on_procedural_add, "")
            return

        self.menu.open_at(
            button.x, button.y, list(self._procedural_kinds),
            self._on_procedural_choice, title="Procedural Layer", above=True,
        )

    def _on_procedural_choice(self, identifier):
        self._emit(self.on_procedural_add, identifier)

    def set_adjustment_kinds(self, kinds):
        """The adjustment registry, as the menu the button opens."""
        kinds = list(kinds)
        if kinds != self._adjustment_kinds:
            self._adjustment_kinds = kinds
            self.adjustment_button.tooltip = (
                f"Add an adjustment layer ({len(kinds)} kinds)"
                if kinds else "Add an adjustment layer"
            )
            self.needs_redraw = True

    def _procedural_tooltip(self):
        count = len(self._procedural_kinds)
        return (f"Add a procedural layer ({count} patterns)"
                if count else "Add a procedural layer")

    def _on_adjustment_clicked(self, button):
        """Which adjustment. A menu, for the same reason patterns get one.

        An adjustment layer is a filter that behaves like a layer: it changes
        everything under it, inside whatever group it sits in. Which filter is
        what the layer *is*, and naming it is how the layer gets its name --
        an "Adjustment" that turns out to be a hue shift is a row you have to
        open to identify.
        """
        if not self._adjustment_kinds:
            self._emit(self.on_adjustment_add, "")
            return

        self.menu.open_at(
            button.x, button.y, list(self._adjustment_kinds),
            self._on_adjustment_choice, title="Adjustment Layer", above=True,
        )

    def _on_adjustment_choice(self, identifier):
        self._emit(self.on_adjustment_add, identifier)

    def _on_layer_prop(self, owner, key, value):
        self._emit(self.on_layer_prop, owner, key, value)

    def _on_layer_prop_action(self, action, *args):
        self._emit(self.on_layer_prop_action, action, *args)

    def sync_properties(self, rows):
        """Show `rows` under the list, and give the section the room it needs.

        An empty spec collapses the section to nothing rather than leaving a
        titled void: the list is what the panel is for, and the properties are
        a guest in it.
        """
        rows = list(rows or ())
        if rows != self.properties.rows:
            self.properties.set_rows(rows)
            self.needs_redraw = True

        wanted = 0
        if rows:
            scale = max(0.01, self._ui_scale)
            needed = int(math.ceil(self.properties.content_height() / scale))
            share = int(self.layers.height / scale * self.PROPERTIES_SHARE)
            wanted = max(0, min(needed, share))

        if wanted != self._properties_height:
            self._properties_height = wanted
            self.layers.child_heights[self._properties_index] = wanted
            # Only this panel re-flows; `update()` re-lays the whole tree out
            # and it only does that when the region itself changed.
            self.layers.on_resize()
            self.needs_redraw = True

    def _on_mask_choice(self, identifier):
        if identifier in ('WHITE', 'BLACK'):
            self._emit(self.on_mask_add, identifier)
        elif identifier == 'PROC_ADD':
            self._emit(self.on_mask_procedural, True)
        elif identifier == 'PROC_REMOVE':
            self._emit(self.on_mask_procedural, False)
        elif identifier == 'PROC_BAKE':
            self._emit(self.on_mask_bake)
        elif identifier == 'INVERT':
            self._emit(self.on_mask_invert)
        else:
            self._emit(self.on_mask_toggle)

    def _effect_title(self):
        """What the Effect menu is about to act on. Named, so it is checkable."""
        if self._active_part == 'MASK':
            return "Add to the Mask"
        if self._active_part == 'COLOR':
            return "Add to the Color"
        return "Add Effect"

    def _on_effect_clicked(self, button):
        """What can go inside the selected half of this layer.

        Two entries, and neither of them asks *where*. Where is the half of
        the layer with the border round its chip, which the user selected by
        clicking it -- asking again would be asking a question already
        answered on screen.

        * **Filter** transforms what is in that half. Which filter is chosen
          afterwards, in the properties: the seven differ only in their
          parameters, so the choice belongs beside them.
        * **Paint** is an image of its own, brushed by hand. Nothing to
          choose: it is one thing, and what it becomes is painted.
        * **Procedural** generates a pattern and blends it in. That one *is*
          asked up front, for the same reason the Procedural Layer button asks
          -- four different things to make, not four settings of one.
        """
        if not self._active_exists or self._active_part == 'NONE':
            return

        items = [('FILTER', "Filter"), ('PAINT', "Paint")]
        if self._procedural_kinds:
            items.append(('PATTERN', "Procedural..."))

        self.menu.open_at(
            button.x, button.y, items, self._on_effect_choice,
            title=self._effect_title(), above=True,
        )

    def _on_effect_choice(self, identifier):
        if identifier in ('FILTER', 'PAINT'):
            self._emit(self.on_effect_add, identifier, "")
            return

        if identifier != 'PATTERN':
            return

        # A second menu rather than a submenu, the same shape the colour tags
        # use: one level of nesting is all this needs, and a submenu that
        # opens sideways has edges to get right that a reopened menu does not.
        self.menu.open_at(
            self.effect_button.x, self.effect_button.y,
            list(self._procedural_kinds), self._on_effect_pattern_choice,
            title=self._effect_title(), above=True,
        )

    def _on_effect_pattern_choice(self, identifier):
        self._emit(self.on_effect_add, 'PATTERN', identifier)

    def _on_group_clicked(self, _button):
        if self._multi_select:
            self._emit(self.on_layer_group_selected)
        else:
            self._emit(self.on_layer_add, 'GROUP')

    def _on_delete_clicked(self, button):
        # The button lives at the bottom of the panel, so the menu grows up
        # from its top edge rather than off the bottom of the window.
        self._open_delete_menu(button.x, button.y + button.height, above=True)

    def _on_layer_row_context(self, index, _local_x, _local_y):
        """Right click on a row: the commands that do not fit on it.

        The row is made active first. A menu that acts on a layer other than
        the one it was opened over is the kind of thing that only shows up
        after it has already deleted the wrong one -- unless the row is part
        of the current selection, in which case the selection is what the
        commands are about and taking it apart would be wrong.
        """
        row = self._row(index)
        if row is None:
            return False

        if not row.get("selected"):
            self._on_layer_activate(index)

        self._context_row = index
        x, y, _width, height = self._row_rect(index)
        return self._open_row_menu(
            self._last_mouse[0], min(y + height, self.height)
        )

    def _open_row_menu(self, x, y):
        row = self._row(self._context_row)
        if row is None:
            return False

        multi = self._multi_select
        items = []
        if not multi:
            items.append(('RENAME', "Rename"))
        items.append(('SOLO', "Solo Layer"))
        if row.get("has_mask") and not multi:
            items.append(('SOLO_MASK', "Solo Mask"))
            items.append(('MASK_TOGGLE',
                          "Enable Mask" if not row.get("mask_on")
                          else "Disable Mask"))
        # Only a painted mask is a file. A generated one is a pattern in the
        # shader with no pixels to write out, so there is nothing to offer.
        if row.get("paint_mask") and not multi:
            items.append(('EXPORT_MASK', "Export Mask..."))
        if not multi and self._active_part != 'NONE':
            items.append(('EFFECT', f"Add Filter to the "
                                    f"{self._active_part.title()}"))
        items.append(('TAG', "Color Tag..."))
        if multi:
            items.append(('GROUP', f"Group {self._selected_count} Layers"))

        # Copy and paste have keys, and a key nothing on screen mentions is a
        # key nobody finds. Paste only appears when there is something to
        # paste -- a permanently greyed entry says less than no entry.
        items.append(('COPY', f"Copy {self._selected_count} Layers"
                              if multi else "Copy Layer"))
        if self._clipboard_count:
            items.append(('PASTE', f"Paste {self._clipboard_count} Layer(s)"))

        items.append(('DELETE', "Delete..."))

        return self.menu.open_at(
            x, y, items, self._on_row_menu_choice,
            title=row.get("name", ""),
        )

    def _on_row_menu_choice(self, identifier):
        index = self._context_row
        if identifier == 'RENAME':
            x, y, width, height = self._row_rect(index)
            self._on_layer_row_double_click(index, self._name_local_x(index), 0)
        elif identifier == 'SOLO':
            self._emit(self.on_layer_solo, 'LAYER', self._layer_index(index))
        elif identifier == 'SOLO_MASK':
            self._emit(self.on_layer_solo, 'MASK', self._layer_index(index))
        elif identifier == 'MASK_TOGGLE':
            self._emit(self.on_mask_toggle, self._layer_index(index))
        elif identifier == 'EXPORT_MASK':
            self._emit(self.on_mask_export, self._layer_index(index))
        elif identifier == 'GROUP':
            self._emit(self.on_layer_group_selected)
        elif identifier == 'COPY':
            self._emit(self.on_layer_copy)
        elif identifier == 'PASTE':
            self._emit(self.on_layer_paste)
        elif identifier == 'EFFECT':
            self._emit(self.on_effect_add, 'FILTER', "")
        elif identifier == 'TAG':
            # A second menu rather than a submenu: one level of nesting is
            # all this needs, and a submenu that opens sideways has edges to
            # get right that a reopened menu does not.
            self._open_tag_menu()
        elif identifier == 'DELETE':
            x, y, _width, height = self._row_rect(index)
            self._open_delete_menu(self._last_mouse[0], min(y + height, self.height))

    def _name_local_x(self, index):
        """A row-local x inside the name zone, for opening the rename field."""
        zones = self._row_zones(
            0, 0, self.layer_list.row_width(),
            max(1, self.px(self.ROW_HEIGHT)), self._row(index) or {},
        )
        return zones['NAME'][0] + 1

    def _open_tag_menu(self):
        row = self._row(self._context_row) or {}
        current = row.get("tag") or 'NONE'
        items = []
        for identifier in theme.TAG_ORDER:
            label = theme.TAG_LABELS.get(identifier, identifier)
            if identifier == current:
                label = f"{label}  *"
            items.append((identifier, label, theme.TAG_COLORS.get(identifier)))

        x, y, _width, height = self._row_rect(self._context_row)
        self.menu.open_at(
            self._last_mouse[0], min(y + height, self.height), items,
            self._on_tag_choice, title="Color Tag",
        )

    def _on_tag_choice(self, identifier):
        self._emit(self.on_layer_tag, identifier)

    def _delete_items(self):
        """What there is to delete right now, most obvious first."""
        count = self._selected_count
        if count > 1:
            # The count is the whole message: "Delete Layer" over five
            # selected layers is a lie by omission.
            items = [('LAYER', f"Delete {count} Layers, Keep Contents")
                     if self._selection_has_group else
                     ('LAYER', f"Delete {count} Layers")]
            if self._selection_has_group:
                items.append(('SUBTREE', f"Delete {count} Layers and Contents"))
        elif self._active_is_group:
            items = [('LAYER', "Delete Group, Keep Contents")]
            if self._active_has_children:
                items.append(('SUBTREE', "Delete Group and Contents"))
        else:
            items = [('LAYER', "Delete Layer")]

        if count == 1 and self._active_has_paint_mask:
            items.append(('MASK', "Delete Mask"))

        return items

    def _open_delete_menu(self, x, y, above=False):
        """Delete what there is to delete, and only ask when there is a choice.

        A menu is how you pick between things. With one thing to pick it is a
        confirmation dialog that says the same word twice, and it was on every
        single delete -- the common case being one plain layer with no mask
        and no children, where the menu has exactly one entry.

        The rule is the count, not a preference: one candidate goes straight
        through, two or more open the menu. Nothing gets deleted without being
        named, and nothing gets a dialog for a choice that is not one.
        """
        if not self._active_exists:
            return False

        # A selected effect is the smallest thing on screen and the one the
        # highlight is on. Deleting the layer under it would be deleting
        # something other than what is selected.
        if self._active_effect_row() >= 0:
            return self._emit(self.on_layer_prop_action, 'effect_remove')

        items = self._delete_items()
        if len(items) == 1:
            self._on_delete_choice(items[0][0])
            return True

        return self.menu.open_at(
            x, y, items, self._on_delete_choice, title="Delete", above=above
        )

    def _active_effect_row(self):
        """The row index of the effect being edited, or -1."""
        index = self.layer_list.active_index
        row = self._row(index)
        if row is not None and row.get("type") == 'EFFECT':
            return index
        return -1

    def _on_delete_choice(self, identifier):
        if identifier == 'LAYER':
            self._emit(self.on_layer_remove, 'KEEP')
        elif identifier == 'SUBTREE':
            self._emit(self.on_layer_remove, 'DELETE')
        else:
            self._emit(self.on_mask_remove)

    def _on_row_blend_selected(self, identifier):
        if self._blend_row < 0:
            return

        row = self._row(self._blend_row) or {}
        if row.get("type") == 'EFFECT':
            layer_index, effect_index, on_mask = self._effect_of(row)
            self._emit(self.on_effect_blend, layer_index, effect_index,
                       on_mask, identifier)
            return

        self._emit(self.on_blend_change,
                   self._layer_index(self._blend_row), identifier)

    def _on_row_fill_color(self, color):
        if self._fill_row >= 0:
            self._emit(self.on_fill_color,
                       self._layer_index(self._fill_row), color)

    def _on_row_name_committed(self, text):
        row = self._rename_row
        self._rename_row = -1
        if row >= 0:
            self._emit(self.on_layer_rename, self._layer_index(row), text)

    # --- symmetry ---------------------------------------------------------

    # --- brush strip ------------------------------------------------------

    def _format_size(self, value):
        return f"{int(round(value * self._brush_size_max))} px"

    def _format_strength(self, value):
        return f"{value:.3f}"

    def _on_brush_selected(self, identifier):
        self._emit(self.on_brush_select, identifier)

    def _on_brush_blend_selected(self, value):
        self._emit(self.on_brush_blend, value)

    def _on_brush_color(self, color):
        self._emit(self.on_brush_color, color)

    def _on_brush_secondary(self, color):
        self._emit(self.on_brush_secondary_color, color)

    def _on_brush_swap(self, _button):
        self._emit(self.on_brush_swap)

    def _on_brush_size(self, value):
        self._emit(self.on_brush_size, int(round(value * self._brush_size_max)))

    def _on_brush_strength(self, value):
        self._emit(self.on_brush_strength, value)

    # --- tools ------------------------------------------------------------

    def _make_tool_handler(self, identifier):
        """One handler per tool. The row is a radio, not four toggles.

        `ToggleButton` flips itself before calling us, so clicking the tool
        that is already selected would turn it off and leave the window with
        no tool at all. `set_tool` puts every button back where it belongs,
        which makes that click a no-op instead.
        """
        def handler(_value):
            if self.active_tool != identifier:
                self._emit(self.on_tool_change, identifier)
            self.set_tool(identifier)
        return handler

    def set_tool(self, identifier):
        """Select a tool and swap the settings strip. Never writes back."""
        if identifier not in self.tool_buttons:
            return

        for key, button in self.tool_buttons.items():
            self._set_toggle(button, key == identifier)

        if self.active_tool == identifier:
            return

        self.active_tool = identifier
        # Popovers belong to the strip that is going away; leaving one open
        # would float it over a strip that no longer has a button for it.
        self._close_strip_popovers()
        if self.width and self.height:
            self.on_resize()
        self.needs_redraw = True

    @property
    def is_fill_tool(self):
        return self.active_tool != 'BRUSH'

    def _close_strip_popovers(self):
        for popover in self.popovers.values():
            popover.close()
        self.fill_options.close()

    # --- fill -------------------------------------------------------------

    def _make_fill_select_handler(self, identifier):
        def handler(_value):
            # A radio, not a toggle: pressing the one already down changes
            # nothing rather than leaving no mode selected.
            self._emit(self.on_fill_prop, "select_mode", identifier)
        return handler

    def _make_fill_mode_handler(self, identifier):
        def handler(_value):
            for key, button in self.fill_mode_buttons.items():
                self._set_toggle(button, key == identifier)
            self._emit(self.on_fill_prop, "mode", identifier)
        return handler

    def _on_fill_scope(self, value):
        # Which property this writes depends on the tool: Polygon Fill picks
        # between a triangle and a face, Mesh Fill between a part and the whole
        # object. The strip only knows there is one scope control.
        key = "mesh_scope" if self.active_tool == 'MESH_FILL' else "polygon_scope"
        self._emit(self.on_fill_prop, key, value)

    def _on_fill_blend(self, value):
        self._emit(self.on_fill_prop, "blend", value)

    def _on_fill_alpha(self, value):
        self._emit(self.on_fill_prop, "strength", value)

    def _fill_widgets(self):
        """Everything the fill strip lays out, in layout order."""
        return (
            self.fill_scope, self.fill_select_row, self.fill_blend,
            self.brush_color, self.brush_secondary, self.brush_swap,
            self.fill_alpha, self.fill_mode_row, self.fill_options,
        )

    def sync_fill(self, info):
        """Push the fill settings into the strip. `info` is None outside paint.

        Same contract as `sync_brush`: values land on the widgets directly, so
        syncing never writes back into Blender.
        """
        available = info is not None
        info = info or {}

        for widget in self._fill_widgets():
            if widget.enabled != available:
                widget.enabled = available
                self.needs_redraw = True

        if not available:
            return

        scopes = info.get("scopes") or ()
        if list(self.fill_scope.items) != list(scopes):
            self.fill_scope.items = list(scopes)
            self.needs_redraw = True
        self._set_dropdown(self.fill_scope, info.get("scope"))

        blends = info.get("blends") or ()
        if list(self.fill_blend.items) != list(blends):
            self.fill_blend.items = list(blends)
            self.needs_redraw = True
        self._set_dropdown(self.fill_blend, info.get("blend"))

        strength = info.get("strength")
        if strength is not None:
            self._set_slider(self.fill_alpha,
                             min(1.0, max(0.0, float(strength))))

        mode = info.get("mode")
        if mode is not None:
            for key, button in self.fill_mode_buttons.items():
                self._set_toggle(button, key == mode)

        select = info.get("select")
        if select is not None:
            for key, button in self.fill_select_buttons.items():
                self._set_toggle(button, key == select)

        rows = info.get("options")
        if rows is not None:
            self.fill_options.panel.set_rows(rows, info.get("options_width"))

    def sync_brush(self, info):
        """Push Blender's brush state into the strip.

        `info` is None when there is no brush to show -- outside texture paint,
        or before a canvas exists. The strip then goes dim rather than showing
        values that belong to nothing.

        Values are written onto the widgets directly, never through their
        callbacks, so syncing never writes back into Blender.
        """
        available = info is not None
        info = info or {}

        for widget in self._brush_widgets():
            if widget.enabled != available:
                widget.enabled = available
                self.needs_redraw = True

        if not available:
            return

        items = info.get("brushes") or ()
        if list(self.brush_picker.items) != list(items):
            self.brush_picker.items = list(items)
            self.needs_redraw = True
        self._set_dropdown(self.brush_picker, info.get("brush"))

        blends = info.get("blends") or ()
        if list(self.brush_blend.items) != list(blends):
            self.brush_blend.items = list(blends)
            self.needs_redraw = True
        self._set_dropdown(self.brush_blend, info.get("blend"))

        self._set_swatch(self.brush_color, info.get("color"))
        self._set_swatch(self.brush_secondary, info.get("secondary_color"))

        maximum = int(info.get("size_max") or self._brush_size_max)
        if maximum > 0 and maximum != self._brush_size_max:
            self._brush_size_max = maximum
            self.needs_redraw = True

        size = info.get("size")
        if size is not None:
            self._set_slider(self.brush_size,
                             min(1.0, max(0.0, size / self._brush_size_max)))

        strength = info.get("strength")
        if strength is not None:
            self._set_slider(self.brush_strength,
                             min(1.0, max(0.0, float(strength))))

        self._set_toggle(self.brush_size_pressure, info.get("size_pressure"))
        self._set_toggle(self.brush_strength_pressure,
                         info.get("strength_pressure"))

    def sync_brush_panels(self, panels):
        """Push the popover contents in. `panels` is None outside paint mode.

        The rows are plain dicts rebuilt from RNA every redraw -- see
        gui/props.py. Replacing them is deliberately cheap; nothing here owns
        a value, so nothing here can go stale.
        """
        by_id = {panel["id"]: panel for panel in (panels or ())}

        for identifier, popover in self.popovers.items():
            panel = by_id.get(identifier)
            if panel is None:
                popover.panel.set_rows(())
                if popover.open:
                    popover.close()
                continue
            popover.panel.set_rows(panel.get("rows"), panel.get("width"))

    def _on_brush_panel_toggled(self, value):
        self._emit(self.on_brush_panel_toggle, bool(value))

    def sync_brush_properties(self, rows):
        """Push the brush panel's sections in, rebuilt from RNA every redraw.

        The very rows the popovers get, wrapped a level deeper -- so a value
        changed in one is the same value in the other and there is no second
        copy to drift.
        """
        rows = list(rows or ())
        if rows != self.brush_panel_rows.rows:
            self.brush_panel_rows.set_rows(rows)
            self.needs_redraw = True

    def _on_brush_prop(self, owner, key, value):
        self._emit(self.on_brush_prop, owner, key, value)

    def _on_brush_action(self, action, *args):
        self._emit(self.on_brush_action, action, *args)

    def _brush_widgets(self):
        """Everything the strip dims when there is nothing to paint with.

        In layout order, left to right -- the popovers come last because that
        is where they sit, and a regression test reads this to check the strip
        does not overlap itself.
        """
        return (
            self.brush_picker, self.brush_blend, self.brush_color,
            self.brush_secondary, self.brush_swap, self.brush_size,
            self.brush_size_pressure, self.brush_strength,
            self.brush_strength_pressure,
        ) + tuple(
            self.popovers[identifier] for identifier, _label in self.BRUSH_POPOVERS
        ) + (self.brush_panel_button,)

    def _set_dropdown(self, dropdown, value):
        if value is not None and dropdown.value != value:
            dropdown.value = value
            self.needs_redraw = True

    def _set_swatch(self, swatch, color):
        if color is None:
            return
        color = tuple(color)
        if len(color) == 3:
            color = color + (1.0,)
        if tuple(swatch.color) != color:
            swatch.color = color
            self.needs_redraw = True

    def _set_slider(self, slider, value):
        # A drag in progress owns the value: overwriting it every frame from
        # Blender makes the handle stutter under the pointer.
        if slider.dragging:
            return
        if abs(slider.value - value) > 1e-4:
            slider.value = value
            self.needs_redraw = True

    def _set_toggle(self, toggle, value):
        if value is None:
            return
        if toggle.active != bool(value):
            toggle.active = bool(value)
            self.needs_redraw = True

    def _make_symmetry_handler(self, axis):
        """One handler per axis, so the toggle carries which axis it is."""
        def handler(value):
            self._emit(self.on_symmetry_toggle, axis, value)
        return handler

    def _on_symmetry_plane_toggled(self, value):
        self._emit(self.on_symmetry_plane_toggle, value)

    def _on_wireframe_toggled(self, value):
        self._emit(self.on_wireframe_toggle, value)

    def sync_wireframe(self, enabled):
        """Follow the viewport's Wireframe overlay, whoever last set it."""
        if enabled is None:
            return
        self._set_toggle(self.wire_button, enabled)

    def set_symmetry_shapes(self, shapes):
        """The mirror planes, already projected into region pixels.

        Each shape is `{"color": rgba, "loop": [(x, y), ...]}`. Doing the
        projection outside keeps every matrix and every view calculation in
        window.py, where bpy lives -- the toolkit only ever sees flat points.
        """
        shapes = tuple(shapes or ())
        if shapes != self._symmetry_shapes:
            self._symmetry_shapes = shapes
            self.needs_redraw = True

    def _draw_symmetry_shapes(self):
        """Draw the planes into the viewport gap, under all the chrome."""
        if not self._symmetry_shapes or not self.viewport.visible:
            return

        width = max(1.0, self.px(1))
        with draw.clip(
            self.viewport.x, self.viewport.y, self.viewport.width, self.viewport.height
        ):
            for shape in self._symmetry_shapes:
                loop = shape["loop"]
                if len(loop) < 3:
                    continue
                colour = shape["color"]
                # A wash so the plane reads as a surface, then its edges. The
                # wash alone is unreadable against a lit model; the edges
                # alone are hard to tell from the mesh's own wireframe.
                draw.polygon(loop, colour[:3] + (0.10,), smooth=False)
                draw.polyline(loop, colour[:3] + (0.85,), width, closed=True)

    def sync_symmetry(self, flags, show_planes=None):
        """Mirror Blender's mirroring state into the header.

        `flags` maps axis name to bool, or is None when there is nothing to
        read -- no mesh object in the session. The controls then go dim rather
        than offering to mirror an object that has no mesh.

        Written straight onto `.active` rather than through the toggle, so
        syncing never fires the callback that would write it back.
        """
        changed = False
        available = flags is not None

        for axis, button in self.symmetry_buttons.items():
            state = bool(flags.get(axis)) if available else False
            if button.active != state:
                button.active = state
                changed = True
            if button.enabled != available:
                button.enabled = available
                changed = True

        if self.plane_button.enabled != available:
            self.plane_button.enabled = available
            changed = True

        if show_planes is not None and self.plane_button.active != bool(show_planes):
            self.plane_button.active = bool(show_planes)
            changed = True

        if changed:
            self.needs_redraw = True

    def sync_thumbnails(self, thumbnails):
        """Hand over GPU textures for the layer rows.

        Deliberately not part of `sync_layers`: textures are objects that can
        differ between frames, and comparing them would make the interface
        redraw forever.
        """
        self._thumbnails = thumbnails or {}

    def set_blend_items(self, items, scalar_only=()):
        """Give the blend menu its options: [(identifier, label), ...].

        `scalar_only` is the subset that means something for a single number.
        A layer writing only to Roughness, Metallic or Height gets that
        shorter list, because the rest -- Hue, Overlay, Color Burn -- have no
        float arithmetic behind them and would quietly do nothing.
        """
        self._blend_items = list(items)
        self._scalar_blend = {identifier for identifier in scalar_only}
        self.row_blend.items = self._blend_items

    def blend_label(self, identifier):
        return self.row_blend.label_for(identifier)

    def _blend_items_for(self, row):
        """The blend modes this row is allowed to offer."""
        if not row.get("scalar_only"):
            return self._blend_items
        return [
            item for item in self._blend_items if item[0] in self._scalar_blend
        ]

    # --- objects and materials --------------------------------------------

    def sync_clipboard(self, count):
        """How many layers are waiting to be pasted, for the row menu."""
        self._clipboard_count = int(count)

    def sync_targets(self, objects, materials, active_object=-1,
                     active_material=-1):
        """Feed the panel the models in the set and the active one's slots.

        Plain dicts, like every other sync: an RNA wrapper handed to the
        toolkit is a wrapper the toolkit still holds after the redraw that
        produced it, pointing at whatever now sits at that index.

        Each object row: `{"name", "materials", "no_uv", "prepared"}`.
        Each material row: `{"name", "layers", "prepared", "color"}`.
        """
        self._object_rows = list(objects)
        self._material_rows = list(materials)

        self.object_list.rows = self._object_rows
        self.object_list.active_index = active_object
        self.material_list.rows = self._material_rows
        self.material_list.active_index = active_material

        height = self._targets_wanted_height()
        if height != self._targets_height:
            self._targets_height = height
            self.on_resize()
            self.request_redraw()

    def _targets_wanted_height(self):
        """How tall the panel wants to be for what is in it, unscaled.

        Sized to its contents rather than given a fixed share: a session with
        one model and one material would otherwise spend a third of the
        column saying so, and the layer list is what the column is for.
        """
        pad = theme.PAD
        gap = theme.GAP
        title = theme.FONT_SIZE_TITLE + theme.PAD

        rows = max(self.TARGETS_MIN_ROWS, len(self._object_rows)) \
            + max(self.TARGETS_MIN_ROWS, len(self._material_rows))

        return int(
            title
            + pad * 2
            + self.TARGET_LABEL * 2
            + rows * self.TARGET_ROW
            + gap * 3
        )

    def _draw_target_row(self, x, y, width, height, active, hovered):
        """The selection and hover backing every row in both lists shares."""
        inset = self.px(theme.LIST_INSET)
        radius = theme.radius(height, self._ui_scale)
        if active:
            draw.rounded_rect(x + inset, y, width - inset * 2, height, radius,
                              theme.LIST_SEL)
        elif hovered:
            draw.rounded_rect(x + inset, y, width - inset * 2, height, radius,
                              theme.LIST_HOVER)

    def _draw_object_row(self, item, index, x, y, width, height, active,
                         hovered):
        self._draw_target_row(x, y, width, height, active, hovered)

        pad = self.px(theme.LIST_ROW_PAD)
        size = self.px(theme.FONT_SIZE)
        _, text_h = draw.text_dimensions("Ag", size)
        text_y = y + (height - text_h) // 2

        # The count on the right, the name on the left, and the name gives way
        # first: a model with eight slots is worth knowing about, and "8" is
        # two characters against a name that can be forty.
        count = str(item.get("materials", 0))
        count_w, _ = draw.text_dimensions(count, size)
        right = x + width - pad

        colour = theme.WIDGET_TEXT if active else theme.WIDGET_TEXT_DIM
        draw.text(right - count_w, text_y, count, size, theme.WIDGET_TEXT_DIM)

        name_right = right - count_w - self.px(theme.GAP)
        name = item.get("name", "")
        if item.get("no_uv"):
            # Not a warning icon: the word is the warning, and it is the one
            # thing that stops this model from being paintable at all.
            name += "  (no UV)"
            colour = theme.DANGER if not active else theme.WIDGET_TEXT

        draw.text_elided(x + pad, text_y, name, size, colour,
                         max(0, name_right - x - pad))

    def _draw_material_row(self, item, index, x, y, width, height, active,
                           hovered):
        self._draw_target_row(x, y, width, height, active, hovered)

        pad = self.px(theme.LIST_ROW_PAD)
        size = self.px(theme.FONT_SIZE)
        _, text_h = draw.text_dimensions("Ag", size)
        text_y = y + (height - text_h) // 2

        # A slot with no PaintR2 stack in it is drawn dim and says so, rather
        # than being hidden: it is a real slot on the model, and hiding it
        # would make the list disagree with the material list Blender shows.
        prepared = item.get("prepared", True)
        count = str(item.get("layers", 0)) if prepared else "-"
        count_w, _ = draw.text_dimensions(count, size)
        right = x + width - pad
        draw.text(right - count_w, text_y, count, size, theme.WIDGET_TEXT_DIM)

        chip = self.px(self.TARGET_ROW - 10)
        chip_x = x + pad
        colour = item.get("color")
        if colour is not None:
            draw.rounded_rect(
                chip_x, y + (height - chip) // 2, chip, chip,
                theme.radius(chip, self._ui_scale), tuple(colour),
            )
            draw.rounded_rect_outline(
                chip_x, y + (height - chip) // 2, chip, chip,
                theme.radius(chip, self._ui_scale), theme.OUTLINE,
                max(1, self.px(1)),
            )
            text_x = chip_x + chip + self.px(theme.LIST_CHIP_GAP + 2)
        else:
            text_x = chip_x

        if active:
            text_colour = theme.WIDGET_TEXT
        elif prepared:
            text_colour = theme.WIDGET_TEXT_DIM
        else:
            text_colour = theme.WIDGET_TEXT_DIM[:3] + (0.6,)

        draw.text_elided(
            text_x, text_y, item.get("name", ""), size, text_colour,
            max(0, right - count_w - self.px(theme.GAP) - text_x),
        )

    def _on_object_activate(self, index, extend=False, span=False,
                            local_x=None):
        if not 0 <= index < len(self._object_rows):
            return
        self._emit(self.on_object_select, self._object_rows[index]["name"])

    def _on_material_activate(self, index, extend=False, span=False,
                              local_x=None):
        if not 0 <= index < len(self._material_rows):
            return
        self._emit(self.on_material_select,
                   self._material_rows[index].get("slot", index))

    # --- layer list -------------------------------------------------------

    def sync_layers(self, rows, active_index, active=None):
        """Feed the interface a fresh snapshot of the stack.

        Called every redraw with plain dicts, never with RNA objects: those
        are transient and must not outlive the call that produced them.

        Controls the user is currently manipulating are left alone, so a drag
        is not fought by the value coming back from the data.
        """
        changed = False

        if rows != self.layer_list.rows:
            self.layer_list.rows = rows
            changed = True
        if active_index != self.layer_list.active_index:
            self.layer_list.active_index = active_index
            changed = True

        self._active_exists = active is not None
        self._active_mask_on = bool(active and active.get("mask_on"))

        # Which half is selected, and what the Effect button can therefore do.
        # A filter belongs to one half of one layer: with no layer, or with
        # the layer selected but neither of its halves, there is nowhere to
        # put one -- and a button that looks live and reports a refusal is
        # worse than one that says up front it is not available.
        # Several layers selected has no single answer to "which layer's
        # half", and an ambiguous edit is not made at all.
        several = int(active.get("selected_count", 1)) > 1 if active else False
        part = 'NONE' if several else (active.get("part") if active else 'NONE')
        part = part or 'NONE'
        if part != self._active_part:
            self._active_part = part
            changed = True

        can_add_effect = self._active_exists and part != 'NONE'
        if self.effect_button.enabled != can_add_effect:
            self.effect_button.enabled = can_add_effect
            changed = True
        wanted_tip = (
            f"Add a filter or a pattern to the {part.title()}"
            if can_add_effect
            else "Select a layer's Color or Mask to add a filter"
        )
        if self.effect_button.tooltip != wanted_tip:
            self.effect_button.tooltip = wanted_tip
            changed = True
        self._active_has_paint_mask = bool(active and active.get("paint_mask"))
        self._active_has_proc_mask = bool(active and active.get("proc_mask"))
        self._active_is_group = bool(active and active.get("is_group"))
        self._active_has_children = bool(active and active.get("has_children"))

        count = int(active.get("selected_count", 1)) if active else 0
        multi = count > 1
        if count != self._selected_count:
            self._selected_count = count
            changed = True
        if multi != self._multi_select:
            self._multi_select = multi
            self.group_button.tooltip = (
                "Put the selected layers in a group" if multi else "Add a group"
            )
            changed = True

        has_group = any(
            row.get("selected") and row.get("type") == 'GROUP' for row in rows
        ) if multi else False
        if has_group != self._selection_has_group:
            self._selection_has_group = has_group
            changed = True

        # Disabled, not merely inert: a button that looks live and does nothing
        # is worse than one that says it is not available. Adding a layer is
        # exempt: it makes a new one rather than editing any of these.
        #
        # Ordering and deleting are exempt too, and were not always: moving
        # five layers up one step and deleting five layers both mean exactly
        # one thing, so refusing them was refusing the reason to select five
        # layers in the first place. The mask button is the one that stays
        # single-layer -- five masks from one click is a guess, not a command.
        wanted = self._active_exists
        for button in (*self.order_row.children(), self.delete_button):
            if button.enabled != wanted:
                button.enabled = wanted
                changed = True

        single = self._active_exists and not multi
        if self.mask_button.enabled != single:
            self.mask_button.enabled = single
            changed = True

        has_mask = bool(active and active["has_mask"])

        if self._active_has_mask != has_mask:
            self._active_has_mask = has_mask
            self.mask_button.active = has_mask
            self.mask_button.tooltip = (
                "Mask: invert or disable" if has_mask else "Add a mask"
            )
            changed = True

        if changed:
            self.needs_redraw = True

    # --- layer row --------------------------------------------------------

    def _row_zones(self, x, y, width, height, row=None):
        """Where each control sits inside a layer row.

        Called both to draw (with the row's real rectangle) and to hit-test
        (with x=0, so the caller can compare against a row-local coordinate).
        `row` is the row's own dict: its depth decides the indent and its type
        decides whether there is a disclosure triangle to leave room for.
        """
        row = row or {}
        has_mask = bool(row.get("has_mask"))

        pad = self.px(self.ROW_PADDING)
        gap = self.px(self.ROW_GAP)
        toggle = self.px(self.TOGGLE_SIZE)
        chip = self.px(self.CHIP_SIZE)
        control = self.px(self.CONTROL_HEIGHT)

        def centered(left, size, box=None):
            return (left, y + (height - (box or size)) // 2, size, box or size)

        cursor = x + pad + self.px(self.INDENT) * int(row.get("depth", 0))

        zones = {}
        # The twisty column is reserved on *every* row, and only a group draws
        # a chevron in it. Reserved rather than added, because it used to be
        # added: a group's own controls started one twisty (13 px) in and its
        # children started one indent (14 px) in, so a layer inside a group sat
        # a single pixel to the right of the group holding it. Nothing about
        # that read as containment. With the column always present, depth is
        # the only thing that moves a row, and one level in is one indent in.
        #
        # A layer's filters have no twisty of their own -- selecting the half
        # is the disclosure now -- but they keep the column for the same
        # reason: their eye lines up under the eye of the row above.
        twisty = self.px(self.TWISTY_SIZE)
        if row.get("type") == 'GROUP':
            zones['TWISTY'] = centered(cursor, twisty)
        cursor += twisty + gap // 2

        zones['TOGGLE'] = centered(cursor, toggle)
        cursor += toggle + gap

        zones['CONTENT'] = centered(cursor, chip)
        cursor += chip

        if has_mask:
            cursor += self.px(theme.LIST_CHIP_GAP)
            zones['MASK'] = centered(cursor, chip)
            cursor += chip

        # An effect row keeps the layer row's rhythm but owns none of its
        # controls: no paint target, no mask, no colour to pick. What it does
        # own is its blend and its opacity, which sit where they always do.
        if row.get("type") == 'EFFECT':
            zones.pop('MASK', None)

        # The right-hand controls are pinned to the right edge, so a narrow
        # panel eats the name from the left. Drop them instead of squeezing:
        # blend first, then opacity. Both are still reachable -- blend from
        # the row menu, opacity from the properties section.
        name_x = cursor + gap
        name_min = self.px(self.NAME_MIN)
        right = x + width - pad
        control_y = y + (height - control) // 2

        blend_width = self.px(self.BLEND_WIDTH)
        opacity_width = self.px(self.OPACITY_WIDTH)

        if right - blend_width - gap - opacity_width - gap - name_x >= name_min:
            blend_x = right - blend_width
            zones['BLEND'] = (blend_x, control_y, blend_width, control)
            opacity_x = blend_x - gap - opacity_width
            zones['OPACITY'] = (opacity_x, control_y, opacity_width, control)
            name_right = opacity_x - gap
        elif right - opacity_width - gap - name_x >= name_min:
            opacity_x = right - opacity_width
            zones['OPACITY'] = (opacity_x, control_y, opacity_width, control)
            name_right = opacity_x - gap
        else:
            name_right = right

        zones['NAME'] = (name_x, y, max(0, name_right - name_x), height)
        return zones

    def _draw_layer_row(self, item, index, x, y, width, height, active, hovered):
        # Only selection and hover are painted, as a rounded inset -- the way
        # Blender draws a list. Rows with a background of their own would
        # square off the corners of the box they sit in, and a separator line
        # under each one is not something Blender's lists have either.
        inset = self.px(theme.LIST_INSET)
        radius = theme.radius(height, self._ui_scale)
        if active:
            draw.rounded_rect(x + inset, y, width - inset * 2, height, radius,
                              theme.LIST_SEL)
        elif item.get("selected"):
            # Selected but not the active one: the same colour, half strength.
            # One row has to read as "this is the one being edited", so the
            # others cannot use the full selection colour.
            draw.rounded_rect(x + inset, y, width - inset * 2, height, radius,
                              theme.LIST_SEL[:3] + (theme.LIST_SEL[3] * 0.45,))
        elif hovered:
            draw.rounded_rect(x + inset, y, width - inset * 2, height, radius,
                              theme.LIST_HOVER)

        # A solo is a state the whole list is in, not a mark on one row: the
        # row being looked at keeps its colour and every other one goes quiet,
        # which is what makes it obvious that something is being filtered.
        solo = item.get("solo") or ""
        if item.get("soloed_elsewhere"):
            draw.rounded_rect(x + inset, y, width - inset * 2, height, radius,
                              theme.DRAG_VEIL[:3] + (0.35,))
        elif solo:
            draw.rounded_rect_outline(
                x + inset, y, width - inset * 2, height, radius,
                theme.ACCENT, max(1, self.px(1)),
            )

        # The colour tag: a stripe down the left edge, the way the outliner
        # marks a collection. Drawn outside the row's controls rather than as
        # a chip, so a tagged row loses no width to it.
        tag = theme.TAG_COLORS.get(item.get("tag") or 'NONE')
        if tag:
            strip = self.px(theme.LIST_TAG_WIDTH)
            draw.rect(x + inset, y + self.px(2), strip,
                      height - self.px(4), tag)

        zones = self._row_zones(x, y, width, height, item)

        if item.get("type") == 'EFFECT':
            # Before the layer chrome rather than after it: an effect row
            # draws a recessed strip over the whole row, and anything drawn
            # first would only be painted over.
            self._draw_effect_row(item, zones, x, y, width, height, active)
            return

        # The tree lines. Indent alone says how deep a row is but not what it
        # is under: with two groups open, a child three rows below its header
        # is just a row that starts further in. One thin line per level of
        # nesting, running under the twisty of the group that owns it.
        depth = int(item.get("depth", 0))
        if depth:
            pad = self.px(self.ROW_PADDING)
            indent = self.px(self.INDENT)
            twisty = self.px(self.TWISTY_SIZE)
            thickness = max(1, self.px(1))
            for level in range(depth):
                draw.rect(
                    x + pad + indent * level + twisty // 2,
                    y,
                    thickness,
                    height,
                    theme.OUTLINE,
                )

        # A group opens and closes. Blender's outliner draws the same solid
        # triangle, pointing down when open and right when closed.
        if 'TWISTY' in zones:
            tx, ty, mark, _ = zones['TWISTY']
            arrow = icons.chevron_down if item.get("expanded") else icons.chevron_right
            arrow(tx, ty, mark, theme.WIDGET_TEXT_DIM)

        # Visibility, as Blender's outliner shows it: an eye, open or closed.
        tx, ty, toggle, _ = zones['TOGGLE']
        if item["visible"]:
            icons.eye_open(tx, ty, toggle, theme.WIDGET_TEXT)
        else:
            icons.eye_closed(tx, ty, toggle, theme.WIDGET_TEXT_DIM)

        # The layer's own content: a Fill layer shows its colour, a Paint
        # layer its actual pixels. Bordered when it is the selected half.
        content_texture, mask_texture = self._thumbnails.get(
            item.get("index", index), (None, None)
        )

        cx, cy, chip, _ = zones['CONTENT']

        type_icon = self.KIND_ICONS.get(item.get("kind")) or \
            self.TYPE_ICONS.get(item.get("type"))
        if item.get("color") is not None:
            draw.rect(cx, cy, chip, chip, item["color"])
        elif type_icon is not None:
            # A generated layer has no pixels to show, so the chip says what
            # kind of thing it is instead. That is also why there is no
            # separate type column: the chip already is one.
            self._draw_type_chip(cx, cy, chip, type_icon)
        else:
            self._draw_thumbnail(cx, cy, chip, content_texture)
        # The border is the selection. Not "the active layer's colour chip" --
        # the *selected half*, which is a thing a layer can be without either
        # half being picked, and which is what decides where a filter lands.
        self._chip_border(cx, cy, chip, item.get("target") == 'COLOR')
        self._draw_effect_mark(cx, cy, chip, item.get("color_effects"))

        if 'MASK' in zones:
            mx, my, _, _ = zones['MASK']
            if item.get("mask_procedural") and mask_texture is None:
                self._draw_type_chip(mx, my, chip, icons.gradient)
            else:
                self._draw_thumbnail(mx, my, chip, mask_texture)
            if not item.get("mask_on"):
                # A disabled mask still shows, dimmed, so it is not forgotten
                # -- and it carries an X, because dim on its own is what a
                # dark mask looks like anyway. Shift-clicking the chip is what
                # puts the X there and what takes it away again.
                draw.rect(mx, my, chip, chip, (0.0, 0.0, 0.0, 0.55))
                icons.cross(mx, my, chip, theme.WIDGET_TEXT)
            self._chip_border(mx, my, chip, item.get("target") == 'MASK')
            self._draw_effect_mark(mx, my, chip, item.get("mask_effects"))

        name_x, _, name_width, _ = zones['NAME']
        size = self.px(theme.FONT_SIZE)
        _, text_height = draw.text_dimensions(item["name"], size)
        if not item["visible"]:
            name_color = theme.WIDGET_TEXT_DIM
        elif active:
            name_color = theme.WIDGET_TEXT_SEL
        else:
            name_color = theme.WIDGET_TEXT
        draw.text_elided(
            name_x,
            y + (height - text_height) // 2,
            item["name"],
            size,
            name_color,
            name_width,
        )

        if 'OPACITY' in zones:
            self._draw_opacity_chip(zones['OPACITY'], item.get("opacity", 1.0))
        if 'BLEND' in zones:
            self._draw_blend_chip(zones['BLEND'], item.get("blend", 'MIX'))

    def _draw_effect_mark(self, x, y, size, count):
        """A thin green rule under a chip whose half carries filters.

        The list shows one half's filters at a time, which leaves a question
        the rows cannot answer: does the *other* half have any? This is the
        answer, and it is deliberately not a number or a badge -- it says
        "there is something here", which is all that is needed to decide
        whether to go and look.
        """
        if not count:
            return

        thickness = max(1, self.px(theme.LIST_MARK_HEIGHT))
        draw.rect(x, y - self.px(theme.LIST_MARK_GAP) - thickness,
                  size, thickness, theme.EFFECT_MARK)

    def _draw_effect_row(self, item, zones, x, y, width, height, active):
        """An effect, drawn as something *inside* a layer rather than beside it.

        Reported as looking like a separate layer under the one it belongs to,
        and it did: the same background, the same chip, the same height, one
        indent apart. What fixes that is containment rather than decoration --

        * the row sits in a **recessed strip** that starts at the indent, so
          the filters of one half read as one block;
        * an **elbow** joins it to the tree line, the way an outliner joins a
          child to its parent;
        * it is **shorter** than a layer row's controls and its text is the
          small size, so the two are never mistaken at a glance.

        The strip itself carries the selection, rather than the row behind it.
        A selected effect used to show only a border round its chip while the
        owning layer wore the full selection wash, which said the layer was
        what was being edited when it was not.
        """
        inset = self.px(theme.LIST_INSET)
        pad = self.px(self.ROW_PADDING)
        indent = self.px(self.INDENT)
        depth = int(item.get("depth", 0))

        # The strip. It starts one indent in from the owner's edge -- the
        # owner's own row begins at `pad + indent * (depth - 1)`, so this is
        # exactly one level further in, and that is what makes it read as held
        # by the row above rather than as a row of its own. It used to start
        # 6 px from the *owner's* indent, which put it to the left of the
        # owner's eye and made the filter look like the next layer down.
        left = x + pad + indent * depth
        strip_y = y + self.px(1)
        strip_h = max(1, height - self.px(2))
        radius = theme.radius(strip_h, self._ui_scale)
        selected = bool(active or item.get("active_effect"))
        draw.rounded_rect(
            left, strip_y, max(0, x + width - inset - left), strip_h, radius,
            theme.LIST_SEL if selected else theme.BOX_INNER,
        )

        # The elbow: down the parent's line, then across into the strip.
        thickness = max(1, self.px(1))
        spine = x + pad + indent * max(0, depth - 1) + self.px(self.TWISTY_SIZE) // 2
        draw.rect(spine, y + height // 2, left - spine, thickness,
                  theme.OUTLINE)

        tx, ty, toggle, _ = zones['TOGGLE']
        if item["visible"]:
            icons.eye_open(tx, ty, toggle, theme.WIDGET_TEXT_DIM)
        else:
            icons.eye_closed(tx, ty, toggle, theme.WIDGET_TEXT_DIM)

        # Smaller than a layer's chip: an effect is a smaller thing, and the
        # size difference is read before the glyph is. The glyph says what it
        # is rather than where it applies -- the half it applies to is the one
        # selected above it, and there is only ever one on screen.
        cx, cy, chip, _ = zones['CONTENT']
        small = max(self.px(8), int(chip * 0.75))
        cx += (chip - small) // 2
        cy += (chip - small) // 2

        glyph = self.EFFECT_ICONS.get(
            item.get("effect_type"), icons.adjustment
        )
        glyph(cx, cy, small,
              theme.WIDGET_TEXT_SEL if selected else theme.WIDGET_TEXT_DIM)

        name_x, _, name_width, _ = zones['NAME']
        size = self.px(theme.FONT_SIZE_SMALL)
        label = item["name"]
        _, text_height = draw.text_dimensions(label, size)
        if not item["visible"]:
            name_color = theme.WIDGET_TEXT_DIM
        elif selected:
            name_color = theme.WIDGET_TEXT_SEL
        else:
            name_color = theme.WIDGET_TEXT
        draw.text_elided(
            name_x, y + (height - text_height) // 2, label, size,
            name_color, name_width,
        )

        if 'OPACITY' in zones:
            self._draw_opacity_chip(zones['OPACITY'], item.get("opacity", 1.0))
        if 'BLEND' in zones:
            self._draw_blend_chip(zones['BLEND'], item.get("blend", 'MIX'))

    def _draw_opacity_chip(self, rect, opacity):
        """The opacity readout: a bar you drag sideways, not a label.

        Full opacity draws no bar at all. Every layer starting out at 100%
        meant a column of identical blue blocks, which said nothing; an empty
        chip makes the layers that *are* turned down the ones that stand out.
        """
        x, y, width, height = rect
        radius = theme.radius(height, self._ui_scale)

        draw.widget(x, y, width, height, radius, theme.WIDGET,
                    outline=theme.OUTLINE, emboss=theme.EMBOSS)

        filled = int(width * min(1.0, max(0.0, opacity)))
        if 0 < filled < width:
            draw.rounded_rect(x, y, filled, height, radius, theme.METER,
                              corners=draw.CORNER_LEFT)

        size = self.px(theme.FONT_SIZE_SMALL)
        draw.text_centered(
            x + width * 0.5,
            y + height * 0.5,
            f"{opacity * 100:.0f}",
            size,
            theme.WIDGET_TEXT,
        )

    def _draw_blend_chip(self, rect, blend):
        """The blend mode, on the row, as a dropdown that opens in place."""
        x, y, width, height = rect
        pad = self.px(5)
        radius = theme.radius(height, self._ui_scale)

        draw.widget(x, y, width, height, radius, theme.WIDGET,
                    outline=theme.OUTLINE, emboss=theme.EMBOSS)

        mark = self.px(9)
        size = self.px(theme.FONT_SIZE_SMALL)
        label = self.blend_label(blend)
        _, text_height = draw.text_dimensions(label, size)
        draw.text_elided(
            x + pad,
            y + (height - text_height) // 2,
            label,
            size,
            theme.WIDGET_TEXT if blend != 'MIX' else theme.WIDGET_TEXT_DIM,
            width - pad - mark - self.px(2),
        )

        # The same solid triangle the full-size Dropdown draws.
        icons.chevron_down(
            x + width - self.px(3) - mark,
            y + (height - mark) * 0.5,
            mark,
            theme.WIDGET_TEXT_DIM,
        )

    def _draw_type_chip(self, x, y, size, icon):
        """A chip that carries an icon rather than pixels."""
        draw.rect(x, y, size, size, theme.WIDGET)
        inset = max(1, size // 6)
        icon(x + inset, y + inset, size - inset * 2, theme.WIDGET_TEXT)

    def _draw_thumbnail(self, x, y, size, texture):
        """A layer's pixels, over a checkerboard so alpha reads correctly."""
        draw.checkerboard(
            x, y, size, size, max(3, size // 3),
            (0.45, 0.45, 0.45, 1.0), (0.32, 0.32, 0.32, 1.0),
        )
        if texture is not None:
            draw.image_rect(x, y, size, size, texture)

    def _chip_border(self, x, y, size, selected):
        colour = theme.WIDGET_TEXT if selected else theme.OUTLINE
        draw.rounded_rect_outline(
            x, y, size, size, self.px(2), colour, max(1.0, self.px(2 if selected else 1))
        )

    def _row(self, index):
        rows = self.layer_list.rows
        return rows[index] if 0 <= index < len(rows) else None

    def _layer_index(self, row_index):
        """The place in the stack a row refers to.

        The two stopped being the same number when groups arrived: a closed
        group's children are not drawn, so the fourth row on screen can be the
        ninth layer. Every callback out of here reports the stack position,
        which is the only one the data side knows about.
        """
        row = self._row(row_index)
        if row is None:
            return row_index
        return int(row.get("index", row_index))

    def _row_rect(self, index):
        """A row's rectangle in region coordinates, for anchoring an overlay."""
        pitch = max(1, self.px(self.ROW_HEIGHT))
        return (
            self.layer_list.x,
            self.layer_list.row_top(index) - pitch,
            self.layer_list.row_width(),
            pitch,
        )

    def _zone_at(self, index, local_x):
        """Which control a row-local x lands on."""
        row = self._row(index) or {}
        zones = self._row_zones(
            0, 0, self.layer_list.row_width(),
            max(1, self.px(self.ROW_HEIGHT)),
            row,
        )

        # The small squares get a little slack on each side; they are 11-16
        # pixels wide and hitting them exactly is not a fair ask.
        slack = self.px(3)
        for key in ('TWISTY', 'TOGGLE', 'CONTENT', 'MASK'):
            rect = zones.get(key)
            if rect and rect[0] - slack <= local_x < rect[0] + rect[2] + slack:
                return key

        for key in ('OPACITY', 'BLEND'):
            rect = zones.get(key)
            if rect and rect[0] <= local_x < rect[0] + rect[2]:
                return key

        return 'NAME'

    def _effect_of(self, row):
        """`(layer_index, effect_index, on_mask)` for an effect row."""
        return (
            int(row.get("index", -1)),
            int(row.get("effect", -1)),
            bool(row.get("on_mask")),
        )

    def _on_effect_row_click(self, index, local_x):
        """An effect row's controls. Returns what `on_row_click` expects."""
        row = self._row(index)
        zone = self._zone_at(index, local_x)
        layer_index, effect_index, on_mask = self._effect_of(row)

        if zone == 'TOGGLE':
            self._emit(self.on_effect_toggle, layer_index, effect_index, on_mask)
            return True

        if zone == 'BLEND':
            self._open_blend_menu(index)
            return True

        if zone == 'OPACITY':
            self._opacity_row = index
            self._opacity_value = row.get("opacity", 1.0)
            return 'CAPTURE'

        # Anywhere else selects it, which is what makes the properties
        # section point at it. Not a plain row activation: an effect is not a
        # layer and selecting one must not disturb the layer selection beyond
        # collapsing it onto the owner.
        self._emit(self.on_effect_select, layer_index, effect_index, on_mask)
        return True

    def _on_layer_row_click(self, index, local_x, _local_y, alt=False,
                            shift=False):
        """Controls inside a row act instead of selecting the row."""
        row = self._row(index)
        if row is None:
            return False

        if row.get("type") == 'EFFECT':
            return self._on_effect_row_click(index, local_x)

        # Shift on the mask chip switches the mask off and on again. The eye
        # beside it does that for the layer, and a mask had no equivalent
        # short of the menu -- so the gesture is the eye's, aimed at the chip
        # it lands on. Only on the chip: shift anywhere else on the row is a
        # range selection and has to stay one.
        if shift and row.get("has_mask") and \
                self._zone_at(index, local_x) == 'MASK':
            self._emit(self.on_mask_toggle, self._layer_index(index))
            return True

        # Alt-click isolates, wherever on the row it lands: the gesture is
        # Photoshop's, and it is about the layer rather than about whichever
        # control happens to be under the pointer. Alt-clicking the mask chip
        # asks for the mask on its own, which is the same sentence about a
        # different thing.
        if alt:
            zone = self._zone_at(index, local_x)
            mode = 'MASK' if zone == 'MASK' and row.get("has_mask") else 'LAYER'
            return self._emit(
                self.on_layer_solo, mode, self._layer_index(index)
            )

        zone = self._zone_at(index, local_x)

        # With several rows selected there is no answer to "which one is this
        # editing", so a click on a control is taken as a plain click instead:
        # it collapses the selection onto this row, and the next click edits.
        # Opening a group is not an edit, so it still works.
        if self._multi_select and zone != 'TWISTY':
            return False

        if zone == 'TWISTY':
            self._emit(self.on_group_toggle, self._layer_index(index))
            return True

        if zone == 'TOGGLE':
            self._emit(self.on_layer_toggle, self._layer_index(index))
            return True

        # CONTENT and MASK are deliberately *not* handled here. Selecting a
        # half of a layer is a selection, so it belongs on the same path as
        # selecting the layer -- and a click this path takes is never half of
        # a double click, which the colour chip needs.

        if zone == 'BLEND':
            self._open_blend_menu(index)
            return True

        if zone == 'OPACITY':
            self._opacity_row = index
            self._opacity_value = row.get("opacity", 1.0)
            return 'CAPTURE'

        return False

    def _on_layer_row_drag(self, index, delta_x):
        if index != self._opacity_row:
            return

        step = delta_x / max(1, self.px(self.VALUE_DRAG_RANGE))
        value = min(1.0, max(0.0, self._opacity_value + step))
        if value == self._opacity_value:
            return

        self._opacity_value = value

        row = self._row(index) or {}
        if row.get("type") == 'EFFECT':
            layer_index, effect_index, on_mask = self._effect_of(row)
            self._emit(self.on_effect_opacity, layer_index, effect_index,
                       on_mask, value)
            return

        self._emit(self.on_opacity_change, self._layer_index(index), value)

    def _on_layer_row_double_click(self, index, local_x, _local_y):
        row = self._row(index)
        if row is None or self._multi_select:
            return

        zone = self._zone_at(index, local_x)

        # A fill layer's chip carries its colour, and the first click on it
        # now means "select this half" -- so picking the colour has to be the
        # second one. Two things worth doing to one chip, and the click count
        # is what separates them.
        if zone == 'CONTENT' and row.get("color") is not None:
            self._open_fill_picker(index)
            return

        if zone != 'NAME':
            return

        x, y, width, height = self._row_rect(index)
        zones = self._row_zones(x, y, width, height, row)
        name_x, _, name_width, _ = zones['NAME']

        inset = self.px(3)
        self._rename_row = index
        self.name_field.open_at(
            name_x - inset,
            y + inset,
            max(self.px(40), name_width + inset * 2),
            height - inset * 2,
            row["name"],
        )

    def _open_blend_menu(self, index):
        row = self._row(index)
        if row is None:
            return

        x, y, width, height = self._row_rect(index)
        zones = self._row_zones(x, y, width, height, row)

        rect = zones.get('BLEND')
        if rect is None:
            # The panel is too narrow to carry the chip; open the menu over
            # the row itself rather than doing nothing.
            rect = (x + width // 2, y, self.px(self.BLEND_WIDTH), height)

        self._blend_row = index
        self.row_blend.items = self._blend_items_for(row)
        self.row_blend.set_rect(*rect)
        self.row_blend.value = row.get("blend", 'MIX')
        self.row_blend.open = True
        self.set_overlay(self.row_blend)

    def _open_fill_picker(self, index):
        row = self._row(index)
        if row is None or row.get("color") is None:
            return

        x, y, width, height = self._row_rect(index)
        zones = self._row_zones(x, y, width, height, row)

        self._fill_row = index
        self.fill_swatch.set_rect(*zones['CONTENT'])
        self.fill_swatch.color = tuple(row["color"])
        self.picker.open_for(self.fill_swatch)

    def _row_part(self, index, local_x):
        """Which half of a layer a click at `local_x` selects.

        The chips are the two halves; everything else on the row is the layer
        itself, which is a third state and not a shorthand for the first one.
        A group has no halves at all -- no content to paint, no mask chip.
        """
        row = self._row(index) or {}
        if row.get("type") == 'GROUP' or local_x is None:
            return 'NONE'

        zone = self._zone_at(index, local_x)
        if zone == 'CONTENT':
            return 'COLOR'
        if zone == 'MASK' and row.get("has_mask"):
            return 'MASK'
        return 'NONE'

    def _on_layer_activate(self, index, extend=False, span=False, local_x=None):
        row = self._row(index) or {}
        if row.get("type") == 'EFFECT':
            # Selecting an effect selects its owner *and* the effect. Without
            # the first half the properties section would be editing an
            # effect on a layer that is not the active one.
            layer_index, effect_index, on_mask = self._effect_of(row)
            self._emit(self.on_effect_select, layer_index, effect_index, on_mask)
            return

        # One message, carrying both the row and the half of it that was hit.
        # Two would be two undo steps and two redraws for one click, and they
        # could arrive in either order.
        #
        # With several rows selected the click is a collapse and nothing else,
        # which is the rule the row controls already follow: the first click
        # after a multi-selection puts the panel on one row, the second one
        # does the thing. A half selected on the way past would be a control
        # answering a click that was not aimed at it.
        part = 'NONE' if self._multi_select else self._row_part(index, local_x)
        self._emit(self.on_layer_select, self._layer_index(index), extend, span,
                   part)

    def _on_layer_empty_click(self, extend, span):
        """A click under the last row means none of them.

        Not with a modifier held: ctrl and shift are how a selection is
        *built*, and missing the last row by three pixels while adding to one
        must not throw the selection away.
        """
        if extend or span:
            return
        self._emit(self.on_layer_deselect)

    def _on_layer_box_select(self, row_indices, extend):
        """A rubber band from the empty space picked these rows out.

        Rows, not layers: a filter row carries its owner's stack index, so a
        band that grazed one would otherwise select that layer twice and, worse,
        select it because of something that is not a layer. They are dropped
        here rather than in the list, which knows nothing about what a row is.

        A band that ends up covering nothing is "none of them" -- the same
        answer the click it grew out of would have given.
        """
        layers = []
        for index in row_indices:
            row = self._row(index)
            if row is None or row.get("type") == 'EFFECT':
                continue
            layer_index = self._layer_index(index)
            if layer_index not in layers:
                layers.append(layer_index)

        if not layers and not extend:
            self._emit(self.on_layer_deselect)
            return

        self._emit(self.on_layer_box_select, layers, bool(extend))

    def _on_layer_spring_open(self, index):
        """A drag has rested on a closed group: open it and let them aim in.

        Only ever opens. The same rest over an open group would otherwise
        close it, which is the one thing a hand hovering over a drop target is
        certainly not asking for.
        """
        row = self._row(index)
        if row is None or row.get("type") != 'GROUP' or row.get("expanded"):
            return
        self._emit(self.on_group_toggle, self._layer_index(index))

    def _depth_range(self, gap):
        """The nesting a drop into `gap` is allowed to mean: (shallowest, deepest).

        One gap on screen can mean two different things. Between the last child
        of a group and the layer after it, a drop is either "still in the
        group" or "out of it, underneath it" -- the rows cannot tell those
        apart, and neither can a y coordinate. What bounds them is the pair
        around the gap: nothing may nest deeper than one level into the row
        above, and nothing may sit shallower than the row below, or it would
        cut that row off from its own group.
        """
        rows = self.layer_list.rows
        above = rows[gap - 1] if 0 < gap <= len(rows) else None
        below = rows[gap] if 0 <= gap < len(rows) else None

        if above is None:
            return 0, 0

        deepest = int(above.get("depth", 0))
        if above.get("type") == 'GROUP':
            # Closed groups take drops too. Nothing is lost by it -- the data
            # side opens a group it has just dropped something into, so the
            # layer is never put somewhere it cannot be seen.
            deepest += 1

        shallowest = int(below.get("depth", 0)) if below is not None else 0
        return min(shallowest, deepest), deepest

    def _gap_depth(self, gap):
        """How deeply nested a drop *between* two rows lands.

        The shallowest the gap allows, which is the depth of the row below it.
        That is the rule that makes the obvious gesture obvious: a drop under
        a group -- under all of it -- lands beside the group, not in it, and
        the only way into a group is to drop on the group itself.
        """
        return self._depth_range(gap)[0]

    def _drop_offset(self, gap):
        """Where to draw the drop line, so the nesting is visible before the drop."""
        if gap < 0:
            return 0
        return self.px(self.ROW_PADDING) + self.px(self.INDENT) * self._gap_depth(gap)

    def _row_subtree(self, row_index):
        """The rows `row_index` owns on screen, itself included."""
        rows = self.layer_list.rows
        if not (0 <= row_index < len(rows)):
            return range(0, 0)

        base = int(rows[row_index].get("depth", 0))
        end = row_index + 1
        while end < len(rows) and int(rows[end].get("depth", 0)) > base:
            end += 1
        return range(row_index, end)

    def _dragged_rows(self, from_index):
        """Every row travelling with a drag that started on `from_index`.

        A drag that starts on a selected row moves the whole selection, which
        is the point of being able to select several: three layers and a group
        go into a folder in one gesture. Starting anywhere else moves only the
        row that was grabbed.
        """
        rows = self.layer_list.rows
        pressed = self._row(from_index)
        if pressed is None:
            return set()

        # An effect is not a layer and does not travel like one. Reordering
        # effects is the Up/Down buttons' job; a drag that looked like it
        # would move one somewhere else would be lying.
        if pressed.get("type") == 'EFFECT':
            return set()

        if self._multi_select and pressed.get("selected"):
            starts = [
                position for position, row in enumerate(rows) if row.get("selected")
            ]
        else:
            starts = [from_index]

        out = set()
        for start in starts:
            out.update(self._row_subtree(start))
        return out

    def _drop_accepts(self, from_index, row_index):
        """Whether dropping *onto* that row means anything.

        Only a group can hold another layer, and not one that is already
        travelling with the drag -- a group cannot be put inside itself, nor
        inside something it is being carried with.
        """
        row = self._row(row_index)
        if row is None or row.get("type") != 'GROUP':
            return False

        return row_index not in self._dragged_rows(from_index)

    def _is_effect_row(self, index):
        row = self._row(index)
        return bool(row) and row.get("type") == 'EFFECT'

    def _drop_refuses(self, from_index, row_index):
        """A group that is refused because it is part of the drag itself.

        Only groups get the refusal mark. Aiming at the middle of an ordinary
        layer is not a mistake -- there is nothing to go inside -- so marking
        it would say no to a question nobody asked.
        """
        row = self._row(row_index)
        if row is None or row.get("type") != 'GROUP':
            return False
        return row_index in self._dragged_rows(from_index)

    def _on_layer_reorder(self, from_index, to_index, onto_index=-1):
        """Turn a drop into "under this layer, this deep".

        A gap on screen is not a gap in the stack -- a closed group hides its
        children, so the row above a gap can have any number of layers between
        it and the next one. The row above does say unambiguously which layer
        the block lands under, and the data side turns that into a position.

        Dropping *onto* a group is the other half: it is the only way in, and
        it puts the block at the top of that group.

        This is the one gesture a multiple selection does allow: it moves
        every selected layer, which is unambiguous in a way that editing one
        property of five layers is not.
        """
        if self._is_effect_row(from_index):
            return

        if onto_index >= 0:
            row = self._row(onto_index)
            if row is not None:
                self._emit(
                    self.on_layer_reorder,
                    self._layer_index(from_index),
                    int(row.get("index", onto_index)),
                    int(row.get("depth", 0)) + 1,
                    True,
                )
            return

        above = self._row(to_index - 1) if to_index > 0 else None
        after = -1 if above is None else int(above.get("index", to_index - 1))

        self._emit(self.on_layer_reorder, self._layer_index(from_index), after,
                   self._gap_depth(to_index), False)

    # --- overlay ----------------------------------------------------------

    def _overlay_interrupts_gesture(self):
        """An overlay opening ends whatever the list was in the middle of."""
        lst = getattr(self, "layer_list", None)
        if lst is not None:
            lst.cancel_double_click()

    def set_overlay(self, widget, over=None):
        """Make `widget` the thing drawn and hit-tested above everything else.

        Overlays stack. A popover is an overlay, and the dropdowns, menus and
        number fields *inside* it are overlays of their own -- opening one has
        to leave the popover standing, or picking a stroke method would shut
        the panel it was picked in. `over` names the overlay this one opens
        inside; without it the new overlay replaces whatever was up, which is
        what one dropdown opening while another is open should do.

        Dropdown, ColorPicker, Menu, TextField and Popover can all be
        overlays; they share the `draw_popup` / `handle_popup` / `close` shape
        rather than a base class.
        """
        if widget in self._overlays:
            self.needs_redraw = True
            return

        if over is not None and over in self._overlays:
            above = self._overlays.index(over) + 1
            if above < len(self._overlays):
                self.clear_overlay(self._overlays[above])
        elif self._overlays:
            self.clear_overlay(self._overlays[0])

        self._overlays.append(widget)
        self._overlay_interrupts_gesture()
        self.needs_redraw = True

    def clear_overlay(self, widget=None):
        """Close `widget` and everything stacked on top of it. Default: the top.

        The stack is cut *before* the closers run: every overlay's `close()`
        calls back into here, and the entry being gone is what stops that from
        recursing.
        """
        if not self._overlays:
            return

        if widget is None:
            widget = self._overlays[-1]
        if widget not in self._overlays:
            return

        index = self._overlays.index(widget)
        doomed = self._overlays[index:]
        del self._overlays[index:]

        for other in reversed(doomed):
            if other is widget:
                continue
            closer = getattr(other, "close", None)
            if closer is not None:
                closer()

        self.needs_redraw = True

    @property
    def _overlay(self):
        """The overlay taking input: the top of the stack, or None."""
        return self._overlays[-1] if self._overlays else None

    @property
    def has_capture(self):
        """True while a widget owns the pointer -- a drag is in progress.

        The window can have a second editor beside ours now, and a drag that
        wanders over it still belongs to the widget that started it. This is
        what the modal operator asks before deciding an event is not ours.
        """
        return self._captured is not None

    @property
    def has_overlay(self):
        """True while a menu, picker, popover or text field owns the input.

        The modal operator asks before acting on Escape: with an overlay up,
        Escape belongs to that overlay (cancel the rename), not to the window.
        """
        return bool(self._overlays)

    # --- layout -----------------------------------------------------------

    def update(self, region_width, region_height, ui_scale, ui_state=None):
        """Re-layout if the region size, scale or panel widths changed.

        There is no inset parameter and there must not be one: the region is
        ours from edge to edge. See the module docstring.
        """
        changed = (
            region_width != self.width
            or region_height != self.height
            or ui_scale != self._ui_scale
        )
        self._ui_scale = ui_scale
        if ui_state is not None:
            self._apply_ui_state(ui_state)

        if changed:
            self.set_rect(0, 0, region_width, region_height)
            self.needs_redraw = True

    def _apply_ui_state(self, ui_state):
        brush_open = bool(getattr(ui_state, "show_brush_panel", False))
        brush_width = getattr(ui_state, "brush_panel_width",
                              self._brush_panel_width)
        # The panel describes a brush, so it goes with the brush strip: a fill
        # tool's strip has no switch for it, and a panel nothing on screen can
        # close is a panel the user is stuck with.
        brush_visible = brush_open and not self.is_fill_tool

        width = getattr(ui_state, "right_panel_width", self._layers_width)
        visible = getattr(ui_state, "show_right_panel", True)
        targets = getattr(ui_state, "show_targets_panel", True)
        left_width = getattr(ui_state, "left_panel_width", self._display_width)
        left_visible = getattr(ui_state, "show_left_panel", False)
        page = getattr(ui_state, "left_panel_page", 'DISPLAY')

        changed = (
            self.layers.visible != visible
            or self._show_targets != targets
            or self._layers_width != width
            or self.display.visible != left_visible
            or self._display_width != left_width
            or self._left_page != page
            or self.brush_panel.visible != brush_visible
            or self._brush_panel_width != brush_width
            or self.brush_panel_button.active != brush_open
        )
        if not changed:
            return

        self.brush_panel.visible = brush_visible
        self._brush_panel_width = brush_width
        # Pressed against the property rather than the click: the panel stays
        # open across a trip through a fill tool, and the button has to say so
        # when the brush comes back.
        self.brush_panel_button.active = brush_open

        self.layers.visible = visible
        # Two switches, both of which have to be on: the column's, and its
        # own. Closing the column closes what is in it.
        self._show_targets = targets
        self.targets.visible = visible and targets
        self.targets_button.active = targets
        self._layers_width = width
        self.display.visible = left_visible
        self._display_width = left_width
        self._left_page = page
        self.display.title = "Bake" if page == 'BAKE' else "Display"
        # The buttons are the panel's state, so they follow the property
        # rather than the click: closing the panel any other way still
        # un-presses them, and only the page being shown reads as pressed.
        self.display_button.active = left_visible and page == 'DISPLAY'
        self.bake_button.active = left_visible and page == 'BAKE'
        self.set_rect(0, 0, self.width, self.height)
        self.needs_redraw = True

    def _show(self, widgets, visible):
        for widget in widgets:
            if widget.visible != visible:
                widget.visible = visible
                self.needs_redraw = True

    def _layout_brush_strip(self, control_y, control_h, pad, gap):
        """Blender's tool header, in our own widgets."""
        self._show(self._fill_widgets(), False)
        self._show((self.brush_color, self.brush_secondary, self.brush_swap),
                   True)

        swatch_w = self.px(self.BRUSH_SWATCH)
        pressure_w = self.px(self.BRUSH_PRESSURE_WIDTH)

        cursor = pad
        for widget, width in (
            (self.brush_picker, self.px(self.BRUSH_PICKER_WIDTH)),
            (self.brush_blend, self.px(self.BRUSH_BLEND_WIDTH)),
        ):
            widget.visible = True
            widget.set_rect(cursor, control_y, width, control_h)
            cursor += width + gap

        cursor += pad - gap
        for widget in (self.brush_color, self.brush_secondary):
            widget.set_rect(cursor, control_y, swatch_w, control_h)
            cursor += swatch_w + gap
        self.brush_swap.set_rect(cursor, control_y, pressure_w, control_h)
        cursor += pressure_w + pad

        # Each value gets its pressure toggle right beside it: which one it
        # belongs to has to be obvious without reading the tooltip.
        slider_w = self.px(self.BRUSH_SLIDER_WIDTH)
        for slider, toggle in (
            (self.brush_size, self.brush_size_pressure),
            (self.brush_strength, self.brush_strength_pressure),
        ):
            slider.visible = True
            toggle.visible = True
            slider.set_rect(cursor, control_y, slider_w, control_h)
            cursor += slider_w
            toggle.set_rect(cursor, control_y, pressure_w, control_h)
            cursor += pressure_w + pad

        # Blender's popovers, in Blender's order, right of Strength -- which
        # is exactly where its own tool header puts them.
        for identifier, _label in self.BRUSH_POPOVERS:
            popover = self.popovers[identifier]
            popover.visible = True
            popover_w = self.px(
                self.BRUSH_POPOVER_WIDTHS.get(identifier, self.BRUSH_POPOVER_WIDTH)
            )
            popover.set_rect(cursor, control_y, popover_w, control_h)
            cursor += popover_w + gap

        # Last, where Options was. It opens the panel that holds every one of
        # the popovers to its left, so it belongs at the end of them.
        self.brush_panel_button.visible = True
        self.brush_panel_button.set_rect(
            cursor, control_y, self.px(self.BRUSH_PROPERTIES_WIDTH), control_h
        )

    def _layout_fill_strip(self, control_y, control_h, pad, gap):
        """The three fill tools' strip. Laid out in the brush strip's rhythm.

        Same order, same spacing and the same colour swatches, so switching
        tools does not move the one control both of them have.
        """
        self._show(self._brush_widgets(), False)
        self._show((self.brush_color, self.brush_secondary, self.brush_swap),
                   True)
        self._show(self._fill_widgets(), True)

        # UV Island Fill has nothing to choose: an island is an island.
        has_scope = self.active_tool in ('POLY_FILL', 'MESH_FILL')
        self._show((self.fill_scope,), has_scope)

        swatch_w = self.px(self.BRUSH_SWATCH)
        pressure_w = self.px(self.BRUSH_PRESSURE_WIDTH)

        cursor = pad
        if has_scope:
            width = self.px(self.FILL_SCOPE_WIDTH)
            self.fill_scope.set_rect(cursor, control_y, width, control_h)
            cursor += width + gap

        # Beside the scope, because the two together are the whole answer to
        # "what does a press take": this many faces, chosen this way.
        width = control_h * len(self.fill_select_buttons)
        self.fill_select_row.set_rect(cursor, control_y, width, control_h)
        cursor += width + gap

        width = self.px(self.FILL_BLEND_WIDTH)
        self.fill_blend.set_rect(cursor, control_y, width, control_h)
        cursor += width + gap

        cursor += pad - gap
        for widget in (self.brush_color, self.brush_secondary):
            widget.set_rect(cursor, control_y, swatch_w, control_h)
            cursor += swatch_w + gap
        self.brush_swap.set_rect(cursor, control_y, pressure_w, control_h)
        cursor += pressure_w + pad

        width = self.px(self.FILL_SLIDER_WIDTH)
        self.fill_alpha.set_rect(cursor, control_y, width, control_h)
        cursor += width + pad

        width = self.px(self.FILL_MODE_WIDTH) * len(self.fill_mode_buttons)
        self.fill_mode_row.set_rect(cursor, control_y, width, control_h)
        cursor += width + pad

        self.fill_options.set_rect(
            cursor, control_y, self.px(self.FILL_OPTIONS_WIDTH), control_h
        )

    def on_resize(self):
        pad = self.px(theme.PAD)
        gap = self.px(theme.GAP)
        header_h = self.px(theme.HEADER_HEIGHT)
        button_h = self.px(theme.BUTTON_HEIGHT)

        floor = 0
        ceiling = self.height
        top = ceiling - header_h

        self.header.set_rect(0, top, self.width, header_h)

        # Two groups. Everything that says *what* we are looking at is packed
        # along the left edge, the way Blender packs its own headers. Mirroring
        # is centred on the region -- it is the one control that acts on the
        # model rather than on the document, and the middle is where the eye
        # goes back to between strokes.
        button_y = top + (header_h - button_h) // 2
        title_w = self.px(90)
        plane_w = self.px(self.HEADER_BUTTON)
        close_w = self.px(64)
        axis_w = self.px(self.SYMMETRY_BUTTON)
        label_w = self.px(44)

        # --- centre: Mirror  X | Y | Z  [planes] [wire] ---
        row_w = axis_w * len(self.SYMMETRY_AXES)
        wire_w = self.px(self.HEADER_BUTTON)
        group_w = label_w + gap + row_w + gap + plane_w + gap + wire_w
        group_x = (self.width - group_w) // 2

        self.symmetry_label.set_rect(group_x, top, label_w, header_h)
        self.symmetry_row.set_rect(group_x + label_w + gap, button_y,
                                   row_w, button_h)
        cursor = group_x + label_w + gap + row_w + gap
        self.plane_button.set_rect(cursor, button_y, plane_w, button_h)
        cursor += plane_w + gap
        self.wire_button.set_rect(cursor, button_y, wire_w, button_h)

        # --- left: PaintR2, then what is being painted ---
        # The centred group is centred on the region, not on the space left
        # between the two ends, so a narrow window is the case where it can
        # meet them. Nothing is re-laid out for that -- the status readout is
        # what gives way, below.
        # Sized to its text so the readout ends where the text ends; it must
        # also stop before the centred group, whatever the object is called.
        status_w, _ = draw.text_dimensions(
            self.status_label.text, self.px(theme.FONT_SIZE_SMALL)
        )
        room = group_x - pad * 2 - title_w - gap
        status_w = int(max(0, min(status_w, room)))

        self.title.set_rect(pad, top, title_w, header_h)
        self.status_label.set_rect(pad + title_w + gap, top, status_w, header_h)

        # --- right: Display, Shader Graph, then Close ---
        # Close stays hard against the corner, where it has always been; the
        # other two go beside it because they are the controls that change
        # what the *window* is rather than what the document is.
        graph_w = self.px(self.GRAPH_BUTTON)
        display_w = self.px(self.DISPLAY_BUTTON)
        cursor = self.width - pad

        cursor -= close_w
        self.close_button.set_rect(cursor, button_y, close_w, button_h)
        cursor -= gap + graph_w
        self.graph_button.set_rect(cursor, button_y, graph_w, button_h)
        cursor -= gap + display_w
        self.display_button.set_rect(cursor, button_y, display_w, button_h)
        cursor -= gap + display_w
        self.bake_button.set_rect(cursor, button_y, display_w, button_h)

        # Beside them, and square: it switches the panel the layer stack
        # shares its column with, so it belongs with the other two that
        # change what the window shows rather than what the document is.
        targets_w = self.px(self.HEADER_BUTTON)
        cursor -= gap + targets_w
        self.targets_button.set_rect(cursor, button_y, targets_w, button_h)

        # --- the settings strip, directly under the header ------------------
        # One band, two possible contents. The tool row below decides which:
        # a brush and a fill have almost nothing in common to show, and
        # stacking both strips would cost a row of height to controls that are
        # inert half the time.
        bar_h = self.px(self.BRUSH_BAR_HEIGHT)
        bar_y = top - bar_h
        self.brush_bar.set_rect(0, bar_y, self.width, bar_h)

        control_h = self.px(theme.BUTTON_HEIGHT)
        control_y = bar_y + (bar_h - control_h) // 2

        if self.is_fill_tool:
            self._layout_fill_strip(control_y, control_h, pad, gap)
        else:
            self._layout_brush_strip(control_y, control_h, pad, gap)

        # --- the tool row, under the strip ----------------------------------
        tool_h = self.px(self.TOOL_BAR_HEIGHT)
        tool_y = bar_y - tool_h
        self.tool_bar.set_rect(0, tool_y, self.width, tool_h)

        tool_w = self.px(self.TOOL_BUTTON)
        self.tool_row.set_rect(
            pad,
            tool_y + (tool_h - tool_w) // 2,
            tool_w * len(self.TOOLS),
            tool_w,
        )

        band_y = floor
        band_h = max(0, tool_y - band_y)

        layers_w = self.px(self._layers_width) if self.layers.visible else 0
        display_w = self.px(self._display_width) if self.display.visible else 0
        brush_w = self.px(self._brush_panel_width) if self.brush_panel.visible else 0

        # The canvas is what is left between the panels. None of them is an
        # inset on the region -- the region is ours edge to edge, and the
        # viewport is the hole we leave in it.
        self.viewport.set_rect(
            display_w, band_y,
            max(0, self.width - layers_w - brush_w - display_w), band_h
        )

        if self.display.visible:
            self.display.set_rect(0, band_y, display_w, band_h)

        # Immediately left of the layer column, full height: what is being
        # painted with, next to what is being painted on.
        if self.brush_panel.visible:
            self.brush_panel.set_rect(
                self.width - layers_w - brush_w, band_y, brush_w, band_h
            )

        if self.layers.visible:
            column_x = self.width - layers_w

            # The right-hand column, split: what is being painted on top, its
            # layers underneath. The top panel gets what it asked for, capped
            # -- a set with twelve models in it must not push the layer list
            # off the bottom of the window.
            targets_h = 0
            if self.targets.visible:
                cap = int(band_h * self.TARGETS_SHARE)
                targets_h = min(self.px(self._targets_height), cap)
                self.targets.set_rect(
                    column_x, band_y + band_h - targets_h, layers_w, targets_h
                )

            self.layers.set_rect(
                column_x, band_y, layers_w, max(0, band_h - targets_h)
            )

            self._fit_target_lists(targets_h)

    def _fit_target_lists(self, panel_height):
        """Give each of the two lists the rows it has, and share what is left.

        Written here rather than left to `Panel`, which splits its flexible
        children evenly: one model and six materials would put three empty
        rows above a list that is scrolling.
        """
        if not self.targets.visible or panel_height <= 0:
            return

        pad = self.px(theme.PAD)
        gap = self.px(theme.GAP)
        title = self.px(theme.FONT_SIZE_TITLE + theme.PAD)
        labels = self.px(self.TARGET_LABEL) * 2

        available = panel_height - title - pad * 2 - labels - gap * 3
        if available <= 0:
            return

        row = max(1, self.px(self.TARGET_ROW))
        objects = max(self.TARGETS_MIN_ROWS, len(self._object_rows))
        materials = max(self.TARGETS_MIN_ROWS, len(self._material_rows))

        wanted = (objects + materials) * row
        if wanted <= available:
            object_h = objects * row
        else:
            # Not enough room for both. They give way in proportion, and each
            # keeps at least one row -- a list showing nothing is a list that
            # looks broken rather than short.
            share = available * objects / max(1, objects + materials)
            object_h = max(row, min(available - row, int(share)))

        self.targets.child_heights[self._object_list_index] = \
            object_h / max(self._ui_scale, 0.0001)
        self.targets.child_heights[self._material_list_index] = \
            (available - object_h) / max(self._ui_scale, 0.0001)
        self.targets.on_resize()

    # --- drawing ----------------------------------------------------------

    def draw(self):
        # Deliberately no full-region fill: Blender has already drawn the live
        # viewport across the whole area, and every panel paints its own opaque
        # background on top. The gap left over *is* the canvas.
        with draw.blend_alpha():
            # Under everything: the planes belong to the model, so the panels
            # have to cover them, not the other way round.
            self._draw_symmetry_shapes()
            super().draw()
            self._draw_header_edge()

            for overlay in self._overlays:
                overlay.draw_popup()

            self._draw_fill_shape()
            self._draw_splitter()
            self._draw_shortcuts()
            self._draw_tooltip()

            if self.debug:
                self._draw_debug()

        self.needs_redraw = False

    def _draw_header_edge(self):
        """Hard lines separating the two bars at the top of the window.

        The header, the settings strip and the tool row are different greys,
        but a colour step alone reads as a gradient at a glance. A one-pixel border is what makes
        each bar look like a bar -- it is the same thing Blender draws between
        an editor's header and its content.
        """
        edge = max(1, self.px(1))
        for y in (self.header.y + self.header.height - edge,
                  self.brush_bar.y + self.brush_bar.height - edge,
                  self.brush_bar.y,
                  self.tool_bar.y):
            draw.rect(0, y, self.width, edge, theme.BORDER)

    def _draw_debug(self):
        size = self.px(theme.FONT_SIZE_SMALL)
        lines = (
            f"region {self.width}x{self.height}  scale {self._ui_scale:.2f}",
            f"mouse {self._last_mouse[0]},{self._last_mouse[1]}",
            f"hover {self._hovered.name if self._hovered else '-'}"
            f"  capture {self._captured.name if self._captured else '-'}"
            f"  overlay {self._overlay.name if self._overlay else '-'}",
            f"event {self._last_event_repr}",
        )

        y = self.height - self.px(theme.HEADER_HEIGHT) - self.px(14)
        for text in lines:
            draw.text(self.px(6), y, text, size, theme.DEBUG_TEXT)
            y -= self.px(13)

    def request_redraw(self):
        self.needs_redraw = True

    def restart_tooltip(self):
        """Put the tooltip clock back to zero.

        `_update_hover` does this for the widget tree, but an overlay handles
        its own pointer -- a popover full of rows has to say when the pointer
        has moved to a different one, or its tooltips appear the instant the
        panel opens and never wait.
        """
        self._hover_since = time.monotonic()
        if self._tooltip_text:
            self._tooltip_text = ""
            self.needs_redraw = True

    # --- events -----------------------------------------------------------

    def capture(self, widget):
        """Route every following event to `widget` until it releases."""
        self._captured = widget

    def release(self, widget):
        if self._captured is widget:
            self._captured = None

    def is_over_ui(self, mouse_x, mouse_y):
        """True when (mouse_x, mouse_y) is on PaintR2 chrome rather than the
        live viewport.

        The modal operator uses this to decide whether an event belongs to us
        or to Blender. Anything outside the region counts as ours, so a drag
        that leaves the window does not suddenly start orbiting the view.
        """
        if self._overlay is not None:
            return True

        if not (0 <= mouse_x < self.width and 0 <= mouse_y < self.height):
            return True
        return not self.viewport.contains(mouse_x, mouse_y)

    # --- splitters --------------------------------------------------------

    def splitter_at(self, mouse_x, mouse_y):
        """Which panel edge is under the pointer: 'LAYERS', 'BRUSH',
        'DISPLAY', None.

        The inner edge only. The outer one is the window's own border and
        dragging it would be asking to resize Blender's window from inside a
        3D viewport.
        """
        grab = self.px(self.SPLITTER_GRAB)

        if self.layers.visible:
            top = self.targets if self.targets.visible else self.layers
            bottom = self.layers.y
            ceiling = top.y + top.height
            if bottom <= mouse_y < ceiling and abs(mouse_x - self.layers.x) <= grab:
                return 'LAYERS'

        panel = self.brush_panel
        if panel.visible and panel.y <= mouse_y < panel.y + panel.height:
            # Its left edge. The right one is where the layer column starts,
            # and that edge already belongs to the layer panel's splitter.
            if abs(mouse_x - panel.x) <= grab:
                return 'BRUSH'

        if self.display.visible and self.display.y <= mouse_y < self.display.y + self.display.height:
            edge = self.display.x + self.display.width
            if abs(mouse_x - edge) <= grab:
                return 'DISPLAY'

        return None

    def _drag_splitter(self, mouse_x):
        """Resize the panel being dragged. True when the width changed."""
        if self._splitter == 'LAYERS':
            low, high = self.LAYERS_WIDTH_RANGE
            wanted = (self.width - mouse_x) / max(1e-6, self._ui_scale)
            wanted = int(max(low, min(high, round(wanted))))
            if wanted == self._layers_width:
                return False
            self._layers_width = wanted
        elif self._splitter == 'BRUSH':
            low, high = self.BRUSH_PANEL_WIDTH_RANGE
            # Measured from the layer column's edge, not the window's: this
            # panel's right-hand side moves when the layer panel is resized.
            layers_w = self.px(self._layers_width) if self.layers.visible else 0
            wanted = (self.width - layers_w - mouse_x) / max(1e-6, self._ui_scale)
            wanted = int(max(low, min(high, round(wanted))))
            if wanted == self._brush_panel_width:
                return False
            self._brush_panel_width = wanted
        else:
            low, high = self.DISPLAY_WIDTH_RANGE
            wanted = mouse_x / max(1e-6, self._ui_scale)
            wanted = int(max(low, min(high, round(wanted))))
            if wanted == self._display_width:
                return False
            self._display_width = wanted

        self.set_rect(0, 0, self.width, self.height)
        self.needs_redraw = True
        # Written straight through, not on release: the panel width lives in
        # the .blend, and a drag that only persisted when it ended would lose
        # the size to any interruption.
        self._emit(
            self.on_panel_width,
            self._splitter,
            {'LAYERS': self._layers_width,
             'BRUSH': self._brush_panel_width}.get(self._splitter,
                                                  self._display_width),
        )
        return True

    def handle_event(self, event):
        """Dispatch `event` into the tree. Returns True when consumed."""
        self._last_mouse = (event.mouse_x, event.mouse_y)
        self._last_event_repr = f"{event.type}/{event.value}"

        if self.debug:
            self.needs_redraw = True

        # A splitter drag owns the pointer outright: it started on a two-pixel
        # line and the hand is not going to stay on it.
        if self._splitter is not None:
            if event.is_move:
                self._drag_splitter(event.mouse_x)
                return True
            if event.is_left_release:
                self._splitter = None
                self.needs_redraw = True
            return True

        # A drag in progress owns the pointer, overlay or not: a slider inside
        # a popover has to keep getting motion after the pointer leaves it, and
        # the popover would otherwise swallow the events on its way past.
        if self._captured is not None:
            if event.is_move:
                self._last_mouse = (event.mouse_x, event.mouse_y)
            return self._captured.handle(event)

        # An open dropdown swallows everything until it closes.
        if self._overlay is not None:
            if self._overlay.handle_popup(event):
                return True

        # The splitter comes *after* the overlay, and this order is the whole
        # of a bug that was live for as long as the splitters were: a popover
        # is 330 px wide and opens over the panel edge, so its right-hand
        # column -- every eye toggle, the right end of every curve editor --
        # sat within grab distance of the splitter line underneath it. The
        # click started a panel resize instead of hitting the control that was
        # drawn on top of it. Chrome that is painted over does not get to
        # answer first.
        if event.is_left_press:
            side = self.splitter_at(event.mouse_x, event.mouse_y)
            if side is not None:
                self._splitter = side
                self.needs_redraw = True
                return True

        if event.is_move:
            over = self.splitter_at(event.mouse_x, event.mouse_y)
            if over != self._over_splitter:
                self._over_splitter = over
                self.needs_redraw = True

        # The shortcut sheet is dismissed by anything that is not the key that
        # raised it. A help overlay that has to be turned off the same way it
        # was turned on traps whoever opened it by accident.
        if self.shortcuts_visible and (
            event.is_left_press or (event.type == 'ESC' and event.is_press)
        ):
            self.shortcuts_visible = False
            self.needs_redraw = True
            return True

        # Keys go through the table in `gui/keymap.py`, never through a branch
        # written here: a binding added by hand used to eat an older one
        # without either of them saying so.
        if event.is_press and self._handle_key(event):
            return True

        if event.is_move:
            self._update_hover(event)

        return super().handle(event)

    def _key_contexts(self, mouse_x, mouse_y):
        """Which of the keymap's contexts the pointer is in, narrowest first."""
        contexts = []
        if self.layers.visible and self.layers.contains(mouse_x, mouse_y):
            contexts.append('LAYERS')
        if not self.viewport.contains(mouse_x, mouse_y):
            contexts.append('CHROME')
        contexts.append('ANY')
        return tuple(contexts)

    def _handle_key(self, event):
        """Resolve a key press against the table and run it. True if taken."""
        action = keymap.resolve(
            event.type,
            getattr(event, "ctrl", False),
            getattr(event, "shift", False),
            getattr(event, "alt", False),
            self._key_contexts(event.mouse_x, event.mouse_y),
        )
        if not action:
            return False

        handler = self._key_actions.get(action)
        if handler is None:
            return False

        if handler(event) is False:
            # The binding exists but there was nothing for it to do here --
            # no active layer, no mask. Not consumed, so Blender still gets
            # the key rather than the window swallowing it in silence.
            return False

        self.needs_redraw = True
        return True

    def _key_delete(self, event):
        return self._open_delete_menu(event.mouse_x, event.mouse_y)

    def _key_brush_swap(self, _event):
        if not self.brush_swap.enabled:
            return False
        self._emit(self.on_brush_swap)
        return True

    def _key_step(self, delta):
        """Move the active row by `delta`, and keep it on screen."""
        rows = self.layer_list.rows
        if not rows:
            return False
        current = self.layer_list.active_index
        target = max(0, min(len(rows) - 1, (0 if current < 0 else current) + delta))
        if target == current:
            return False
        self._on_layer_activate(target)
        self.layer_list.scroll_into_view(target)
        return True

    def _key_shortcuts(self, _event):
        self.shortcuts_visible = not self.shortcuts_visible
        return True

    def _build_key_actions(self):
        """Action name -> what it does. Returning False means "not here"."""
        return {
            'delete': self._key_delete,
            'brush_swap': self._key_brush_swap,
            'layer_prev': lambda _e: self._key_step(-1),
            'layer_next': lambda _e: self._key_step(1),
            'layer_move_up': lambda _e: self._emit(self.on_layer_move, 'UP'),
            'layer_move_down': lambda _e: self._emit(self.on_layer_move, 'DOWN'),
            'group_selected': lambda _e: self._emit(self.on_layer_group_selected),
            'mask_toggle': lambda _e: self._emit(self.on_mask_toggle),
            'mask_solo': lambda _e: self._emit(self.on_layer_solo, 'MASK', -1),
            'layer_solo': lambda _e: self._emit(self.on_layer_solo, 'LAYER', -1),
            # `_ask` rather than `_emit`: with nothing on the clipboard the
            # paste is not ours, and a swallowed Ctrl V is a key Blender never
            # sees.
            'layer_copy': lambda _e: self._ask(self.on_layer_copy),
            'layer_paste': lambda _e: self._ask(self.on_layer_paste),
            'shortcuts': self._key_shortcuts,
            # These two answer for themselves: when there is no fill to undo
            # the binding reports "not handled" and Blender gets the key.
            'fill_undo': lambda _e: self._ask(self.on_fill_undo, False),
            'fill_redo': lambda _e: self._ask(self.on_fill_undo, True),
        }

    def _ask(self, callback, *args):
        """Call the owner and hand back *its* answer, not merely "wired".

        For bindings the window only sometimes owns: the callback is the one
        thing that knows whether there was anything to do.
        """
        if callback is None:
            return False
        return bool(callback(*args))

    def _update_hover(self, event):
        hit = None
        for widget in self.walk():
            if widget is self:
                continue
            if widget.visible and widget.contains(event.mouse_x, event.mouse_y):
                hit = widget

        # Restart the tooltip clock on any movement, not only on a change of
        # widget: sliding along a layer row moves between controls without
        # leaving the list.
        self._hover_since = time.monotonic()
        if self._tooltip_text:
            self._tooltip_text = ""
            self.needs_redraw = True

        if hit is self._hovered:
            return

        if self._hovered is not None:
            self._hovered.hovered = False
        if hit is not None:
            hit.hovered = True

        self._hovered = hit
        self.needs_redraw = True

    # --- tooltips ---------------------------------------------------------

    def _tooltip_for_hover(self):
        """What the thing under the pointer would say, or "" for nothing."""
        if self._captured is not None:
            return ""

        overlay = self._overlay
        if isinstance(overlay, Popover):
            # A popover is an overlay, but it is also a panel full of rows the
            # user needs the tooltips for -- they carry the RNA descriptions.
            return overlay.panel.tooltip_at(*self._last_mouse)
        if overlay is not None:
            return ""

        widget = self._hovered
        if widget is None:
            return ""

        if widget is self.layer_list:
            return self._row_tooltip()

        return widget.tooltip if widget.visible else ""

    def _row_tooltip(self):
        index = self.layer_list.hovered_index
        row = self._row(index)
        if row is None:
            return ""

        zone = self._zone_at(index, self.layer_list.hover_x - self.layer_list.x)
        if row.get("type") == 'EFFECT':
            return "Click to edit it below - drag the bar for opacity"
        if zone == 'CONTENT':
            if row.get("color") is not None:
                # Two things to do to one chip, so the tooltip has to carry
                # the click count -- there is nowhere else it can be said.
                return "Select the colour - double-click to change it"
            return "Select the colour - filters and the brush go here"
        return self.ROW_TOOLTIPS.get(zone, "")

    def tick(self):
        """Advance the clocks. True when the interface must redraw.

        Called from the modal operator on its timer, because the two things
        that most need a heartbeat happen while the pointer is *not* moving: a
        tooltip has to appear after a rest, and a drag held at the bottom edge
        of the layer list has to keep scrolling it.
        """
        moved = self.layer_list.animate()

        due = ""
        if time.monotonic() - self._hover_since >= self.TOOLTIP_DELAY:
            due = self._tooltip_for_hover()

        if due != self._tooltip_text:
            self._tooltip_text = due
            moved = True

        if moved:
            self.needs_redraw = True
        return moved

    @property
    def cursor(self):
        """Which pointer shape the interface is asking for, by name.

        A string, not a call: `gui/` has no window to set a cursor on. The
        modal operator reads this and hands it to Blender. The names are
        Blender's own `cursor_modal_set` identifiers.
        """
        if self._splitter is not None or self._over_splitter is not None:
            return 'MOVE_X'
        if self.layer_list.is_dragging():
            return 'HAND_CLOSED'
        if self.layer_list.is_scrolling():
            return 'SCROLL_Y'
        return 'DEFAULT'

    @property
    def wants_fast_tick(self):
        """True while something is mid-animation and the beat is too slow.

        The heartbeat is set for tooltips -- a seventh of a second, which is
        plenty for a clock and far too coarse for motion. Rather than run the
        whole window fast for the sake of the rare second a layer is being
        dragged, the operator is told when that second is happening.
        """
        return self.layer_list.animating()

    def set_fill_shape(self, points, filled=False):
        """The box or lasso being drawn over the model, in region pixels.

        `filled` is for the box only. `draw.polygon` is a triangle fan and a
        lasso is not convex, so filling one would paint an area nobody drew --
        the outline alone is the honest picture of a lasso.
        """
        shape = list(points) if points else None
        if shape != self._fill_shape or filled != self._fill_shape_filled:
            self._fill_shape = shape
            self._fill_shape_filled = bool(filled)
            self.needs_redraw = True

    def _draw_fill_shape(self):
        """The gesture itself: an outline over the viewport while it is drawn.

        Clipped to the viewport: a lasso drawn across a panel stops at its
        edge rather than being painted over it, which is what says the panel
        is not part of the model.
        """
        shape = self._fill_shape
        if not shape or len(shape) < 2:
            return

        thickness = max(1, self.px(1))
        with draw.clip(self.viewport.x, self.viewport.y,
                       self.viewport.width, self.viewport.height):
            if self._fill_shape_filled and len(shape) >= 3:
                draw.polygon(shape, theme.ACCENT[:3] + (0.12,), smooth=False)
            draw.polyline(shape, theme.ACCENT, thickness, closed=True)

    def _draw_splitter(self):
        """A line on the edge under the pointer, so the grab zone is findable.

        Nothing is drawn until the pointer is on it: a permanent handle would
        be a third vertical line beside two panel borders that already read as
        edges.
        """
        side = self._splitter or self._over_splitter
        if side is None:
            return

        panel = self.layers if side == 'LAYERS' else self.display
        x = panel.x if side == 'LAYERS' else panel.x + panel.width
        thickness = max(1, self.px(2))
        draw.rect(
            x - thickness // 2, panel.y, thickness, panel.height,
            theme.ACCENT if self._splitter else
            theme.ACCENT[:3] + (theme.ACCENT[3] * 0.5,),
        )

    def _draw_shortcuts(self):
        """The key list, raised by F1. Read straight off the keymap table.

        Not a hand-written sheet: a list of shortcuts kept separately from the
        shortcuts goes out of date on the first change, and then it is worse
        than nothing.
        """
        if not self.shortcuts_visible:
            return

        rows = keymap.listing()
        if not rows:
            return

        size = self.px(theme.FONT_SIZE)
        title_size = self.px(theme.FONT_SIZE_TITLE)
        pad = self.px(14)
        gap = self.px(10)
        line = int(draw.text_dimensions("Ag", size)[1]) + self.px(8)

        key_width = max(
            int(draw.text_dimensions(accel, size)[0]) for accel, _, _ in rows
        )
        label_width = max(
            int(draw.text_dimensions(label, size)[0]) for _, label, _ in rows
        )

        # A heading for each context: the same key means two things depending
        # on where the pointer is, and a flat list of keys would be a lie.
        headings = {
            'LAYERS': "Over the layer panel",
            'CHROME': "Anywhere on the interface",
            'ANY': "Anywhere",
        }
        groups = []
        for context in keymap.CONTEXTS:
            entries = [row for row in rows if row[2] == context]
            if entries:
                groups.append((headings.get(context, context), entries))

        title = "Shortcuts"
        _, title_height = draw.text_dimensions(title, title_size)
        height = pad * 2 + int(title_height) + gap
        for _heading, entries in groups:
            height += line + len(entries) * line + gap
        width = pad * 2 + key_width + gap + label_width
        width = max(width, int(draw.text_dimensions(title, title_size)[0]) + pad * 2)

        x = (self.width - width) // 2
        y = (self.height - height) // 2

        radius = theme.radius(height, self._ui_scale, theme.PANEL_ROUNDNESS)
        draw.shadow(x, y, width, height, radius, theme.SHADOW_FAC,
                    self.px(theme.SHADOW_WIDTH))
        draw.rounded_rect(x, y, width, height, radius, theme.MENU_BACK)
        draw.rounded_rect_outline(x, y, width, height, radius, theme.MENU_OUTLINE)

        cursor = y + height - pad - int(title_height)
        draw.text(x + pad, cursor, title, title_size, theme.MENU_TEXT)
        cursor -= gap

        for heading, entries in groups:
            cursor -= line
            draw.text(x + pad, cursor, heading, size, theme.WIDGET_TEXT_DIM)
            for accel, label, _context in entries:
                cursor -= line
                draw.text(x + pad, cursor, accel, size, theme.ACCENT)
                draw.text(x + pad + key_width + gap, cursor, label, size,
                          theme.MENU_TEXT)
            cursor -= gap

    def _draw_tooltip(self):
        text = self._tooltip_text
        if not text:
            return

        size = self.px(theme.FONT_SIZE_SMALL)
        pad = self.px(6)
        text_width, text_height = draw.text_dimensions(text, size)

        width = int(text_width) + pad * 2
        height = int(text_height) + pad * 2

        # Below and right of the pointer, the way every other tooltip sits;
        # flipped back inside the region when it would fall off an edge.
        x = self._last_mouse[0] + self.px(14)
        y = self._last_mouse[1] - height - self.px(10)

        x = max(0, min(x, self.width - width))
        y = max(0, min(y, self.height - height))

        radius = theme.radius(height, self._ui_scale, theme.PANEL_ROUNDNESS)
        draw.shadow(x, y, width, height, radius, theme.SHADOW_FAC,
                    self.px(theme.SHADOW_WIDTH))
        draw.rounded_rect(x, y, width, height, radius, theme.TOOLTIP_BACK)
        draw.rounded_rect_outline(x, y, width, height, radius, theme.TOOLTIP_OUTLINE)
        draw.text(x + pad, y + pad, text, size, theme.TOOLTIP_TEXT)

    def set_status(self, text):
        if self.status_label.text != text:
            self.status_label.text = text
            # The header group is packed, not spread, so a longer or shorter
            # readout moves the buttons that follow it.
            self.on_resize()
            self.needs_redraw = True
