import json
import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_node(script: str):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_undock_target_preserves_cursor_grab_offset():
    data = _run_node(textwrap.dedent(
        """
        import { _computeUndockTarget } from './static/js/modalSnap.js';
        const target = _computeUndockTarget({
          cx: 360,
          cy: 85,
          refW: 500,
          refH: 300,
          grabOffsetX: 120,
          grabOffsetY: 24,
          viewportW: 1200,
          viewportH: 800,
        });
        console.log(JSON.stringify(target));
        """
    ))

    # Old peel-off behavior centered the floating window at x=360, producing
    # left=110. The cursor then appeared detached from the grabbed header spot.
    assert data == {"left": 240, "top": 61}


def test_undock_target_clamps_to_viewport_when_grab_point_would_push_offscreen():
    data = _run_node(textwrap.dedent(
        """
        import { _computeUndockTarget } from './static/js/modalSnap.js';
        const target = _computeUndockTarget({
          cx: 990,
          cy: 20,
          refW: 400,
          refH: 240,
          grabOffsetX: 12,
          grabOffsetY: 10,
          viewportW: 1000,
          viewportH: 700,
        });
        console.log(JSON.stringify(target));
        """
    ))

    assert data == {"left": 592, "top": 10}


def test_window_drag_passes_grab_offset_when_peeling_docked_window():
    source = (ROOT / "static/js/windowDrag.js").read_text(encoding="utf-8")
    assert "content.style.animation = 'none';" in source
    assert "content.style.transform = 'none';" in source
    assert source.index("content.style.transform = 'none';") < source.index("const rect = content.getBoundingClientRect();")
    assert "void content.offsetWidth;" in source
    assert "grabOffsetX = cx - rect.left;" in source
    assert "rightDock.onMove(cx, cy, { grabOffsetX, grabOffsetY })" in source
    assert "leftDock.onMove(cx, cy, { grabOffsetX, grabOffsetY })" in source


def test_window_drag_uses_native_pointer_capture_and_drag_lock():
    source = (ROOT / "static/js/windowDrag.js").read_text(encoding="utf-8")
    css = (ROOT / "static/style.css").read_text(encoding="utf-8")

    assert "window.PointerEvent" in source
    assert "header.setPointerCapture?.(e.pointerId)" in source
    assert "header.releasePointerCapture?.(pointerId)" in source
    assert "window-dragging-active" in source
    assert "document.body.style.cursor = 'grabbing';" in source
    assert "header.style.touchAction = 'none';" in source
    assert "body.window-dragging-active" in css


def test_window_drag_pointer_events_preserve_enable_touch_option():
    source = (ROOT / "static/js/windowDrag.js").read_text(encoding="utf-8")

    assert "if (enableTouch) header.style.touchAction = 'none';" in source
    assert "if (!enableTouch && e.pointerType === 'touch') return;" in source
    assert source.index("if (!enableTouch && e.pointerType === 'touch') return;") < source.index("_startDrag(e.clientX, e.clientY);")


def test_window_drag_commits_live_transform_before_releasing_drag_lock():
    source = (ROOT / "static/js/windowDrag.js").read_text(encoding="utf-8")
    css = (ROOT / "static/style.css").read_text(encoding="utf-8")

    # Direct left/top updates fight the flex-centered modal layout in the real
    # app. Live dragging uses a compositor transform, then freezes the rendered
    # rect into fixed left/top before removing the drag/no-transition guard.
    assert "content.style.setProperty('position', 'fixed', 'important');" in source
    assert "content.style.setProperty('left', left + 'px', 'important');" in source
    assert "content.style.setProperty('top', top + 'px', 'important');" in source
    assert "content.style.setProperty('margin', '0', 'important');" in source
    assert "content.classList.add('window-drag-positioned');" in source
    assert ".modal-content.window-drag-positioned" in css
    assert "let dragDx = 0, dragDy = 0;" in source
    assert "translate3d(${dragDx}px, ${dragDy}px, 0)" in source
    assert "const _commitDragTransform = () =>" in source
    assert "const r = content.getBoundingClientRect();" in source
    assert "_pinDragPosition(r.left, r.top, 'none');" in source
    assert "const r = _commitDragTransform();" in source
    assert "if (onDragEnd)" in source
    assert "requestAnimationFrame(() =>" in source
    assert "_pinDragPosition(r.left, r.top, 'none');" in source
    assert source.index("const r = _commitDragTransform();") < source.index("if (onDragEnd) {")
    assert source.index("if (onDragEnd) {") < source.rindex("_pinDragPosition(r.left, r.top, 'none');")
    assert "content.style.left = (startLeft + cx - startX) + 'px';" not in source
    assert "content.style.top = (startTop + cy - startY) + 'px';" not in source
    assert "pendingMove" not in source
    assert "content.style.transition = 'none';" in source
    assert "content.style.transition = previousContentTransition;" in source


def test_window_drag_self_heals_lost_mouseup_and_uses_non_passive_touchmove():
    source = (ROOT / "static/js/windowDrag.js").read_text(encoding="utf-8")

    assert "ev.buttons === 0" in source
    assert "{ passive: false }" in source
    assert "document.addEventListener('touchmove', onMove, { passive: false })" in source


def test_service_worker_registration_uses_root_scope_with_allowed_header():
    index = (ROOT / "static/index.html").read_text(encoding="utf-8")
    app = (ROOT / "app.py").read_text(encoding="utf-8")

    assert "navigator.serviceWorker.register('/static/sw.js', { scope: '/' })" in index
    assert 'resp.headers["Service-Worker-Allowed"] = "/"' in app


def test_sw_reset_is_scoped_to_odysseus_worker_and_cache_names():
    reset = (ROOT / "static/js/swReset.js").read_text(encoding="utf-8")

    assert "new URL(url).pathname === swPath" in reset
    assert "const cachePrefix = options.cachePrefix || 'odysseus-'" in reset
    assert "key.startsWith(cachePrefix)" in reset
    assert "const odysseusRegs = regs.filter(isOdysseusWorker)" in reset
