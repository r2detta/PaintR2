# SPDX-License-Identifier: GPL-3.0-or-later
"""The modal event loop that feeds the custom interface.

Because PaintR2 draws its own widgets, it also has to route its own input.
This operator runs for as long as the PaintR2 window is open, translates each
Blender event into a `UIEvent` with region-local coordinates, and hands it to
the widget tree.

Routing is split by where the mouse is:

* over PaintR2 chrome -> the event is consumed, so the viewport keymap
  underneath never fires;
* over the viewport gap -> the event is passed through, so orbiting, selection
  and the paint tools behave exactly as in a normal 3D Viewport.

A widget that has captured the mouse (a drag in progress) wins regardless of
where the pointer has wandered to.
"""

import bpy

from .. import paint
from .. import session
from .. import window as window_mod
from ..gui.widget import UIEvent
from .op_fill import FillStroke


# Events that must keep flowing to Blender. Window focus changes and timers
# are not ours to swallow, and doing so wedges the rest of the application.
# A tooltip has to appear while the pointer is *not* moving, and a modal
# operator only ever hears about movement. This timer is the heartbeat that
# lets the interface notice the pointer has come to rest.
_TOOLTIP_TICK = 0.15

# The beat the interface runs at while something is moving. A drag that is
# being animated needs frames, not clock ticks, and 0.15 s of them looks like
# the list is stuttering rather than sliding. Swapped in only for as long as
# the animation lasts: the rest of the time this window costs one timer wake
# every seventh of a second, and a redraw here is a full 3D render.
_ANIMATION_TICK = 1.0 / 60.0

_PASS_THROUGH_TYPES = {
    'WINDOW_DEACTIVATE',
    'TIMER',
    'TIMER0',
    'TIMER1',
    'TIMER2',
    'TIMER_JOBS',
    'TIMER_AUTOSAVE',
    'TIMER_REPORT',
    'TIMERREGION',
}

# Pointer and tablet input. Everything else that reaches Blender is a keyboard
# shortcut, and a keyboard shortcut is the one thing that can change what the
# strip shows without moving the pointer -- so those get a redraw scheduled
# after Blender has run them. Mouse events are left out on purpose: dragging
# and painting already redraw the region continuously, and one extra frame per
# motion event would be paid on every stroke.
_POINTER_TYPES = {
    'MOUSEMOVE',
    'INBETWEEN_MOUSEMOVE',
    'LEFTMOUSE',
    'MIDDLEMOUSE',
    'RIGHTMOUSE',
    'BUTTON4MOUSE',
    'BUTTON5MOUSE',
    'BUTTON6MOUSE',
    'BUTTON7MOUSE',
    'WHEELUPMOUSE',
    'WHEELDOWNMOUSE',
    'WHEELINMOUSE',
    'WHEELOUTMOUSE',
    'MOUSEROTATE',
    'MOUSESMARTZOOM',
    'TRACKPADPAN',
    'TRACKPADZOOM',
    'PEN',
    'ERASER',
}


class PAINTR2_OT_ui_modal(bpy.types.Operator):
    """Run the PaintR2 interface event loop"""

    bl_idname = "paintr2.ui_modal"
    bl_label = "PaintR2 Interface"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        state = session.state
        if not state.is_open:
            return {'CANCELLED'}

        # Once per window, and here because it makes a datablock to ask its
        # question -- which the draw handler that builds every panel spec
        # must not do. See `window.prime_enum_items`.
        window_mod.prime_enum_items()

        self._prev_mouse = None
        # The pointer shape currently set on the window, by name.
        self._cursor = 'DEFAULT'
        self._over_ui = None
        self._fill_stroke = None
        # The box or lasso being drawn, in region pixels, and which.
        self._fill_shape = None
        self._fill_shape_mode = 'CLICK'
        self._tick = _TOOLTIP_TICK
        self._timer = context.window_manager.event_timer_add(
            self._tick, window=context.window
        )
        state.modal_running = True
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        """The event loop. Guarded, because a raise here is a dead window.

        Blender cancels a modal operator that raises, and cancelling this one
        leaves the host area stripped of its own regions with nothing handling
        events -- an interface that is drawn and does not answer, with the
        reason in a console the user may not have open. So a failure closes
        the window and says why, once.
        """
        try:
            return self._modal(context, event)
        except Exception:  # noqa: BLE001
            import traceback

            print("[PaintR2] the interface raised, closing the window:")
            traceback.print_exc()
            try:
                window_mod.close()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            return self._finish()

    def _modal(self, context, event):
        state = session.state

        if not state.is_open or state.root is None:
            return self._finish()

        if session.find_window() is None:
            # The window went away without telling us; the watchdog will do
            # the rest of the cleanup.
            return self._finish()

        if event.type in _PASS_THROUGH_TYPES:
            # Timers are not ours to swallow -- other add-ons and Blender's own
            # jobs run on them -- but they are the only thing that fires while
            # the pointer sits still. Three things are read off that beat: the
            # tooltip clock, whether Blender changed something the strip
            # mirrors, and whether a Blender region has come back up. Without
            # the second one the interface only caught up when the pointer
            # moved, because a redraw was tagged solely by our own event
            # handling.
            if event.type == 'TIMER':
                # A region raised by a mode change -- the asset shelf is the
                # one that does this -- goes back down within a tick.
                window_mod.keep_host_stripped()

                # And the wireframe colour follows the preference, the tool
                # and Blender's own overlay switch, none of which come
                # through us.
                window_mod.sync_wire_theme()

                redraw = state.root.tick()

                # The poll is the expensive half of this beat -- it rebuilds
                # the whole row snapshot to compare it, measured at 0.46 ms
                # against 0.001 ms for everything else here. It is skipped when
                # the answer cannot matter: `tick()` already said the region is
                # repainting, and the draw pass rebuilds those rows anyway. On
                # a drag that runs the heartbeat at sixty a second, that is the
                # difference between doing the work twice a frame and once.
                if not redraw and window_mod.poll_external_changes(context):
                    redraw = True
                if redraw:
                    area = session.find_area()
                    if area is not None:
                        area.tag_redraw()

                self._retune(context, state.root)
            return {'PASS_THROUGH'}

        current_window = getattr(context, "window", None)
        if current_window is None or current_window.as_pointer() != state.window_ptr:
            return {'PASS_THROUGH'}

        area = session.find_area()
        region = session.find_region(area)
        if region is None:
            return {'PASS_THROUGH'}

        ui_event = UIEvent(event, region.x, region.y, self._prev_mouse)
        self._prev_mouse = (ui_event.mouse_x, ui_event.mouse_y)

        # The window is not always one area any more: the shader graph gets an
        # editor of its own beside ours. An event over that editor is its
        # event, and passing it through is the whole of the handling -- without
        # this the fill tools would cast rays for clicks landing on nodes.
        #
        # Unless a widget of ours has the pointer. A layer dragged left across
        # the boundary is still being dragged, and the release that ends it may
        # well land over there.
        outside = not (
            0 <= ui_event.mouse_x < region.width
            and 0 <= ui_event.mouse_y < region.height
        )
        if outside and not (state.root.has_capture or state.root.has_overlay):
            return {'PASS_THROUGH'}

        over_ui = state.root.is_over_ui(ui_event.mouse_x, ui_event.mouse_y)

        # Crossing between the panels and the viewport: park or re-arm the
        # brush cursor. Only on the crossing, never per event.
        if over_ui != self._over_ui:
            self._over_ui = over_ui
            # A fill tool has taken the left button; a brush circle following
            # the pointer would be advertising a stroke that will not happen.
            paint.set_brush_cursor(
                not over_ui and not window_mod.is_fill_tool(context), context
            )

        # The fill tools answer "which faces would this click take" before the
        # click. Only on MOUSEMOVE: INBETWEEN_MOUSEMOVE arrives at input-device
        # rate and each one of these is a ray cast against the scene.
        if event.type == 'MOUSEMOVE':
            if over_ui or self._fill_stroke is not None or event.alt:
                moved = window_mod.clear_fill_preview()
            else:
                space = session.find_space()
                moved = window_mod.update_fill_preview(
                    context,
                    region,
                    getattr(space, "region_3d", None) if space is not None else None,
                    ui_event.mouse_x,
                    ui_event.mouse_y,
                )
            if moved and area is not None:
                area.tag_redraw()

        # Blender fires INBETWEEN_MOUSEMOVE at input-device rate. Over the
        # panels that resolution buys nothing -- hover only changes per row --
        # while each one costs a full redraw of the region, viewport included.
        if over_ui and event.type == 'INBETWEEN_MOUSEMOVE':
            return {'RUNNING_MODAL'}

        # Escape is a deliberate escape hatch while the interface is still a
        # shell. Only over the chrome -- in the viewport it belongs to Blender,
        # which uses it to cancel operators -- and never while a menu, picker
        # or text field is up, because there Escape means "cancel that".
        if (
            over_ui
            and not state.root.has_overlay
            and event.type == 'ESC'
            and event.value == 'PRESS'
        ):
            window_mod.request_close()
            return self._finish()

        # The tree always sees the event, even over the viewport, so hover
        # state stays correct as the mouse crosses the boundary.
        consumed = state.root.handle_event(ui_event)

        # Whatever that event started or ended, the pointer says so: a row in
        # the air gets a closed hand, the scrollbar gets a scroll cursor.
        self._sync_cursor(context, state.root)

        if state.root.needs_redraw and area is not None:
            area.tag_redraw()

        # A fill stroke keeps the mouse until the button comes up, even if the
        # pointer has wandered onto the panels: releasing over a panel has to
        # end the stroke, not leave it running.
        if self._fill_stroke is not None or not (consumed or over_ui):
            if self._drive_fill(context, event, ui_event, region):
                if area is not None:
                    area.tag_redraw()
                return {'RUNNING_MODAL'}

        if consumed or over_ui:
            return {'RUNNING_MODAL'}

        # Over the viewport: hand the event to Blender so navigation, selection
        # and the paint tools work exactly as they do in a normal viewport.
        # A keyboard shortcut there may well be one that moves a value the
        # strip shows -- X swaps the brush colours -- and Blender does not
        # redraw this region for it, so ask for one after it has run.
        if event.type not in _POINTER_TYPES and event.value in {'PRESS', 'RELEASE'}:
            session.tag_redraw_soon()

        return {'PASS_THROUGH'}

    def _drive_fill(self, context, event, ui_event, region):
        """Press, sweep and release for the three fill tools.

        Returns True when the event was ours. A fill tool consumes the left
        button whether or not the fill can actually run: passing it through to
        Blender because there is no paintable layer would put down a brush
        stroke the user did not ask for, which is worse than doing nothing.
        """
        if event.type != 'LEFTMOUSE' and self._fill_stroke is None:
            return False

        space = session.find_space()
        rv3d = getattr(space, "region_3d", None) if space is not None else None

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if not window_mod.is_fill_tool(context):
                return False

            # Alt+LMB is not ours. On the keymaps that emulate a three-button
            # mouse -- and on Industry Compatible -- Alt+LMB *is* the middle
            # button, which is how the view is orbited; Shift+Alt and Ctrl+Alt
            # are pan and zoom off the same rule, so Alt alone is the test.
            # Taking the button unconditionally left the fill tools with no way
            # to turn the model, which is not a tool you can use.
            if event.alt:
                return False

            self._fill_stroke = FillStroke(context)

            # Box and lasso are a shape drawn first and filled on release; a
            # click fills as it goes. The stroke object is the same either way
            # -- what changes is when it is told what to fill.
            mode = window_mod.fill_select_mode(context)
            if mode in ('RECT', 'LASSO'):
                self._fill_shape = [(ui_event.mouse_x, ui_event.mouse_y)]
                self._fill_shape_mode = mode
                window_mod.set_fill_shape(self._fill_shape, mode)
                return True

            if region is not None and rv3d is not None:
                self._fill_stroke.add(
                    context, region, rv3d, ui_event.mouse_x, ui_event.mouse_y
                )
            return True

        if self._fill_stroke is None:
            return False

        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            if self._fill_shape is not None:
                shape = window_mod.fill_shape_points(
                    self._fill_shape, self._fill_shape_mode
                )
                if region is not None and rv3d is not None:
                    self._fill_stroke.add_shape(context, region, rv3d, shape)
                self._end_shape()

            self._fill_stroke.finish()
            self._fill_stroke = None
            return True

        if self._fill_shape is not None:
            if event.type in ('MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'):
                point = (ui_event.mouse_x, ui_event.mouse_y)
                if self._fill_shape_mode == 'RECT':
                    # Two corners are the whole of a rectangle; anything else
                    # in the list would be a corner the hand did not put there.
                    self._fill_shape[1:] = [point]
                elif point != self._fill_shape[-1]:
                    self._fill_shape.append(point)
                window_mod.set_fill_shape(self._fill_shape, self._fill_shape_mode)
            return True

        # Sweeping. Every face the pointer crosses joins the same stroke, and
        # the stroke remembers what it has already filled, so a pass back over
        # covered ground does not lay the colour down twice.
        if event.type in ('MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'):
            if region is not None and rv3d is not None:
                self._fill_stroke.add(
                    context, region, rv3d, ui_event.mouse_x, ui_event.mouse_y
                )
            return True

        return False

    def _end_shape(self):
        self._fill_shape = None
        self._fill_shape_mode = 'CLICK'
        window_mod.set_fill_shape(None, 'CLICK')

    def _end_fill(self):
        """Never leave a stroke half-open, or a highlight on a closed window."""
        if self._fill_stroke is not None:
            self._fill_stroke.finish()
            self._fill_stroke = None
        self._end_shape()
        window_mod.clear_fill_preview()

    def cancel(self, context):
        self._end_fill()
        self._restore_cursor(context)
        self._remove_timer(context)
        session.state.modal_running = False

    def _finish(self):
        context = bpy.context
        self._end_fill()
        self._restore_cursor(context)
        self._remove_timer(context)
        session.state.modal_running = False
        return {'FINISHED'}

    def _sync_cursor(self, context, root):
        """Give the pointer the shape the interface is asking for.

        `gui/` has no window to set a cursor on, so it names one and this
        applies it. Set once per change, not per event: `cursor_modal_set` is
        a mode, and calling it on every mouse move is how a cursor ends up
        stuck after the gesture that wanted it is over.
        """
        wanted = root.cursor
        if wanted == self._cursor:
            return

        window = getattr(context, "window", None)
        if window is None:
            return

        try:
            if wanted == 'DEFAULT':
                window.cursor_modal_restore()
            else:
                window.cursor_modal_set(wanted)
        except (AttributeError, RuntimeError, TypeError):
            return

        self._cursor = wanted

    def _retune(self, context, root):
        """Run the heartbeat fast while the interface is animating, slow after.

        Two rates rather than one: the fast one exists for the second or so a
        layer is in the air, and paying for it the rest of the time would mean
        sixty viewport renders a second for a window that is sitting still.
        """
        wanted = _ANIMATION_TICK if root.wants_fast_tick else _TOOLTIP_TICK
        if wanted == self._tick:
            return

        self._remove_timer(context)
        self._tick = wanted
        window = session.find_window()
        if window is None:
            return
        self._timer = context.window_manager.event_timer_add(
            wanted, window=window
        )

    def _remove_timer(self, context):
        timer = getattr(self, "_timer", None)
        if timer is None:
            return
        self._timer = None
        try:
            context.window_manager.event_timer_remove(timer)
        except (RuntimeError, TypeError, ReferenceError):
            pass

    def _restore_cursor(self, context):
        """Never leave Blender with the brush cursor switched off."""
        if self._cursor != 'DEFAULT':
            window = getattr(context, "window", None)
            if window is not None:
                try:
                    window.cursor_modal_restore()
                except (AttributeError, RuntimeError):
                    pass
            self._cursor = 'DEFAULT'

        if self._over_ui:
            try:
                paint.set_brush_cursor(True, context)
            except (AttributeError, RuntimeError):
                pass
            self._over_ui = False


CLASSES = (PAINTR2_OT_ui_modal,)
