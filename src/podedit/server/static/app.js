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
  const $btnCut = $('btn-cut');
  const $btnPasteBefore = $('btn-paste-before');
  const $btnPasteAfter = $('btn-paste-after');
  const $saveStatus = $('save-status');
  const $kpiOps = $('kpi-ops');
  const $kpiCut = $('kpi-cut');
  const $kpiElapsed = $('kpi-elapsed');
  const $skipPill = $('skip-pill');
  const $clipboardPill = $('clipboard-pill');
  const $kpiTime = $('kpi-time');
  const $btnPlay = $('btn-play');
  const $timeCurrent = $('time-current');
  const $timeDuration = $('time-duration');
  const $scrubber = $('scrubber');
  const $btnAudition = $('btn-audition');
  const $modePill = $('mode-pill');
  const $waveform = $('waveform');

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
    ops: [],           // active delete/move ops
    mergedDeletes: [], // normalized [start, end][] cached from ops
    timeline: [],      // [{sourceStart, sourceEnd, editedStart, editedEnd, originOpId}] in edited order
    editedDuration: 0, // sum of keep-range lengths; the "podcast duration after cuts"
    sourceDuration: 0, // unedited audio duration (from /api/audio/info)
    isScrubbing: false,
    previewMode: false,  // false = audio is the source m4a; true = it's a rendered preview wav
    previewURL: null,
    previewCacheKey: null,
    previewDuration: null,  // exact rendered duration from the server (xfade trims a few ms per seam)
    renderSeq: 0,  // monotonic counter; render responses older than the current seq are discarded
    undoStack: [],     // {type:'add'|'remove', op}
    redoStack: [],
    selection: null,   // {anchor: wordIdx, extent: wordIdx} (inclusive)
    clipboard: null,   // {src_start, src_end, duration_sec}
    pasteAnchor: null, // wordIdx used by Paste Before/After
    sessionTemplate: null,
    saveStatus: 'idle',
    saveTimer: null,
    saveDirty: false,
    saveSeq: 0,        // monotonic; only the latest save can claim 'saved'
  };

  function recomputeMergedDeletes() {
    const deletes = state.ops.filter((o) => o.op === 'delete');
    if (deletes.length === 0) { state.mergedDeletes = []; return; }
    const sorted = deletes.map((o) => [o.start, o.end]).sort((a, b) => a[0] - b[0]);
    const merged = [sorted[0].slice()];
    for (let i = 1; i < sorted.length; i++) {
      const cur = sorted[i];
      const last = merged[merged.length - 1];
      if (cur[0] <= last[1]) last[1] = Math.max(last[1], cur[1]);
      else merged.push(cur.slice());
    }
    state.mergedDeletes = merged;
  }

  function renumberSegments(segments) {
    let edited = 0;
    const out = [];
    for (const seg of segments) {
      const span = seg.sourceEnd - seg.sourceStart;
      if (span <= 0) continue;
      out.push({
        sourceStart: seg.sourceStart,
        sourceEnd: seg.sourceEnd,
        editedStart: edited,
        editedEnd: edited + span,
        originOpId: seg.originOpId ?? null,
      });
      edited += span;
    }
    return out;
  }

  function cutSourceRange(segments, start, end, movedOriginOpId) {
    const kept = [];
    const cut = [];
    for (const seg of segments) {
      const cs = Math.max(seg.sourceStart, start);
      const ce = Math.min(seg.sourceEnd, end);
      if (ce <= cs) {
        kept.push(seg);
        continue;
      }
      if (seg.sourceStart < cs) {
        kept.push({
          sourceStart: seg.sourceStart,
          sourceEnd: cs,
          editedStart: seg.editedStart,
          editedEnd: seg.editedStart + (cs - seg.sourceStart),
          originOpId: seg.originOpId ?? null,
        });
      }
      cut.push({
        sourceStart: cs,
        sourceEnd: ce,
        editedStart: seg.editedStart + (cs - seg.sourceStart),
        editedEnd: seg.editedStart + (ce - seg.sourceStart),
        originOpId: movedOriginOpId ?? seg.originOpId ?? null,
      });
      if (ce < seg.sourceEnd) {
        kept.push({
          sourceStart: ce,
          sourceEnd: seg.sourceEnd,
          editedStart: seg.editedStart + (ce - seg.sourceStart),
          editedEnd: seg.editedEnd,
          originOpId: seg.originOpId ?? null,
        });
      }
    }
    return { kept: renumberSegments(kept), cut };
  }

  function targetInsideSourceRange(segments, target, sourceStart, sourceEnd) {
    for (const seg of segments) {
      if (target >= seg.editedStart && target < seg.editedEnd) {
        const src = seg.sourceStart + (target - seg.editedStart);
        return src >= sourceStart && src < sourceEnd;
      }
    }
    return false;
  }

  function translateTargetAfterCut(target, cut) {
    let removedBefore = 0;
    for (const seg of cut) {
      if (seg.editedEnd <= target) removedBefore += seg.editedEnd - seg.editedStart;
      else if (seg.editedStart < target && target < seg.editedEnd) removedBefore += target - seg.editedStart;
    }
    return Math.max(0, target - removedBefore);
  }

  function insertSegmentsAt(segments, inserts, target) {
    const total = segments.reduce((sum, seg) => sum + (seg.sourceEnd - seg.sourceStart), 0);
    target = Math.max(0, Math.min(target, total));
    const out = [];
    let inserted = false;
    for (const seg of segments) {
      if (!inserted && target <= seg.editedStart) {
        out.push(...inserts);
        inserted = true;
      }
      if (!inserted && seg.editedStart < target && target < seg.editedEnd) {
        const split = seg.sourceStart + (target - seg.editedStart);
        out.push({ sourceStart: seg.sourceStart, sourceEnd: split, editedStart: seg.editedStart, editedEnd: target, originOpId: seg.originOpId ?? null });
        out.push(...inserts);
        out.push({ sourceStart: split, sourceEnd: seg.sourceEnd, editedStart: target, editedEnd: seg.editedEnd, originOpId: seg.originOpId ?? null });
        inserted = true;
      } else {
        out.push(seg);
      }
    }
    if (!inserted) out.push(...inserts);
    return renumberSegments(out);
  }

  function compileTimeline(sourceDuration, ops) {
    if (sourceDuration <= 0) return [];
    let segments = [{ sourceStart: 0, sourceEnd: sourceDuration, editedStart: 0, editedEnd: sourceDuration, originOpId: null }];
    for (const op of ops) {
      if (op.op === 'delete') {
        segments = cutSourceRange(segments, op.start, op.end, null).kept;
      } else if (op.op === 'move') {
        if (targetInsideSourceRange(segments, op.target_edited_t, op.src_start, op.src_end)) continue;
        const cutResult = cutSourceRange(segments, op.src_start, op.src_end, op.op_id);
        segments = cutResult.kept;
        if (cutResult.cut.length) {
          segments = insertSegmentsAt(segments, cutResult.cut, translateTargetAfterCut(op.target_edited_t, cutResult.cut));
        }
      }
      segments = renumberSegments(segments);
    }
    return segments;
  }

  // Virtual timeline: the "edited" timeline is what the user sees on the
  // scrubber and time displays. The audio element keeps playing on the source
  // timeline; sourceToEdited()/editedToSource() bridge between the two.
  function rebuildTimeline() {
    state.timeline = compileTimeline(state.sourceDuration, state.ops);
    state.editedDuration = state.timeline.reduce((sum, r) => sum + (r.sourceEnd - r.sourceStart), 0);
  }

  function editedToSource(t) {
    if (!state.timeline.length) return 0;
    t = Math.max(0, Math.min(t, state.editedDuration));
    for (const r of state.timeline) {
      if (t >= r.editedStart && t <= r.editedEnd) {
        return r.sourceStart + (t - r.editedStart);
      }
    }
    return state.timeline[state.timeline.length - 1].sourceEnd;
  }

  function sourceToEdited(t) {
    if (!state.timeline.length) return 0;
    for (const r of state.timeline) {
      if (t >= r.sourceStart && t < r.sourceEnd) return r.editedStart + (t - r.sourceStart);
    }
    const next = state.timeline.find((r) => t < r.sourceStart);
    if (next) return next.editedStart;
    return state.editedDuration;
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
  state.sourceDuration = info.duration_sec;
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
      if (state.clipboard) {
        setPasteAnchor(startIdx);
        clearSelection();
        logKPI('ui.paste.anchor', { word_idx: startIdx, t: words[startIdx].start });
        return;
      }
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
    $btnCut.disabled = !r;
    $btnClear.disabled = !r;
  }

  function renderPasteAnchor() {
    for (const w of words) w.el.classList.toggle('paste-anchor', state.pasteAnchor === w.idx);
    const canPaste = !!state.clipboard && state.pasteAnchor != null;
    $btnPasteBefore.disabled = !canPaste;
    $btnPasteAfter.disabled = !canPaste;
    if (state.clipboard) {
      $clipboardPill.hidden = false;
      $clipboardPill.textContent = `${fmtMs(state.clipboard.duration_sec)} copied`;
    } else {
      $clipboardPill.hidden = true;
      $clipboardPill.textContent = '';
    }
  }

  function setPasteAnchor(i) {
    state.pasteAnchor = i;
    renderPasteAnchor();
  }

  function clearSelection() {
    state.selection = null;
    renderSelection();
  }

  // ------- ops -------
  function applyDeleteRange(start, end) {
    if (end <= start) return;
    // Any edit invalidates the rendered preview — jump back to the source so
    // the user isn't auditioning a stale render. revertToSource bumps
    // renderSeq, so any in-flight Audition response is also discarded.
    if (state.previewMode) revertToSource('apply-delete');
    else state.renderSeq += 1;  // still bump so a stale response can't land on us
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

  function cutSelected() {
    const r = selectionRange();
    if (!r) return;
    state.clipboard = { src_start: r.start, src_end: r.end, duration_sec: r.end - r.start };
    state.pasteAnchor = null;
    clearSelection();
    renderPasteAnchor();
    logKPI('ui.clipboard.cut', { start: r.start, end: r.end, duration: r.end - r.start });
  }

  function applyMoveRange(srcStart, srcEnd, targetEditedT) {
    if (srcEnd <= srcStart || targetEditedT < 0) return;
    if (state.previewMode) revertToSource('apply-move');
    else state.renderSeq += 1;
    const op = {
      op_id: newOpId(),
      op: 'move',
      src_start: srcStart,
      src_end: srcEnd,
      target_edited_t: targetEditedT,
      note: null,
    };
    state.ops.push(op);
    state.undoStack.push({ type: 'add', op });
    state.redoStack = [];
    kpi.opsCount += 1;
    if (!kpi.firstOpAt) kpi.firstOpAt = Date.now() / 1000;
    logKPI('ui.op.move', {
      op_id: op.op_id,
      src_start: srcStart,
      src_end: srcEnd,
      target_edited_t: targetEditedT,
      duration: srcEnd - srcStart,
    });
    state.clipboard = null;
    state.pasteAnchor = null;
    rerenderOps();
    renderPasteAnchor();
    refreshButtons();
    scheduleSave();
  }

  function findPlayheadWord() {
    const t = state.previewMode ? editedToSource($player.currentTime) : $player.currentTime;
    const idx = findActive(t);
    return idx >= 0 ? idx : 0;
  }

  function pasteClipboard(where) {
    if (!state.clipboard) return;
    const anchorIdx = state.pasteAnchor != null ? state.pasteAnchor : findPlayheadWord();
    const anchor = words[anchorIdx];
    if (!anchor) return;
    const anchorEdited = sourceToEdited(anchor.start);
    const targetEditedT = where === 'after' ? anchorEdited + (anchor.end - anchor.start) : anchorEdited;
    applyMoveRange(state.clipboard.src_start, state.clipboard.src_end, targetEditedT);
  }

  function undo() {
    const entry = state.undoStack.pop();
    if (!entry) return;
    if (state.previewMode) revertToSource('undo');
    else state.renderSeq += 1;
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
    if (state.previewMode) revertToSource('redo');
    else state.renderSeq += 1;
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

  function syncTimelineUI() {
    // In preview mode the rendered wav is *shorter* than the virtual edited
    // duration by ~crossfade_ms × seams (acrossfade overlaps adjacent keeps).
    // Show the actual playable duration so the scrubber doesn't run past EOF.
    const dur = state.previewMode && state.previewDuration != null
      ? state.previewDuration
      : state.editedDuration;
    $scrubber.max = String(dur);
    $timeDuration.textContent = fmt(dur);
  }

  function rerenderOps() {
    // Snapshot the user's current EDITED position so we can preserve it across
    // the timeline rebuild. Without this, adding a cut to the left of the
    // playhead would visibly snap the scrubber backwards.
    const prevEdited = state.timeline.length ? sourceToEdited($player.currentTime) : null;
    recomputeMergedDeletes();
    rebuildTimeline();
    syncTimelineUI();
    if (prevEdited != null) {
      const newSource = editedToSource(prevEdited);
      if (Math.abs(newSource - $player.currentTime) > 0.05) {
        $player.currentTime = newSource;
      }
    }
    // Tag every word as deleted if it lies entirely within ANY merged delete
    // range or if no compiled segment contains it. Strict inclusion avoids
    // half-shading words that only partly fall in a cut.
    for (const w of words) {
      const inCompiledTimeline = state.timeline.some((r) => w.start >= r.sourceStart && w.end <= r.sourceEnd);
      const inCut = !inCompiledTimeline || state.mergedDeletes.some(([s, e]) => w.start >= s && w.end <= e);
      w.el.classList.toggle('deleted', inCut);
    }
    renderPasteAnchor();
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
    // Never seek past source duration — that's a no-op in some browsers and
    // can confuse the readyState. The caller already handles "edited end".
    targetEnd = Math.max(0, Math.min(targetEnd, state.sourceDuration));
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
    // In preview mode the audio source IS the rendered file, so currentTime
    // is already in edited space; sourceToEdited would double-map.
    const edt = state.previewMode ? t : sourceToEdited(t);
    $kpiTime.textContent = `${t.toFixed(2)}s`;

    // Mirror the playhead into the edited timeline displays — unless the user
    // is actively dragging the scrubber, in which case they own those values.
    if (!state.isScrubbing) {
      $timeCurrent.textContent = fmt(edt);
      $scrubber.value = String(edt);
    }

    // Source-mode-only: edited-end detection and preview-skip. In preview mode
    // the audio file already has the cuts baked in, so it'll just hit 'ended'.
    // Edited-end detection: if the source playhead has reached the last
    // keep range's sourceEnd, the user's "edited podcast" is over. Pause and
    // pin the playhead. Without this, deleting the tail of the audio would
    // make tick() repeatedly try to skip past the trailing delete and run
    // off the end of the source file.
    if (!state.previewMode && !isSkipping && !state.isScrubbing && state.timeline.length > 0) {
      const lastKeep = state.timeline[state.timeline.length - 1];
      if (t >= lastKeep.sourceEnd - 0.001) {
        if (!$player.paused) {
          $player.pause();
          logKPI('ui.preview.edited_end', { source_t: t, edited_t: state.editedDuration });
        }
        // Pin to exactly the edited end so we don't drift into a tail-delete.
        if (Math.abs($player.currentTime - lastKeep.sourceEnd) > 0.01) {
          $player.currentTime = lastKeep.sourceEnd;
        }
      }
    }

    // Preview-skip: jump out of any merged delete range. Suppressed while the
    // user is scrubbing so we don't fight their drag, and skipped in preview
    // mode where the file already has the cuts.
    if (!state.previewMode && !isSkipping && !state.isScrubbing) {
      for (const [s, e] of state.mergedDeletes) {
        if (t >= s && t < e) {
          // If this delete extends to the source end (no keep range after it),
          // we're past the edited end — handled by the block above. Don't
          // try to seek past sourceDuration.
          const hasKeepAfter = state.timeline.some((r) => r.sourceStart >= e - 0.001);
          if (!hasKeepAfter) break;
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
    paintWaveform();
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  // ------- toolbar wiring -------
  $btnDelete.addEventListener('click', deleteSelected);
  $btnUndo.addEventListener('click', undo);
  $btnRedo.addEventListener('click', redo);
  $btnClear.addEventListener('click', clearSelection);
  $btnCut.addEventListener('click', cutSelected);
  $btnPasteBefore.addEventListener('click', () => pasteClipboard('before'));
  $btnPasteAfter.addEventListener('click', () => pasteClipboard('after'));

  // ------- custom player controls -------
  $btnPlay.addEventListener('click', () => {
    if ($player.paused) {
      const p = $player.play();
      if (p && typeof p.catch === 'function') p.catch(() => {});
    } else {
      $player.pause();
    }
  });
  $player.addEventListener('play', () => { $btnPlay.textContent = '⏸'; });
  $player.addEventListener('pause', () => { $btnPlay.textContent = '▶'; });
  $player.addEventListener('ended', () => { $btnPlay.textContent = '▶'; });

  // Scrubber: 'input' fires continuously while dragging; 'change' fires on
  // release. We only commit the seek on release so we don't issue dozens of
  // seeks during a single drag.
  $scrubber.addEventListener('input', () => {
    state.isScrubbing = true;
    const edt = parseFloat($scrubber.value);
    $timeCurrent.textContent = fmt(edt);
  });
  $scrubber.addEventListener('change', () => {
    const edt = parseFloat($scrubber.value);
    // In preview mode the file is already in edited space; map only when the
    // source m4a is loaded.
    const target = state.previewMode ? edt : editedToSource(edt);
    $player.currentTime = target;
    state.isScrubbing = false;
    logKPI('ui.scrubber.seek', { edited: edt, target_source: target, mode: state.previewMode ? 'preview' : 'source' });
  });

  // ------- audition (server-side rendered preview) -------
  function setModePill(mode, text) {
    $modePill.className = `mode-pill mode-${mode}`;
    $modePill.textContent = text || mode;
  }
  setModePill('source', 'source');

  function switchPlayerSrc(newUrl, newEditedTime, wasPreviewMode) {
    const wasPlaying = !$player.paused && !$player.ended;
    $player.pause();
    $player.src = newUrl;
    // After changing src the element reloads; seek + resume on loadedmetadata.
    $player.addEventListener('loadedmetadata', () => {
      state.previewMode = wasPreviewMode;
      const target = wasPreviewMode ? newEditedTime : editedToSource(newEditedTime);
      $player.currentTime = Math.max(0, Math.min(target, $player.duration || target));
      if (wasPlaying) {
        const p = $player.play();
        if (p && typeof p.catch === 'function') p.catch(() => {});
      }
    }, { once: true });
  }

  function revertToSource(reason) {
    if (!state.previewMode) return;
    const currentEdited = $player.currentTime;  // already edited-space in preview
    state.previewURL = null;
    state.previewCacheKey = null;
    state.previewDuration = null;
    state.renderSeq += 1;  // any in-flight render's response is now stale
    setModePill('source', 'source');
    logKPI('ui.audition.revert', { reason });
    switchPlayerSrc(info.url, currentEdited, false);
    syncTimelineUI();
  }

  async function renderAndAudition() {
    if (state.editedDuration <= 0) {
      setModePill('error', 'empty');
      setTimeout(() => setModePill(state.previewMode ? 'preview' : 'source', state.previewMode ? 'preview' : 'source'), 1500);
      return;
    }
    const mySeq = ++state.renderSeq;
    setModePill('rendering', 'rendering…');
    $btnAudition.disabled = true;
    const t0 = performance.now();
    try {
      const r = await fetch('/api/preview/render', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status} ${(await r.text()).slice(0, 200)}`);
      const reply = await r.json();
      // Discard if the user edited (and bumped renderSeq) while we were rendering.
      if (mySeq !== state.renderSeq) {
        logKPI('ui.audition.discarded_stale', { my_seq: mySeq, current_seq: state.renderSeq });
        return;
      }
      const latency = performance.now() - t0;
      state.previewURL = reply.url;
      state.previewCacheKey = reply.cache_key;
      state.previewDuration = reply.duration_sec;
      logKPI('ui.audition.rendered', {
        latency_ms: latency, cached: reply.cached, bytes: reply.bytes,
        cache_key: reply.cache_key, preview_duration: reply.duration_sec,
      });
      // Snapshot edited position now so we can land at the same spot in the preview.
      const curEdited = sourceToEdited($player.currentTime);
      setModePill('preview', reply.cached ? 'preview (cached)' : 'preview');
      switchPlayerSrc(reply.url, curEdited, true);
      syncTimelineUI();
    } catch (e) {
      if (mySeq !== state.renderSeq) return;  // late failure for a discarded render
      setModePill('error', 'render error');
      logKPI('ui.audition.error', { error: e.message });
    } finally {
      $btnAudition.disabled = false;
    }
  }

  $btnAudition.addEventListener('click', renderAndAudition);

  // ------- waveform (W7) -------
  // Render the envelope above the scrubber. Playhead + delete-range shading
  // both repaint on every rAF so they always reflect the live state, but the
  // (expensive) envelope itself is drawn once into an offscreen canvas so the
  // per-frame redraw is just a single drawImage call.
  let waveformData = null;             // {min:[], max:[], duration_sec, step_sec}
  let waveformBaseCanvas = null;       // pre-rendered envelope
  let waveformCssWidth = 0;
  let waveformDevicePixelRatio = 1;

  function buildBaseWaveformCanvas(cssWidth, cssHeight) {
    if (!waveformData) return;
    const dpr = window.devicePixelRatio || 1;
    const off = document.createElement('canvas');
    off.width = Math.max(1, Math.round(cssWidth * dpr));
    off.height = Math.max(1, Math.round(cssHeight * dpr));
    const ctx = off.getContext('2d');
    ctx.scale(dpr, dpr);
    ctx.fillStyle = '#62aeff';
    ctx.globalAlpha = 0.6;
    const mid = cssHeight / 2;
    const n = waveformData.min.length;
    for (let i = 0; i < n; i++) {
      const x = (i / n) * cssWidth;
      const w = Math.max(1, cssWidth / n);
      const mn = waveformData.min[i];
      const mx = waveformData.max[i];
      const top = mid - mx * mid;
      const bot = mid - mn * mid;
      ctx.fillRect(x, top, w, Math.max(1, bot - top));
    }
    waveformBaseCanvas = off;
    waveformCssWidth = cssWidth;
    waveformDevicePixelRatio = dpr;
  }

  function paintWaveform() {
    if (!waveformData) return;
    const dpr = window.devicePixelRatio || 1;
    const cssW = $waveform.clientWidth;
    const cssH = $waveform.clientHeight || 48;
    if ($waveform.width !== Math.round(cssW * dpr) ||
        $waveform.height !== Math.round(cssH * dpr) ||
        !waveformBaseCanvas ||
        waveformCssWidth !== cssW) {
      $waveform.width = Math.round(cssW * dpr);
      $waveform.height = Math.round(cssH * dpr);
      buildBaseWaveformCanvas(cssW, cssH);
    }
    const ctx = $waveform.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, $waveform.width, $waveform.height);
    if (waveformBaseCanvas) ctx.drawImage(waveformBaseCanvas, 0, 0);
    ctx.scale(dpr, dpr);

    // Shade merged delete ranges so the waveform doubles as a cut overview.
    // Coordinates are in source-time space; the rendered preview wav has its
    // cuts baked in so we don't shade there.
    if (!state.previewMode && state.mergedDeletes.length > 0 && state.sourceDuration > 0) {
      ctx.fillStyle = 'rgba(255, 80, 80, 0.35)';
      for (const [s, e] of state.mergedDeletes) {
        const x = (s / state.sourceDuration) * cssW;
        const w = ((e - s) / state.sourceDuration) * cssW;
        ctx.fillRect(x, 0, w, cssH);
      }
    }

    // Playhead — uses source time so the line tracks the scrubbable position.
    const dur = state.previewMode
      ? (state.previewDuration != null ? state.previewDuration : ($player.duration || 1))
      : state.sourceDuration;
    if (dur > 0) {
      const x = ($player.currentTime / dur) * cssW;
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(x - 1, 0, 2, cssH);
    }
  }

  $waveform.addEventListener('click', (ev) => {
    if (!waveformData) return;
    const rect = $waveform.getBoundingClientRect();
    const frac = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
    // The scrubber and the time displays live in *edited* space. The waveform
    // pixels still represent source content (we shade deletes onto them) but
    // a click is "I want to listen from this position in the podcast",
    // which matches the scrubber: frac maps to the edited timeline. In source
    // mode we then bridge back to a source currentTime; in preview mode the
    // player's clock already is edited time.
    let targetT;
    if (state.previewMode) {
      const dur = state.previewDuration != null ? state.previewDuration : ($player.duration || 1);
      targetT = frac * dur;
    } else {
      const editedT = frac * state.editedDuration;
      targetT = editedToSource(editedT);
    }
    $player.currentTime = targetT;
    const p = $player.play();
    if (p && typeof p.catch === 'function') p.catch(() => {});
    logKPI('ui.waveform.seek', { frac, t: $player.currentTime, mode: state.previewMode ? 'preview' : 'source' });
  });

  window.addEventListener('resize', () => {
    waveformBaseCanvas = null;  // force re-render on next paint
  });

  fetchJSON('/api/waveform?points=2000').then((wf) => {
    waveformData = wf;
    paintWaveform();
    logKPI('ui.waveform.loaded', { points: wf.target_points, duration_sec: wf.duration_sec });
  }).catch((e) => {
    logKPI('ui.waveform.error', { error: e.message });
  });

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
    if (mod && (e.key === 'x' || e.key === 'X')) {
      if (!state.selection) return;
      e.preventDefault();
      cutSelected();
      return;
    }
    if (mod && (e.key === 'v' || e.key === 'V')) {
      if (!state.clipboard) return;
      e.preventDefault();
      pasteClipboard('after');
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
      return;
    }
    if (e.key === ' ' || e.code === 'Space') {
      // Toggle playback; don't swallow Space when a real form input is focused
      // (currently only the scrubber range, where Space has no useful default).
      const t = document.activeElement;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA') && t !== $scrubber) return;
      e.preventDefault();
      if ($player.paused) {
        const p = $player.play();
        if (p && typeof p.catch === 'function') p.catch(() => {});
      } else {
        $player.pause();
      }
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
  renderPasteAnchor();
  refreshButtons();
})();
