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

  // ------- utilities -------
  const fmt = (s) => {
    if (s == null || isNaN(s)) return '–';
    const m = Math.floor(s / 60);
    const r = s - m * 60;
    return `${m}:${r.toFixed(2).padStart(5, '0')}`;
  };
  const fmtMs = (s) => `${s.toFixed(1)}s`;
  function uuid8() {
    return 'xxxxxxxx'.replace(/x/g, () => ((Math.random() * 16) | 0).toString(16));
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
    undoStack: [],     // {type:'add'|'remove', op}
    redoStack: [],
    selection: null,   // {anchor: wordIdx, extent: wordIdx} (inclusive)
    sessionTemplate: null,  // {schema_version, timeline_basis, source_audio, transcript_ref, created_at, ops}
    saveStatus: 'idle',
    saveTimer: null,
    saveDirty: false,
  };
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
      span.dataset.idx = String(idx);
      span.textContent = w.text;
      const word = { el: span, start: w.start, end: w.end, idx, segIdx };
      words.push(word);
      span.addEventListener('click', (ev) => onWordClick(idx, ev));
      div.appendChild(span);
      idx += 1;
    });
    $tx.appendChild(div);
  });

  // ------- selection + clicks -------
  function onWordClick(i, ev) {
    if (ev.shiftKey && state.selection) {
      state.selection = { anchor: state.selection.anchor, extent: i };
      renderSelection();
      return;
    }
    // single click: seek + reset selection anchor to this word
    state.selection = { anchor: i, extent: i };
    renderSelection();
    $player.currentTime = words[i].start;
    const p = $player.play();
    if (p && typeof p.catch === 'function') p.catch(() => {});
    logKPI('ui.click.word', { word_idx: i, t: words[i].start });
  }

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
    const op = { op_id: 'op-' + uuid8(), op: 'delete', start, end, note: null };
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
    // Tag every word as deleted if it lies entirely within a delete op.
    // Using strict inclusion (start >= op.start && end <= op.end) avoids
    // half-shading words that only partly fall in a cut.
    for (const w of words) {
      const inCut = state.ops.some((op) => w.start >= op.start && w.end <= op.end);
      w.el.classList.toggle('deleted', inCut);
    }
    updateStats();
  }

  function refreshButtons() {
    $btnUndo.disabled = state.undoStack.length === 0;
    $btnRedo.disabled = state.redoStack.length === 0;
  }

  function updateStats() {
    const cut = state.ops.reduce((s, o) => s + (o.end - o.start), 0);
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
    setSaveStatus('saving');
    const t = performance.now();
    try {
      const body = JSON.stringify(buildSessionJSON());
      const r = await fetch('/api/session', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body,
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const reply = await r.json();
      state.saveDirty = false;
      setSaveStatus('saved');
      logKPI('ui.session.saved', { latency_ms: performance.now() - t, ops: reply.ops });
    } catch (e) {
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
  function tick() {
    const t = $player.currentTime;

    // Preview-skip: if the playhead is inside a delete op, jump to its end.
    // Loop in case adjacent cuts chain.
    for (let i = 0; i < state.ops.length; i++) {
      let jumped = false;
      for (const op of state.ops) {
        if (t >= op.start && t < op.end) {
          $player.currentTime = op.end;
          jumped = true;
          break;
        }
      }
      if (!jumped) break;
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

  // ------- initial paint -------
  rerenderOps();
  refreshButtons();
})();
