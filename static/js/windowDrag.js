// Shared window-drag helper. Replaces the duplicated mousedown / mousemove
// / mouseup + snap-to-top fullscreen + left/right edge dock patterns that
// were copy-pasted across calendar.js, tasks.js, gallery.js, emailLibrary.js,
// documentLibrary.js, theme.js. Behavior stays identical to the old per-file
// copies — each callsite provides its own enter/exit-fullscreen callbacks
// since the CSS class + inline styles differ per modal.
//
// API:
//   makeWindowDraggable(modal, { content, header, ...options })
//     modal:           the wrapping .modal element (or a standalone pane)
//     content:         the element being moved (usually .modal-content)
//     header:          the drag handle (usually .modal-header)
//     fsClass:         optional class name representing "fullscreen" state
//     onEnterFullscreen: optional () => void — called when cursor releases
//                        near the top edge (within SNAP_PX). Caller is
//                        responsible for adding fsClass + applying inline
//                        styles that produce the fullscreen layout.
//     onExitFullscreen:  optional (cx, cy) => void — called mid-drag when
//                        the cursor leaves the fullscreen "unsnap" band
//                        (down > UNSNAP_PX OR near either horizontal edge
//                        in dock-snap range). Caller restores windowed
//                        inline styles centered around the cursor.
//     skipSelector:    CSS selector for elements inside `header` whose
//                        clicks should NOT start a drag (close button,
//                        form fields, etc). Default: 'button, input, select'
//     onDragEnd:       optional (state) => void — fires after mouseup
//                        WHEN no snap was committed. state = { rect } so
//                        callers can persist the final position.
//     enableTouch:     bool — also wire touchstart/touchmove/touchend
//                        with the same drag (no fs/dock on touch). Default
//                        true on desktop, irrelevant on mobile (mobileSkip).
//     mobileSkip:      drag is disabled below this viewport width.
//                        Default 768. Set to 0 to never skip.
//     enableDock:      bool — enable left + right edge docks.
//                        Default true.
//     enableFullscreen: bool — enable top-edge fullscreen snap.
//                        Default true when onEnterFullscreen is supplied.

import { makeEdgeDockController } from './modalSnap.js';
import { makeWindowResizable } from './windowResize.js';

const SNAP_PX = 6;        // cursor distance from top edge for fullscreen snap
const UNSNAP_PX = 24;     // cursor distance from top before fullscreen exits
const DOCK_EDGE_PX = 60;  // cursor distance from L/R edge to trigger dock
                          // exit while still in fullscreen state

// CSS-var lookup for the rail+sidebar width — used to decide where the
// "left edge" effectively is during a fullscreen drag-out (the cursor
// has to pass the rail to count as "near left").
function _leftNavWidth() {
  const rs = getComputedStyle(document.documentElement);
  const rail = parseInt(rs.getPropertyValue('--icon-rail-w') || '48', 10) || 0;
  const sb = parseInt(rs.getPropertyValue('--sidebar-w') || '0', 10) || 0;
  return rail + sb;
}

export function makeWindowDraggable(modal, options = {}) {
  const content = options.content;
  const header = options.header;
  if (!content || !header) return;
  const fsClass = options.fsClass || null;
  const onEnterFullscreen = options.onEnterFullscreen || null;
  const onExitFullscreen = options.onExitFullscreen || null;
  const enableFullscreen = options.enableFullscreen !== false && !!onEnterFullscreen;
  const onDragEnd = options.onDragEnd || null;
  const onDragStart = options.onDragStart || null;
  const skipSelector = options.skipSelector || 'button, input, select';
  const mobileSkip = (typeof options.mobileSkip === 'number') ? options.mobileSkip : 768;
  const enableTouch = options.enableTouch !== false;
  const enableDock = options.enableDock !== false && !!modal;

  header.style.cursor = 'grab';
  header.style.userSelect = 'none';
  if (enableTouch) header.style.touchAction = 'none';

  // Edge/corner resize. Every draggable window also becomes resizable — the
  // same gesture a native desktop window uses (grab an edge or corner, drag).
  // Skipped on mobile (windows are full-screen sheets there) and while the
  // window is fullscreen-snapped or docked. Wired here so all ~12 callsites
  // get it without per-file changes.
  if (options.enableResize !== false) {
    const _dockClasses = ['modal-right-docked', 'modal-left-docked'];
    makeWindowResizable(content, {
      modal,
      mobileSkip,
      minWidth: options.minWidth,
      minHeight: options.minHeight,
      isLocked: () => (fsClass && modal && modal.classList.contains(fsClass))
        || (modal && _dockClasses.some((c) => modal.classList.contains(c))),
      storageKey: options.resizeStorageKey
        || (modal && modal.id ? 'winsize-' + modal.id
          : (content.id ? 'winsize-' + content.id : null)),
    });
  }

  const rightDock = enableDock ? makeEdgeDockController(modal, 'right') : null;
  // Left dock is opt-in (enableLeftDock). For most windows it's off — the
  // sidebar lives on the left, so a left dock collides with it. The email
  // window enables it so you can park the message on the left and read it
  // while replying in the document on the right.
  const leftDock = (enableDock && options.enableLeftDock) ? makeEdgeDockController(modal, 'left') : null;

  // Per-drag state, reset on mousedown.
  let dragging = false;
  let startX = 0, startY = 0;
  let startLeft = 0, startTop = 0;
  let dragDx = 0, dragDy = 0;
  let grabOffsetX = 0, grabOffsetY = 0;
  let snapHint = null;
  // Whether the pointer actually moved beyond a small threshold this drag.
  // Used to suppress the synthetic click the browser fires on mouseup —
  // header click handlers (e.g. "collapse expanded card / back to list")
  // would otherwise fire after a drag and collapse the modal contents.
  let movedDuringDrag = false;
  const MOVE_THRESHOLD = 4;
  let previousBodyCursor = '';
  let previousBodyUserSelect = '';
  let previousContentTransition = '';
  let previousContentWillChange = '';

  const _beginDragLock = () => {
    previousBodyCursor = document.body.style.cursor;
    previousBodyUserSelect = document.body.style.userSelect;
    document.body.classList.add('window-dragging-active');
    document.body.style.cursor = 'grabbing';
    document.body.style.userSelect = 'none';
  };

  const _endDragLock = () => {
    document.body.classList.remove('window-dragging-active');
    document.body.style.cursor = previousBodyCursor;
    document.body.style.userSelect = previousBodyUserSelect;
    content.style.transition = previousContentTransition;
    content.style.willChange = previousContentWillChange;
  };

  const _pinDragPosition = (left, top, transform = 'none') => {
    // Windowed drag placement must beat modal-specific fullscreen/docked/mobile
    // CSS. Plain inline left/top can lose to !important rules when mouseup
    // removes temporary drag classes, which lets the flex overlay re-center the
    // pane. This helper owns the windowed drag state until a snap/dock/open
    // reset explicitly releases it.
    content.classList.add('window-drag-positioned');
    content.style.setProperty('position', 'fixed', 'important');
    content.style.setProperty('left', left + 'px', 'important');
    content.style.setProperty('top', top + 'px', 'important');
    content.style.setProperty('margin', '0', 'important');
    content.style.setProperty('transform', transform, 'important');
  };

  const _releaseDragPosition = () => {
    content.classList.remove('window-drag-positioned');
    content.style.removeProperty('position');
    content.style.removeProperty('left');
    content.style.removeProperty('top');
    content.style.removeProperty('margin');
    content.style.removeProperty('transform');
  };

  const _applyDragTransform = () => {
    _pinDragPosition(
      startLeft,
      startTop,
      (dragDx || dragDy) ? `translate3d(${dragDx}px, ${dragDy}px, 0)` : 'none'
    );
  };

  const _commitDragTransform = () => {
    const r = content.getBoundingClientRect();
    // Freeze the rendered rect while the drag lock/no-transition class is
    // still active. If we remove the transform first, the flex-centered modal
    // layout can briefly reclaim the pane and snap it back to center.
    content.style.transition = 'none';
    _pinDragPosition(r.left, r.top, 'none');
    void content.offsetWidth;
    startLeft = r.left;
    startTop = r.top;
    dragDx = 0;
    dragDy = 0;
    return r;
  };

  const _cancelDragTransform = () => {
    dragDx = 0;
    dragDy = 0;
    content.style.setProperty('transform', 'none', 'important');
  };

  const _showSnapHint = (on) => {
    // Top-edge fullscreen hint. Side hints come from the dock controllers.
    if (!on) {
      if (snapHint) { snapHint.remove(); snapHint = null; }
      return;
    }
    if (snapHint) return;
    snapHint = document.createElement('div');
    snapHint.className = 'modal-snap-hint';
    snapHint.style.cssText =
      'position:fixed;left:0;top:0;right:0;bottom:0;' +
      'background:color-mix(in srgb, var(--accent-primary, #60a5fa) 12%, transparent);' +
      'border:2px dashed color-mix(in srgb, var(--accent-primary, #60a5fa) 60%, transparent);' +
      'z-index:9998;pointer-events:none;';
    document.body.appendChild(snapHint);
  };

  const _enterFs = () => {
    if (!onEnterFullscreen) return;
    if (fsClass && modal && modal.classList.contains(fsClass)) return;
    onEnterFullscreen();
  };
  const _exitFs = (cx, cy) => {
    if (!onExitFullscreen) return;
    if (fsClass && modal && !modal.classList.contains(fsClass)) return;
    onExitFullscreen(cx, cy);
    // After exit, re-anchor the drag offsets to the new windowed rect so
    // the drag continues smoothly from the cursor's position.
    const r = content.getBoundingClientRect();
    startX = cx; startY = cy;
    startLeft = r.left; startTop = r.top;
    _cancelDragTransform();
  };

  const _isFullscreen = () => fsClass && modal && modal.classList.contains(fsClass);

  const _startDrag = (cx, cy) => {
    dragging = true;
    _beginDragLock();
    if (modal) modal.classList.add('modal-dragging');
    // Match windowResize.js: kill the one-shot open animation before measuring.
    // The modal-enter animation owns `transform`; if we measure/drag while that
    // transform is still active, the visible panel can feel offset from the
    // cursor. Dragging is direct manipulation, so precision beats replaying the
    // intro flourish.
    content.style.animation = 'none';
    previousContentTransition = content.style.transition;
    previousContentWillChange = content.style.willChange;
    content.style.transition = 'none';
    content.style.willChange = 'transform';
    content.style.transform = 'none';
    // Force style recalculation before measuring; otherwise Chromium can still
    // report the animation fill/first-frame transform for this tick, which
    // makes the visual panel and drag math de-sync.
    void content.offsetWidth;
    const rect = content.getBoundingClientRect();
    if (onDragStart) {
      try { onDragStart({ rect, cx, cy }); } catch (_) {}
    }
    startX = cx; startY = cy;
    startLeft = rect.left; startTop = rect.top;
    grabOffsetX = cx - rect.left;
    grabOffsetY = cy - rect.top;
    // Pin position so the drag follows the cursor instead of fighting the
    // flex-centered overlay. Use the same cascade-proof path as mouseup so
    // start/move/end all agree about the authoritative windowed placement.
    _pinDragPosition(startLeft, startTop, 'none');
    dragDx = 0;
    dragDy = 0;
  };

  const _onMove = (cx, cy) => {
    if (!dragging) return;

    // Fullscreen state: unsnap on drag-down or drag toward either horizontal
    // edge. Update dock hover immediately after exit so a fast release
    // commits the dock instead of dropping the modal mid-air.
    if (_isFullscreen()) {
      // Corner guard: ignore the side edges while the cursor is still in the
      // top fullscreen band, so dragging across the top corners keeps
      // fullscreen instead of flipping into a corner dock.
      const inTopBand = cy <= SNAP_PX;
      const nearRight = !inTopBand && (window.innerWidth - cx) <= DOCK_EDGE_PX;
      const nearLeft = !inTopBand && (cx - _leftNavWidth()) <= DOCK_EDGE_PX;
      // Dragging a fullscreen window to a SIDE edge → keep it fullscreen and
      // just arm the side-dock hint; releasing there docks it (handled in
      // _onEnd, which drops the fullscreen class). Previously this exited
      // fullscreen first, which re-CENTERED the window — so it looked like
      // it "centered instead of docking". Only a downward drag unsnaps to a
      // windowed (centered) modal.
      if (nearRight && rightDock) {
        if (leftDock) leftDock.release();
        rightDock.onMove(cx, cy);
        return;
      }
      if (nearLeft && leftDock) {
        if (rightDock) rightDock.release();
        leftDock.onMove(cx, cy);
        return;
      }
      if (cy > UNSNAP_PX) {
        _exitFs(cx, cy);
        if (rightDock) rightDock.onMove(cx, cy);
        if (leftDock) leftDock.onMove(cx, cy);
      } else {
        if (rightDock) rightDock.release();
        if (leftDock) leftDock.release();
      }
      return;
    }
    // Right-docked: pulling away from the right edge un-docks. Same for left.
    if (rightDock && modal && modal.classList.contains('modal-right-docked')) {
      if (rightDock.onMove(cx, cy, { grabOffsetX, grabOffsetY })) {
        const r = content.getBoundingClientRect();
        startX = cx; startY = cy;
        startLeft = r.left; startTop = r.top;
        _cancelDragTransform();
        grabOffsetX = cx - r.left;
        grabOffsetY = cy - r.top;
      }
      return;
    }
    if (leftDock && modal && modal.classList.contains('modal-left-docked')) {
      if (leftDock.onMove(cx, cy, { grabOffsetX, grabOffsetY })) {
        const r = content.getBoundingClientRect();
        startX = cx; startY = cy;
        startLeft = r.left; startTop = r.top;
        _cancelDragTransform();
        grabOffsetX = cx - r.left;
        grabOffsetY = cy - r.top;
      }
      return;
    }
    // Windowed: just follow the cursor.
    if (Math.abs(cx - startX) > MOVE_THRESHOLD || Math.abs(cy - startY) > MOVE_THRESHOLD) {
      movedDuringDrag = true;
    }
    // Windowed: keep the authoritative layout anchor fixed and move the pane
    // on the compositor while the pointer is active. On mouseup we freeze the
    // transformed rendered rect back into left/top before removing drag lock.
    dragDx = cx - startX;
    dragDy = cy - startY;
    _applyDragTransform();
    // Corner guard: in the top fullscreen band the side docks stay OFF, so a
    // top corner only ever snaps to fullscreen — never the corner hybrid.
    const inTopBand = cy <= SNAP_PX;
    _showSnapHint(enableFullscreen && inTopBand);
    if (inTopBand) {
      if (rightDock) rightDock.release();
      if (leftDock) leftDock.release();
    } else {
      if (rightDock) rightDock.onMove(cx, cy);
      if (leftDock) leftDock.onMove(cx, cy);
    }
  };

  const _onEnd = (cx, cy) => {
    if (!dragging) return;
    dragging = false;
    _showSnapHint(false);

    // Top edge wins over side edges — fullscreen is the more common gesture.
    if (enableFullscreen && typeof cy === 'number' && cy <= SNAP_PX) {
      _cancelDragTransform();
      _releaseDragPosition();
      _endDragLock();
      if (modal) modal.classList.remove('modal-dragging');
      if (rightDock) rightDock.release();
      if (leftDock) leftDock.release();
      _enterFs();
      return;
    }
    if (rightDock && rightDock.hovering()) {
      _cancelDragTransform();
      _releaseDragPosition();
      _endDragLock();
      if (modal) modal.classList.remove('modal-dragging');
      if (leftDock) leftDock.release();
      if (fsClass && modal) modal.classList.remove(fsClass);  // dock takes over from fullscreen
      rightDock.commit();
      return;
    }
    if (leftDock && leftDock.hovering()) {
      _cancelDragTransform();
      _releaseDragPosition();
      _endDragLock();
      if (modal) modal.classList.remove('modal-dragging');
      if (rightDock) rightDock.release();
      if (fsClass && modal) modal.classList.remove(fsClass);
      leftDock.commit();
      return;
    }

    const r = _commitDragTransform();
    _endDragLock();
    if (modal) modal.classList.remove('modal-dragging');
    if (rightDock) rightDock.release();
    if (leftDock) leftDock.release();
    if (onDragEnd) {
      try { onDragEnd({ rect: r }); } catch (_) {}
    }
    // Some modal-specific drag-end handlers persist/restore inline left/top
    // after the shared helper commits the rendered rect. Plain style assignment
    // strips our !important priority, which lets the centered flex overlay win
    // on release. Re-assert the committed windowed rect after callbacks, and
    // once more on the next frame to survive synchronous render cleanup.
    _pinDragPosition(r.left, r.top, 'none');
    requestAnimationFrame(() => {
      if (!content.isConnected) return;
      if (!content.classList.contains('window-drag-positioned')) return;
      if (modal && (modal.classList.contains('modal-right-docked') || modal.classList.contains('modal-left-docked'))) return;
      if (fsClass && modal && modal.classList.contains(fsClass)) return;
      _pinDragPosition(r.left, r.top, 'none');
    });
  };

  const _swallowSyntheticClickIfNeeded = () => {
    if (!movedDuringDrag) return;
    const swallow = (clickEv) => {
      clickEv.stopPropagation();
      clickEv.preventDefault();
    };
    header.addEventListener('click', swallow, { capture: true, once: true });
    // Safety: if no click fires (some browsers), drop the listener.
    setTimeout(() => header.removeEventListener('click', swallow, { capture: true }), 50);
  };

  const _canStartFrom = (target) => !(skipSelector && target?.closest && target.closest(skipSelector));

  if (typeof window !== 'undefined' && window.PointerEvent) {
    header.addEventListener('pointerdown', (e) => {
      if (mobileSkip > 0 && window.innerWidth <= mobileSkip) return;
      if (!enableTouch && e.pointerType === 'touch') return;
      if (e.button !== undefined && e.button !== 0) return;
      if (!_canStartFrom(e.target)) return;
      e.preventDefault();
      movedDuringDrag = false;
      _startDrag(e.clientX, e.clientY);
      try { header.setPointerCapture?.(e.pointerId); } catch (_) {}
      const pointerId = e.pointerId;
      const onMove = (ev) => {
        if (ev.pointerId !== pointerId) return;
        ev.preventDefault();
        // Self-heal a missed pointerup/mouseup. If the button is no longer
        // down, finish the drag instead of letting the window keep chasing
        // future pointermove events.
        if ((ev.pointerType === 'mouse' || ev.pointerType === 'pen') && ev.buttons === 0) {
          onUp(ev);
          return;
        }
        _onMove(ev.clientX, ev.clientY);
      };
      const onUp = (ev) => {
        if (ev.pointerId !== pointerId) return;
        _onEnd(ev.clientX, ev.clientY);
        try { header.releasePointerCapture?.(pointerId); } catch (_) {}
        header.removeEventListener('pointermove', onMove);
        header.removeEventListener('pointerup', onUp);
        header.removeEventListener('pointercancel', onUp);
        _swallowSyntheticClickIfNeeded();
      };
      header.addEventListener('pointermove', onMove, { passive: false });
      header.addEventListener('pointerup', onUp);
      header.addEventListener('pointercancel', onUp);
    }, { passive: false });
  } else {
    header.addEventListener('mousedown', (e) => {
      if (mobileSkip > 0 && window.innerWidth <= mobileSkip) return;
      if (!_canStartFrom(e.target)) return;
      e.preventDefault();
      movedDuringDrag = false;
      _startDrag(e.clientX, e.clientY);
      const onMove = (ev) => {
        if (ev.buttons === 0) { onUp(ev); return; }
        _onMove(ev.clientX, ev.clientY);
      };
      const onUp = (ev) => {
        _onEnd(ev.clientX, ev.clientY);
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        _swallowSyntheticClickIfNeeded();
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });

    if (enableTouch) {
      header.addEventListener('touchstart', (e) => {
        if (mobileSkip > 0 && window.innerWidth <= mobileSkip) return;
        if (!_canStartFrom(e.target)) return;
        const t = e.touches[0];
        if (!t) return;
        e.preventDefault();
        movedDuringDrag = false;
        _startDrag(t.clientX, t.clientY);
        const onMove = (ev) => {
          ev.preventDefault();
          const tt = ev.touches[0];
          if (tt) _onMove(tt.clientX, tt.clientY);
        };
        const onEnd = (ev) => {
          const tt = (ev.changedTouches && ev.changedTouches[0]) || null;
          _onEnd(tt ? tt.clientX : null, tt ? tt.clientY : null);
          document.removeEventListener('touchmove', onMove);
          document.removeEventListener('touchend', onEnd);
          document.removeEventListener('touchcancel', onEnd);
          _swallowSyntheticClickIfNeeded();
        };
        document.addEventListener('touchmove', onMove, { passive: false });
        document.addEventListener('touchend', onEnd);
        document.addEventListener('touchcancel', onEnd);
      }, { passive: false });
    }
  }
}
