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
    assert "grabOffsetX = cx - rect.left;" in source
    assert "rightDock.onMove(cx, cy, { grabOffsetX, grabOffsetY })" in source
    assert "leftDock.onMove(cx, cy, { grabOffsetX, grabOffsetY })" in source
