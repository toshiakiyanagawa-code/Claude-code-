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
  const $btnExport = $('btn-export');
  const $exportFormat = $('export-format');
  const $btnCopyTranscript = $('btn-copy-transcript');
  const $btnOpen = $('btn-open');
  const $libraryModal = $('library-modal');
  const $libraryList = $('library-list');
  const $libraryStatus = $('library-status');
  const $libraryDirHint = $('library-dir-hint');
  const $libraryCurrentPath = $('library-current-path');
  const $btnLibraryUpload = $('btn-library-upload');
  const $libraryFileInput = $('library-file-input');
  const $libraryUploadHint = $('library-upload-hint');
  const $libraryBreadcrumbs = $('library-breadcrumbs');
  const $libraryEnvBanner = $('library-env-banner');
  const $btnLibraryClose = $('btn-library-close');
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
  async function fetchJSONDetailed(url, init) {
    const r = await fetch(url, init);
    const text = await r.text();
    let body = null;
    if (text) {
      try { body = JSON.parse(text); } catch (_) { body = { detail: text }; }
    }
    if (!r.ok) {
      const detail = body && body.detail ? body.detail : `HTTP ${r.status}`;
      const err = new Error(`${url} -> HTTP ${r.status}: ${detail}`);
      err.status = r.status;
      err.detail = detail;
      throw err;
    }
    return body;
  }
  async function fetchJSONWithTimeout(url, init, timeoutMs) {
    if (typeof AbortController === 'undefined') return fetchJSON(url, init);
    const controller = new AbortController();
    const timeout = setTimeout(() => {
      console.log('[podedit] fetchJSONWithTimeout abort timeout fired', { url, timeoutMs });
      controller.abort();
    }, timeoutMs);
    try {
      const mergedInit = { ...(init || {}), signal: controller.signal };
      console.log('[podedit] fetchJSONWithTimeout fetching', { url, timeoutMs });
      return await fetchJSON(url, mergedInit);
    } catch (e) {
      if (e && e.name === 'AbortError') {
        const timeoutError = new Error(`${url} timed out after ${Math.round(timeoutMs / 1000)}s`);
        timeoutError.name = 'AbortError';
        throw timeoutError;
      }
      throw e;
    } finally {
      clearTimeout(timeout);
      console.log('[podedit] fetchJSONWithTimeout finished/cleaned up', { url, timeoutMs });
    }
  }
  const LIBRARY_DIAG_KEY = 'podedit.libraryDiagnostics';
  function libraryErrorText(e) {
    const name = e && e.name ? e.name : 'Error';
    const message = e && e.message ? e.message : String(e);
    const stack = e && e.stack ? String(e.stack).split('\n')[0] : 'no stack';
    return `${name}: ${message} @ ${stack}`;
  }
  function summarizeError(e) {
    return libraryErrorText(e);
  }
  function writeLibraryDiag(event, extra) {
    try {
      const prev = JSON.parse(localStorage.getItem(LIBRARY_DIAG_KEY) || '[]');
      const items = Array.isArray(prev) ? prev : [];
      items.push({
        ts: new Date().toISOString(),
        event,
        href: window.location.href,
        userAgent: navigator.userAgent,
        ...extra,
      });
      localStorage.setItem(LIBRARY_DIAG_KEY, JSON.stringify(items.slice(-30)));
    } catch (_) {
      // Diagnostics must never break the user-visible modal path.
    }
  }
  function setLibraryStatus(message, extra) {
    const text = extra ? `${message}\n${extra}` : message;
    $libraryStatus.textContent = text;
    $libraryStatus.hidden = false;
    writeLibraryDiag('status', { message: text });
  }
  function setLibraryError(e) {
    const text = libraryErrorText(e);
    $libraryStatus.hidden = false;
    $libraryStatus.textContent = text;
    writeLibraryDiag('error-status', { error: text });
  }
  function bodyPreview(text) {
    return String(text || '').slice(0, 200)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;')
      .replace(/\r/g, '\\r')
      .replace(/\n/g, '\\n')
      .replace(/\t/g, '\\t');
  }
  async function fetchLibraryWithDiagnostics(seq) {
    const url = '/api/library';
    const startedAt = performance.now();
    const active = () => libraryOpen && seq === libraryLoadSeq;
    const setActiveLibraryStatus = (message, extra) => {
      if (active()) setLibraryStatus(message, extra);
      else writeLibraryDiag('stale-status-skipped', { seq, message, extra });
    };
    setActiveLibraryStatus('ステップ 1: /api/library を取得中 (タイムアウト 10秒)…');
    writeLibraryDiag('fetch-start', { seq, url });
    console.log('[podedit] library step 1: fetching /api/library (timeout 10s)…', { seq, url });
    const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    const timeout = controller
      ? setTimeout(() => {
          console.log('[podedit] library fetch abort timeout fired', { seq, url });
          writeLibraryDiag('fetch-abort-timeout-fired', { seq, url, timeoutMs: 10000 });
          controller.abort();
        }, 10000)
      : null;
    try {
      const init = {
        cache: 'no-store',
        headers: { Accept: 'application/json' },
      };
      if (controller) init.signal = controller.signal;
      const r = await fetch(url, init);
      const contentType = r.headers.get('content-type') || '(none)';
      setActiveLibraryStatus(
        `ステップ 2: 応答 status=${r.status} content-type=${contentType} size=読み込み中…`,
        `ok=${r.ok} redirected=${r.redirected} type=${r.type} url=${r.url}`,
      );
      const text = await r.text();
      const elapsedMs = Math.round(performance.now() - startedAt);
      const size = text.length;
      setActiveLibraryStatus(
        `ステップ 2: 応答 status=${r.status} content-type=${contentType} size=${size}`,
        `ok=${r.ok} redirected=${r.redirected} type=${r.type} elapsed=${elapsedMs}ms url=${r.url}`,
      );
      console.log('[podedit] library step 2: response', {
        seq,
        status: r.status,
        ok: r.ok,
        redirected: r.redirected,
        type: r.type,
        contentType,
        size,
        url: r.url,
        elapsedMs,
      });
      writeLibraryDiag('fetch-response', {
        seq,
        status: r.status,
        ok: r.ok,
        redirected: r.redirected,
        type: r.type,
        contentType,
        url: r.url,
        elapsedMs,
        size,
      });
      if (!/application\/json/i.test(contentType)) {
        throw new Error(`Expected JSON but got content-type=${contentType}; body starts ${bodyPreview(text)}`);
      }
      setActiveLibraryStatus('ステップ 3: JSON を解析中…');
      console.log('[podedit] library step 3: parsing JSON', { seq, size });
      writeLibraryDiag('fetch-body', { seq, bodyBytes: text.length, preview: text.slice(0, 240) });
      if (!r.ok) {
        throw new Error(`${url} -> HTTP ${r.status}: body starts ${bodyPreview(text)}`);
      }
      try {
        return JSON.parse(text);
      } catch (e) {
        const parseError = new Error(
          `JSON parse failed: ${e.message}; content-type=${contentType}; body starts ${bodyPreview(text)}`,
        );
        parseError.name = e && e.name ? e.name : 'SyntaxError';
        throw parseError;
      }
    } catch (e) {
      if (e && e.name === 'AbortError') {
        const abortError = new Error(`${url} aborted by 10s timeout`);
        abortError.name = 'AbortError';
        throw abortError;
      }
      throw e;
    } finally {
      if (timeout) clearTimeout(timeout);
      console.log('[podedit] library fetch finished/cleaned up', { seq, url });
    }
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
    hasActive: true,
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
    tx = await fetchJSONDetailed('/api/transcript');
    [info, session] = await Promise.all([
      fetchJSON('/api/audio/info'),
      fetchJSON('/api/session'),
    ]);
  } catch (e) {
    if (e && e.status === 503) {
      state.hasActive = false;
      info = { name: '音声未読み込み', duration_sec: 0, sample_rate: null, channels: null, codec: null, url: '' };
      tx = { segments: [] };
      session = { ops: [] };
      $tx.innerHTML = '<div class="status empty">音声が読み込まれていません。<br>上の「音声を開く」(O) をクリックして、Codespace 内のファイルを選ぶか、アップロードしてください。</div>';
    } else {
      $tx.innerHTML = `<div class="status error">読み込みに失敗しました: ${e.message}</div>`;
      return;
    }
  }
  state.sessionTemplate = session;
  state.ops = session.ops || [];
  state.sourceDuration = info.duration_sec;
  logKPI('ui.loaded', {
    latency_ms: performance.now() - t0,
    has_existing_session: state.ops.length > 0,
    has_active: state.hasActive,
  });

  $name.textContent = info.name;
  $meta.textContent = [
    fmt(info.duration_sec),
    info.sample_rate && `${info.sample_rate}Hz`,
    info.channels && `${info.channels}ch`,
    info.codec,
    tx.model_config && `· model: ${tx.model_config.model}`,
  ].filter(Boolean).join(' · ');
  if (state.hasActive) $player.src = info.url;

  // Diagnostic logging: if seeks are silently ignored, we still see seeking/
  // seeked/error events in the KPI log.
  $player.addEventListener('seeking', () => logKPI('ui.audio.seeking', { t: $player.currentTime }));
  $player.addEventListener('seeked', () => logKPI('ui.audio.seeked', { t: $player.currentTime }));
  $player.addEventListener('error', () => logKPI('ui.audio.error', {
    code: $player.error && $player.error.code, message: $player.error && $player.error.message,
  }));
  $player.addEventListener('stalled', () => logKPI('ui.audio.stalled', { t: $player.currentTime }));

  // ------- render -------
  if (state.hasActive) $tx.innerHTML = '';
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
      span.addEventListener('pointerdown', (ev) => onWordPointerDown(wordIdx, ev));
      span.addEventListener('mouseenter', () => onWordMouseEnter(wordIdx));
      div.appendChild(span);
      idx += 1;
    });
    $tx.appendChild(div);
  });

  // ------- selection + move-drag clicks -------
  // Pointerdown on an unselected word keeps the W4 drag-to-select behavior.
  // Pointerdown inside an existing selection starts as a plain click and only
  // becomes a move once the pointer has travelled far enough to show intent.
  const MOVE_DRAG_THRESHOLD_PX = 4;
  const MOVE_DRAG_AUTOSCROLL_EDGE_PX = 80;
  let dragAnchor = null;  // word idx where selection drag started; null when idle
  let dragMoved = false;  // did selection drag cross at least one other word
  let dragStartT = 0;
  let activePointerId = null;
  let activePointerCaptureEl = null;
  let pendingDragMove = null;  // {x, y} latest selection-drag pointer position; processed on rAF
  let isIMEComposing = false;
  let moveDrag = null;  // {phase, pointerId, startX, startY, sourceRange, duration, ...}

  document.addEventListener('compositionstart', () => { isIMEComposing = true; });
  document.addEventListener('compositionend', () => { isIMEComposing = false; });

  function onWordPointerDown(i, ev) {
    if (ev.button !== undefined && ev.button !== 0) return;  // left button only
    if (ev.isComposing || isIMEComposing) return;
    if (ev.shiftKey && state.selection) {
      state.selection = { anchor: state.selection.anchor, extent: i };
      renderSelection();
      ev.preventDefault();
      return;
    }

    const r = selectionRange();
    const startsInsideSelection = !!r && i >= r.ws && i <= r.we;
    if (startsInsideSelection && state.previewMode) {
      // Simpler preview-mode policy: drag-to-move is disabled while auditioning
      // because DOM word timestamps remain source-time while audio is edited-time.
      ev.preventDefault();
      return;
    }
    if (startsInsideSelection) {
      beginTentativeMoveDrag(i, ev, r);
      ev.preventDefault();
      return;
    }

    beginSelectionDrag(i, ev);
    ev.preventDefault();
  }

  function beginSelectionDrag(i, ev) {
    dragAnchor = i;
    dragMoved = false;
    dragStartT = performance.now();
    activePointerId = ev.pointerId;
    pendingDragMove = null;
    state.selection = { anchor: i, extent: i };
    renderSelection();
    capturePointer(ev);
  }

  function beginTentativeMoveDrag(i, ev, sourceRange) {
    activePointerId = ev.pointerId;
    moveDrag = {
      phase: 'tentative',
      pointerId: ev.pointerId,
      startWordIdx: i,
      startX: ev.clientX,
      startY: ev.clientY,
      lastX: ev.clientX,
      lastY: ev.clientY,
      sourceRange,
      duration: sourceRange.end - sourceRange.start,
      candidate: null,
      ghostEl: null,
      caretEl: null,
      autoScrollRaf: null,
      startedAt: performance.now(),
    };
    capturePointer(ev);
  }

  function capturePointer(ev) {
    activePointerCaptureEl = ev.currentTarget;
    try { activePointerCaptureEl.setPointerCapture(ev.pointerId); } catch (_) {}
  }

  function releaseActivePointerCapture() {
    if (activePointerId === null || !activePointerCaptureEl) return;
    try { activePointerCaptureEl.releasePointerCapture(activePointerId); } catch (_) {}
    activePointerCaptureEl = null;
  }

  function onWordMouseEnter(i) {
    if (dragAnchor === null) return;
    extendSelectionDrag(i);
  }

  function wordTimelineSegmentIndex(w) {
    // Returns the index into state.timeline that contains this word, or -1
    // if the word is in the trailing cut block (no surviving segment).
    if (!w || !state.timeline.length) return -1;
    for (let i = 0; i < state.timeline.length; i++) {
      const r = state.timeline[i];
      if (w.start >= r.sourceStart - 1e-3 && w.end <= r.sourceEnd + 1e-3) return i;
    }
    return -1;
  }

  function extendSelectionDrag(i) {
    if (dragAnchor === null) return;
    // Selection in the UI maps to a *single* contiguous source range
    // (the anchor's timeline segment). After a Move op the DOM is in edited
    // order, so a user dragging across timeline-segment boundaries would
    // pick up arbitrary source content. Refuse to extend across boundaries:
    // the selection stops at whichever side of the boundary the anchor lives.
    const anchorTLSeg = wordTimelineSegmentIndex(words[dragAnchor]);
    const candidateTLSeg = wordTimelineSegmentIndex(words[i]);
    if (anchorTLSeg >= 0 && candidateTLSeg !== anchorTLSeg) return;
    if (i !== dragAnchor) dragMoved = true;
    if (!state.selection || state.selection.extent !== i) {
      state.selection = { anchor: dragAnchor, extent: i };
      renderSelection();
    }
  }

  function handlePlainWordClick(startIdx) {
    if (state.clipboard) {
      setPasteAnchor(startIdx);
      clearSelection();
      logKPI('ui.paste.anchor', { word_idx: startIdx, t: words[startIdx].start });
      return;
    }
    // Plain click: seek + play. Selection collapses to this one word so D
    // can still delete a single word if the user wants.
    state.selection = { anchor: startIdx, extent: startIdx };
    renderSelection();
    const w = words[startIdx];
    $player.currentTime = w.start;
    const p = $player.play();
    if (p && typeof p.catch === 'function') p.catch(() => {});
    logKPI('ui.click.word', { word_idx: startIdx, t: w.start });
  }

  function finishSelectionDrag() {
    if (dragAnchor === null) return;
    const wasDrag = dragMoved;
    const startIdx = dragAnchor;
    dragAnchor = null;
    dragMoved = false;
    releaseActivePointerCapture();
    activePointerId = null;
    pendingDragMove = null;

    if (!wasDrag) {
      handlePlainWordClick(startIdx);
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

  function ensureMoveDragVisuals() {
    if (!moveDrag || moveDrag.ghostEl) return;
    const ghost = document.createElement('div');
    ghost.className = 'move-ghost';

    // Clone every selected word into the ghost so the user literally sees the
    // text they grabbed travelling with the cursor — not a truncated preview.
    // The source spans stay in place at 0.45 opacity (.move-source) so the
    // user keeps the layout context.
    const content = document.createElement('div');
    content.className = 'move-ghost-content';
    const r = moveDrag.sourceRange;
    for (let i = r.ws; i <= r.we; i++) {
      const clone = document.createElement('span');
      clone.className = 'move-ghost-word';
      clone.textContent = words[i].el.textContent || '';
      content.appendChild(clone);
    }
    const badge = document.createElement('span');
    badge.className = 'move-ghost-badge';
    badge.textContent = `${fmtMs(moveDrag.duration)}`;
    ghost.append(content, badge);
    document.body.appendChild(ghost);

    const caret = document.createElement('div');
    caret.className = 'drop-caret';
    caret.hidden = true;
    document.body.appendChild(caret);

    moveDrag.ghostEl = ghost;
    moveDrag.caretEl = caret;
    for (let i = r.ws; i <= r.we; i++) {
      words[i].el.classList.add('move-source');
    }
  }

  function updateMoveGhost(x, y) {
    if (!moveDrag?.ghostEl) return;
    moveDrag.ghostEl.style.transform = `translate(${x + 12}px, ${y + 12}px)`;
  }

  function isPointInTranscript(x, y) {
    const rect = $tx.getBoundingClientRect();
    return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
  }

  function wordFromPoint(x, y) {
    // Clamp to viewport interior so a pointer dragged just past the
    // bottom (or top) edge still hit-tests the boundary-most visible
    // word. Without this, document.elementFromPoint returns null
    // whenever y exceeds window.innerHeight, freezing the selection at
    // whatever was on screen when the cursor crossed the edge — this
    // teamed up with the mouseleave bug to produce the "30 sec wall".
    const cx = Math.max(1, Math.min(window.innerWidth - 1, x));
    const cy = Math.max(1, Math.min(window.innerHeight - 1, y));
    let el = document.elementFromPoint(cx, cy);
    while (el && !el.classList?.contains('word')) el = el.parentElement;
    if (el && el.classList.contains('word')) {
      const i = parseInt(el.dataset.idx, 10);
      if (!Number.isNaN(i)) return words[i] || null;
    }
    return null;
  }

  function nearestWordOnLine(x, y) {
    let best = null;
    let bestDx = Infinity;
    for (const w of words) {
      const rect = w.el.getBoundingClientRect();
      if (y < rect.top || y > rect.bottom) continue;
      const dx = x < rect.left ? rect.left - x : (x > rect.right ? x - rect.right : 0);
      if (dx < bestDx) {
        best = w;
        bestDx = dx;
      }
    }
    return best;
  }

  function findDropCandidate(x, y) {
    if (!isPointInTranscript(x, y)) return null;
    const word = wordFromPoint(x, y) || nearestWordOnLine(x, y);
    if (!word) return null;
    const rect = word.el.getBoundingClientRect();
    const where = x < rect.left + rect.width / 2 ? 'before' : 'after';
    return {
      wordIdx: word.idx,
      where,
      x: where === 'before' ? rect.left : rect.right,
      top: rect.top,
      bottom: rect.bottom,
      targetEditedT: where === 'before' ? sourceToEdited(word.start) : sourceToEdited(word.end),
    };
  }

  function updateDropCaret(candidate) {
    if (!moveDrag?.caretEl) return;
    if (!candidate) {
      moveDrag.caretEl.hidden = true;
      return;
    }
    moveDrag.caretEl.hidden = false;
    moveDrag.caretEl.style.left = `${candidate.x}px`;
    moveDrag.caretEl.style.top = `${candidate.top}px`;
    moveDrag.caretEl.style.height = `${Math.max(14, candidate.bottom - candidate.top)}px`;
  }

  function updateMoveDropTarget(x, y) {
    if (!moveDrag) return;
    const candidate = findDropCandidate(x, y);
    moveDrag.candidate = candidate;
    updateDropCaret(candidate);
  }

  function startMoveAutoScroll() {
    if (!moveDrag || moveDrag.autoScrollRaf) return;
    const tickScroll = () => {
      if (!moveDrag || moveDrag.phase !== 'dragging') return;
      const y = moveDrag.lastY;
      let velocity = 0;
      if (y < MOVE_DRAG_AUTOSCROLL_EDGE_PX) {
        velocity = -((MOVE_DRAG_AUTOSCROLL_EDGE_PX - y) / MOVE_DRAG_AUTOSCROLL_EDGE_PX) * 18;
      } else if (y > window.innerHeight - MOVE_DRAG_AUTOSCROLL_EDGE_PX) {
        velocity = ((y - (window.innerHeight - MOVE_DRAG_AUTOSCROLL_EDGE_PX)) / MOVE_DRAG_AUTOSCROLL_EDGE_PX) * 18;
      }
      if (velocity !== 0) {
        $tx.scrollTop += velocity;
        updateMoveDropTarget(moveDrag.lastX, moveDrag.lastY);
      }
      moveDrag.autoScrollRaf = requestAnimationFrame(tickScroll);
    };
    moveDrag.autoScrollRaf = requestAnimationFrame(tickScroll);
  }

  function enterMoveDrag() {
    if (!moveDrag || moveDrag.phase !== 'tentative') return;
    moveDrag.phase = 'dragging';
    ensureMoveDragVisuals();
    updateMoveGhost(moveDrag.lastX, moveDrag.lastY);
    updateMoveDropTarget(moveDrag.lastX, moveDrag.lastY);
    startMoveAutoScroll();
  }

  function cleanupMoveDrag() {
    if (!moveDrag) return;
    if (moveDrag.autoScrollRaf) cancelAnimationFrame(moveDrag.autoScrollRaf);
    if (moveDrag.ghostEl) moveDrag.ghostEl.remove();
    if (moveDrag.caretEl) moveDrag.caretEl.remove();
    for (let i = moveDrag.sourceRange.ws; i <= moveDrag.sourceRange.we; i++) {
      words[i].el.classList.remove('move-source');
    }
    releaseActivePointerCapture();
    moveDrag = null;
    activePointerId = null;
  }

  function cancelMoveDrag() {
    cleanupMoveDrag();
  }

  function finishMoveDrag(x, y) {
    if (!moveDrag) return;
    const wasDragging = moveDrag.phase === 'dragging';
    const startIdx = moveDrag.startWordIdx;
    const r = moveDrag.sourceRange;
    const candidate = wasDragging ? moveDrag.candidate || findDropCandidate(x, y) : null;
    cleanupMoveDrag();

    if (!wasDragging) {
      handlePlainWordClick(startIdx);
      return;
    }
    if (!candidate || (candidate.wordIdx >= r.ws && candidate.wordIdx <= r.we)) return;
    applyMoveRange(r.start, r.end, candidate.targetEditedT);
    logKPI('ui.drag.move', {
      ws: r.ws, we: r.we, start: r.start, end: r.end,
      target_word_idx: candidate.wordIdx, where: candidate.where,
      target_edited_t: candidate.targetEditedT,
    });
  }

  document.addEventListener('pointermove', (e) => {
    if (activePointerId !== null && e.pointerId !== activePointerId) return;
    if (moveDrag) {
      moveDrag.lastX = e.clientX;
      moveDrag.lastY = e.clientY;
      if (moveDrag.phase === 'tentative') {
        const dx = e.clientX - moveDrag.startX;
        const dy = e.clientY - moveDrag.startY;
        if (Math.hypot(dx, dy) >= MOVE_DRAG_THRESHOLD_PX) enterMoveDrag();
      }
      if (moveDrag.phase === 'dragging') {
        updateMoveGhost(e.clientX, e.clientY);
        updateMoveDropTarget(e.clientX, e.clientY);
        e.preventDefault();
      }
      return;
    }
    if (dragAnchor === null) return;
    pendingDragMove = { x: e.clientX, y: e.clientY };
  });

  document.addEventListener('pointerup', (e) => {
    if (activePointerId !== null && e.pointerId !== activePointerId) return;
    if (moveDrag) {
      finishMoveDrag(e.clientX, e.clientY);
      return;
    }
    finishSelectionDrag();
  });

  document.addEventListener('pointercancel', (e) => {
    if (activePointerId !== null && e.pointerId !== activePointerId) return;
    if (moveDrag) cancelMoveDrag();
    if (dragAnchor !== null) finishSelectionDrag();
  });

  // Selection drag does NOT end on mouseleave: the user routinely drags
  // past the viewport edge to keep extending the selection while the
  // transcript auto-scrolls. Pointer capture + pointerup/pointercancel
  // already cover legitimate drag-end. Killing the drag here was the
  // real "30 sec wall" — the moment pointer y crossed the document edge,
  // dragAnchor went null and every subsequent rAF tick did nothing.
  // Move-drag still cancels here because dropping into another window
  // can't produce a meaningful target word.
  document.addEventListener('mouseleave', () => {
    if (moveDrag) cancelMoveDrag();
  });

  // Backstop for sensitivity: track pointer position during selection drag and
  // resolve the word directly under the cursor each rAF. mouseenter alone
  // misses words when dragging fast across line breaks or inter-word gaps.
  function dragMoveTick() {
    if (pendingDragMove && dragAnchor !== null) {
      const w = wordFromPoint(pendingDragMove.x, pendingDragMove.y);
      if (w) extendSelectionDrag(w.idx);
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
    $btnDelete.disabled = !state.hasActive || !r;
    $btnCut.disabled = !state.hasActive || !r;
    $btnClear.disabled = !state.hasActive || !r;
  }

  function setSelection(anchor, extent) {
    state.selection = { anchor, extent };
    renderSelection();
  }

  function renderPasteAnchor() {
    for (const w of words) w.el.classList.toggle('paste-anchor', state.pasteAnchor === w.idx);
    const canPaste = state.hasActive && !!state.clipboard && state.pasteAnchor != null;
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

  function extendSelectionByKeyboard(dir) {
    if (dir !== -1 && dir !== 1) return;
    let anchor;
    let extent;
    const hadSelection = !!state.selection;
    if (hadSelection) {
      anchor = state.selection.anchor;
      extent = state.selection.extent;
    } else {
      if (activeIdx < 0) return;
      anchor = activeIdx;
      extent = activeIdx;
    }

    const next = Math.max(0, Math.min(words.length - 1, extent + dir));
    const anchorTLSeg = wordTimelineSegmentIndex(words[anchor]);
    const candidateTLSeg = wordTimelineSegmentIndex(words[next]);
    if (anchorTLSeg >= 0 && candidateTLSeg !== anchorTLSeg) {
      if (!hadSelection) setSelection(anchor, anchor);
      return;
    }
    setSelection(anchor, next);
    logKPI('ui.keyboard.select', { anchor, extent: next, direction: dir });
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
    rebuildTranscriptDOM();
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

  function rebuildTranscriptDOM() {
    // Walk the timeline in EDITED order and move existing word spans into
    // .segment paragraphs in that order. Words deleted entirely (i.e. they
    // sit in no timeline segment) live in a trailing .segment-deleted block
    // so the user can still see them and undo. The word DOM elements are the
    // same objects — they keep their listeners, .selected/.deleted classes
    // get re-applied below. This is what makes a Move op visually rearrange
    // the transcript, not just rewrite the underlying ops list.
    //
    // Snapshot the active word's screen-relative offset so we can re-anchor
    // the scroll position after the wholesale DOM swap. Without this every
    // edit jumps the user back to the top of the transcript.
    const activeWord = activeIdx >= 0 ? words[activeIdx]?.el : null;
    const anchorOffset = activeWord ? activeWord.getBoundingClientRect().top : null;
    const fallbackScrollTop = window.scrollY;

    $tx.innerHTML = '';

    // Per timeline segment, split words back into runs by their original
    // ASR segIdx so familiar paragraph boundaries survive delete-only edits.
    // After a Move op the moved range becomes its own .segment block, which
    // is what the user sees as "the text landed here".
    const placed = new Set();
    for (const tlSeg of state.timeline) {
      const wordsInSeg = words
        .filter((w) => w.start >= tlSeg.sourceStart - 1e-3 && w.end <= tlSeg.sourceEnd + 1e-3)
        .sort((a, b) => a.start - b.start);
      let currentDiv = null;
      let lastSegIdx = -1;
      for (const w of wordsInSeg) {
        if (currentDiv == null || w.segIdx !== lastSegIdx) {
          currentDiv = document.createElement('div');
          currentDiv.className = 'segment';
          const time = document.createElement('span');
          time.className = 'seg-time';
          time.textContent = fmt(w.start);
          currentDiv.appendChild(time);
          $tx.appendChild(currentDiv);
          lastSegIdx = w.segIdx;
        }
        currentDiv.appendChild(w.el);
        placed.add(w.idx);
      }
    }

    // Stash any word the timeline doesn't currently cover in a final
    // strikethrough block so the user can still spot/undo deleted material.
    const orphans = words.filter((w) => !placed.has(w.idx));
    if (orphans.length > 0) {
      const div = document.createElement('div');
      div.className = 'segment segment-deleted';
      const label = document.createElement('span');
      label.className = 'seg-time';
      label.textContent = 'cut';
      div.appendChild(label);
      for (const w of orphans) div.appendChild(w.el);
      $tx.appendChild(div);
    }

    // Re-anchor scroll: if the playhead's word was on screen before the
    // rebuild, keep it at the same vertical offset; otherwise restore the
    // raw scrollY. Either is much less jarring than the implicit jump-to-top.
    if (activeWord && anchorOffset != null && activeWord.isConnected) {
      const newTop = activeWord.getBoundingClientRect().top;
      const delta = newTop - anchorOffset;
      if (Math.abs(delta) > 0.5) window.scrollBy(0, delta);
    } else if (fallbackScrollTop !== window.scrollY) {
      window.scrollTo(0, fallbackScrollTop);
    }
  }

  function refreshButtons() {
    $btnUndo.disabled = !state.hasActive || state.undoStack.length === 0;
    $btnRedo.disabled = !state.hasActive || state.redoStack.length === 0;
    $btnAudition.disabled = !state.hasActive;
    $btnExport.disabled = !state.hasActive;
    $btnCopyTranscript.disabled = !state.hasActive;
    $btnPlay.disabled = !state.hasActive;
    $scrubber.disabled = !state.hasActive;
    $exportFormat.disabled = !state.hasActive;
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
    if (!state.hasActive) return { ops: [] };
    return { ...state.sessionTemplate, ops: state.ops };
  }
  function scheduleSave() {
    if (!state.hasActive) return;
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
  // Force any pending autosave debounce to fire NOW and await it. Used before
  // Audition / Export so the server's /api/preview/render snapshots the
  // latest ops, not a 300ms-stale view that's still sitting in the timer.
  // Safe to call when nothing is dirty — it returns immediately.
  async function flushSave() {
    if (!state.hasActive) return;
    if (!state.saveDirty && !state.saveTimer) return;
    clearTimeout(state.saveTimer);
    state.saveTimer = null;
    await doSave();
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
    if (!state.hasActive) return;
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
    if (!state.hasActive) return;
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
      // Flush any pending autosave so the server's /api/preview/render
      // snapshots the latest ops, not a 300ms-debounce-stale view.
      await flushSave();
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

  // ------- export (W8) -------
  // Streams the same audio the audition player would receive, but framed as
  // an attachment download — server adds Content-Disposition + a friendly
  // filename, and (for mp3) transcodes the cached wav via libmp3lame V2.
  //
  // We always trigger a fresh render before downloading so the bytes match
  // the current state.ops. If a render is already current (state.previewCacheKey
  // matches the current ops_hash via cache_key reuse on the server), the
  // server short-circuits with `cached: true` and we pay just the round trip.
  async function exportEditedAudio() {
    if (!state.hasActive) return;
    if (state.editedDuration <= 0) {
      setModePill('error', 'empty');
      setTimeout(() => setModePill(state.previewMode ? 'preview' : 'source',
        state.previewMode ? 'preview' : 'source'), 1500);
      return;
    }
    const fmt = $exportFormat.value === 'wav' ? 'wav' : 'mp3';
    const mySeq = ++state.renderSeq;
    const prevLabel = $btnExport.textContent;
    $btnExport.disabled = true;
    $btnExport.textContent = fmt === 'mp3' ? 'rendering + mp3…' : 'rendering…';
    const t0 = performance.now();
    try {
      // Flush any pending autosave so the server's /api/preview/render
      // snapshots the latest ops, not a 300ms-debounce-stale view. Without
      // this, "edit → press Export immediately" downloads the previous state.
      await flushSave();
      // Always re-render: cheap if cached on the server; safe if we're stale.
      const r = await fetch('/api/preview/render', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (!r.ok) throw new Error(`render HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`);
      const reply = await r.json();
      // If the user edited mid-export, the cache_key would still be valid
      // (it hashes ops) but we'd be downloading a stale render. Bail.
      if (mySeq !== state.renderSeq) {
        logKPI('ui.export.discarded_stale', { my_seq: mySeq, current_seq: state.renderSeq });
        return;
      }
      const cacheKey = reply.cache_key;
      $btnExport.textContent = fmt === 'mp3' ? 'transcoding…' : 'downloading…';
      const url = `/api/export/${encodeURIComponent(cacheKey)}?fmt=${encodeURIComponent(fmt)}`;
      // HEAD precheck so a transcode failure (e.g. ffmpeg timeout, 500) is
      // surfaced to the user before we kick off the browser's download flow.
      // Without this, an anchor click on a 500 would just show the browser's
      // generic error page and leave the toolbar quietly re-enabled.
      const head = await fetch(url, { method: 'HEAD' });
      if (!head.ok) {
        // Try to extract a server-side detail from a follow-up GET body
        // (HEAD doesn't include the JSON detail). Best-effort only.
        let detail = `HTTP ${head.status}`;
        try {
          const probe = await fetch(url);
          const txt = await probe.text();
          detail = `${head.status}: ${txt.slice(0, 200)}`;
        } catch (_) { /* ignore — surface the HEAD status */ }
        throw new Error(`export ${detail}`);
      }
      // Navigate via a hidden anchor so the browser's download manager
      // handles the file save. fetch() would buffer the whole blob.
      const a = document.createElement('a');
      a.href = url;
      a.rel = 'noopener';
      // download attribute hints filename but server's Content-Disposition wins.
      a.download = '';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      logKPI('ui.export.requested', {
        fmt,
        cache_key: cacheKey,
        latency_ms: performance.now() - t0,
        render_cached: reply.cached,
      });
    } catch (e) {
      if (mySeq !== state.renderSeq) return;
      setModePill('error', 'export error');
      setTimeout(() => setModePill(state.previewMode ? 'preview' : 'source',
        state.previewMode ? 'preview' : 'source'), 2500);
      logKPI('ui.export.error', { error: e.message });
    } finally {
      $btnExport.disabled = false;
      $btnExport.textContent = prevLabel;
    }
  }
  $btnExport.addEventListener('click', exportEditedAudio);

  async function writeClipboardText(text) {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      await navigator.clipboard.writeText(text);
      return;
    }
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    ta.style.top = '0';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
    } finally {
      document.body.removeChild(ta);
    }
  }

  let copyStatusSeq = 0;
  async function copyEditedTranscript() {
    const paragraphs = [];
    for (const segment of $tx.querySelectorAll('.segment')) {
      if (segment.classList.contains('segment-deleted')) continue;
      const text = Array.from(segment.querySelectorAll('.word'))
        .filter((el) => !el.classList.contains('deleted'))
        .map((el) => el.textContent || '')
        .join('');
      if (text) paragraphs.push(text);
    }
    const text = paragraphs.join('\n\n').trimEnd();
    await writeClipboardText(text);
    const copiedSeq = ++copyStatusSeq;
    $saveStatus.textContent = 'copied';
    setTimeout(() => {
      if (copyStatusSeq === copiedSeq) setSaveStatus(state.saveStatus);
    }, 1500);
    logKPI('ui.transcript.copied', { chars: text.length });
  }

  $btnCopyTranscript.addEventListener('click', () => {
    if (!state.hasActive) return;
    copyEditedTranscript().catch((e) => {
      setSaveStatus('error:' + e.message);
    });
  });

  // Persist the export format choice across sessions — small but the modal
  // selector is the only stateful input in the toolbar, so saving it costs us
  // nothing and matches "the UI remembers your settings" expectations.
  const EXPORT_FMT_KEY = 'podedit.exportFormat';
  try {
    const saved = localStorage.getItem(EXPORT_FMT_KEY);
    if (saved === 'mp3' || saved === 'wav') $exportFormat.value = saved;
  } catch (_) { /* localStorage disabled or full — fall through to default */ }
  $exportFormat.addEventListener('change', () => {
    try { localStorage.setItem(EXPORT_FMT_KEY, $exportFormat.value); }
    catch (_) { /* non-fatal */ }
  });

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

  if (state.hasActive) {
    fetchJSON('/api/waveform?points=2000').then((wf) => {
      waveformData = wf;
      paintWaveform();
      logKPI('ui.waveform.loaded', { points: wf.target_points, duration_sec: wf.duration_sec });
    }).catch((e) => {
      logKPI('ui.waveform.error', { error: e.message });
    });
  }

  // ------- keyboard shortcuts -------
  document.addEventListener('keydown', (e) => {
    // Never grab keys while an IME is composing — Japanese/CJK input would
    // otherwise lose characters mid-conversion.
    if (e.isComposing || e.keyCode === 229) return;
    const mod = e.metaKey || e.ctrlKey;
    if (e.shiftKey && (e.key === 'ArrowRight' || e.key === 'ArrowLeft')) {
      const t = document.activeElement;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) return;
      e.preventDefault();
      extendSelectionByKeyboard(e.key === 'ArrowRight' ? 1 : -1);
      return;
    }
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
      if (libraryOpen) {
        e.preventDefault();
        hideLibraryModal();
        return;
      }
      if (moveDrag) {
        e.preventDefault();
        cancelMoveDrag();
        return;
      }
      clearSelection();
      return;
    }
    if (e.key === 'o' || e.key === 'O') {
      // Plain 'o' opens the library modal — no modifier, so we still get it
      // while a selection is active. Skip if a real input is focused so the
      // user can type literal 'o' anywhere that exists in the future.
      const t = document.activeElement;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA')) return;
      if (mod) return;  // don't grab Ctrl+O / Cmd+O — those are browser shortcuts
      e.preventDefault();
      showLibraryModal();
      return;
    }
    if (e.key === 'e' || e.key === 'E') {
      // Plain 'e' exports the current edit. Skip if focus is on a form input
      // (currently the export-format <select> or future inputs) so the user
      // can still type/keystroke-navigate within them.
      const t = document.activeElement;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) return;
      if (mod) return;  // don't grab Ctrl+E / Cmd+E
      if (!state.hasActive) return;
      e.preventDefault();
      exportEditedAudio();
      return;
    }
    if (e.key === 't' || e.key === 'T') {
      const t = document.activeElement;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) return;
      if (mod) return;
      if (!state.hasActive) return;
      e.preventDefault();
      copyEditedTranscript().catch((err) => {
        setSaveStatus('error:' + err.message);
      });
      return;
    }
    if (e.key === ' ' || e.code === 'Space') {
      // Toggle playback; don't swallow Space when a real form input is focused.
      // SELECT included so Space on the preset / export-format dropdowns opens
      // the menu instead of toggling playback. The scrubber <input range> is
      // the only INPUT we intentionally keep playback-bound on.
      const t = document.activeElement;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')
          && t !== $scrubber) return;
      e.preventDefault();
      if (!state.hasActive) return;
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

  // ------- library / open-file dialog (W7.6) -------
  // The server can swap which (audio, transcript, session) it's serving in
  // place. We just POST the chosen filename and then full-page-reload so
  // every cached resource (transcript, waveform, audio) re-fetches against
  // the new state. Page reload also gives us the existing beforeunload
  // unsaved-work warning for free.
  let libraryOpen = false;
  let librarySwitching = false;
  let libraryReturnFocus = null;
  let libraryLoadSeq = 0;
  let libraryBrowsePath = null;
  let libraryBrowseMode = false;

  async function fetchFsBrowse(path, seq) {
    const suffix = path ? `?path=${encodeURIComponent(path)}` : '';
    const url = `/api/fs/browse${suffix}`;
    setLibraryStatus('ディレクトリを取得中…', path || '既定のライブラリパス');
    writeLibraryDiag('fs-browse-start', { seq, url, path });
    const r = await fetch(url, {
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    });
    const text = await r.text();
    if (!r.ok) throw new Error(`${url} -> HTTP ${r.status}: ${bodyPreview(text)}`);
    try {
      return JSON.parse(text);
    } catch (e) {
      throw new Error(`JSON parse failed: ${e.message}; body starts ${bodyPreview(text)}`);
    }
  }

  async function fetchCurrentLibraryListing(seq) {
    if (libraryBrowseMode) return fetchFsBrowse(libraryBrowsePath, seq);
    return fetchLibraryWithDiagnostics(seq);
  }

  async function navigateLibraryTo(path) {
    if (!libraryOpen) return;
    const seq = ++libraryLoadSeq;
    try {
      const data = await fetchFsBrowse(path, seq);
      if (!libraryOpen || seq !== libraryLoadSeq) return;
      libraryBrowseMode = true;
      libraryBrowsePath = data && data.path ? data.path : path;
      lastTranscribedLibrary = data;
      populateLibrary(data);
      const entries = Array.isArray(data && data.entries) ? data.entries : [];
      setLibraryStatus('ディレクトリを読み込みました。', `entries=${entries.length} path=${libraryBrowsePath || '—'}`);
    } catch (e) {
      if (!libraryOpen || seq !== libraryLoadSeq) return;
      setLibraryError(e);
    }
  }

  let cachedHealth = null;
  async function ensureEnvBanner() {
    if (!$libraryEnvBanner) return;
    if (!cachedHealth) {
      try {
        const r = await fetch('/api/health', { cache: 'no-store' });
        if (r.ok) cachedHealth = await r.json();
      } catch (_) { /* non-fatal */ }
    }
    if (cachedHealth && cachedHealth.is_codespaces) {
      const kb = Math.round((cachedHealth.chunked_upload_chunk_size || 524288) / 1024);
      $libraryEnvBanner.innerHTML =
        `<strong>Codespaces で動作中</strong> · ` +
        `アップロードは ${kb}KB ずつの分割転送で、最大 500MB まで対応します。` +
        `転送中はボタンに進捗 % が表示されます。`;
      $libraryEnvBanner.hidden = false;
    }
  }

  function showLibraryModal() {
    try {
      if (libraryOpen) return;
      libraryOpen = true;
      const seq = ++libraryLoadSeq;
      const startedAt = performance.now();
      libraryReturnFocus = document.activeElement;
      $libraryModal.hidden = false;
      ensureEnvBanner();
      $libraryList.innerHTML = '';
      $libraryDirHint.textContent = '確認中…';
      if ($libraryCurrentPath) $libraryCurrentPath.textContent = '確認中…';
      if ($libraryBreadcrumbs) $libraryBreadcrumbs.innerHTML = '';
      libraryBrowsePath = null;
      libraryBrowseMode = false;
      setLibraryStatus('ステップ 0: ダイアログを開きました', `seq=${seq}`);
      writeLibraryDiag('modal-open', { seq });
      console.log('[podedit] library modal opened', { seq });
      // Move keyboard focus into the dialog so Esc works without an extra click.
      $btnLibraryClose.focus();
      const heartbeat = setInterval(() => {
        if (!libraryOpen || seq !== libraryLoadSeq) {
          clearInterval(heartbeat);
          return;
        }
        const elapsedSec = ((performance.now() - startedAt) / 1000).toFixed(1);
        setLibraryStatus('ステップ 1: /api/library を取得中 (タイムアウト 10秒)…', `elapsed=${elapsedSec}s seq=${seq}`);
      }, 1000);
      const watchdog = setTimeout(() => {
        if (!libraryOpen || seq !== libraryLoadSeq) return;
        setLibraryStatus(
          '診断タイムアウト: ライブラリの取得が完了していません',
          '11秒経過しても取得が保留中です。直近の進行ステップは localStorage["podedit.libraryDiagnostics"] を確認してください。',
        );
        writeLibraryDiag('watchdog-timeout', { seq });
      }, 11000);
      (async () => {
        try {
          const data = await fetchLibraryWithDiagnostics(seq);
          if (!libraryOpen || seq !== libraryLoadSeq) {
            writeLibraryDiag('stale-response-ignored', { seq, currentSeq: libraryLoadSeq });
            return;
          }
          // Cache so the polling code can re-render with fresh progress
          // labels without re-fetching the library every 2 seconds.
          lastTranscribedLibrary = data;
          const entries = Array.isArray(data && data.entries) ? data.entries : [];
          const libraryDir = data && data.library_dir ? data.library_dir : '—';
          setLibraryStatus(`ステップ 4: ${entries.length} 件の項目を描画中…`, `library_dir=${libraryDir}`);
          console.log('[podedit] library step 4: rendering entries', { seq, entries: entries.length, libraryDir });
          // If a transcription is already running (kicked off in a previous
          // modal-open or before a page reload), pick up its state before we
          // render so the running row shows the live progress label.
          try {
            const sr = await fetch('/api/library/transcribe/status', { cache: 'no-store' });
            if (sr.ok) {
              const sdata = await sr.json();
              transcribeJob = sdata.job || null;
              // queued and running both represent an in-flight job — the
              // queued window is short but real, so reattach polling for both.
              if (transcribeJob && (transcribeJob.status === 'running' || transcribeJob.status === 'queued')) {
                schedulePoll();
              }
            }
          } catch (_) {
            // Status poll is best-effort — the main library list still renders.
          }
          populateLibrary(data);
          setLibraryStatus('ライブラリを読み込みました。', `entries=${entries.length} library_dir=${libraryDir}`);
          writeLibraryDiag('render-complete', { seq, entries: entries.length, libraryDir });
        } catch (e) {
          if (!libraryOpen || seq !== libraryLoadSeq) {
            writeLibraryDiag('stale-error-ignored', { seq, error: summarizeError(e) });
            return;
          }
          const summary = summarizeError(e);
          setLibraryError(e);
          console.log('[podedit] library load failed', { seq, error: summary, raw: e });
          writeLibraryDiag('error', { seq, error: summary });
        } finally {
          clearInterval(heartbeat);
          clearTimeout(watchdog);
        }
      })();
    } catch (e) {
      const summary = summarizeError(e);
      try {
        setLibraryError(e);
      } catch (_) {
        // If even status rendering failed, localStorage is the last fallback.
      }
      console.log('[podedit] library modal sync failure', { error: summary, raw: e });
      writeLibraryDiag('sync-error', { error: summary });
    }
  }
  window.addEventListener('error', (ev) => {
    if (!libraryOpen) return;
    const error = ev.error || new Error(ev.message || 'Unhandled window error');
    setLibraryError(error);
    console.log('[podedit] library visible window error', { error: summarizeError(error), raw: error });
  });
  window.addEventListener('unhandledrejection', (ev) => {
    if (!libraryOpen) return;
    const reason = ev.reason || new Error('Unhandled promise rejection');
    setLibraryError(reason);
    console.log('[podedit] library visible unhandled rejection', { error: summarizeError(reason), raw: reason });
  });
  function hideLibraryModal() {
    if (!libraryOpen) return;
    libraryOpen = false;
    libraryLoadSeq += 1;
    $libraryModal.hidden = true;
    // No UI to update once the modal is closed — stop the polling tick so we
    // don't burn battery. The worker thread keeps transcribing regardless;
    // we'll re-attach to its state on the next modal open.
    stopPoll();
    // Restore focus to whatever element triggered the open, so screen readers
    // and keyboard users don't lose their place.
    if (libraryReturnFocus && typeof libraryReturnFocus.focus === 'function') {
      libraryReturnFocus.focus();
    }
    libraryReturnFocus = null;
  }
  function populateLibrary(data) {
    const entries = Array.isArray(data && data.entries) ? data.entries : [];
    const libraryDir = data && data.library_dir ? data.library_dir : '—';
    const currentPath = data && data.path ? data.path : libraryDir;
    const parentPath = data && data.parent ? data.parent : null;
    const active = data && data.active ? data.active : '';
    const activePath = data && data.active_path ? data.active_path : '';
    setLibraryStatus(`ステップ 4: ${entries.length} 件の項目を描画中…`, `path=${currentPath}`);
    console.log('[podedit] populateLibrary start', { entries: entries.length, libraryDir, currentPath, active });
    $libraryDirHint.textContent = libraryDir;
    if ($libraryCurrentPath) $libraryCurrentPath.textContent = currentPath;
    renderLibraryBreadcrumbs(currentPath);
    $libraryList.innerHTML = '';
    if (!entries.length && !parentPath) {
      $libraryStatus.textContent = `${currentPath} に音声ファイルが見つかりません。`;
      $libraryStatus.hidden = false;
      console.log('[podedit] populateLibrary empty', { currentPath });
      return;
    }
    $libraryStatus.hidden = true;
    // Treat both 'queued' (transient) and 'running' as in-flight so the
    // entry's row shows the progress label rather than a Transcribe button.
    const runningName = transcribeJob
      && (transcribeJob.status === 'running' || transcribeJob.status === 'queued')
      ? transcribeJob.name : null;
    const runningPath = transcribeJob
      && (transcribeJob.status === 'running' || transcribeJob.status === 'queued')
      ? transcribeJob.audio_path : null;
    if (parentPath) {
      const li = document.createElement('li');
      li.className = 'selectable library-nav-entry';
      const name = document.createElement('span');
      name.className = 'library-name';
      name.textContent = '..';
      const meta = document.createElement('span');
      meta.className = 'library-meta';
      meta.textContent = '親ディレクトリ';
      li.append(name, meta);
      li.addEventListener('click', () => navigateLibraryTo(parentPath));
      $libraryList.appendChild(li);
    }
    for (const e of entries) {
      const li = document.createElement('li');
      if (e.kind === 'dir') {
        li.className = 'selectable library-dir-entry';
        const name = document.createElement('span');
        name.className = 'library-name';
        name.textContent = e.name;
        const meta = document.createElement('span');
        meta.className = 'library-meta';
        meta.textContent = 'ディレクトリ';
        li.append(name, meta);
        li.addEventListener('click', () => navigateLibraryTo(e.path));
        $libraryList.appendChild(li);
        continue;
      }
      const entryPath = e.path || e.audio_path || '';
      const isActive = entryPath ? entryPath === activePath : e.name === active;
      const isTranscribing = runningPath ? entryPath === runningPath : e.name === runningName;
      // While a transcription is in progress for this entry we render a
      // dedicated "transcribing…" row rather than the normal selectable /
      // Transcribe button — we don't want the user clicking Transcribe twice.
      const selectable = e.has_transcript && !isActive && !isTranscribing;
      li.className = [
        selectable ? 'selectable' : 'disabled',
        isActive ? 'active' : '',
        isTranscribing ? 'transcribing' : '',
      ].filter(Boolean).join(' ');
      const name = document.createElement('span');
      name.className = 'library-name';
      name.textContent = e.name;
      const meta = document.createElement('span');
      meta.className = 'library-meta';
      const dur = e.duration_sec != null
        ? `${Math.floor(e.duration_sec / 60)}:${String(Math.round(e.duration_sec) % 60).padStart(2, '0')}`
        : '—';
      const tBadge = e.has_transcript
        ? '<span class="badge ok">文字起こし済み</span>'
        : '<span class="badge missing">文字起こしなし</span>';
      const sBadge = e.has_session ? '<span class="badge session">セッションあり</span>' : '';
      const activeBadge = isActive ? '<span class="badge ok">使用中</span>' : '';
      meta.innerHTML = `${dur}${tBadge}${sBadge}${activeBadge}`;
      li.append(name, meta);
      // Per-entry action button area: Transcribe for entries without a
      // transcript, a live progress label for the entry being transcribed.
      if (isTranscribing) {
        const progress = document.createElement('span');
        progress.className = 'library-progress';
        progress.dataset.role = 'progress';
        progress.textContent = formatTranscribeProgress(transcribeJob);
        li.append(progress);
      } else if (!e.has_transcript) {
        const btn = document.createElement('button');
        btn.className = 'library-action library-transcribe-btn';
        btn.type = 'button';
        btn.textContent = '文字起こし';
        // Stop click from bubbling to the row's selectable handler (which
        // we wouldn't have attached here anyway, but it's cheap insurance
        // against future regressions).
        btn.addEventListener('click', (ev) => {
          ev.stopPropagation();
          startTranscribe(e);
        });
        // If any other job is already running, disable so the user can't
        // queue a second one. The 409 from the server would catch this
        // too, but it's nicer UX to grey out preemptively.
        if (transcribeJob && (transcribeJob.status === 'running' || transcribeJob.status === 'queued')) {
          btn.disabled = true;
          btn.title = '別の文字起こしが進行中です';
        }
        li.append(btn);
      }
      if (selectable) {
        li.addEventListener('click', () => selectLibraryEntry(e));
      }
      $libraryList.appendChild(li);
    }
  }

  function renderLibraryBreadcrumbs(path) {
    if (!$libraryBreadcrumbs) return;
    $libraryBreadcrumbs.innerHTML = '';
    if (!path || path === '—') return;
    const root = document.createElement('button');
    root.type = 'button';
    root.textContent = '/';
    root.addEventListener('click', () => navigateLibraryTo('/'));
    $libraryBreadcrumbs.appendChild(root);
    let acc = '';
    for (const part of path.split('/').filter(Boolean)) {
      acc = `${acc}/${part}`;
      const sep = document.createElement('span');
      sep.textContent = '/';
      sep.className = 'library-breadcrumb-sep';
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = part;
      const target = acc;
      btn.addEventListener('click', () => navigateLibraryTo(target));
      $libraryBreadcrumbs.append(sep, btn);
    }
  }
  // ----- transcribe-from-modal (W7.7) -----
  // Stores the most-recent /api/library/transcribe/status job so populateLibrary
  // can render the running row distinctly. null = no job seen this session.
  let transcribeJob = null;
  let transcribePollTimer = null;
  let transcribePollInFlight = false;  // setInterval can overlap if a tick is slower than the 2s period — guard it
  let lastTranscribedLibrary = null;  // cached library payload so we can re-render with new progress
  function formatTranscribeProgress(job) {
    if (!job) return '文字起こし中…';
    const covered = job.progress_audio_sec || 0;
    const total = job.duration_sec || 0;
    const coveredStr = `${covered.toFixed(0)}秒`;
    const totalStr = total ? ` / ${Math.round(total)}秒` : '';
    const elapsed = job.elapsed_sec || 0;
    const mm = Math.floor(elapsed / 60);
    const ss = Math.floor(elapsed % 60).toString().padStart(2, '0');
    const rtf = job.rtf ? ` · RTF ${job.rtf.toFixed(1)}x` : '';
    const model = job.model ? ` · ${job.model}` : '';
    return `文字起こし中… ${coveredStr}${totalStr} 処理済み · 経過 ${mm}:${ss}${rtf}${model}`;
  }
  // ----- transcribe quality preset (W9) -----
  // Mapping from preset name to the (model, beam_size) tuple the server
  // accepts. Kept in JS so we don't need a round-trip to discover the
  // available options. ``balanced`` is the default — meaningful accuracy
  // win over tiny while still finishing a 30-min file in well under
  // 30 min on a 2-vCPU Codespace.
  const TRANSCRIBE_PRESETS = {
    fast:     { model: 'tiny',  beam_size: 1, label: 'tiny · greedy' },
    balanced: { model: 'small', beam_size: 1, label: 'small · greedy' },
    quality:  { model: 'small', beam_size: 5, label: 'small · beam=5' },
  };
  const TRANSCRIBE_PRESET_KEY = 'podedit.transcribePreset';
  const $transcribePreset = $('transcribe-preset');
  const $transcribePresetHint = $('transcribe-preset-hint');
  function currentTranscribePreset() {
    const v = $transcribePreset ? $transcribePreset.value : 'balanced';
    return TRANSCRIBE_PRESETS[v] ? v : 'balanced';
  }
  function updatePresetHint() {
    if (!$transcribePresetHint) return;
    const cur = TRANSCRIBE_PRESETS[currentTranscribePreset()];
    $transcribePresetHint.textContent = `${cur.label} + 日本語ポッドキャスト用プロンプト`;
  }
  if ($transcribePreset) {
    // Restore last choice; ignore unknown values (e.g. preset renamed).
    try {
      const saved = localStorage.getItem(TRANSCRIBE_PRESET_KEY);
      if (saved && TRANSCRIBE_PRESETS[saved]) $transcribePreset.value = saved;
    } catch (_) { /* localStorage unavailable */ }
    updatePresetHint();
    $transcribePreset.addEventListener('change', () => {
      try { localStorage.setItem(TRANSCRIBE_PRESET_KEY, $transcribePreset.value); }
      catch (_) { /* non-fatal */ }
      updatePresetHint();
    });
  }
  async function startTranscribe(entry) {
    const preset = TRANSCRIBE_PRESETS[currentTranscribePreset()];
    const name = entry && entry.name ? entry.name : String(entry || '');
    const path = entry && (entry.path || entry.audio_path) ? (entry.path || entry.audio_path) : null;
    try {
      const payload = {
        model: preset.model,
        beam_size: preset.beam_size,
        // initial_prompt / hotwords intentionally omitted: server falls
        // back to its DEFAULT_JA_PODCAST_PROMPT, which is the right
        // default for this MVP's target content. Custom prompts can be
        // added via a future settings UI.
      };
      if (path) payload.path = path;
      else payload.name = name;
      const r = await fetch('/api/library/transcribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        const text = await r.text();
        throw new Error(`HTTP ${r.status}: ${text.slice(0, 200)}`);
      }
      transcribeJob = await r.json();
      logKPI('ui.library.transcribe.started', {
        name, path, model: transcribeJob.model, beam_size: preset.beam_size,
        preset: currentTranscribePreset(),
      });
      // Re-render with the new running state, then start polling.
      if (lastTranscribedLibrary) populateLibrary(lastTranscribedLibrary);
      schedulePoll();
    } catch (e) {
      setLibraryStatus(`文字起こしを開始できませんでした: ${e.message}`);
    }
  }
  function schedulePoll() {
    if (transcribePollTimer) return;
    transcribePollTimer = setInterval(pollTranscribeStatus, 2000);
    // Also fire one immediately so the UI doesn't show stale progress
    // for two whole seconds after kickoff.
    pollTranscribeStatus();
  }
  function stopPoll() {
    if (!transcribePollTimer) return;
    clearInterval(transcribePollTimer);
    transcribePollTimer = null;
  }
  async function pollTranscribeStatus() {
    // setInterval can re-fire while a previous tick's fetch is still in
    // flight (slow network / paused tab). Without a guard, two concurrent
    // ticks observing the same `done` status would each re-fetch the library
    // and double-log KPI/finished events. Skip overlapping ticks.
    if (transcribePollInFlight) return;
    transcribePollInFlight = true;
    try {
      const r = await fetch('/api/library/transcribe/status', { cache: 'no-store' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      transcribeJob = data.job || null;
      // Update the inline progress label on the running entry without a
      // full re-render — cheaper, and avoids losing scroll/focus state.
      const progressEl = $libraryList.querySelector('li.transcribing [data-role="progress"]');
      if (progressEl && transcribeJob) {
        progressEl.textContent = formatTranscribeProgress(transcribeJob);
      }
      if (!transcribeJob || transcribeJob.status === 'done' || transcribeJob.status === 'error') {
        stopPoll();
        if (transcribeJob && transcribeJob.status === 'done') {
          logKPI('ui.library.transcribe.finished', {
            name: transcribeJob.name,
            elapsed_sec: transcribeJob.elapsed_sec,
            rtf: transcribeJob.rtf,
          });
          // Re-fetch the library so the just-transcribed entry flips to
          // has_transcript:true and becomes selectable.
          if (libraryOpen) {
            const seq = ++libraryLoadSeq;
            try {
              const refreshed = await fetchCurrentLibraryListing(seq);
              // The user could have closed the modal during the refresh
              // await; if so, drop the result so we don't repopulate a
              // hidden modal with stale state.
              if (!libraryOpen || seq !== libraryLoadSeq) return;
              lastTranscribedLibrary = refreshed;
              populateLibrary(refreshed);
              setLibraryStatus(
                `文字起こしが完了しました: ${transcribeJob.name}`,
                `${transcribeJob.segments} セグメント · ${transcribeJob.elapsed_sec.toFixed(0)}秒`,
              );
            } catch (err) {
              if (libraryOpen) setLibraryError(err);
            }
          }
        } else if (transcribeJob && transcribeJob.status === 'error') {
          setLibraryStatus(
            `文字起こしに失敗しました: ${transcribeJob.name}`,
            transcribeJob.error || 'エラー詳細なし',
          );
          logKPI('ui.library.transcribe.failed', {
            name: transcribeJob.name,
            error: transcribeJob.error,
          });
          // Re-render so the Transcribe button comes back enabled.
          if (lastTranscribedLibrary) populateLibrary(lastTranscribedLibrary);
        }
      }
    } catch (e) {
      console.log('[podedit] transcribe status poll failed', { error: String(e) });
      // Don't kill the poll on a single transient error — the worker still
      // owns the actual job. We'll try again on the next tick.
    } finally {
      transcribePollInFlight = false;
    }
  }
  async function selectLibraryEntry(entry) {
    if (librarySwitching) return;
    const name = entry && entry.name ? entry.name : String(entry || '');
    const path = entry && (entry.path || entry.audio_path) ? (entry.path || entry.audio_path) : null;
    librarySwitching = true;
    $libraryStatus.textContent = `${name} に切り替え中…`;
    $libraryStatus.hidden = false;
    try {
      const payload = path ? { path } : { name };
      const r = await fetch('/api/library/select', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        const text = await r.text();
        throw new Error(`HTTP ${r.status} ${text.slice(0, 200)}`);
      }
      logKPI('ui.library.selected', { name, path });
      // The page reload picks up the new transcript / waveform / audio.
      // beforeunload above will warn if there's unsaved work.
      window.location.reload();
    } catch (e) {
      $libraryStatus.textContent = `切り替えに失敗しました: ${e.message}`;
      librarySwitching = false;
    }
  }
  function showOverwritePrompt(file) {
    $libraryStatus.hidden = false;
    $libraryStatus.textContent = `同名ファイルが既にあります: ${file.name}　`;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = '上書きしてアップロード';
    btn.className = 'library-action';
    btn.style.marginLeft = '6px';
    btn.addEventListener('click', () => {
      btn.disabled = true;
      uploadFileToLibrary(file, { overwrite: true });
    });
    $libraryStatus.appendChild(btn);
  }

  async function uploadFileToLibrary(file, opts) {
    opts = opts || {};
    const overwrite = !!opts.overwrite;
    const maxBytes = 500 * 1024 * 1024;
    if (file.size > maxBytes) {
      setLibraryStatus(`ファイルが大きすぎます: ${(file.size / 1024 / 1024).toFixed(1)} MB (上限 500 MB)`);
      return;
    }

    async function readUploadError(r) {
      const text = await r.text();
      try {
        const reply = JSON.parse(text);
        return { detail: reply && reply.detail ? String(reply.detail) : text.slice(0, 200), status: r.status };
      } catch (_) {
        return { detail: text.slice(0, 200), status: r.status };
      }
    }

    $btnLibraryUpload.disabled = true;
    const prev = $btnLibraryUpload.textContent;
    $btnLibraryUpload.textContent = `${file.name} をアップロード中…`;
    try {
      logKPI('ui.library.upload.fetch_started', { name: file.name, bytes: file.size, overwrite });

      const init = await fetch('/api/library/upload/init', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ basename: file.name, size: file.size, overwrite }),
      });
      if (!init.ok) {
        const err = await readUploadError(init);
        if (init.status === 409 && /^already_exists:/.test(err.detail)) {
          // Surface a friendly status and let the user retry with overwrite.
          showOverwritePrompt(file);
          logKPI('ui.library.upload.fetch_failed', {
            message: err.detail, name: file.name, status: 409, reason: 'already_exists',
          });
          return;
        }
        throw new Error(err.detail);
      }
      const started = await init.json();
      logKPI('ui.library.upload.init_ok', {
        upload_id: started.upload_id,
        chunk_size: started.chunk_size,
        name: file.name,
        bytes: file.size,
      });

      const uploadId = encodeURIComponent(started.upload_id);
      const chunkSize = started.chunk_size;
      if (!Number.isInteger(chunkSize) || chunkSize <= 0) {
        throw new Error('invalid chunk_size from server');
      }
      const chunks = file.size === 0 ? 0 : Math.ceil(file.size / chunkSize);
      let bytesReceived = 0;

      for (let index = 0; index < chunks; index += 1) {
        const start = index * chunkSize;
        const end = Math.min(start + chunkSize, file.size);
        const chunk = await fetch(`/api/library/upload/${uploadId}/chunk`, {
          method: 'PUT',
          headers: { 'X-Chunk-Index': String(index) },
          body: file.slice(start, end),
        });
        if (!chunk.ok) {
          throw new Error((await readUploadError(chunk)).detail);
        }
        const progress = await chunk.json();
        bytesReceived = progress.bytes_received;
        const pct = file.size ? Math.floor((bytesReceived / file.size) * 100) : 100;
        $btnLibraryUpload.textContent = `${file.name} を ${pct}%...`;
        logKPI('ui.library.upload.chunk_progress', {
          chunk_index: index,
          bytes_received: bytesReceived,
          total_bytes: file.size,
        });
      }

      const finalize = await fetch(`/api/library/upload/${uploadId}/finalize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chunks }),
      });
      if (!finalize.ok) {
        throw new Error((await readUploadError(finalize)).detail);
      }
      const reply = await finalize.json();
      logKPI('ui.library.upload.fetch_ok', { basename: reply.basename, bytes: reply.bytes });
      logKPI('ui.library.uploaded', { basename: reply.basename, bytes: reply.bytes });
      logKPI('ui.library.upload.auto_transcribe_started', { basename: reply.basename, path: reply.path });
      const uploadsDir = reply.path.replace(/\/[^/]+$/, '');
      await navigateLibraryTo(uploadsDir);
      startTranscribe({ name: reply.basename, path: reply.path }).catch((err) => {
        logKPI('ui.library.upload.auto_transcribe_failed', { message: String(err && err.message ? err.message : err) });
        console.log('auto_transcribe_failed', err);
      });
      setLibraryStatus(`${reply.basename} をアップロードしました。文字起こしを開始しています…`);
    } catch (e) {
      logKPI('ui.library.upload.fetch_failed', { message: e.message, name: e.name });
      console.log('ui.library.upload.fetch_failed', { message: e.message, name: e.name, stack: e.stack }, e);
      setLibraryStatus(`アップロードに失敗しました: ${String(e && e.message ? e.message : e)}`);
    } finally {
      $btnLibraryUpload.disabled = false;
      $btnLibraryUpload.textContent = prev;
    }
  }
  $btnOpen.addEventListener('click', showLibraryModal);
  $btnLibraryClose.addEventListener('click', hideLibraryModal);
  if ($btnLibraryUpload && $libraryFileInput) {
    $btnLibraryUpload.addEventListener('click', () => {
      logKPI('ui.library.upload.button_clicked');
      $libraryFileInput.click();
    });
    $libraryFileInput.addEventListener('change', async () => {
      const f = $libraryFileInput.files && $libraryFileInput.files[0];
      logKPI('ui.library.upload.change_fired', { has_file: !!f, name: f ? f.name : null, bytes: f ? f.size : null });
      if (!f) return;
      await uploadFileToLibrary(f);
      $libraryFileInput.value = '';
    });
  }
  $libraryModal.addEventListener('click', (ev) => {
    // click on the dim backdrop closes; clicks inside the panel don't bubble
    if (ev.target === $libraryModal) hideLibraryModal();
  });

  // ------- initial paint -------
  if (state.hasActive) rerenderOps();
  else {
    syncTimelineUI();
    updateStats();
  }
  renderPasteAnchor();
  refreshButtons();
})();
