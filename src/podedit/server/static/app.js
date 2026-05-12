// podedit UI — W4: select, delete, undo/redo, preview-skip, autosave, KPI.

(async function () {
  const $ = (id) => document.getElementById(id);
  const $player = $('player');
  const $name = $('audio-name');
  const $meta = $('audio-meta');
  const $tx = $('transcript');
  const $btnDelete = $('btn-delete');
  const $btnUndo = $('btn-undo');
  const $btnRedo = $('btn-redo');
  const $btnClear = $('btn-clear-sel');
  const $saveStatus = $('save-status');
  const $kpiOps = $('kpi-ops');
  const $kpiCut = $('kpi-cut');
  const $kpiElapsed = $('kpi-elapsed');
  const $skipPill = $('skip-pill');
  const $kpiTime = $('kpi-time');

  // ------- utilities -------
  const fmt = (s) => {
    if (s == null || isNaN(s)) return '–';
    const m = Math.floor(s / 60);
    const r = s - m * 60;
    return `${m}:${r.toFixed(2).padStart(5, '0')}`;
  };
  const fmtMs = (s) => `${s.toFixed(1)}s`;
  function newOpId() {
    // Prefer crypto.randomUUID where available; fall back to 8-char hex.
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return 'op-' + crypto.randomUUID();
    }
    let s = '';
    for (let i = 0; i < 8; i++) s += ((Math.random() * 16) | 0).toString(16);
    return 'op-' + s;
  }
  async function fetchJSON(url, init) {
    const r = await fetch(url, init);
    if (!r.ok) throw new Error(`${url} -> HTTP ${r.status}`);
    return r.json();
  }
  function logKPI(type, extra) {
    const event = { type, client_ts: Date.now() / 1000, ...extra };
    fetch('/api/kpi/event', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(event),
      keepalive: true,  // best-effort delivery even on page unload
    }).catch(() => {});  // KPI is fire-and-forget; UI must not block on it
  }

  // ------- state -------
  const state = {
    ops: [],           // active delete ops
    mergedDeletes: [], // normalized [start, end][] cached from ops; for preview-skip + total
    undoStack: [],     // {type:'add'|'remove', op}
    redoStack: [],
    selection: null,   // {anchor: wordIdx, extent: wordIdx} (inclusive)
    sessionTemplate: null,
    saveStatus: 'idle',
    saveTimer: null,
    saveDirty: false,
    saveSeq: 0,        // monotonic; only the latest save can claim 'saved'
  };

  function recomputeMergedDeletes() {
    if (state.ops.length === 0) { state.mergedDeletes = []; return; }
    const sorted = state.ops.map((o) => [o.start, o.end]).sort((a, b) => a[0] - b[0]);
    const merged = [sorted[0].slice()];
    for (let i = 1; i < sorted.length; i++) {
      const cur = sorted[i];
      const last = merged[merged.length - 1];
      if (cur[0] <= last[1]) last[1] = Math.max(last[1], cur[1]);
      else merged.push(cur.slice());
    }
    state.mergedDeletes = merged;
  }
  const words = [];    // [{el, start, end, idx, segIdx}]
  const kpi = { sessionStartedAt: Date.now() / 1000, firstOpAt: null, opsCount: 0 };

  // ------- load -------
  const t0 = performance.now();
  let info, tx, session;
  try {
    [info, tx, session] = await Promise.all([
      fetchJSON('/api/audio/info'),
      fetchJSON('/api/transcript'),
      fetchJSON('/api/session'),
    ]);
  } catch (e) {
    $tx.innerHTML = `<div class="status error">Failed to load: ${e.message}</div>`;
    return;
  }
  state.sessionTemplate = session;
  state.ops = session.ops || [];
  logKPI('ui.loaded', { latency_ms: performance.now() - t0, has_existing_session: state.ops.length > 0 });

  $name.textContent = info.name;
  $meta.textContent = [
    fmt(info.duration_sec),
    info.sample_rate && `${info.sample_rate}Hz`,
    info.channels && `${info.channels}ch`,
    info.codec,
    tx.model_config && `· model: ${tx.model_config.model}`,
  ].filter(Boolean).join(' · ');
  $player.src = info.url;

  // Diagnostic logging: if seeks are silently ignored, we still see seeking/
  // seeked/error events in the KPI log.
  $player.addEventListener('seeking', () => logKPI('ui.audio.seeking', { t: $player.currentTime }));
  $player.addEventListener('seeked', () => logKPI('ui.audio.seeked', { t: $player.currentTime }));
  $player.addEventListener('error', () => logKPI('ui.audio.error', {
    code: $player.error && $player.error.code, message: $player.error && $player.error.message,
  }));
  $player.addEventListener('stalled', () => logKPI('ui.audio.stalled', { t: $player.currentTime }));

  // ------- render -------
  $tx.innerHTML = '';
  let idx = 0;
  tx.segments.forEach((seg, segIdx) => {
    const div = document.createElement('div');
    div.className = 'segment';
    const time = document.createElement('span');
    time.className = 'seg-time';
    time.textContent = fmt(seg.start);
    div.appendChild(time);
    (seg.words || []).forEach((w) => {
      const span = document.createElement('span');
      span.className = 'word';
      const wordIdx = idx;  // capture per-iteration value
      span.dataset.idx = String(wordIdx);
      span.textContent = w.text;
      const word = { el: span, start: w.start, end: w.end, idx: wordIdx, segIdx };
      words.push(word);
      span.addEventListener('mousedown', (ev) => onWordMouseDown(wordIdx, ev));
      span.addEventListener('mouseenter', () => onWordMouseEnter(wordIdx));
      div.appendChild(span);
      idx += 1;
    });
    $tx.appendChild(div);
  });

  // ------- selection + clicks -------
  // Drag-to-select model:
  //   mousedown on a word → start a tentative selection at that anchor
  //   mouseenter on another word while still held → extend selection (drag)
  //   mouseup → if we never crossed words, treat as a plain click (seek+play);
  //             if we did cross, finalize the drag selection
  // Shift+mousedown still extends from the existing anchor (W3 behavior).
  let dragAnchor = null;  // word idx where mouse went down; null when idle
  let dragMoved = false;  // did we enter at least one other word before mouseup
  let dragStartT = 0;

  function onWordMouseDown(i, ev) {
    if (ev.button !== undefined && ev.button !== 0) return;  // left button only
    if (ev.shiftKey && state.selection) {
      state.selection = { anchor: state.selection.anchor, extent: i };
      renderSelection();
      ev.preventDefault();
      return;
    }
    dragAnchor = i;
    dragMoved = false;
    dragStartT = performance.now();
    state.selection = { anchor: i, extent: i };
    renderSelection();
    ev.preventDefault();
  }

  function onWordMouseEnter(i) {
    if (dragAnchor === null) return;
    if (i !== dragAnchor) dragMoved = true;
    if (!state.selection || state.selection.extent !== i) {
      state.selection = { anchor: dragAnchor, extent: i };
      renderSelection();
    }
  }

  function finishDrag(ev) {
    if (dragAnchor === null) return;
    const wasDrag = dragMoved;
    const startIdx = dragAnchor;
    dragAnchor = null;
    dragMoved = false;

    if (!wasDrag) {
      // Plain click: seek + play. Selection collapses to this one word so D
      // can still delete a single word if the user wants.
      const w = words[startIdx];
      $player.currentTime = w.start;
      const p = $player.play();
      if (p && typeof p.catch === 'function') p.catch(() => {});
      logKPI('ui.click.word', { word_idx: startIdx, t: w.start });
    } else {
      const r = selectionRange();
      if (r) {
        logKPI('ui.drag.select', {
          ws: r.ws, we: r.we, start: r.start, end: r.end,
          duration: r.end - r.start, latency_ms: performance.now() - dragStartT,
        });
      }
    }
  }

  // mouseup fires on document so we still finalize even if the user releases
  // outside the transcript (e.g. on the audio player or scrollbar).
  document.addEventListener('mouseup', finishDrag);
  // Drag can be aborted by leaving the window mid-drag; treat it the same as
  // releasing in place so we don't leave dragAnchor sticky.
  document.addEventListener('mouseleave', finishDrag);

  // Backstop for sensitivity: track mouse position during drag and resolve
  // the word directly under the cursor each rAF. mouseenter alone misses
  // words when the user drags fast across line breaks or through inter-word
  // gaps. Throttled to rAF so we don't thrash DOM hit testing.
  let pendingDragMove = null;  // {x, y} latest mouse position; processed on rAF
  document.addEventListener('mousemove', (e) => {
    if (dragAnchor === null) return;
    pendingDragMove = { x: e.clientX, y: e.clientY };
  });
  function dragMoveTick() {
    if (pendingDragMove && dragAnchor !== null) {
      let el = document.elementFromPoint(pendingDragMove.x, pendingDragMove.y);
      // Walk up in case the hit target is a text node wrapper.
      while (el && !el.classList?.contains('word')) el = el.parentElement;
      if (el && el.classList.contains('word')) {
        const i = parseInt(el.dataset.idx, 10);
        if (!Number.isNaN(i)) onWordMouseEnter(i);
      }
      pendingDragMove = null;
    }
    requestAnimationFrame(dragMoveTick);
  }
  requestAnimationFrame(dragMoveTick);

  function selectionRange() {
    if (!state.selection) return null;
    const a = Math.min(state.selection.anchor, state.selection.extent);
    const b = Math.max(state.selection.anchor, state.selection.extent);
    return { start: words[a].start, end: words[b].end, ws: a, we: b };
  }

  function renderSelection() {
    const r = selectionRange();
    for (const w of words) w.el.classList.remove('selected');
    if (r) for (let i = r.ws; i <= r.we; i++) words[i].el.classList.add('selected');
    $btnDelete.disabled = !r;
    $btnClear.disabled = !r;
  }

  function clearSelection() {
    state.selection = null;
    renderSelection();
  }

  // ------- ops -------
  function applyDeleteRange(start, end) {
    if (end <= start) return;
    const op = { op_id: newOpId(), op: 'delete', start, end, note: null };
    state.ops.push(op);
    state.undoStack.push({ type: 'add', op });
    state.redoStack = [];
    kpi.opsCount += 1;
    if (!kpi.firstOpAt) kpi.firstOpAt = Date.now() / 1000;
    logKPI('ui.op.delete', { op_id: op.op_id, start, end, duration: end - start });
    rerenderOps();
    refreshButtons();
    scheduleSave();
  }

  function deleteSelected() {
    const r = selectionRange();
    if (!r) return;
    applyDeleteRange(r.start, r.end);
    clearSelection();
  }

  function undo() {
    const entry = state.undoStack.pop();
    if (!entry) return;
    if (entry.type === 'add') {
      state.ops = state.ops.filter((o) => o.op_id !== entry.op.op_id);
    } else if (entry.type === 'remove') {
      state.ops.push(entry.op);
    }
    state.redoStack.push(entry);
    logKPI('ui.op.undo', { op_id: entry.op.op_id });
    rerenderOps();
    refreshButtons();
    scheduleSave();
  }

  function redo() {
    const entry = state.redoStack.pop();
    if (!entry) return;
    if (entry.type === 'add') {
      state.ops.push(entry.op);
    } else if (entry.type === 'remove') {
      state.ops = state.ops.filter((o) => o.op_id !== entry.op.op_id);
    }
    state.undoStack.push(entry);
    logKPI('ui.op.redo', { op_id: entry.op.op_id });
    rerenderOps();
    refreshButtons();
    scheduleSave();
  }

  function rerenderOps() {
    recomputeMergedDeletes();
    // Tag every word as deleted if it lies entirely within ANY merged delete
    // range. Strict inclusion avoids half-shading words that only partly fall
    // in a cut; partial-overlap visualization arrives in W5.
    for (const w of words) {
      const inCut = state.mergedDeletes.some(([s, e]) => w.start >= s && w.end <= e);
      w.el.classList.toggle('deleted', inCut);
    }
    updateStats();
  }

  function refreshButtons() {
    $btnUndo.disabled = state.undoStack.length === 0;
    $btnRedo.disabled = state.redoStack.length === 0;
  }

  function updateStats() {
    // Use merged ranges so overlapping/duplicate ops don't double-count.
    const cut = state.mergedDeletes.reduce((s, [a, b]) => s + (b - a), 0);
    $kpiOps.textContent = String(state.ops.length);
    $kpiCut.textContent = fmtMs(cut);
  }

  function tickElapsed() {
    const elapsed = Math.floor(Date.now() / 1000 - kpi.sessionStartedAt);
    const m = Math.floor(elapsed / 60);
    const s = elapsed % 60;
    $kpiElapsed.textContent = `${m}:${String(s).padStart(2, '0')}`;
  }
  setInterval(tickElapsed, 1000);
  tickElapsed();

  // ------- autosave -------
  function setSaveStatus(s) {
    state.saveStatus = s;
    $saveStatus.className = `save-${s.split(':')[0]}`;
    const map = {
      'idle': '·',
      'saving': 'saving…',
      'saved': 'saved',
      'error': '✕ save error',
    };
    $saveStatus.textContent = map[s.split(':')[0]] ?? s;
    $saveStatus.title = s;
  }
  function buildSessionJSON() {
    return { ...state.sessionTemplate, ops: state.ops };
  }
  function scheduleSave() {
    state.saveDirty = true;
    setSaveStatus('idle');
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(doSave, 300);
  }
  async function doSave() {
    const mySeq = ++state.saveSeq;
    setSaveStatus('saving');
    const t = performance.now();
    try {
      const body = JSON.stringify(buildSessionJSON());
      const r = await fetch('/api/session', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body,
      });
      if (!r.ok) throw new Error(`HTTP ${r.status} ${(await r.text()).slice(0, 200)}`);
      const reply = await r.json();
      // A newer save started while we were in flight; let it own the final status.
      if (mySeq !== state.saveSeq) return;
      state.saveDirty = false;
      setSaveStatus('saved');
      logKPI('ui.session.saved', { latency_ms: performance.now() - t, ops: reply.ops });
    } catch (e) {
      if (mySeq !== state.saveSeq) return;
      setSaveStatus('error:' + e.message);
      logKPI('ui.session.save_error', { error: e.message });
    }
  }

  // ------- preview-skip + highlight -------
  let activeIdx = -1;
  function findActive(t) {
    let lo = 0, hi = words.length - 1, ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const w = words[mid];
      if (t < w.start) hi = mid - 1;
      else if (t >= w.end) lo = mid + 1;
      else { ans = mid; break; }
    }
    return ans;
  }
  function scrollIfOffscreen(el) {
    const r = el.getBoundingClientRect();
    const margin = 80;
    if (r.top < margin || r.bottom > window.innerHeight - margin) {
      el.scrollIntoView({ block: 'center', behavior: 'auto' });
    }
  }
  let lastSkipAt = 0;
  let isSkipping = false;  // guard so we don't fire concurrent pause/seek/play

  async function jumpPast(targetEnd) {
    // pause→currentTime=→play with an awaited 'seeked' event. AAC seeks via
    // bare `currentTime = x` can be silently deferred mid-playback over a
    // forwarded proxy; the pause/play cycle forces the audio element to honor
    // the seek immediately.
    isSkipping = true;
    const wasPlaying = !$player.paused && !$player.ended;
    try {
      $player.pause();
      $player.currentTime = targetEnd;
      await new Promise((resolve) => {
        let done = false;
        const finish = () => { if (!done) { done = true; resolve(); } };
        $player.addEventListener('seeked', finish, { once: true });
        setTimeout(finish, 200);  // hard ceiling: don't block playback forever
      });
      if (wasPlaying) {
        const p = $player.play();
        if (p && typeof p.catch === 'function') p.catch(() => {});
      }
    } finally {
      isSkipping = false;
    }
  }

  function tick() {
    const t = $player.currentTime;
    $kpiTime.textContent = `${t.toFixed(2)}s`;

    // Preview-skip: walk the merged (sorted, non-overlapping) delete ranges.
    // When the playhead lands inside one, invoke the async pause/seek/play
    // helper. `isSkipping` keeps tick from firing a second jump while the
    // first is still resolving.
    if (!isSkipping) {
      for (const [s, e] of state.mergedDeletes) {
        if (t >= s && t < e) {
          // 50ms nudge past the boundary — `+0.001` is too small; AAC frame
          // alignment can park currentTime fractionally before the boundary
          // and we'd re-trigger forever.
          const target = e + 0.05;
          $skipPill.classList.add('active');
          $skipPill.textContent = 'SKIP';
          lastSkipAt = performance.now();
          logKPI('ui.preview.skip', { from: t, to: target, range_s: s, range_e: e });
          jumpPast(target);
          break;  // jumpPast is async; resume scanning on next tick
        } else if (t < s) {
          break;  // ranges are sorted; remaining ones are further ahead
        }
      }
    }
    if ($skipPill.classList.contains('active') && performance.now() - lastSkipAt > 350) {
      $skipPill.classList.remove('active');
      $skipPill.textContent = 'no cut';
    }

    const idx = findActive($player.currentTime);
    if (idx !== activeIdx) {
      if (activeIdx >= 0) words[activeIdx].el.classList.remove('active');
      if (idx >= 0) {
        words[idx].el.classList.add('active');
        scrollIfOffscreen(words[idx].el);
      }
      activeIdx = idx;
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  // ------- toolbar wiring -------
  $btnDelete.addEventListener('click', deleteSelected);
  $btnUndo.addEventListener('click', undo);
  $btnRedo.addEventListener('click', redo);
  $btnClear.addEventListener('click', clearSelection);

  // ------- keyboard shortcuts -------
  document.addEventListener('keydown', (e) => {
    // Never grab keys while an IME is composing — Japanese/CJK input would
    // otherwise lose characters mid-conversion.
    if (e.isComposing || e.keyCode === 229) return;
    const mod = e.metaKey || e.ctrlKey;
    if (mod && (e.key === 'z' || e.key === 'Z')) {
      e.preventDefault();
      if (e.shiftKey) redo(); else undo();
      return;
    }
    if (e.key === 'd' || e.key === 'D') {
      // Delete shortcut only fires when there's a selection; never grab plain
      // letters from text inputs (there are none in this UI yet).
      if (!state.selection) return;
      e.preventDefault();
      deleteSelected();
      return;
    }
    if (e.key === 'Backspace' || e.key === 'Delete') {
      if (state.selection) {
        e.preventDefault();
        deleteSelected();
      }
      return;
    }
    if (e.key === 'Escape') {
      clearSelection();
    }
  });

  // ------- guard against losing unsaved work on tab close -------
  window.addEventListener('beforeunload', (e) => {
    if (state.saveDirty || state.saveStatus.startsWith('saving') || state.saveStatus.startsWith('error')) {
      e.preventDefault();
      e.returnValue = '';
    }
  });

  // ------- initial paint -------
  rerenderOps();
  refreshButtons();
})();
