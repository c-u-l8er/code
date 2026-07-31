'use strict';

const $ = (id) => document.getElementById(id);
const state = {
  lanes: [], sel: null, health: {}, models: {}, busy: false, log: null,
  workers: {}, limits: {}, consults: [], consult: null, history: [],
  goals: [], goal: null, observations: [], dismissed: new Set(), resumeOf: null,
  findings: [], findingsAll: false, findingsSeen: -1, direction: null,
  obligations: [], obligationsSeen: -1, workspace: null, supervisor: null,
  settings: null, preview: null, pstamp: null, depAll: false, prAll: false,
  specBusy: null, specPoll: null, specWork: null,
  // How the thread is being read: `feed` is every line in the order it
  // happened, `brief` is a second model's read of the same thing. Kept in the
  // browser rather than in the harness's state, because it is what this person
  // wants to look at right now and not a fact about the workspace - but kept
  // ACROSS reloads, because a preference the console forgets every time you
  // press F5 is one you end up setting instead of using.
  dock: localStorage.getItem('amp.dock') === 'brief' ? 'brief' : 'feed',
  // Which spec runs the operator has folded open or shut, by row. The Specs pane
  // rewrites itself every four seconds while anything is live, so without this
  // the fold is recomputed from a default on every tick and the operator's press
  // is undone. Kept out of the render path deliberately: this is what the person
  // did, and it outranks what the row would have opened as.
  specOpen: {},
  // Transcripts of older runs, by run id, once fetched. An older run is finished,
  // so its rounds do not change - and without this the repaint would drop back to
  // the placeholder and ask the server for the same bytes every four seconds.
  specRounds: {},
};

async function api(path, opts) {
  const r = await fetch(path, opts);
  return r.json();
}
const post = (path, body) =>
  api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
}

/** The first line with anything on it. Long harness prompts are summarised, not shown. */
function _firstLine(s) {
  const l = String(s || '').split('\n').find((x) => x.trim()) || '';
  return l.length > 120 ? l.slice(0, 120) + '…' : l;
}

// ---------------------------------------------------------------- render

const ROLE_DOES = {
  worker: 'writes the code',
  architect: 'answers when a worker escalates',
  supervisor: 'holds the workspace against its mission',
};

function roleTitle(r) {
  return `${r.role} — ${ROLE_DOES[r.role] || ''}` +
    `\nrunning as: ${r.model}` +
    (r.ready ? '' : `\nnot available: ${r.why}`) +
    '\nchange it in Settings';
}

function renderHealth() {
  const h = state.health;
  const dot = (ok, label, title) =>
    `<span class="h" title="${esc(title || '')}"><span class="dot ${ok ? 'ok' : 'bad'}"></span>${label}</span>`;
  // A lit dot has to mean the thing it names is actually connected. Two ways
  // that was false: codex vanished entirely when it was unsigned and unused, so
  // there was no unlit light to notice; and the OpenRouter dot was labelled
  // "gpt-5.6", which read as "GPT is connected" and stayed green while codex -
  // the other place GPT can come from - was signed out. It only ever meant a
  // key is on file, so it now says so.
  //
  // The third light is the architect, named for whichever backend is answering,
  // because "is the architect up" is the question being asked and the answer
  // moved when the backend did. It is lit by the same check the server refuses
  // rounds with, so the light and the behaviour cannot disagree.
  // Three lights, one per role, each naming the model answering as it. The two
  // bare `claude` / `codex` dots that used to sit here named an INSTALL, not a
  // job: they went green because a CLI was signed in, which says nothing about
  // whether anything is using it or what for. A role light says who does the
  // work, who is asked when it gets stuck, and who checks it is the right work.
  //
  // Off is drawn differently from broken. A role you switched off is grey and
  // says so; a role that is on and cannot answer is red and says why.
  $('health').innerHTML = (h.roles || [])
    .map((r) => `<span class="h ${r.on ? '' : 'off'}" title="${esc(roleTitle(r))}">` +
      `<span class="dot ${!r.on ? 'off' : r.ready ? 'ok' : 'bad'}"></span>` +
      `${esc(r.role)}: <b>${esc(r.model)}</b></span>`)
    .join('');

  const problems = [];
  if (!h.claude_installed)
    problems.push('Claude Code missing &mdash; <code>npm i -g @anthropic-ai/claude-code</code>');
  else if (h.claude_auth)
    problems.push(
      `${esc(h.claude_auth)} <button id="btn-connect" class="primary">Connect Claude</button>`
    );
  if (h.codex_needed && !h.codex_installed)
    problems.push('Codex CLI missing &mdash; <code>npm i -g @openai/codex</code>');
  // Offered whenever the CLI is there and unsigned, not only when a lane
  // already depends on it: signing in is the precondition for using the
  // subscription at all, so gating the offer on already using it is circular.
  else if (h.codex_installed && !h.codex_logged_in)
    problems.push(
      `Codex not signed in <button id="btn-connect-codex" class="primary">Connect ChatGPT</button>`
    );
  if ((h.unbound_lanes || []).length)
    problems.push(
      `${h.unbound_lanes.length} codex lane(s) have no env id &mdash; run <code>codex cloud</code>, copy the id, paste it on the lane`
    );
  // Only ever say the architect is down, never which vendor is missing a key:
  // with the backend switchable, an OpenRouter warning while the architect runs
  // happily on your subscription is a warning about nothing. The codex case is
  // already covered above by the Connect ChatGPT offer, which is actionable.
  // Not a problem if you switched it off. A banner that nags about the absence
  // of a thing you just turned off is teaching you to ignore the banner.
  const archOff = (h.roles || []).some((r) => r.role === 'architect' && !r.on);
  if (!h.architect_ready && !archOff &&
      !(h.architect_backend === 'codex' && h.codex_installed))
    problems.push(
      `No architect (${esc(h.architect_backend || 'unset')}) &mdash; escalation is disabled`
    );

  const b = $('banner');
  if (problems.length) {
    b.innerHTML = problems.join(' &nbsp;·&nbsp; ');
    b.classList.remove('hidden');
    b.classList.toggle('bad', !h.claude_installed || !!h.claude_auth);
    const c = $('btn-connect');
    if (c) c.onclick = connectClaude;
    const x = $('btn-connect-codex');
    if (x) x.onclick = connectCodex;
  } else {
    b.classList.add('hidden');
  }
}

// ---------------------------------------------------------------- running work

function span(s) {
  return s >= 60 ? `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s` : `${s}s`;
}

// "Running" on its own is the least useful thing the board can say — a worker
// three minutes into a build and a worker wedged for an hour look identical.
// So a live row leads with what the worker is doing, read out of the
// transcript it writes as it goes, and how long ago it last said anything.
function doingLine(w) {
  if (w.phase === 'starting') return '<span class="muted">starting up…</span>';
  const what = w.doing ? `${esc(w.phase)} <span class="what">${esc(w.doing)}</span>` : esc(w.phase);
  // Quiet is only worth flagging once it is longer than any single tool call
  // plausibly takes. Under a minute it is just the worker thinking.
  const quiet =
    w.quiet_s < 60
      ? ''
      : ` <span class="${w.stalled ? 'err' : 'warnt'}">· quiet ${span(w.quiet_s)}` +
        (w.stalled ? ' — looks hung' : w.long_running ? ', but still working' : '') +
        '</span>';
  return what + quiet;
}

function renderRunning() {
  const rows = [];
  for (const l of state.lanes)
    for (const t of l.tasks || []) {
      if (t.status !== 'running' || !t.task_id) continue;
      const w = state.workers[t.task_id];
      const near = w && (w.rss_gb / w.mem_gb > 0.8 || w.elapsed_s / w.timeout_s > 0.8);
      const btns =
        `<button class="stop" data-lane="${esc(l.name)}" data-task="${esc(t.task_id)}">Stop</button>` +
        (w && w.stalled
          ? `<button class="restart" data-lane="${esc(l.name)}" data-task="${esc(t.task_id)}">Restart</button>`
          : '');
      rows.push(
        `<span class="run ${w && w.stalled ? 'stalled' : near ? 'near' : ''}">` +
        `<span class="dot"></span><b>${esc(l.name)}</b>` +
        (w
          ? ` ${doingLine(w)}<span class="muted cost">${span(w.elapsed_s)} of ${span(w.timeout_s)} · ` +
            `${w.rss_gb.toFixed(1)} of ${w.mem_gb.toFixed(0)} GB · ${w.cpu_pct}% cpu · ` +
            `${w.turns} steps${w.adopted ? ' · picked up after a restart' : ''}</span>`
          : ' <span class="muted">no live process — record is stale</span>') +
        btns + `</span>`
      );
    }
  const el = $('running');
  el.classList.toggle('hidden', !rows.length);
  el.innerHTML = rows.join('');
  // While anything is live this keeps ticking on its own, so the numbers are
  // still true for someone who just opened the page and dispatched nothing.
  // The slow beat matters as much: a worker started from another window, or by
  // a relay, is invisible until something asks - so an idle board still looks.
  clearTimeout(renderRunning.timer);
  renderRunning.timer = setTimeout(refresh, rows.length ? 3000 : 10000);
  el.querySelectorAll('.stop').forEach((b) =>
    b.addEventListener('click', async () => {
      b.disabled = true;
      b.textContent = 'stopping…';
      await post('/api/cancel', { lane: b.dataset.lane, task_id: b.dataset.task });
      refresh();
      loadChat();
    })
  );
  // Spending a second budget on a prompt that already burned one is the
  // operator's call, so it is asked for plainly rather than assumed.
  el.querySelectorAll('.restart').forEach((b) =>
    b.addEventListener('click', async () => {
      if (!confirm(`Stop the ${b.dataset.lane} worker and run the same task again?\n\n` +
                   'That spends a second budget on it.')) return;
      b.disabled = true;
      b.textContent = 'restarting…';
      await post('/api/restart', { lane: b.dataset.lane, task_id: b.dataset.task });
      refresh();
      loadChat();
    })
  );
}

// ---------------------------------------------------------------- sign-in

async function connectClaude() {
  const dlg = $('signin');
  const st = $('si-status');
  dlg.classList.remove('hidden');
  $('si-link').innerHTML = '';
  $('si-code').value = '';
  st.textContent = 'starting sign-in…';
  st.className = 'muted';

  const r = await post('/api/auth/start');
  if (!r.ok) {
    st.textContent = r.error;
    st.className = 'err';
    return;
  }
  $('si-link').innerHTML =
    `<a href="${esc(r.url)}" target="_blank" rel="noreferrer">Open the Anthropic sign-in page</a>`;
  st.textContent = 'Chrome should have opened. Approve, then paste the code below.';
}

async function submitCode() {
  const st = $('si-status');
  const code = $('si-code').value.trim();
  if (!code) return;
  st.textContent = 'connecting…';
  st.className = 'spin';
  const r = await post('/api/auth/code', { code });
  if (!r.ok) {
    st.textContent = r.error;
    st.className = 'err';
    return;
  }
  st.textContent = 'connected — workers can authenticate now.';
  st.className = 'good';
  await refresh();
  setTimeout(() => $('signin').classList.add('hidden'), 1200);
}

// ---------------------------------------------------------------- settings

// The list is rendered from what the server reports, not written into the HTML,
// so "what is this actually spending on?" is answered by the code that spends.
function renderSpend(s) {
  const el = $('set-actions');
  // Struck through when the architect cannot run at all, not merely when
  // OpenRouter is off: with the backend switchable, "off" and "cannot run" are
  // different questions and this list is answering the second one.
  el.className = 'spend' + (s.architect_ready ? '' : ' off');
  el.innerHTML = (s.architect_actions || [])
    .map(
      (a) =>
        `<div class="s ${a.unattended ? 'auto' : ''}">` +
        `<span class="n">${esc(a.action)}` +
        (a.unattended ? ' <span class="tag">· no click</span>' : '') +
        `</span>` +
        `<span class="w">${esc(a.fires)}` +
        (a.note ? ` <span class="tag">(${esc(a.note)})</span>` : '') +
        `</span></div>`
    )
    .join('');
}

async function openSettings() {
  $('settings').classList.remove('hidden');
  $('set-status').textContent = '';
  $('set-status').className = 'muted';
  $('db-report').className = 'dbreport hidden';
  loadDb();
  const s = await post('/api/settings');
  if (!s.ok) {
    $('set-status').textContent = s.error;
    $('set-status').className = 'err';
    return;
  }
  renderSettings(s);
}

// One option in a role's dropdown. Every role now lists every model, so most of
// the work here is saying why the ones you cannot pick are there at all.
//
// `cannot` and `why_not` are drawn differently because they are different facts.
// `cannot` means this model can never hold this role - it is disabled, and no
// amount of signing in will change that, so the reason is stated on the line
// itself. `why_not` means it could, but not today; it stays SELECTABLE, because
// signing in is the fix and the operator is allowed to pick it first and fix it
// after. Collapsing the two into one grey entry would send you off to fix
// something that is not broken.
// A choice as this file expects it, whatever the server sent.
//
// This console serves app.js from disk but holds amp.py in memory, so after any
// edit the two are out of step until someone restarts it - the frontend is new
// and the backend is whatever was loaded hours ago. That is not an edge case,
// it is every single edit. When a bare string arrives here it means exactly
// that, and the whole panel rendered `undefined` for want of these four lines.
function roleChoice(c) {
  return typeof c === 'string'
    ? { value: c, note: '', cannot: '', why_not: '', stale: true }
    : c;
}

function roleOption(r, raw) {
  const c = roleChoice(raw);
  // Say what it resolves to BEFORE any complaint about it, so the line reads
  // "architect -> opus (not signed in)" and not "architect (not signed in) ->
  // opus", which invites you to think the architect itself is the problem.
  const label = c.value
    + (c.value === 'architect' ? ` \u2192 ${r.model}` : '')
    + (c.cannot ? ' \u2014 ' + c.cannot : c.why_not ? ' (' + c.why_not + ')' : '');
  return `<option value="${esc(c.value)}"`
    + (c.value === r.choice ? ' selected' : '')
    + (c.cannot ? ' disabled' : '')
    + ` title="${esc(c.note || '')}">${esc(label)}</option>`;
}

function renderRoles(roles) {
  // A short menu and a full one look the same if nobody says which you are
  // looking at. If the server is still on the old code, the lists it sends are
  // the old per-role ones - so say that, and say the fix, rather than let it
  // read as a feature that did not ship.
  const stale = (roles || []).some((r) =>
    (r.choices || []).some((c) => typeof c === 'string'));
  $('set-roles').innerHTML = (stale
    ? '<p class="err">This console is running an older copy of the harness, so '
      + 'each role is still offering its old short list. Restart it to get every '
      + 'model for every role.</p>'
    : '') + (roles || [])
    .map((r) => `<div class="role-row ${r.on ? '' : 'off'}" data-role="${esc(r.role)}">` +
      `<label class="switch"><input type="checkbox" class="role-on"${r.on ? ' checked' : ''}>` +
      `<span class="role-name">${esc(r.role)}</span></label>` +
      `<span class="role-does">${esc(ROLE_DOES[r.role] || '')}</span>` +
      `<select class="role-model"${r.on ? '' : ' disabled'}>` +
      (r.choices || []).map((c) => roleOption(r, c)).join('') +
      `</select>` +
      // Whether the chosen one can actually answer right now, in the words that
      // say what to do about it. A dropdown that lets you pick a model and then
      // says nothing when it is signed out is a trap.
      `<span class="role-status ${r.ready ? 'good' : r.on ? 'err' : 'muted'}">` +
      `${esc(r.ready ? 'ready' : r.why || 'not available')}</span>` +
      `</div>`)
    .join('');
  $('set-roles').querySelectorAll('.role-row').forEach((n) => {
    const role = n.dataset.role;
    n.querySelector('.role-on').onchange = (e) => setRole(role, { on: e.target.checked });
    n.querySelector('.role-model').onchange = (e) => setRole(role, { model: e.target.value });
  });
}

async function setRole(role, patch) {
  const st = $('set-status');
  const s = await post('/api/role/set', { role, ...patch });
  if (!s.ok) {
    st.textContent = s.error;
    st.className = 'err';
    // The control is showing a setting the server refused; put it back.
    if (state.settings) renderRoles(state.settings.roles);
    return;
  }
  renderSettings(s);
  st.textContent = 'on' in patch
    ? patch.on
      ? `${role} is on again.`
      : `${role} is off — anything that would have used it will now refuse and say so.`
    : `${role} is ${patch.model} now — open threads carry on with it from their next round.`;
  st.className = 'good';
  // The header lights come off the same reading, so they move with this.
  await refresh();
}

function renderSettings(s) {
  state.settings = s;
  renderRoles(s.roles);
  $('set-openrouter').checked = !!s.openrouter_enabled;
  $('set-auto-escalate').checked = !!s.auto_escalate;
  // The slider is only written from the server's value while you are not
  // dragging it. Settings is re-rendered by every save, and putting the round
  // trip's answer back under a thumb you are still holding is how a slider
  // fights you.
  const bar = $('set-bar');
  if (document.activeElement !== bar) bar.value = Math.round((s.adopt_confidence ?? 0.6) * 100);
  $('set-bar-out').textContent = bar.value + '%';
  const nb = $('set-need');
  if (document.activeElement !== nb) nb.value = Math.round((s.adopt_need ?? 0.5) * 100);
  $('set-need-out').textContent = nb.value + '%';
  $('set-cal').innerHTML = calBlock(s.calibration, (s.adopt_confidence ?? 0.6));
  // Same rule as the sliders: never write a value back under a field the
  // operator is still typing in. The ceiling comes from the server rather than
  // the HTML so the box cannot offer a number the server will refuse.
  const sr = $('set-spec-rounds');
  if (s.spec_max_rounds_limit) sr.max = s.spec_max_rounds_limit;
  if (document.activeElement !== sr && s.spec_max_rounds) sr.value = s.spec_max_rounds;
  renderSpend(s);
}

async function setFlag(key, value) {
  const st = $('set-status');
  const s = await post('/api/settings/set', { [key]: value });
  if (!s.ok) {
    st.textContent = s.error;
    st.className = 'err';
    return;
  }
  renderSettings(s);
  st.textContent =
    key === 'architect_backend'
      ? `architect is ${value} now — open threads carry on with it from their next round.`
      : key === 'openrouter_enabled'
      ? value
        ? 'OpenRouter credit may be spent again.'
        : 'OpenRouter off — nothing here can spend it. Workers are unaffected.'
      : key === 'adopt_confidence'
      ? `goals with ${Math.round(value * 100)}% odds or better start on their own. ` +
        'Everything already waiting was just re-judged against it.'
      : key === 'adopt_need'
      ? `only goals the mission wants ${Math.round(value * 100)}% or more start on their ` +
        'own. Everything already waiting was just re-judged against it.'
      : key === 'spec_max_rounds'
      ? `spec runs get ${value} review rounds per document before they cap. ` +
        'Runs already stopped stay stopped.'
      : 'saved.';
  st.className = 'good';
  await refresh();
}

// ---------------------------------------------------------------- database
//
// The panel answers one question the rest of Settings does not: where does the
// work you have done actually live, and is the second copy of it current. So
// it leads with what the mirror holds rather than with its switches.

function mb(n) {
  return n == null ? '—' : n >= 1e6 ? (n / 1e6).toFixed(2) + ' MB'
    : n >= 1e3 ? (n / 1e3).toFixed(1) + ' kB' : n + ' B';
}

function renderDb(d) {
  state.db = d;
  const box = $('db-box');
  if (!d || d.ok === false) {
    box.innerHTML = `<span class="err">${esc((d && d.error) || 'cannot read the database')}</span>`;
    return;
  }
  if (!d.exists) {
    box.innerHTML = '<span class="muted">No database yet. <b>Back up now</b> creates it '
      + 'and copies everything currently on disk into it.</span>';
  } else {
    box.innerHTML =
      `<div class="dbrow"><span class="k">file</span><code class="path">${esc(d.path)}</code></div>` +
      `<div class="dbrow"><span class="k">holding</span>` +
      `<b>${d.docs}</b> documents, <b>${d.revisions}</b> versions ` +
      `<span class="muted">(${mb(d.bytes)} on disk, compressed from ` +
      `${mb(d.doc_bytes)} of current files)</span></div>` +
      `<div class="dbrow"><span class="k">last save</span>${esc(d.last_write || '—')}</div>` +
      (d.workspaces || []).map((w) =>
        `<div class="dbrow"><span class="k">${esc(w.workspace)}</span>` +
        `${w.docs} documents, ${mb(w.bytes)}</div>`).join('') +
      (d.failures && d.failures.n
        ? `<div class="dbrow err"><span class="k">failed</span>${d.failures.n} write(s), ` +
          `last ${esc(d.failures.last)} at ${esc(d.failures.at)} — the JSON files are ` +
          `unaffected, this is the copy falling behind</div>`
        : '');
  }
  const s = d.settings || {};
  $('set-db-mirror').checked = s.mirror === '1';
  if (document.activeElement !== $('set-db-keep')) $('set-db-keep').value = s.history_keep;
  if (document.activeElement !== $('set-db-sweep')) $('set-db-sweep').value = s.sweep_min;
  $('db-sync').innerHTML = d.exists
    ? `<div class="dbrow"><span class="k">this machine</span><code>${esc(d.device)}</code></div>` +
      `<div class="dbrow"><span class="k">change number</span>${d.cursor}` +
      ` <span class="muted">— a sync would resume from here</span></div>` +
      `<div class="dbrow"><span class="k">never stored</span><span class="v">` +
      `${(d.excluded || []).map((e) => `<code>${esc(e)}</code>`).join('')}</span></div>`
    : '<span class="muted">Nothing to describe until the database exists.</span>';
}

function dbReport(html, cls) {
  const n = $('db-report');
  n.className = 'dbreport ' + (cls || '');
  n.innerHTML = html;
}

async function loadDb() {
  renderDb(await api('/api/db'));
}

async function dbSet(key, value) {
  const d = await post('/api/db/set', { [key]: value });
  if (!d.ok && d.error) {
    dbReport(esc(d.error), 'err');
    return loadDb();
  }
  renderDb(d);
  dbReport(
    key === 'mirror'
      ? value
        ? 'Saves are being copied into the database again.'
        : 'The database is no longer being written. Your JSON files are untouched and '
          + 'everything already in the database is still there.'
      : key === 'history_keep'
      ? `Keeping the newest ${value} versions of each file. Trimming happens when you `
        + 'ask for it, so nothing was removed just now.'
      : Number(value) > 0
      ? `Sweeping every ${value} minutes.`
      : 'Sweeping is off — the button still works.',
    'good');
}

async function dbBackup() {
  dbReport('copying…', 'muted');
  const r = await post('/api/db/backup');
  if (!r.ok) return dbReport(esc(r.error || 'failed'), 'err');
  renderDb(r.status);
  dbReport(`Copied ${r.written} changed of ${r.scanned} files`
    + (r.failed ? `, <span class="err">${r.failed} failed</span>` : '') + '.', 'good');
}

async function dbVerify() {
  dbReport('comparing…', 'muted');
  const v = await post('/api/db/verify');
  if (!v.ok) return dbReport(esc(v.error || 'failed'), 'err');
  const list = (label, rows) => rows.length
    ? `<div class="dbrow"><span class="k">${label}</span><span class="v">` +
      rows.slice(0, 12).map((p) => `<code>${esc(p)}</code>`).join('') +
      (rows.length > 12 ? `<span class="muted">+${rows.length - 12} more</span>` : '') +
      '</span></div>'
    : '';
  dbReport(
    `<div class="dbrow"><span class="k">matching</span>${v.current} files</div>` +
    list('changed since', v.stale) +
    list('not copied', v.missing) +
    // Not a fault. A file that is gone from disk but still held here is the
    // reason the database exists, so it is reported in its own words.
    list('kept after being deleted', v.held) +
    (v.clean ? '<div class="dbrow good">The copy is current.</div>' : ''),
    v.clean ? 'good' : '');
}

async function dbPrune() {
  const keep = Number($('set-db-keep').value) || undefined;
  dbReport('trimming…', 'muted');
  const r = await post('/api/db/prune', { keep });
  if (!r.ok) return dbReport(esc(r.error || 'failed'), 'err');
  renderDb(r.status);
  dbReport(`Removed ${r.removed} old versions, ${r.kept} kept `
    + `(${r.keep} per file) and the file was compacted. The current copy of every `
    + 'document is untouched.', 'good');
}

// The ChatGPT flow runs the other way round: the code goes from here into the
// page, so there is nothing to submit and the console waits instead. One timer,
// cleared on every exit, so cancelling and reconnecting cannot leave two.
let cxPoll = null;

function cxStop() {
  if (cxPoll) clearInterval(cxPoll);
  cxPoll = null;
}

async function connectCodex() {
  const st = $('cx-status');
  $('cx-signin').classList.remove('hidden');
  $('cx-link').innerHTML = '';
  $('cx-code').classList.add('hidden');
  st.textContent = 'starting sign-in…';
  st.className = 'muted';
  cxStop();

  const r = await post('/api/codex-auth/start');
  if (!r.ok) {
    st.textContent = r.error;
    st.className = 'err';
    return;
  }
  if (r.connected) {
    await cxDone('already connected.');
    return;
  }
  $('cx-link').innerHTML =
    `<a href="${esc(r.url)}" target="_blank" rel="noreferrer">Open the ChatGPT device page</a>`;
  $('cx-code').textContent = r.code;
  $('cx-code').classList.remove('hidden');
  st.textContent = 'Chrome should have opened. Enter that code, then approve.';
  st.className = 'spin';
  cxPoll = setInterval(cxCheck, 2000);
}

async function cxCheck() {
  const st = $('cx-status');
  const r = await post('/api/codex-auth/poll');
  if (!r.ok) {
    cxStop();
    st.textContent = r.error;
    st.className = 'err';
    return;
  }
  if (r.connected) await cxDone('connected — the architect can run on your subscription.');
}

async function cxDone(msg) {
  cxStop();
  const st = $('cx-status');
  st.textContent = msg;
  st.className = 'good';
  await refresh();
  setTimeout(() => $('cx-signin').classList.add('hidden'), 1200);
}

// Rebuilt only when the markup actually changes, and scrolled back to where you
// left it when it does.
//
// This used to replace the whole list on every refresh - every 3s while anything
// is live - and `.lane-list` is a scroll container. Replacing all of a scroller's
// children makes the browser re-anchor, and doing it on a beat walks the list
// downward about a centimetre a tick: a panel that scrolls itself away while you
// are reading it. Nothing was scrolling it; it was being rebuilt underneath.
//
// Most of those rebuilds were also identical, because lanes change far more
// slowly than the poll. So the cheap fix and the correct one are the same:
// don't touch the DOM unless the output differs. Same rule the obligations and
// findings lists already follow.
// What each lane is rated on, in the order the work actually travels: is there
// somewhere to go, is it designed, did the goals land, did the workers, how far
// up the evidence ladder a review has judged it, what is still holding, and how
// much of what came back has been settled.
//
// This list used to be the last five tasks the lane ran. That is a log, and a
// log answers "what happened" - which is the one question the panes behind it
// already answer, eleven times over. It never answered "which lane is the one
// holding this up", and that is the only question a sidebar is any good at.
//
// Every number here is measured off a record already on disk (see
// `amp.lane_ratings`), carries the counts it was computed from in its tooltip,
// and reads `unscored` rather than 0% when nothing has been measured - because
// the fix for unmeasured is to measure and the fix for low is to work, and a
// zero that means both is worse than no number.
const LANE_RATINGS = [
  ['direction', 'direction'],
  ['spec', 'spec'],
  ['goals', 'goals'],
  ['workers', 'workers'],
  ['evidence', 'evidence'],
  ['standing', 'standing'],
  ['settled', 'settled'],
];

/** One lane rating as a narrow bar. Same shape as `meter`, sized for the
 *  sidebar, and titled with the counts rather than an adjective. */
function laneMeter(label, r) {
  if (!r) return '';
  const why = esc(r.why || '');
  // The empty track is drawn even with nothing in it, so the seven rows line up
  // in one column. Without it an unmeasured rating collapses to a dash beside
  // its label and reads as a missing row rather than a missing measurement.
  if (r.value === null || r.value === undefined)
    return `<div class="lr none" title="${why}"><span class="ll">${label}</span>` +
      `<span class="lm"></span><span class="lv">&mdash;</span></div>`;
  const pc = Math.round(r.value * 100);
  const bar = r.bar === null || r.bar === undefined ? null : r.bar;
  const tone = bar === null ? 'flat' : r.value >= bar ? 'good' : 'low';
  return `<div class="lr ${tone}" title="${why}"><span class="ll">${label}</span>` +
    `<span class="lm"><i style="width:${pc}%"></i>` +
    (bar === null ? '' : `<u style="left:${Math.round(bar * 100)}%"></u>`) +
    `</span><span class="lv">${pc}%</span></div>`;
}

/** The automation stage picker. Not a badge: the badge said which worker binary
 *  would run, which is on the Dispatch pane anyway. This says how far work is
 *  allowed to be carried in this lane unattended, and it is enforced - see
 *  `amp.stage_admits`, called from proposal_hold, spec_auto_on and
 *  goal_dispatch. A rung the harness cannot drive on its own is marked, so the
 *  dropdown never implies an automation that does not exist. */
function stagePick(l) {
  const opts = (state.stages || []).map((s) => {
    const mark = s.automated ? '' : ' \u00b7 by hand';
    return `<option value="${esc(s.key)}"${s.key === l.stage ? ' selected' : ''} ` +
      `title="${esc(s.means)} (${esc(s.who)})">${esc(s.key)}${mark}</option>`;
  }).join('');
  if (!opts) return `<span class="pill">${esc(l.stage || '')}</span>`;
  return `<select class="stage-pick" data-lane="${esc(l.name)}" ` +
    `title="how far work is carried in this lane without you">${opts}</select>`;
}

function renderLanes() {
  const el = $('lane-list');
  if (!state.lanes.length) {
    paintLanes(el, '<div class="empty">No lanes configured.</div>');
    return;
  }
  const html = state.lanes
    .map((l) => {
      const rt = l.ratings || {};
      const rows = LANE_RATINGS.map(([k, label]) => laneMeter(label, rt[k])).join('');
      return (
        `<div class="lane ${state.sel === l.name ? 'sel' : ''}" data-lane="${esc(l.name)}">` +
        `<div class="n"><span class="name">${esc(l.name)}</span>` +
        (l.mode && l.mode !== 'build' ? `<span class="pill mode">${esc(l.mode)}</span>` : '') +
        (l.bound ? '' : '<span class="pill unbound">no env</span>') +
        stagePick(l) +
        `</div><div class="repo">${esc(l.repo)} &middot; ${esc(l.branch)}</div>` +
        `<div class="lrs">${rows}</div></div>`
      );
    })
    .join('');

  if (!paintLanes(el, html)) return;   // unchanged: listeners are still on the
                                       // nodes that are still there
  el.querySelectorAll('.lane').forEach((n) =>
    n.addEventListener('click', () => select(n.dataset.lane))
  );
  // A click on the dropdown is not a click on the lane: picking a stage for a
  // lane you are not in is a thing you should be able to do without moving.
  el.querySelectorAll('.stage-pick').forEach((s) => {
    s.addEventListener('click', (e) => e.stopPropagation());
    s.addEventListener('change', async (e) => {
      e.stopPropagation();
      const r = await post('/api/lane/stage', { lane: s.dataset.lane, stage: s.value });
      if (r && r.ok === false) alert(r.error || 'could not set the stage');
      refresh();
      if ($('pane-blockers').classList.contains('active')) loadBlockers();
    });
  });
}

/** Write `html` into `el` only if it differs. Returns whether it wrote. */
function paintLanes(el, html) {
  if (el.dataset.painted === html) return false;
  // The list now contains a control. Rebuilding it while someone has that
  // control open closes their dropdown mid-choice - and the poll runs every 3s,
  // so it would happen most times they tried. Leave it alone until they are
  // done; the next tick paints it.
  if (el.contains(document.activeElement) && document.activeElement !== el) return false;
  const top = el.scrollTop;
  el.innerHTML = html;
  el.dataset.painted = html;
  el.scrollTop = top;                  // a real change must not send you to the
                                       // top of a list you were reading either
  return true;
}

function select(name) {
  const changed = state.sel !== name;
  state.sel = name;
  const lane = state.lanes.find((l) => l.name === name);
  ['d-lane', 'f-lane', 'a-lane', 'l-lane', 'p-lane', 'b-lane'].forEach((id) => ($(id).textContent = name || '—'));
  fillSessions(name);
  if (lane) {
    $('d-branch').value = lane.branch || 'main';
    $('d-backend').value = lane.backend;
  }
  applyBackend();
  renderLanes();
  renderResumeChip();
  bindEnvPromptIfNeeded(lane);
  if (changed) {
    state.consult = null;
    // The frame belongs to the lane it was started for; carrying it across a
    // lane change would show you another lane's site under this lane's name.
    state.preview = null;
    state.pstamp = null;
    $('p-frame').classList.add('hidden');
    $('p-frame').removeAttribute('src');
    $('p-empty').classList.remove('hidden');
    // Same rule for every other pane that holds one lane's output: the heading
    // now says the new lane, so anything still on screen underneath it is the
    // old lane's work wearing the new lane's name. Drop it, then reload
    // whichever pane is actually being looked at.
    state.log = null;
    $('l-out').innerHTML = '<div class="empty">Pick a session, then Load.</div>';
    $('l-live').classList.add('hidden');
    $('f-out').textContent = 'Poll a lane, then Load.';
    // A goal id belongs to the lane it was opened in, so it cannot stay open
    // across the change.
    state.goal = null;
    renderGoals();
    if ($('pane-log').classList.contains('active')) loadLog();
    if ($('pane-ask').classList.contains('active')) loadConsults(name);
    if ($('pane-direction').classList.contains('active')) loadDirection();
    // Always - every rung of this diagram is one lane's rung, so a lane change
    // makes all of it somebody else's.
    state.flow = null;
    if ($('pane-blockers').classList.contains('active')) loadBlockers();
    // Always - the tab shows one lane's documents now, so a lane change is a
    // change of everything on it.
    if ($('pane-specs').classList.contains('active')) loadSpec();
    // Only when it is narrowed. Widened, this pane is not about the selected
    // lane at all, so nothing on it went stale - and reloading it anyway would
    // spend a wrangler start-up per site to redraw the same screen.
    if ($('pane-publish').classList.contains('active') && !state.depAll) loadDeploy();
    if ($('pane-prs').classList.contains('active') && !state.prAll) loadPrs();
    if (previewActive()) loadPreview();
  }
}

// The two workers do not share a control surface: budget/model/reply are claude
// only, best-of/attempt/apply are codex only.
function applyBackend() {
  const claude = $('d-backend').value === 'claude';
  document.querySelectorAll('.claude-only').forEach((n) => n.classList.toggle('hidden', !claude));
  document.querySelectorAll('.codex-only').forEach((n) => n.classList.toggle('hidden', claude));
  $('d-prompt').placeholder = claude
    ? 'Task for the Claude worker.\n\nIt runs in an isolated worktree on branch amp/<lane>, so it cannot disturb your live checkout. If it stops to ask, answer with Reply to last.'
    : 'Task for the Codex Cloud worker.\n\nWrite it self-contained: the cloud CLI has no message/answer channel, so a task that stops to ask a question cannot be replied to headlessly.';
}

$('d-backend').onchange = async () => {
  applyBackend();
  if (!state.sel) return;
  await post('/api/lane/backend', { lane: state.sel, backend: $('d-backend').value });
  refresh();
};

function bindEnvPromptIfNeeded(lane) {
  if (!lane || lane.bound) return;
  $('d-status').innerHTML =
    `<span class="err">lane has no env id.</span> ` +
    `<button id="btn-bind">Paste env id</button>`;
  const b = $('btn-bind');
  if (b)
    b.onclick = async () => {
      const id = prompt(`Codex Cloud environment id for "${lane.name}"\n\nFind it by running: codex cloud`);
      if (!id) return;
      const r = await post('/api/lane/env', { lane: lane.name, env_id: id.trim() });
      $('d-status').innerHTML = r.ok
        ? `<span class="good">bound</span>`
        : `<span class="err">${esc(r.error)}</span>`;
      if (r.ok) refresh();
    };
}

// Adding a lane is a two-field job — a name and a directory — because the
// server reads the repo off the git origin and checks everything else itself.
// The point is that the errors come back as sentences, so this form only has to
// show them, not re-implement any of the checks.
function wireLaneAdd() {
  const box = $('lane-add');
  const msg = $('la-msg');
  const show = (on) => {
    box.classList.toggle('hidden', !on);
    msg.textContent = '';
    if (on) $('la-name').focus();
  };
  $('btn-lane-add').onclick = () => show(box.classList.contains('hidden'));
  $('la-cancel').onclick = () => show(false);
  $('la-go').onclick = async () => {
    const name = $('la-name').value.trim();
    if (!name) return;
    msg.className = 'msg';
    msg.textContent = 'checking…';
    const r = await post('/api/lane/add', {
      lane: name,
      path: $('la-path').value.trim(),
      branch: $('la-branch').value.trim() || 'main',
    });
    if (!r.ok) {
      msg.className = 'msg err';
      msg.textContent = r.error;
      return;
    }
    ['la-name', 'la-path', 'la-branch'].forEach((id) => ($(id).value = ''));
    show(false);
    await refresh();
    select(r.lane.name);
  };
  ['la-name', 'la-path', 'la-branch'].forEach((id) =>
    $(id).addEventListener('keydown', (e) => e.key === 'Enter' && $('la-go').click())
  );
}

// ---------------------------------------------------------------- transcript

// ---------------------------------------------------------------- markdown
//
// Rulings, worker reports and packets are all written as markdown, and reading a
// pipe-table as raw text is the difference between a verdict and a wall of noise.
// Everything is escaped BEFORE any markup is added, so nothing an architect or a
// worker writes can become an element - the only tags here are the ones below.

function mdInline(s) {
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, t, u) =>
      /^(https?:|\/|#)/.test(u) ? `<a href="${u}" target="_blank" rel="noreferrer">${t}</a>` : m);
}

// Single-line fields - a done-condition, a task, an objective. They are not
// documents, so they get the inline half only: backticks, bold, links. Escaping
// first is what makes this safe, and is the whole reason it is a named helper
// rather than a call to mdInline at each site where it would be easy to forget.
const mdi = (s) => mdInline(esc(s));

const ROW = (r) => r.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((c) => c.trim());
const IS_RULE = (r) => /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(r) && r.includes('-');

function md(src) {
  // Fenced code comes out first: nothing inside a fence is markdown. A fence
  // inside a numbered list is indented, and its own indentation has to be part
  // of the match - otherwise the placeholder is left sitting behind whitespace,
  // stops being a line of its own, and the whole block silently disappears.
  const fences = [];
  const s = String(src || '').replace(
    /^([ \t]*)```[^\n`]*\n([\s\S]*?)^[ \t]*```[ \t]*$/gm,
    (_, pad, code) => {
      const body = pad ? code.replace(new RegExp('^' + pad, 'gm'), '') : code;
      fences.push(body.replace(/\n+$/, ''));
      return `\u0000${fences.length - 1}\u0000`;
    }
  );
  const lines = esc(s).split('\n');
  const out = [];
  let para = [];
  let list = null;

  const flushPara = () => {
    if (para.length) out.push(`<p>${mdInline(para.join(' '))}</p>`);
    para = [];
  };
  const flushList = () => {
    if (list)
      out.push(`<${list.tag}>${list.items.map((i) => `<li>${mdInline(i)}</li>`).join('')}</${list.tag}>`);
    list = null;
  };
  const flush = () => { flushPara(); flushList(); };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    let m;

    if ((m = line.match(/^\s*\u0000(\d+)\u0000\s*$/))) {
      flush();
      out.push(`<pre class="code"><code>${esc(fences[+m[1]])}</code></pre>`);
    } else if (!line.trim()) {
      flush();
    } else if (line.includes('|') && IS_RULE(lines[i + 1] || '')) {
      flush();
      const head = ROW(line);
      const rows = [];
      for (i += 2; i < lines.length && lines[i].includes('|'); i++) rows.push(ROW(lines[i]));
      i--;
      out.push(
        '<table><thead><tr>' + head.map((h) => `<th>${mdInline(h)}</th>`).join('') +
        '</tr></thead><tbody>' +
        rows.map((r) => '<tr>' + r.map((c) => `<td>${mdInline(c)}</td>`).join('') + '</tr>').join('') +
        '</tbody></table>'
      );
    } else if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      flush();
      const n = Math.min(m[1].length + 2, 6);
      out.push(`<h${n}>${mdInline(m[2])}</h${n}>`);
    } else if (/^\s*(---+|\*\*\*+|___+)\s*$/.test(line)) {
      flush();
      out.push('<hr>');
    } else if ((m = line.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/))) {
      flushPara();
      const tag = /\d/.test(m[1]) ? 'ol' : 'ul';
      if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
      list.items.push(m[2]);
    } else if ((m = line.match(/^&gt;\s?(.*)$/))) {
      flush();
      out.push(`<blockquote>${mdInline(m[1])}</blockquote>`);
    } else if (list) {
      // A wrapped continuation belongs to the bullet above it, not to a new one.
      list.items[list.items.length - 1] += ' ' + line.trim();
    } else {
      para.push(line.trim());
    }
  }
  flush();
  // A fence that ended up mid-sentence rather than on a line of its own still
  // has to come back. Losing text quietly is the one failure worth ruling out.
  return out.join('').replace(/\u0000(\d+)\u0000/g,
    (_, n) => `<code>${esc(fences[+n])}</code>`);
}

const TURN_LABEL = { text: '', thinking: 'thinking', tool: '', result: '' };

// Units spelled out rather than abbreviated: "5 min ago" is read at a glance,
// "5m ago" is read as a minute or a month depending on who is reading it.
function timeAgo(iso) {
  const t = Date.parse(iso || '');
  if (!t) return '';
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  const plural = (v, unit) => `${v} ${unit}${v === 1 ? '' : 's'} ago`;
  if (s < 10) return 'just now';
  if (s < 60) return `${s} sec ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return plural(Math.floor(s / 3600), 'hour');
  return plural(Math.floor(s / 86400), 'day');
}

// The stamp carries its own instant, so it can be re-read later without
// re-rendering the turn it belongs to.
function stamp(at) {
  if (!at) return '';
  return `<span class="at" data-at="${esc(at)}" title="${esc(new Date(at).toLocaleString())}">` +
         `${esc(timeAgo(at))}</span>`;
}

// "3m ago" stops being true a minute after it is written.
function retimeStamps() {
  document.querySelectorAll('.at[data-at]').forEach((el) => {
    el.textContent = timeAgo(el.dataset.at);
  });
}

function atBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 40;
}

function renderLog(events, into = 'l-out') {
  const showThinking = $('l-thinking').checked;
  const rows = events
    .filter((e) => showThinking || e.kind !== 'thinking')
    .map((e) => {
      const who = e.role === 'user' && e.kind !== 'result' ? 'you' : 'worker';
      if (e.kind === 'tool')
        return `<div class="turn tool"><span class="who">${esc(e.tool)}${stamp(e.at)}</span>` +
               `<div class="body">${esc(e.text)}</div></div>`;
      if (e.kind === 'result')
        return `<div class="turn result ${e.error ? 'bad' : ''}">` +
               `<span class="who">result${stamp(e.at)}</span><div class="body">${esc(e.text)}` +
               (e.clipped ? '\n<span class="muted">… clipped</span>' : '') + '</div></div>';
      // Prose is markdown and is rendered; tool calls and their output above are
      // command text, where a stray asterisk means an asterisk.
      return `<div class="turn ${e.kind === 'thinking' ? 'thinking' : who}">` +
             `<span class="who">${esc(TURN_LABEL[e.kind] || who)}${stamp(e.at)}</span>` +
             `<div class="body md">${md(e.text)}</div></div>`;
    });
  const el = $(into);
  // Follow the tail only for someone already reading it. Scrolling up to study an
  // earlier turn must not be undone two seconds later by the next poll.
  const follow = atBottom(el);
  el.innerHTML = rows.length ? rows.join('') : '<div class="empty">Nothing to show.</div>';
  if (follow) el.scrollTop = el.scrollHeight;
}

function fillSessions(lane) {
  const sel = $('l-task');
  const tasks = ((state.lanes.find((l) => l.name === lane) || {}).tasks || [])
    .filter((t) => t.task_id);
  sel.innerHTML = tasks
    .map((t) => `<option value="${esc(t.task_id)}">${esc((t.dispatched_at || '').slice(5, 16))} ` +
                `${esc(t.status || '')} — ${esc((t.title || '').replace(/\n/g, ' ').slice(0, 40))}</option>`)
    .join('');
  return tasks.length;
}

function taskStatus(lane, id) {
  const l = state.lanes.find((x) => x.name === lane);
  return (((l || {}).tasks || []).find((t) => t.task_id === id) || {}).status || '';
}

// A worker writes its transcript as it goes, so a running session can simply be
// re-read. Polling stops the moment the task leaves 'running' - a finished
// transcript never changes again.
function followLog(lane, id) {
  clearTimeout(followLog.timer);
  const live = taskStatus(lane, id) === 'running';
  $('l-live').classList.toggle('hidden', !live);
  if (!live) return;
  followLog.timer = setTimeout(() => {
    if (state.sel === lane && $('l-task').value === id) loadLog(id, { quiet: true });
  }, 2000);
}

let logSeq = 0;

async function loadLog(taskId, { quiet = false } = {}) {
  if (!state.sel) return;
  const id = taskId || $('l-task').value;
  if (!id) {
    $('l-out').innerHTML = '<div class="empty">This lane has no dispatched sessions yet.</div>';
    followLog(state.sel, id);
    return;
  }
  const sel = $('l-task');
  sel.value = id;
  if (sel.value !== id) {
    // The board only projects the last few tasks per lane, but the dock links
    // to every one of them. Without this the select silently reads blank.
    sel.insertAdjacentHTML('afterbegin', `<option value="${esc(id)}">older session</option>`);
    sel.value = id;
  }
  const seq = ++logSeq;
  if (!quiet) $('l-out').innerHTML = '<div class="empty">loading…</div>';
  const r = await api(`/api/log?lane=${encodeURIComponent(state.sel)}&task_id=${encodeURIComponent(id)}`);
  // A poll that lands after you have moved on must not repaint the pane.
  if (seq !== logSeq) return;
  if (!r.ok) {
    $('l-out').innerHTML = `<div class="empty err">${esc(r.error)}</div>`;
    return;
  }
  state.log = r.events;
  renderLog(r.events);
  followLog(state.sel, id);
}

// ---------------------------------------------------------------- orchestrator dock

function dockLine(m) {
  const at = (m.at || '').slice(11, 16);
  const lane = m.lane ? `<span class="dm-lane">${esc(m.lane)}</span>` : '';
  const open = m.task_id
    ? `<button class="dm-open" data-lane="${esc(m.lane)}" data-task="${esc(m.task_id)}">log</button>`
    : '';
  if (m.kind === 'note')
    return `<div class="dm note"><span class="dm-at">${at}</span><span class="dm-body">${esc(m.text)}</span></div>`;
  // Said once, cheaply, and then left alone. It is not news and nothing is
  // waiting on it - the report is where these get read properly.
  if (m.kind === 'idea')
    return `<div class="dm idea"><span class="dm-at">${at}</span>` +
           `<span class="dm-body">${esc(m.text)}</span></div>`;
  // A goal that stopped to ask something, with every question it asked - not
  // the first 400 characters of them joined together, which is what the note
  // this replaces could carry. There is no answer box here on purpose: this
  // feed is repainted wholesale, every two seconds while a turn is running,
  // and a textarea inside it would eat what you were typing at exactly the
  // moment you had just read the reply and were answering it. The button goes
  // to the goal, where the answer box already lives and survives.
  if (m.kind === 'question')
    return `<div class="dm ask"><span class="dm-at">${at}</span><span class="dm-body">` +
           `${lane} stopped and needs a decision` +
           (m.triaged ? '' : ' <span class="pill auto">triaging</span>') +
           `<ul class="qs">${(m.questions || []).map((q) => `<li>${esc(q)}</li>`).join('')}</ul>` +
           `</span><button class="dm-open" data-goal="${esc(m.goal_id)}" ` +
           `data-lane="${esc(m.lane)}">answer</button></div>`;
  // The briefer's side, and yours to it. Drawn as ordinary messages and not as
  // a panel: it is a conversation with someone watching the board, and the one
  // thing it must not look like is the dashboard it exists to replace. Your
  // line to it uses the same bubble as your line to the orchestrator, because
  // it is the same person talking - and the two can never appear together,
  // since each mode only ever shows one of them.
  if (m.kind === 'brief') {
    const spend = m.cost_usd ? ` $${m.cost_usd.toFixed(3)}` : '';
    return `<div class="dm brief"><span class="dm-at">${at}</span>` +
           `<span class="dm-body md">${md(m.text)}` +
           (spend ? `<span class="muted spend">${spend}</span>` : '') + `</span></div>`;
  }
  if (m.kind === 'brief-you' || m.kind === 'you')
    return `<div class="dm said"><span class="dm-at">${at}</span>` +
           `<span class="dm-body">${esc(m.text)}</span></div>`;
  // The harness asking on its own initiative. Marked, and never drawn as `you`:
  // the thread is the record of who decided what, and it is worth nothing if
  // the machine's own prompts appear over the operator's name.
  if (m.kind === 'harness')
    return `<div class="dm said harness"><span class="dm-at">${at}</span>` +
           `<span class="dm-body"><span class="who">harness</span>${esc(_firstLine(m.text))}</span></div>`;
  if (m.kind === 'amp') {
    if (m.status === 'running')
      return `<div class="dm amp working"><span class="dm-at">${at}</span>` +
             `<span class="dm-body"><span class="dot"></span> thinking…</span></div>`;
    const spend = m.cost_usd ? ` $${m.cost_usd.toFixed(3)}` : '';
    return `<div class="dm amp ${m.status === 'completed' ? '' : 'bad'}">` +
           `<span class="dm-at">${at}</span><span class="dm-body md">${md(m.text)}` +
           (spend ? `<span class="muted spend">${spend}</span>` : '') + `</span></div>`;
  }
  if (m.kind === 'dispatch')
    return `<div class="dm out"><span class="dm-at">${at}</span>` +
           `<span class="dm-body">&rarr; ${lane} ${esc(m.model || m.backend || '')}` +
           `${m.resumed ? ' <span class="muted">(reply)</span>' : ''}<br>${esc(m.text)}</span>${open}</div>`;
  // One line each way. The thread itself is a click away, so the dock stays a
  // summary of what is happening rather than a copy of it.
  const thread = m.consult_id
    ? `<button class="dm-open" data-lane="${esc(m.lane)}" data-consult="${esc(m.consult_id)}">thread</button>`
    : '';
  if (m.kind === 'escalation')
    return `<div class="dm esc"><span class="dm-at">${at}</span><span class="dm-body">` +
           `${lane} escalated to GPT-5.6` +
           `${m.trigger === 'auto' ? ' <span class="pill auto">auto</span>' : ''}` +
           `<br>${esc(m.text)}</span>${thread}</div>`;
  if (m.kind === 'ruling')
    return `<div class="dm rule"><span class="dm-at">${at}</span><span class="dm-body">` +
           `${lane} ruling <span class="muted">round ${m.round}</span>` +
           `${m.needs ? ` <span class="pill needs">needs ${m.needs}</span>` : ''}` +
           `<br>${esc(m.text)}</span>${thread}</div>`;
  const ok = m.status === 'completed' || m.status === 'succeeded';
  const cost = m.cost_usd ? ` $${m.cost_usd.toFixed(3)}` : '';
  return `<div class="dm in ${ok ? '' : 'bad'}"><span class="dm-at">${at}</span>` +
         `<span class="dm-body">${lane} <span class="pill ${esc(m.status || '')}">${esc(m.status || '')}</span>` +
         `<span class="muted">${cost}${m.num_turns ? ' · ' + m.num_turns + ' turns' : ''}</span>` +
         `<br>${esc(m.text)}</span>${open}</div>`;
}

// In briefing mode the brief stands in for everything the board DID - the
// dispatches, the results, the rulings, the asides. What survives the filter is
// what a summary cannot stand in for: the operator's own lines, the
// orchestrator answering them, a goal that has stopped and cannot go on without
// a decision, and work that is running RIGHT NOW. Summarising a question you
// are expected to answer, or hiding the reply to what you just typed, would
// make the mode unusable.
//
// A FINISHED worker run is the one case worth spelling out. It reads like it
// belongs - it is addressed to you - but it is precisely what the brief is a
// summary OF, and this board holds 54 of them: keeping them showed the thread
// and its summary at the same time, which is the noise the mode exists to drop.
// A run still going is different: no summary written a minute ago can tell you
// that something is happening now.
const DOCK_LIVE = new Set(['you', 'harness', 'question']);
const dockLive = (m) => DOCK_LIVE.has(m.kind)
  || (m.kind === 'amp' && m.status === 'running');

// The briefer's side of the conversation, and yours to it, as messages rather
// than a panel. They carry their own `at`, so they merge into the thread by time
// like everything else and read as one exchange instead of a summary sitting on
// top of a list.
function briefTurns(b) {
  if (!b || !b.turns) return [];
  return b.turns.map((t) => ({
    kind: t.role === 'you' ? 'brief-you' : 'brief',
    at: t.at, text: t.text, cost_usd: t.cost_usd,
  }));
}

async function loadChat() {
  const brief = state.dock === 'brief';
  // Asked for in the same call as the messages, not a second one: what the
  // briefer is told has to be the thread being drawn beside it, and two walks
  // of the board can disagree by whatever landed between them.
  const r = await api(`/api/chat${brief ? '?brief=1' : ''}`);
  const feed = $('dock-feed');
  const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
  const msgs = brief
    ? [...r.messages.filter(dockLive), ...briefTurns(r.brief)]
        .sort((a, b2) => (a.at || '').localeCompare(b2.at || ''))
    : r.messages;
  const err = brief && r.brief && r.brief.error
    ? `<div class="dm brief bad"><span class="dm-body">${esc(r.brief.error)}</span></div>`
    : '';
  // Drawn at the bottom rather than as a message, because it is not one yet:
  // it is the other side typing, and it belongs where the next line will be.
  const typing = brief && r.brief && r.brief.working
    ? '<div class="dm brief working"><span class="dm-body"><span class="dot"></span>' +
      ' typing…</span></div>'
    : '';
  feed.innerHTML = (
    msgs.length
      ? msgs.map(dockLine).join('')
      : brief
      ? '<div class="empty">Nothing to report yet. Say something and I&rsquo;ll ' +
        'answer from the thread.</div>'
      : '<div class="empty">Ask the orchestrator anything about this workspace, ' +
        'or tell it what you want done.</div>') + err + typing;
  // A turn takes as long as it takes. Keep looking until it lands, then stop:
  // the rest of the feed is derived from the board and refreshes with it. The
  // briefer mid-sentence is the same kind of wait, and needs the same poll.
  clearTimeout(loadChat.timer);
  if (r.messages.some((m) => m.kind === 'amp' && m.status === 'running')
      || (r.brief && r.brief.working))
    loadChat.timer = setTimeout(loadChat, 2000);
  feed.querySelectorAll('.dm-open').forEach((b) =>
    b.addEventListener('click', () =>
      b.dataset.consult ? openConsult(b.dataset.lane, b.dataset.consult)
        : b.dataset.goal ? openGoal(b.dataset.lane, b.dataset.goal)
        : openWorkerLog(b.dataset.lane, b.dataset.task)
    )
  );
  if (atBottom) feed.scrollTop = feed.scrollHeight;
}

// The whole point of the thread: a one-line summary that opens the real thing.
function openWorkerLog(lane, taskId) {
  if (lane !== state.sel) select(lane);
  showTab('log');
  fillSessions(lane);
  loadLog(taskId);
}

// The whole board in one line, above the thread that runs it. Anything worse
// than "idle" is named here so nothing needing you is only visible by clicking.
function renderSummary(s) {
  const bits = [];
  // The cap only earns emphasis once you are at it: that is the moment the next
  // dispatch gets refused, and the only moment the number matters.
  if (s.running)
    bits.push(s.running >= s.cap
      ? `<span class="warnt"><b>${s.running}</b> running - at the cap of ${s.cap}</span>`
      : `<b>${s.running}</b> of ${s.cap} running`);
  // Anything waiting for a slot has to be visible, or it is indistinguishable
  // from having been dropped - which is exactly what used to happen to it.
  const q = s.queued || [];
  if (q.length)
    bits.push(`<span class="warnt">${q.length} queued: ` +
              `${esc(q.map((x) => x.lane).join(', '))}</span>`);
  if (s.failed) bits.push(`<span class="err">${s.failed} failed</span>`);
  if (s.goals)
    bits.push(`${s.goals} goal${s.goals === 1 ? '' : 's'}` +
      (s.goals_stuck ? ` <span class="err">(${s.goals_stuck} stopped)</span>` : ''));
  if (s.threads) bits.push(`${s.threads} thread${s.threads === 1 ? '' : 's'}`);
  if (s.waiting)
    bits.push(`<span class="warnt">${s.waiting} waiting on you: ${esc((s.lanes || []).join(', '))}</span>`);
  // Last, because it is not about the board's health - but never omitted, and a
  // contradiction says so in the word rather than the count.
  if (s.findings)
    bits.push(s.contradicted
      ? `<span class="err">${s.contradicted} contradicted</span>`
      : `<span class="warnt">${s.findings} finding${s.findings === 1 ? '' : 's'}</span>`);
  $('dock-sub').innerHTML = bits.length ? bits.join(' · ') : 'all quiet';
}

// ---------------------------------------------------------- notifications
//
// The three lists below used to stack under the header and push the board off
// the screen. They are the same three lists, kept apart for the same reasons,
// now behind one button. The button carries a count because a strip that is
// always visible is one you stop reading, and a panel you have to remember to
// open is one you never do - the count is the part that has to stay in sight.
//
// Nothing here decides what is in the lists. Each renderer records its own
// total on its container, because each applies a different filter - only the
// failing obligations, only the watch items you have not dismissed - and a
// second copy of those filters here is a second thing to keep in step. Counting
// the rows in the DOM would be wrong for the same reason: "3 shown, 60 more"
// and the settle button are rows too.

function syncNotify() {
  const secs = [$('findings'), $('obligations'), $('watch')];
  const rows = secs.reduce((n, el) => n + Number(el.dataset.n || 0), 0);
  // A contradiction is the one kind that is not a queue, so it colours the
  // count rather than just adding to it.
  const bad = (state.findings || []).some((f) => f.bearing === 'contradicted');
  const badge = $('notify-count');
  badge.textContent = rows ? String(rows) : '';
  badge.classList.toggle('on', !!rows);
  badge.classList.toggle('bad', !!rows && bad);
  $('btn-notify').classList.toggle('lit', !!rows);
  $('notify-empty').classList.toggle('hidden', !!rows);
  $('notify-sub').textContent = rows ? '' : 'nothing needs you';
}

function toggleNotify(open) {
  const el = $('notify');
  const on = open === undefined ? el.classList.contains('hidden') : open;
  // Measured, not guessed. The header wraps at narrow widths, and the panel is
  // fixed over the right of the window - so a top of 0 puts it on top of the
  // button that opened it, which then cannot be clicked again to close it.
  if (on) el.style.top = document.querySelector('header').offsetHeight + 'px';
  el.classList.toggle('hidden', !on);
}

// ---------------------------------------------------------------- findings
//
// What the work has said about the doctrine, and nobody has told you yet.
// Everything else on this page is about whether the machine is running; this is
// the only part about whether the machine is learning anything, which is the
// point of running it. So it sits above the rest and does not auto-clear:
// "noted" is you saying you have read it, and nothing else can say that for you.

const BEARING_WORD = {
  contradicted: 'contradicts',
  advanced: 'advances',
  proposed: 'proposes',
};

function renderFindings(list) {
  const el = $('findings');
  const all = list || [];
  el.dataset.n = all.length;
  el.classList.toggle('hidden', !all.length);
  syncNotify();
  if (!all.length) return;
  const shown = state.findingsAll ? all : all.slice(0, 3);
  const hidden = all.length - shown.length;
  el.innerHTML = shown
    .map((f) =>
      `<div class="fi ${esc(f.bearing)}">` +
      `<span class="fi-tag">${esc(BEARING_WORD[f.bearing] || f.bearing)}</span>` +
      (f.lane ? `<b>${esc(f.lane)}</b> ` : '') +
      `${esc(f.text)}` +
      `<span class="muted"> — ${esc(f.source)}</span>` +
      `<button class="fi-x" data-id="${esc(f.id)}" title="You have read this">noted</button>` +
      `</div>`)
    .join('') +
    (hidden > 0 || state.findingsAll
      ? `<div class="fi more" id="fi-more">${state.findingsAll ? 'show less' : `${hidden} more`}</div>`
      : '') +
    (all.filter((f) => f.bearing === 'contradicted').length > 1
      ? `<div class="fi-settle"><button id="fi-solve" title="One architect turn: work out what each ` +
        `contradiction implies and perform it — take back a rung that was not earned, match one to a ` +
        `later finding that already answered it, or file a proposal. Anything it cannot perform stays ` +
        `open.">Settle contradictions</button>` +
        // Re-rendered from state, not left in the DOM: settling ends by reloading
        // the findings, which rebuilds this whole strip. Holding the outcome in a
        // node here would destroy the one thing the operator needs to read.
        `<span id="fi-solve-out" class="${esc((state.settle || {}).cls || 'muted')}">` +
        `${esc((state.settle || {}).msg || '')}</span></div>`
      : '');
  const more = $('fi-more');
  if (more) more.onclick = () => { state.findingsAll = !state.findingsAll; renderFindings(all); };
  const solve = $('fi-solve');
  if (solve) solve.onclick = () => settleFindings(solve);
  el.querySelectorAll('.fi-x').forEach((b) =>
    b.addEventListener('click', async () => {
      await post('/api/findings/ack', { ids: [b.dataset.id] });
      loadFindings();
    })
  );
}

async function settleFindings(btn) {
  const out = $('fi-solve-out');
  btn.disabled = true;
  btn.textContent = 'settling…';
  out.textContent = 'reading each one against the record';
  const r = await post('/api/findings/settle', {});
  btn.disabled = false;
  btn.textContent = 'Settle contradictions';
  if (!r.ok) {
    state.settle = { msg: r.error || 'failed', cls: 'err' };
  } else {
    const n = (r.settled || []).length;
    const kept = (r.kept || []).length;
    // The kept ones are the point of saying anything here: they are what the
    // architect declined to close, and they are now the whole of what is
    // holding the gate. Naming the count is how the operator knows what is
    // left to read - and "0 settled" is the most useful answer of all, because
    // it means nothing could be honestly closed.
    state.settle = {
      cls: n ? 'good' : 'muted',
      msg:
        `${n} settled` +
        ((r.retractions || []).length ? `, ${r.retractions.length} rung(s) taken back` : '') +
        // Named separately from a retraction, because it is a different outcome:
        // a retraction moves a lane back down the ladder, a correction moves
        // nothing. Folding the two together would report the ladder as having
        // changed when it has not.
        ((r.corrections || []).length
          ? `, ${r.corrections.length} claim(s) corrected without moving a rung` : '') +
        ((r.proposals || []).length ? `, ${r.proposals.length} proposal(s) filed` : '') +
        (kept ? ` — ${kept} left for you to read` : ''),
    };
  }
  out.textContent = state.settle.msg;
  out.className = state.settle.cls;
  loadFindings();
  loadChat();
}

async function loadFindings() {
  try {
    const r = await api('/api/findings?unread=1');
    state.findings = r.findings || [];
  } catch {
    state.findings = [];
  }
  renderFindings(state.findings);
}

// ------------------------------------------------------------ obligations
//
// Things that have to keep being true. Rendered apart from findings on purpose:
// a finding is news about what we believe, an obligation is a chore the harness
// noticed - and mixing them would let a stale docs build sit at the same weight
// as a contradiction.

function renderObligations(list) {
  const el = $('obligations');
  const all = list || [];
  // Only the ones that need something. An obligation quietly holding is the
  // normal case and does not deserve a line on the board.
  const bad = all.filter((o) => o.state === 'drifted' || o.state === 'broken');
  el.dataset.n = bad.length;
  el.classList.toggle('hidden', !bad.length);
  syncNotify();
  if (!bad.length) return;
  el.innerHTML = bad
    .map((o) =>
      `<div class="ob ${esc(o.state)}">` +
      `<span class="ob-tag">${o.state === 'broken' ? 'check broken' : 'out of date'}</span>` +
      `<b>${esc(o.name)}</b> ` +
      // The button goes here rather than at the right edge on purpose: the
      // orchestrator dock is fixed over the right 460px of the window and sits
      // on top of these strips, so a control flushed right is drawn but cannot
      // be clicked. Verified by asking the page what is actually at the button's
      // centre point - it answered `dm-body`, an element of the dock.
      `<button class="ob-x" data-id="${esc(o.id)}" title="Run the check again now">recheck</button>` +
      (o.state === 'broken'
        ? `<span class="muted">its check could not run, so nothing is known yet</span>`
        : `<span class="muted">${esc(o.fix || 'no fix recorded')}</span>`) +
      `</div>`)
    .join('');
  el.querySelectorAll('.ob-x').forEach((b) =>
    b.addEventListener('click', async () => {
      b.disabled = true;
      b.textContent = 'checking';
      await post('/api/obligation/check', { id: b.dataset.id });
      loadObligations();
    })
  );
}

async function loadObligations() {
  try {
    const r = await api('/api/obligations');
    state.obligations = r.obligations || [];
  } catch {
    state.obligations = [];
  }
  renderObligations(state.obligations);
}

// ---------------------------------------------------------------- watch
//
// What the harness can see is wrong, said once, above everything. These are
// facts off the board and the worktrees, not a model's opinion, so they are
// stated flatly and each one names what would fix it.

function renderWatch(list) {
  const el = $('watch');
  const all = (list || []).filter((o) => !state.dismissed.has(o.kind + '|' + (o.lane || '')));
  el.dataset.n = all.length;
  el.classList.toggle('hidden', !all.length);
  syncNotify();
  if (!all.length) return;
  // A banner that fills the window is a banner you scroll past. Only what is
  // actually urgent stays up; the rest is one line you can open.
  const urgent = all.filter((o) => o.severity === 'high');
  const shown = state.watchAll ? all : urgent.slice(0, 3);
  const hidden = all.length - shown.length;
  el.innerHTML = shown
    .map((o, i) =>
      `<div class="ob ${esc(o.severity)}" data-i="${i}">` +
      `<b>${esc(o.lane || '')}</b> ${esc(o.text)}` +
      (o.fix ? ` <span class="muted">&rarr; ${esc(o.fix)}</span>` : '') +
      (o.resume ? ` <button class="ob-go" data-i="${i}">resume it</button>` : '') +
      (o.ratify ? ` <button class="ob-go ob-ratify">it was me</button>` : '') +
      `<button class="ob-x" data-key="${esc(o.kind + '|' + (o.lane || ''))}" title="Hide until it changes">&times;</button>` +
      `</div>`)
    .join('') +
    (hidden > 0 || state.watchAll
      ? `<div class="ob more" id="ob-more">${state.watchAll ? 'show less' : `${hidden} more worth knowing about`}</div>`
      : '');
  const more = $('ob-more');
  if (more) more.onclick = () => { state.watchAll = !state.watchAll; renderWatch(list); };
  el.querySelectorAll('.ob-x').forEach((b) =>
    b.addEventListener('click', () => {
      state.dismissed.add(b.dataset.key);
      renderWatch(state.observations);
    })
  );
  el.querySelectorAll('.ob-go').forEach((b) =>
    b.addEventListener('click', async (e) => {
      e.stopPropagation();
      // Ratifying is the operator saying the doctrine as it stands is theirs.
      // Nothing else in this console can say it, and no agent can reach it.
      if (b.classList.contains('ob-ratify')) {
        await post('/api/doctrine/ratify', {});
        return refresh();
      }
      armResume(shown[Number(b.dataset.i)]);
    })
  );
  el.querySelectorAll('.ob').forEach((n) =>
    n.addEventListener('click', (e) => {
      if (e.target.classList.contains('ob-x')) return;
      if (e.target.classList.contains('ob-go')) return;
      const o = shown[Number(n.dataset.i)];
      if (o.consult_id) openConsult(o.lane, o.consult_id);
      else if (o.goal_id) { if (o.lane) select(o.lane); showTab('goals'); }
      else if (o.lane) select(o.lane);
    })
  );
}

// Arming a session, rather than sending anything. The instruction below is a
// suggestion in a box the operator can still edit, because "carry on" to a
// worker that was cut off mid-edit is the one message worth reading first.
function armResume(o) {
  if (!o || !o.resume || !o.lane) return;
  state.resumeOf = { lane: o.lane, session: o.resume, kind: o.kind };
  select(o.lane);
  showTab('dispatch');
  if (!$('d-prompt').value.trim()) {
    $('d-prompt').value =
      'You were stopped by a limit part way through, not because anything was wrong. ' +
      'Everything you wrote to disk is still here.\n\n' +
      'Do not start anything new. Take stock of what is in this worktree now, ' +
      'get it into a state that builds and passes, commit it on this branch with a ' +
      'message saying what it does, and then tell me in a few lines what is done and ' +
      'what is left. If you are running short of time again, commit and report early.';
  }
  renderResumeChip();
}

function renderResumeChip() {
  const el = $('d-resume');
  if (!el) return;
  const r = state.resumeOf;
  const on = !!(r && r.lane === state.sel);
  el.classList.toggle('hidden', !on);
  if (!on) return;
  el.innerHTML = `continuing session <b>${esc(r.session.slice(0, 8))}</b> in ${esc(r.lane)} ` +
    `&mdash; press <b>Reply to last</b> to send <button class="ob-x" id="d-resume-x">&times;</button>`;
  $('d-resume-x').onclick = () => { state.resumeOf = null; renderResumeChip(); };
}

// ---------------------------------------------------------------- goals

function goalBar(g) {
  const pct = g.dod ? Math.round((g.met / g.dod) * 100) : 0;
  return `<span class="bar"><i style="width:${pct}%"></i></span>`;
}

function renderGoals() {
  const el = $('g-list');
  $('g-lane').textContent = state.sel || '—';
  const mine = (state.goals || []).filter((g) => g.lane === state.sel);
  if (!mine.length) {
    // Through the same cache as every other paint. Writing here and leaving
    // `_html` describing the last lane that HAD goals is what made those goals
    // fail to come back: returning to that lane computed html equal to a
    // signature for a pane that had since been painted empty, the write below
    // was skipped as a no-op, and the empty message stayed on screen.
    const empty = '<div class="empty">No goals in this lane. A goal is an objective with a ' +
      'definition of done — the architect plans it, workers carry it out one task at a time, ' +
      'and it runs itself until it is finished or it needs you.</div>';
    if (el._html !== empty) {
      el._html = empty;
      el.innerHTML = empty;
    }
    return;
  }
  // This runs on every poll tick, so a blind rebuild is destructive twice over.
  // It throws away the body loadGoal filled in and leaves the word "loading"
  // sitting there for good, and it replaces the very node the pointer is on -
  // click a goal at the wrong moment and the click lands on a detached element
  // and is simply lost. Carrying the body across the rebuild fixes the first;
  // re-fetching it below fixes the second by making the rebuild idempotent.
  //
  // Not while someone is typing in it, though. An answer box that empties
  // itself every few seconds is worse than one showing slightly stale counts.
  const prev = state.goal ? $('gb-' + state.goal) : null;
  const carried = prev ? prev.innerHTML : '';
  const typing = prev ? prev.contains(document.activeElement) : false;
  // And not while a publish confirmation is on screen. That panel exists to be
  // read before it is acted on, so a refresh that quietly replaced it would be
  // asking for approval and then swapping out what was approved.
  const confirming = prev ? !!prev.querySelector('.gpub:not(:empty)') : false;
  if (typing || confirming) return;

  const html = mine.map((g) => {
    const open = state.goal === g.id;
    return `<div class="goal ${esc(g.state)} ${open ? 'open' : ''}" data-id="${esc(g.id)}">` +
      // The first paragraph only. An objective is one sentence saying what has
      // to be true, followed by however many paragraphs of grounding the
      // architect needed - and putting all of it in a collapsed row is what
      // makes the list read as a wall. The rest is shown in the body, where
      // there is room to render it properly.
      `<div class="goal-head"><b>${mdi(g.objective.split(/\n\s*\n/)[0])}</b>` +
      `<span class="tag ${esc(g.state)}">${esc(g.state)}</span></div>` +
      `<div class="goal-sub">${goalBar(g)} ${g.met} of ${g.dod} done-conditions met · ` +
      `${g.tasks_done} of ${g.tasks_total} tasks · ${g.rounds} rounds · ` +
      `${(g.cost_tokens / 1000).toFixed(0)}k tokens` +
      (g.now ? ` · <span class="warnt">now: ${esc(g.now)}</span>` : '') +
      (g.stopped_why ? ` · <span class="err">stopped: ${esc(g.stopped_why)}</span>` : '') +
      `</div>` +
      // Deliberately empty: the body is filled in by loadGoal and restored
      // below. Leaving its contents out of the string is what lets the
      // signature compare equal on a tick where only the body has changed.
      (open ? `<div class="goal-body" id="gb-${esc(g.id)}"></div>` : '') +
      `</div>`;
  }).join('');

  // The write is the whole problem. Assigning innerHTML replaces every node in
  // this list, so a control inside it stops existing between the mousedown and
  // the mouseup and the click is never delivered - which is why the publish
  // button could be pressed and do nothing. Most ticks change nothing at all,
  // so compare first and make an unchanged tick a genuine no-op rather than a
  // rebuild that happens to look the same.
  if (el._html !== html) {
    el._html = html;
    el.innerHTML = html;
    const body = state.goal ? $('gb-' + state.goal) : null;
    if (body) body.innerHTML = carried || 'loading…';
    el.querySelectorAll('.goal').forEach((n) =>
      n.addEventListener('click', (e) => {
        if (e.target.closest('.goal-body')) return;
        state.goal = state.goal === n.dataset.id ? null : n.dataset.id;
        renderGoals();
      })
    );
  }
  // The one call site. Every path into this function - a click, a poll tick, a
  // tab switch - now refreshes the open goal instead of only the click path,
  // which is why the body no longer gets stranded on "loading".
  if (state.goal && mine.some((g) => g.id === state.goal)) loadGoal(state.goal);
}

async function loadGoal(gid) {
  const r = await api('/api/goal?id=' + encodeURIComponent(gid));
  const el = $('gb-' + gid);
  if (!el || !r.ok) return;
  const g = r.goal;
  // A check that was run is the only thing on this page that is not somebody's
  // judgement, so it is shown as what it is: the command, and its exit code.
  const dod = (g.done || []).map((d) =>
    `<div class="cond ${d.met ? 'met' : ''}">` +
    `<span class="box">${d.met ? '✓' : '·'}</span><span>${mdi(d.text)}` +
    // The command stays raw on purpose. It is the one thing here that is not
    // somebody's prose, and rendering backticks inside it would change the
    // characters you would have to type to run it yourself.
    (d.check
      ? `<div class="check">${esc(d.check)}` +
        (d.check_exit === 0 ? ' <span class="ok">passed</span>'
          : d.check_exit == null ? ' <span class="muted">not run yet</span>'
            : ` <span class="err">exit ${esc(d.check_exit)}</span>`) + '</div>'
      : '') +
    (d.evidence ? `<div class="ev">${mdi(d.evidence)}</div>` : '') +
    '</span></div>').join('');
  const tasks = (g.tasks || []).map((t) =>
    `<div class="gtask ${esc(t.state)}"><span class="tag ${esc(t.state)}">${esc(t.state)}</span>` +
    `<span>${mdi(t.text)}${t.note ? ` <span class="err">${mdi(t.note)}</span>` : ''}</span></div>`).join('');
  const log = (g.log || []).slice(-12).reverse()
    .map((l) => `<div class="gl"><span class="muted">${esc((l.at || '').slice(11, 16))}</span> ${mdi(l.text)}</div>`).join('');
  const qs = (g.questions || []).length
    ? `<div class="gq"><b>It needs a decision from you</b>` +
      (g.questions || []).map((q) => `<div>${esc(q)}</div>`).join('') +
      `<div class="row"><input id="ga-text" placeholder="Answer it, and it carries on"><button id="ga-go" class="primary">Answer</button></div>` +
      // The other way out of this card. The harness triages a stopped goal on
      // its own, but only when the doctrine or a recorded decision has moved
      // since it last looked - so a question can sit here, already read once,
      // with nothing due to make it read again. This is that: read it again
      // now, against the mission, the doctrine and every decision on record.
      // It settles what the record settles and hands back what is genuinely
      // his, which is the same discipline the heartbeat runs under.
      `<div class="row"><button id="gt-go" title="Have the orchestrator try to settle this from the mission, the doctrine and what you have already decided. Anything only you can answer comes back here.">Try to settle it</button>` +
      `<span id="gt-out" class="muted"></span></div></div>`
    : '';
  // Everything after the first paragraph of the objective: why this goal exists,
  // which file the gap was read out of, what the architect was told not to do.
  // The list row shows the sentence; this is where the case for it lives.
  const [, ...rest] = String(g.objective || '').split(/\n\s*\n/);
  // The architect habitually opens this paragraph with "Why:", so leaving it in
  // under a heading that says Why reads as a stutter. Strip only that exact
  // lead-in, and only when it is the first thing on the line.
  const body = rest.join('\n\n').replace(/^\s*Why:\s*/i, '');
  const why = body.trim() ? `<h4>Why</h4><div class="gwhy md">${md(body)}</div>` : '';
  const html =
    qs + why +
    '<h4>Definition of done</h4>' + (dod || '<div class="empty">none written</div>') +
    '<h4>Tasks</h4>' + (tasks || '<div class="empty">none</div>') +
    '<h4>What it has been doing</h4>' + (log || '<div class="empty">nothing yet</div>') +
    `<div class="row goal-acts">` +
    // Only offered for the two stops it can actually answer. It used to appear
    // on every blocked goal and zero the round count regardless of what the
    // goal had stopped on, which made "carry on anyway" true of stops it had no
    // remedy for.
    (g.state === 'blocked' && ['rounds', 'tokens'].includes(g.stopped_on)
      ? `<button id="gp-go" title="Grant it more of the budget it ran out of. The spend already recorded stands.">Extend budget</button>` : '') +
    // A goal that is still live can be re-planned against the repo as it is now,
    // or aimed somewhere else. A done goal cannot: its evidence is what it is.
    (g.state !== 'done'
      ? `<button id="grec-go" title="Re-plan against the repo as it is now. Conditions already met keep their evidence.">Recalculate</button>` +
        `<button id="gimp-go" title="Ask whether this goal is still aimed at the right thing. Answers first; applies only if you ask again.">Improve objective</button>`
      : '') +
    (g.pr_url
      ? `<a class="prlink" href="${esc(g.pr_url)}" target="_blank" rel="noreferrer">pull request open &rarr;</a>`
      : g.state === 'done' ? `<button id="gpub-go">Open pull request</button>` : '') +
    `<button id="gc-go" class="danger">Abandon</button></div>` +
    `<div id="greopen-out" class="gpub"></div>` +
    `<div id="gpub-out" class="gpub"></div>`;
  // Same reason as the list above, and it matters more here: this is where the
  // buttons are. A goal that is sitting still renders identically every tick,
  // and rewriting it anyway is what made those buttons unclickable.
  if (el._html === html) return;
  el._html = html;
  el.innerHTML = html;
  const ga = $('ga-go');
  if (ga) ga.onclick = async () => {
    const t = $('ga-text').value.trim();
    if (!t) return;
    ga.disabled = true; ga.textContent = 'thinking…';
    await post('/api/goal/answer', { goal_id: gid, text: t });
    await refresh(); loadGoal(gid);
  };
  const gt = $('gt-go');
  if (gt) gt.onclick = async () => {
    gt.disabled = true; gt.textContent = 'reading the record…';
    const r = await post('/api/goal/triage', { goal_id: gid });
    if (!r.ok) {
      // Left enabled on a refusal: "the orchestrator is mid-turn" is a reason to
      // press it again in a minute, not a reason to lose the button.
      gt.disabled = false; gt.textContent = 'Try to settle it';
      const o = $('gt-out'); if (o) o.textContent = r.error || 'it would not go';
      return;
    }
    const o = $('gt-out');
    if (o) o.textContent = 'the orchestrator is on it — watch the thread';
    await refresh();
  };
  const gp = $('gp-go');
  if (gp) gp.onclick = async () => {
    gp.disabled = true; gp.textContent = 'extending…';
    await post('/api/goal/push', { goal_id: gid });
    await refresh(); loadGoal(gid);
  };
  // Two clicks, always. The first asks what would happen and re-runs the
  // checks; the second is the one that leaves this machine. They are not
  // collapsed into one even when everything passes, because the whole point of
  // showing the evidence is that somebody reads it before it is published.
  const pub = $('gpub-go');
  if (pub) pub.onclick = async () => {
    const out = $('gpub-out');
    if (pub.dataset.armed) {
      pub.disabled = true; pub.textContent = 'pushing…';
      const r = await post('/api/goal/publish', { goal_id: gid, confirm: true });
      const rep = r.report || {};
      out.innerHTML = rep.pr_url
        ? `<span class="good">opened</span> <a class="prlink" href="${esc(rep.pr_url)}" target="_blank" rel="noreferrer">${esc(rep.pr_url)}</a>`
        : `<span class="err">${esc((rep.blocked || ['failed']).join(' · '))}</span>`;
      await refresh();
      return;
    }
    // Marked busy before the request, not after it comes back: re-running the
    // done-conditions takes as long as the project's test suite, and until this
    // div is non-empty the poll loop is still free to rebuild the body.
    out.innerHTML = '<span class="muted">re-running every done-condition…</span>';
    pub.disabled = true; pub.textContent = 'checking…';
    const r = await post('/api/goal/publish/report', { goal_id: gid });
    const rep = r.report || {};
    pub.disabled = false;
    if (!rep.ok) {
      pub.textContent = 'Open pull request';
      out.innerHTML = `<b class="err">not publishable</b>` +
        (rep.blocked || []).map((b) => `<div>${mdi(b)}</div>`).join('');
      return;
    }
    pub.textContent = 'Push and open it';
    pub.dataset.armed = '1';
    pub.classList.add('primary');
    out.innerHTML =
      `<b>${esc(rep.ahead)} commit(s)</b> from <code>${esc(rep.branch)}</code> into ` +
      `<code>${esc(rep.base)}</code> of <code>${esc(rep.repo || '?')}</code>` +
      `<div class="muted">${esc(rep.conditions.length)} done-conditions, all re-checked just now:</div>` +
      rep.conditions.map((c) => `<div class="gl">✓ ${mdi(c.text)}</div>`).join('') +
      rep.commits.map((c) => `<div class="gl muted">${esc(c)}</div>`).join('') +
      `<div class="muted">Nothing merges. This opens a pull request and stops.</div>`;
  };
  // Re-planning a goal that is already running. One click, because it does not
  // change what the goal is for: the objective is fixed, met conditions keep the
  // evidence that met them, and what changes is the route.
  const rec = $('grec-go');
  if (rec) rec.onclick = async () => {
    const out = $('greopen-out');
    out.innerHTML = '<span class="muted">re-checking every condition, then re-planning…</span>';
    rec.disabled = true; rec.textContent = 'recalculating…';
    const r = await post('/api/goal/reopen', { goal_id: gid, action: 'recalculate' });
    rec.disabled = false; rec.textContent = 'Recalculate';
    if (!r.ok) {
      out.innerHTML = `<span class="err">${esc(r.error || 'failed')}</span>`;
      return;
    }
    out.innerHTML =
      `<b class="good">re-planned</b> <span class="muted">${esc(r.kept_done)} met condition(s) kept their evidence` +
      ((r.dropped_tasks || []).length ? `, ${(r.dropped_tasks || []).length} task(s) dropped` : '') + '</span>' +
      // Evidence that quietly stops counting is the one outcome worth shouting
      // about, so it is named here rather than left to the log.
      ((r.lost_met || []).length
        ? `<div class="err">no longer part of the definition, though it was met: ` +
          r.lost_met.map((t) => esc(t)).join('; ') + '</div>' : '');
    await refresh(); loadGoal(gid);
  };
  // Two clicks. The first asks what it would change and shows the answer; the
  // second is the one that changes what this goal is for. Re-planning follows
  // automatically, because a new objective with the old plan under it is the
  // one state this must not leave behind.
  const imp = $('gimp-go');
  if (imp) imp.onclick = async () => {
    const out = $('greopen-out');
    if (imp.dataset.armed) {
      imp.disabled = true; imp.textContent = 'applying…';
      const r = await post('/api/goal/reopen', { goal_id: gid, action: 'improve', apply: true });
      imp.disabled = false;
      out.innerHTML = r.ok
        ? '<span class="good">objective replaced, and the plan re-run under it</span>'
        : `<span class="err">${esc(r.error || 'failed')}</span>`;
      await refresh(); loadGoal(gid);
      return;
    }
    out.innerHTML = '<span class="muted">asking whether this is still the right thing to be aiming at…</span>';
    imp.disabled = true; imp.textContent = 'asking…';
    const r = await post('/api/goal/reopen', { goal_id: gid, action: 'improve' });
    imp.disabled = false;
    if (!r.ok) {
      imp.textContent = 'Improve objective';
      out.innerHTML = `<span class="err">${esc(r.error || 'failed')}</span>`;
      return;
    }
    if (!r.objective) {
      // "This is already aimed correctly" is a real answer, not a failure, and
      // arming the button after it would invite a change nobody asked for.
      imp.textContent = 'Improve objective';
      out.innerHTML = `<b>leave it as it is</b><div class="muted">${esc(r.assessment || r.why || '')}</div>`;
      return;
    }
    imp.textContent = 'Use this objective';
    imp.dataset.armed = '1';
    imp.classList.add('primary');
    out.innerHTML =
      `<b>proposed objective</b><div class="gwhy md">${md(r.objective)}</div>` +
      `<div class="muted">${esc(r.what_changed || '')}</div>` +
      `<div class="muted">${esc(r.why || '')}</div>` +
      (r.keeps_the_point ? `<div class="muted">still the point: ${esc(r.keeps_the_point)}</div>` : '') +
      `<div class="muted">Applying this also re-plans the goal under it.</div>`;
  };
  $('gc-go').onclick = async () => {
    if (!confirm('Abandon this goal? Work already done stays in the worktree.')) return;
    await post('/api/goal/close', { goal_id: gid });
    state.goal = null;
    refresh();
  };
}

function wireGoals() {
  $('btn-g-new').onclick = () => {
    $('g-new').classList.toggle('hidden');
    $('g-objective').focus();
  };
  $('g-cancel').onclick = () => $('g-new').classList.add('hidden');
  $('g-go').onclick = async () => {
    const objective = $('g-objective').value.trim();
    if (!objective || !state.sel) return;
    $('g-go').disabled = true;
    $('g-status').textContent = 'planning…';
    const r = await post('/api/goal/open', { lane: state.sel, objective });
    $('g-go').disabled = false;
    if (!r.ok) { $('g-status').innerHTML = `<span class="err">${esc(r.error)}</span>`; return; }
    $('g-status').textContent = 'planned';
    $('g-objective').value = '';
    $('g-new').classList.add('hidden');
    state.goal = r.goal.id;
    await refresh();
    loadGoal(r.goal.id);
  };
}

// ------------------------------------------------------------- direction
//
// Everything else in this console is about the work. This is about what the work
// is for, and it is deliberately the only tab that reads from the doctrine file
// itself rather than restating it: a second copy of the values is a second thing
// that has to be kept true.
//
// Four sections, in this order for a reason. The bet first, because everything
// under it is only worth doing if the bet is. Then what came back - findings
// outrank proposals, since a contradiction changes which direction is even
// worth travelling. Then what we still do not know. Proposals last: they are the
// cheapest thing here to produce and the only one that spends anything.

const pcs = (v) => (v === null || v === undefined ? '—' : Math.round(v * 100) + '%');

/** One score as a bar you can read without arithmetic, with its threshold marked
 *  on the same scale, so comparing them is a look rather than a calculation.
 *  `bar` null draws no tick: not every one of these is a threshold to clear. */
function meter(label, v, bar, cls, why) {
  const t = why ? ` title="${esc(why)}"` : '';
  if (v === null || v === undefined)
    return `<div class="odds none"${t}><span class="sl">${label}</span>` +
      `<span class="on">unscored</span></div>`;
  const pc = Math.round(v * 100);
  const tone = cls || (bar === null || v >= bar ? 'good' : 'low');
  return `<div class="odds ${tone}"${t}><span class="sl">${label}</span>` +
    `<span class="ometer"><i style="width:${pc}%"></i>` +
    (bar === null ? '' : `<u style="left:${Math.round(bar * 100)}%" title="the bar"></u>`) +
    `</span><span class="on">${pc}%</span></div>`;
}

/** All four scores on one proposal, plus what they rank it at.
 *  Four rather than one because they answer different questions and can point
 *  opposite ways: the thing most likely to land is routinely the thing the
 *  mission needs least, and a single number hides exactly that trade. */
function scores(p, d) {
  const bar = typeof d.bar === 'number' ? d.bar : 0.6;
  const nb = typeof d.need_bar === 'number' ? d.need_bar : 0.5;
  const floor = typeof d.sharpen_floor === 'number' ? d.sharpen_floor : 0.15;
  const c = p.cost_usd;
  return `<div class="scores">` +
    meter('will finish', p.confidence, bar, null,
      'the odds a worker fleet finishes this without stopping to ask you for something') +
    meter('mission wants it', p.need, nb, null, p.why_need) +
    `<div class="odds flat"><span class="sl">costs about</span>` +
    `<span class="on">${c === null || c === undefined ? 'uncosted' : '$' + c.toFixed(2)}</span>` +
    (p.worth === null || p.worth === undefined ? ''
      : `<span class="on rank" title="odds × need ÷ cost — how it ranks against the ` +
        `others waiting, never a threshold">ranks ${p.worth}</span>`) + `</div>` +
    meter('room to improve', p.headroom, floor, 'hr', p.why_headroom) +
    `</div>`;
}

/** The bars, and what the scores behind them have so far turned out to be worth.
 *  Shown together on purpose: a threshold on its own is a number asking to be
 *  trusted, and until something has actually settled there is no reason to. Each
 *  score is reported against its own recorded event and they are never averaged
 *  together - being over-confident and under-costed at once would cancel out. */
function calBlock(c, bar) {
  const head = `starts on its own at <b>${Math.round(bar * 100)}%</b> odds and up`;
  if (!c) return `<div class="cal">${head}</div>`;
  const rows = [];
  if (c.n)
    rows.push(`<span class="calb"><i>will finish</i> said ${pcs(c.stated)}, ` +
      `${pcs(c.actual)} finished · ${c.n}</span>`);
  if (c.need && c.need.n)
    rows.push(`<span class="calb"><i>wanted</i> said ${pcs(c.need.stated)}, ` +
      `${pcs(c.need.moved)} moved a rung · ${c.need.n}</span>`);
  if (c.cost && c.cost.n)
    rows.push(`<span class="calb"><i>cost</i> said $${c.cost.stated}, ` +
      `billed $${c.cost.actual} · ${c.cost.n}</span>`);
  if (c.refine && c.refine.n)
    rows.push(`<span class="calb"><i>room</i> said ${pcs(c.refine.stated)}, ` +
      `${pcs(c.refine.actual)} actually rose · ${c.refine.n}</span>`);
  if (!rows.length)
    return `<div class="cal">${head} · <span class="muted">nothing has settled yet, so ` +
           `nothing has checked these numbers</span></div>`;
  const bands = (c.bands || []).filter((b) => b.n).map((b) =>
    `<span class="calb"><i>${Math.round(b.from * 100)}–${Math.round(b.to * 100)}%</i> ` +
    `${b.finished}/${b.n} finished</span>`).join('');
  return `<div class="cal">${head}${rows.map((r) => ' · ' + r).join('')}` +
    (bands ? `<div class="calrow">${bands}</div>` : '') + `</div>`;
}

/** The sharpen button, labelled with the odds that pressing it changes anything.
 *  That number is the whole reason `headroom` is scored: without it the choice
 *  between two held proposals is a coin toss, and half the calls buy nothing. */
function sharpenBtn(p, d) {
  const rounds = p.sharpen_rounds || 0;
  // Not an attempt count any more. The server decides whether another round is
  // worth paying for by what the last one measured, and it says why — which is
  // the useful half: "stopped improving" means this is as good as the objective
  // gets, "hit the spend cap" means it was cut off still climbing.
  if (p.sharpen_done)
    return `<span class="muted">sharpened ${rounds}× — ${esc(p.sharpen_done)}</span>`;
  const h = p.headroom, floor = typeof d.sharpen_floor === 'number' ? d.sharpen_floor : 0.15;
  const known = h !== null && h !== undefined;
  const dim = known && h < floor;
  // What the last round actually did, next to what the next one claims it can
  // do. The claim is the architect's; the gain is the record's.
  const gv = p.sharpen_gain;
  const g = typeof gv === 'number'
    ? ` · last round ${gv >= 0 ? '+' : ''}${Math.round(gv * 100)}%` : '';
  return `<button class="dp-s${dim ? ' dim' : ''}" data-id="${esc(p.id)}"` +
    ` title="${esc(p.why_headroom || 'nobody has judged whether another look would help')}">` +
    (known ? `Improve the odds · ${Math.round(h * 100)}% chance it helps`
           : 'Improve the odds') + g + `</button>`;
}

function dirSection(s, cls) {
  if (!s) return '';
  return `<section class="dsec ${cls || ''}"><h4>${esc(s.title)}</h4>` +
    `<div class="md">${md(s.body)}</div></section>`;
}

// Where an explored proposal came from. Said on the card because "we searched
// the web" is a claim, and a claim with nothing to click is one you have to
// take on trust.
const ORIGIN_WORD = {
  cross_lane: 'found by cross-referencing what the lanes have reported',
  outside: 'found by looking outside this workspace',
  reframe: 'found by re-reading the doctrine',
};

/** An outward link that survives whatever a model put in the string. */
function link(u) {
  const s = String(u || '');
  if (!/^https?:\/\//i.test(s)) return esc(s);
  const href = encodeURI(s).replace(/"/g, '%22').replace(/</g, '%3C').replace(/>/g, '%3E');
  return `<a href="${href}" target="_blank" rel="noreferrer noopener">` +
    `${esc(s.replace(/^https?:\/\//i, '').slice(0, 64))}</a>`;
}

function originLine(p) {
  if (p.source !== 'explore') return '';
  const src = (p.sources || []).map(link).join(' · ');
  return `<div class="dorigin">${esc(ORIGIN_WORD[p.origin] || 'found by exploring')}` +
    (src ? ` — ${src}` : '') + `</div>`;
}

const VERDICT_LINE = {
  ready: ['ok', 'there is somewhere to go'],
  held: ['warn', 'there is somewhere to go, and nothing will go there'],
  nowhere: ['bad', 'nowhere left to go'],
  empty: ['warn', 'nothing waiting'],
  unexplored: ['warn', 'nobody has looked'],
};

/** What the last look actually concluded. Without this the button is a button
    that spends 90 seconds and changes nothing on screen. */
function lastExplore(s) {
  const e = s.last_explore;
  if (!e) return '';
  const when = String(e.at || '').slice(0, 16).replace('T', ' ');
  const how = `looked across every lane${e.web ? ' and searched the web' : ''}, ${esc(when)}`;
  if (e.exhausted) {
    return `<div class="dnowhere"><b>Last look: nothing worth proposing.</b>` +
      `<div class="muted">${esc(how)}</div>` +
      (e.assessment ? `<div class="md">${md(e.assessment)}</div>` : '') +
      (e.why_exhausted ? `<div class="why-x">Why nothing: ${mdi(e.why_exhausted)}</div>` : '') +
      `</div>`;
  }
  return `<div class="dlast"><b>Last look found ${s.last_explore_found || 0}.</b> ` +
    `<span class="muted">${esc(how)}</span>` +
    (e.assessment ? `<div class="md">${md(e.assessment)}</div>` : '') + `</div>`;
}

/** The case: what is actually stopping this, and which decisions are Travis's. */
function caseBlock(c) {
  if (!c) return '';
  const when = String(c.at || '').slice(0, 16).replace('T', ' ');
  return `<div class="dcase"><div class="muted">the case, ${esc(when)}` +
    `${c.lane ? ' · ' + esc(c.lane) : ' · whole stack'} — also in your thread</div>` +
    `<div class="md">${md(c.assessment || '')}</div>` +
    (c.blocked_on || []).map((b) =>
      `<div class="dblock"><span class="whose ${esc(b.whose)}">${esc(b.whose)}</span>` +
      `${mdi(b.what)}<div class="muted">${mdi(b.why)}</div></div>`).join('') +
    (c.decisions || []).map((d) =>
      `<div class="ddec"><b>${mdi(d.decision)}</b>` +
      (d.options || []).map((o) =>
        `<div class="dopt${o === d.recommend ? ' rec' : ''}">${mdi(o)}` +
        (o === d.recommend ? ' <span class="tag">it recommends this</span>' : '') +
        `</div>`).join('') +
      (d.off_options
        ? `<div class="warnt">it recommends ${mdi(d.recommend)}, which is not one of the ` +
          `options it gave</div>` : '') +
      (d.because ? `<div class="muted">because ${mdi(d.because)}</div>` : '') +
      (d.unlocks ? `<div class="unlocks">unlocks: ${mdi(d.unlocks)}</div>` : '') +
      (d.cost_of_waiting
        ? `<div class="muted">leaving it open costs: ${mdi(d.cost_of_waiting)}</div>` : '') +
      `</div>`).join('') +
    (c.direction_change
      ? `<div class="dchange"><b>It thinks the direction itself is wrong.</b>` +
        `<div>${mdi(c.direction_change.what)}</div>` +
        `<div class="muted">${mdi(c.direction_change.why)}</div>` +
        `<div>Instead: ${mdi(c.direction_change.instead)}</div></div>` : '') +
    (c.nothing_needed
      ? `<div class="good">It says nothing is needed: ${mdi(c.why_nothing_needed)}</div>` : '') +
    `</div>`;
}

/** The honest state of the direction, the two buttons that can change it, and
    what the last attempt concluded. */
function exploreBar(d) {
  const s = d.state || {};
  const web = !!d.can_web;
  const [cls, word] = VERDICT_LINE[s.verdict] || ['warn', 'unclear'];
  const gates = (s.gates || []).map((g) =>
    `<div class="dgate"><b>${esc(g.gate)}</b> at ${esc(g.at)}, your limit ${esc(g.limit)}` +
    `<div class="muted">${mdi(g.why)}</div></div>`).join('');

  return `<div class="dstate ${cls}">` +
    `<div class="dverdict">${esc(word)}</div>` +
    `<div class="dhead">${esc(s.headline || '')}</div>` +
    (gates
      ? `<div class="dgates"><div class="muted">and nothing starts by itself while these ` +
        `stand${s.auto_adopt ? '' : ' — though adopt-automatically is off anyway'}:</div>` +
        gates + `</div>`
      : '') +
    `<div class="dexp"><button id="btn-explore" class="primary">` +
    (web ? 'Look for more — search, cross-reference, brainstorm' : 'Look for more') +
    `</button>` +
    (s.case_worth_asking
      ? `<button id="btn-case">Put the case to me</button>` : '') +
    `<span class="muted" id="explore-status">` +
    `looks across every lane at once${web ? ' and searches the web' : ''}` +
    (s.case_worth_asking
      ? '. The second writes up what is actually stopping this and which calls are yours, ' +
        'into your thread.'
      : '.') +
    `</span></div>` +
    lastExplore(s) + caseBlock(s.last_case) + `</div>`;
}

function renderDirection(d) {
  const el = $('dir-out');
  if (!d) { el.textContent = 'nothing to show'; return; }
  state.direction = d;
  $('dir-auto').checked = !!d.auto_adopt;
  $('dir-lane').textContent = state.sel ? `· ${state.sel}` : '· everything';

  const fi = (d.findings || []).slice(0, 12).map((f) =>
    `<div class="fi ${esc(f.bearing)}">` +
    `<span class="fi-tag">${esc(BEARING_WORD[f.bearing] || f.bearing)}</span>` +
    (f.lane ? `<b>${esc(f.lane)}</b> ` : '') + `${mdi(f.text)}` +
    `<span class="muted"> — ${esc(f.source)}, ${esc((f.at || '').slice(0, 10))}</span>` +
    (f.read_at ? '' : `<button class="fi-x" data-id="${esc(f.id)}" title="You have read this">noted</button>`) +
    `</div>`).join('');

  // Claims the stack made, acted on, and later disproved — which never earned a
  // rung, so nothing on the ladder moved for them. That is exactly why they get
  // their own block: every other record on this page is about something that
  // moved, so a false claim that moved nothing has nowhere else to appear, and
  // the failure it causes is a later call confidently repeating a sentence we
  // have already disproved. No buttons: this is a record, not a task. The line
  // about scoring is not reassurance — it names where the correction is read.
  const fixes = (d.corrections || []).map((c) =>
    `<div class="dfix"><span class="tag">${esc(c.lane || '')}</span>` +
    `<b>${mdi(c.claim || '')}</b>` +
    (c.why ? `<div class="muted">${mdi(c.why)}</div>` : '') +
    `<div class="muted">found false ${esc((c.at || '').slice(0, 10))}` +
    (c.finding_id ? ` · from finding ${esc(String(c.finding_id).slice(0, 8))}` : '') +
    `</div></div>`).join('');

  // The architect's open questions and the doctrine's own open theses are shown
  // in one place because they are the same kind of thing - something believed
  // and not settled - but they are not merged: only Travis can move one of these
  // into the file, so a proposed question stays visibly proposed.
  const qs = (d.questions || []).map((p) =>
    `<div class="dq"><b>${mdi(p.text)}</b>` +
    (p.why ? `<div class="muted">${mdi(p.why)}</div>` : '') +
    (p.settled_by ? `<div class="settle">settled by: ${mdi(p.settled_by)}</div>` : '') +
    `<div class="muted">${esc(p.lane || '')} · proposed ${esc((p.at || '').slice(0, 10))}` +
    ` <button class="dq-x" data-id="${esc(p.id)}">not worth chasing</button></div></div>`).join('');

  const bar = typeof d.bar === 'number' ? d.bar : 0.6;
  const floor = typeof d.sharpen_floor === 'number' ? d.sharpen_floor : 0.15;
  const props = (d.proposals || []).map((p) =>
    `<div class="dprop${p.hold ? ' held' : ' ready'}">` +
    scores(p, d) +
    `<b>${mdi(p.text)}</b>` +
    (p.why ? `<div class="muted">${mdi(p.why)}</div>` : '') +
    originLine(p) +
    (p.what_changed ? `<div class="changed">sharpened: ${mdi(p.what_changed)}</div>` : '') +
    (p.alternate_of ? `<div class="changed">another route to the same end</div>` : '') +
    ((p.unknowns || []).length
      ? `<div class="unk"><span>needs, and nobody has established:</span><ul>` +
        p.unknowns.map((u) => `<li>${esc(u)}</li>`).join('') + `</ul></div>` : '') +
    (p.sharpen_reasoning ? `<div class="muted why">${mdi(p.sharpen_reasoning)}</div>` : '') +
    // Said out loud rather than left in a tooltip, because it is the one case
    // where the harness has stopped trying and will not say so anywhere else.
    (p.headroom !== null && p.headroom !== undefined && p.headroom < floor && p.why_headroom
      ? `<div class="changed">nothing left to sharpen: ${mdi(p.why_headroom)}</div>` : '') +
    `<div class="row"><span class="tag">${esc(p.lane || '')}</span>` +
    // The one thing this panel has to make unmissable: whether this gets
    // started without anyone deciding, and if not, what is stopping it.
    // One hold does not wait for a person: an objective over the bars but still
    // carrying room to improve is queued for a sharpening round the harness
    // makes itself. Calling that "waits for you" sends the operator to look at
    // a queue that is already draining, so it says which it is - and only while
    // automation is actually on, because with it off nobody is sharpening
    // anything and the objective really is waiting for a hand.
    // A third case, and the one that was reading as its opposite: a mode or a
    // stage is not the objective waiting for a person, it is a person having
    // already answered about the whole lane. "waits for you" invited a click
    // that silently overruled the switch. This says who is holding it, and the
    // button below makes the overrule deliberate.
    (p.hold
      ? (p.hold_is_policy
          ? `<span class="hold policy">you set this lane to stop — ${esc(p.hold)}</span>`
          : p.hold_is_sharpen && d.auto_adopt
            ? `<span class="hold sharpening">${esc(p.hold)}</span>`
            : `<span class="hold">waits for you — ${esc(p.hold)}</span>`)
      : `<span class="auto-ok">will start on its own</span>`) +
    `<button class="dp-go primary" data-id="${esc(p.id)}">Adopt as a goal</button>` +
    sharpenBtn(p, d) +
    `<button class="dp-x" data-id="${esc(p.id)}">Not now</button></div></div>`).join('');

  const revs = (d.reviews || []).map((r) =>
    `<div class="drev"><div class="muted">${esc((r.at || '').slice(0, 16).replace('T', ' '))}` +
    ` · ${esc(r.lane || '')}${r.auto ? ' · automatic' : ''}</div>` +
    `<div class="md">${md(r.assessment || '')}</div>` +
    (r.ladder || []).map((l) =>
      `<div class="dladder"><code>${esc(l.from || '?')}</code> → <code>${esc(l.to || '?')}</code> ` +
      `${mdi(l.claim || '')}<div class="muted">${mdi(l.evidence || '')}</div></div>`).join('') +
    (r.exhausted ? `<div class="warnt">nothing further worth doing here: ${mdi(r.why_exhausted || '')}</div>` : '') +
    `</div>`).join('');

  el.innerHTML =
    dirSection(d.thesis, 'thesis') +
    `<section class="dsec"><h4>What the work has said back</h4>` +
    (fi || '<div class="empty">nothing reported yet. Workers and the architect file these ' +
      'under DOCTRINE: at the end of every report.</div>') + `</section>` +
    (fixes
      ? `<section class="dsec"><h4>Claims already found false</h4>` +
        `<div class="muted">None of these was ever a rung, so nothing on the ladder was ` +
        `taken back for them — they were simply believed, and are not true. Each one is ` +
        `injected into every proposal scored in its lane, so it stops being repeated.</div>` +
        fixes + `</section>`
      : '') +
    `<section class="dsec"><h4>What we still do not know</h4>` +
    (d.open_theses ? `<div class="md">${md(d.open_theses.body)}</div>` : '') +
    (qs ? `<div class="dqs"><div class="muted">Proposed by the work, not yet in the doctrine ` +
      `— only you can put one there:</div>${qs}</div>` : '') + `</section>` +
    `<section class="dsec"><h4>Where there is left to go</h4>` +
    exploreBar(d) +
    calBlock(d.calibration, bar) +
    (props || '<div class="empty">No proposals. One appears when a goal finishes and the ' +
      'architect judges the lane has somewhere left to travel — or when you go looking ' +
      'above.</div>') +
    (revs ? `<h5>Recent reviews</h5>${revs}` : '') + `</section>` +
    dirSection(d.ladder, '') + dirSection(d.values, '') + dirSection(d.owed, '') +
    `<div class="muted dfoot">The two sections above are read from <code>` +
    `${esc(d.doctrine_path || 'DOCTRINE.md')}</code> and are injected verbatim into every ` +
    `plan, every review, and every worker prompt.</div>`;

  // Wired here rather than in wireDirection because this section is rebuilt on
  // every load, which throws the old node and its handler away with it.
  const ex = $('btn-explore');
  if (ex) ex.addEventListener('click', async () => {
    ex.disabled = true;
    ex.textContent = d.can_web ? 'searching and cross-referencing…' : 'cross-referencing…';
    $('explore-status').textContent =
      'one architect call across every lane. This takes a minute or two.';
    const r = await post('/api/direction/explore', { lane: state.sel || null });
    const rev = r.review || {};
    const n = (rev.proposals || []).length;
    // The result itself is rendered by the state block, which reads it back
    // from the store - so it survives this reload and every one after it. This
    // line is only the receipt for the click.
    $('dir-status').innerHTML = r.ok
      ? (n ? `${n} new ${n === 1 ? 'thing' : 'things'} to look at`
           : 'looked, found nothing worth proposing') +
        ((rev.misfiled || []).length
          ? ` · ${rev.misfiled.length} dropped for naming a lane that does not exist` : '')
      : `<span class="err">${esc(r.error || 'failed')}</span>`;
    loadDirection();
  });

  const cs = $('btn-case');
  if (cs) cs.addEventListener('click', async () => {
    cs.disabled = true; cs.textContent = 'writing it up…';
    $('explore-status').textContent =
      'working out what is actually stopping this, and which calls are yours.';
    const r = await post('/api/direction/case', { lane: state.sel || null });
    const c = r.case || {};
    $('dir-status').innerHTML = r.ok
      ? `the case is in your thread` +
        ((c.decisions || []).length ? ` — ${c.decisions.length} decision(s) for you` : '') +
        (c.direction_change ? ' · it thinks the direction itself is wrong' : '')
      : `<span class="err">${esc(r.error || 'failed')}</span>`;
    loadDirection();
    if (typeof loadChat === 'function') loadChat();
  });

  el.querySelectorAll('.fi-x').forEach((b) =>
    b.addEventListener('click', async () => {
      await post('/api/findings/ack', { ids: [b.dataset.id] });
      loadFindings(); loadDirection();
    }));
  // Adopting asks once, plainly, and only sends `override` after the server has
  // said what would be overridden. The rule itself is deliberately NOT written
  // here: a copy of the lane's mode and stage living in the browser would be a
  // second gate that can disagree with the first one, and it is the first one
  // that actually stops anything. So this arms on the refusal it is given and
  // quotes it back - which also means a hold added later needs no change here.
  el.querySelectorAll('.dp-go').forEach((b) =>
    b.addEventListener('click', async () => {
      const armed = b.dataset.armed === '1';
      b.disabled = true; b.textContent = 'planning…';
      const r = await post('/api/direction/proposal',
                           { id: b.dataset.id, action: 'adopt', override: armed });
      if (!r.ok && r.needs_override) {
        b.disabled = false; b.dataset.armed = '1';
        b.textContent = 'Adopt anyway';
        b.classList.add('warn');
        $('dir-status').innerHTML =
          `<span class="err">${esc(r.hold)}</span> — click again to start it here anyway, ` +
          `or change the lane in Settings.`;
        return;
      }
      $('dir-status').innerHTML = r.ok
        ? `opened goal ${esc(r.goal.id)}` +
          (r.overrode ? ` — overrode: ${esc(r.overrode)}` : '')
        : `<span class="err">${esc(r.error)}</span>`;
      await refresh(); loadDirection();
    }));
  el.querySelectorAll('.dp-s').forEach((b) =>
    b.addEventListener('click', async () => {
      b.disabled = true; b.textContent = 'weighing it up…';
      const r = await post('/api/direction/proposal', { id: b.dataset.id, action: 'sharpen' });
      $('dir-status').innerHTML = r.ok
        ? `${pcs(r.confidence)} odds · mission wants it ${pcs(r.need)} · ` +
          `about $${(r.cost_usd ?? 0).toFixed(2)} · ${pcs(r.headroom)} room left` +
          (r.superseded ? ' · replaced by a version likelier to land' : '') +
          (r.alternates && r.alternates.length
            ? ` · ${r.alternates.length} other route(s) proposed` : '')
        : `<span class="err">${esc(r.error)}</span>`;
      loadDirection();
    }));
  el.querySelectorAll('.dp-x, .dq-x').forEach((b) =>
    b.addEventListener('click', async () => {
      await post('/api/direction/proposal', { id: b.dataset.id, action: 'dismiss' });
      loadDirection();
    }));
}

async function loadDirection() {
  try {
    const r = await api('/api/direction' + (state.sel ? '?lane=' + encodeURIComponent(state.sel) : ''));
    renderDirection(r.direction);
  } catch {
    $('dir-out').innerHTML = '<span class="err">could not read the direction</span>';
  }
  loadLaneDirection();
  loadDoctrine();
}

/* ------------------------------------------------- what one lane is for
 *
 * Six free-text fields, one choice and one lane reference. They are drawn as
 * separate boxes rather than one document because they answer separate
 * questions and are read separately - `direction_block()` quotes each under its
 * own heading, and a single textarea would let a rewrite of one silently carry
 * a stale sentence from another into every proposal in the lane.
 *
 * `represents` is drawn but never drafted. See `draft_lane_direction`.
 */
const LD_FIELDS = [
  ['for', 'What it is for',
   'One sentence, true of this lane and no other.'],
  ['thesis', 'The bet it makes',
   'Stated so it could turn out to be FALSE. No evidence, no rung, no hedging — ' +
   'that is the next box.'],
  ['claim', 'Where that stands today',
   'The rung and the actual numbers. `unrecorded` is a real answer.'],
  ['bar', 'What would clear it',
   'A test somebody could run, not an aspiration.'],
  ['unknown', 'What we do not know',
   'Upstream of the bar: the bar is a test already designed, this is what is ' +
   'still open about the shape of the answer. Empty beats padded.'],
  ['not_for', 'Not this lane\u2019s job',
   'The useful ones name a real temptation — usually work another lane owns.'],
];

/** The judged rung beside the written claim. The whole reason it is here is that
 *  a direction can claim more than any review ever recorded, and while the two
 *  facts live on different pages nobody finds out. */
function ldRung(rung) {
  return rung
    ? `<span class="ld-rung">reviews have it at <b>${esc(rung)}</b></span>`
    : `<span class="ld-rung none">no review has ever moved a claim in this lane — ` +
      `a rung written below is not one it has earned</span>`;
}

/** The lane's own direction, read rather than edited.
 *
 *  This is the half the operator actually looks at. Everything below it on the
 *  page is the workspace's mission and DOCTRINE.md, which is identical in every
 *  lane by design - so with the lane's record hidden inside a closed accordion,
 *  changing lanes changed nothing visible and the screen read as broken.
 *
 *  Headed with the lane name on every block. The workspace calls its own
 *  sections "The thesis" and "What we still do not know", and those are
 *  different claims about a different scope; two unlabelled headings with the
 *  same words is how somebody ends up holding a lane to the stack's bet. */
function ldReadBlock(lane, dir, rung) {
  const el = $('ld-read');
  if (!el) return;
  const said = LD_FIELDS.filter(([k]) => dir[k]);
  if (!said.length) {
    el.innerHTML = `<section class="dsec"><h4>What ${esc(lane)} is for</h4>` +
      `<div class="empty">Nothing recorded for this lane. Everything below is the ` +
      `workspace direction, which is the same in every lane. Open the panel above ` +
      `to write this one, or let it draft from the lane's own evidence.</div></section>`;
    return;
  }
  el.innerHTML = `<section class="dsec"><h4>What ${esc(lane)} is for</h4>` +
    `<div class="muted">This lane only. It is injected into every proposal, every ` +
    `score and every review in this lane, and it is what the work below gets held ` +
    `to. ${ldRung(rung)}</div>` +
    said.map(([k, label]) =>
      `<div class="ldr"><h5>${esc(label)}</h5><div class="md">${md(dir[k])}</div></div>`).join('') +
    (dir.represents ? `<div class="ldr muted">presents <b>${esc(dir.represents)}</b>` +
      ` — that lane's work is what this one has to show</div>` : '') +
    `</section>`;
}

function renderLaneDirection(d) {
  const body = $('ld-body');
  const sum = $('ld-sum');
  if (!state.sel) {
    sum.textContent = '';
    body.textContent = 'select a lane on the left';
    if ($('ld-read')) $('ld-read').innerHTML = '';
    return;
  }
  const row = (d.lanes || []).find((l) => l.name === state.sel);
  if (!row) {
    sum.textContent = '';
    body.innerHTML = `<span class="err">${esc(state.sel)} is not a lane in this workspace</span>`;
    if ($('ld-read')) $('ld-read').innerHTML = '';
    return;
  }
  ldReadBlock(row.name, row.direction || {}, row.rung);
  state.laneDir = d;
  const dir = row.direction || {};
  const n = Object.keys(dir).length;
  sum.innerHTML = `· ${esc(state.sel)} — ` +
    (n ? `${n} of ${(d.fields || []).length} recorded` : 'nothing recorded') +
    ' ' + ldRung(row.rung);

  const boxes = LD_FIELDS.map(([k, label, hint]) =>
    `<label class="ld-f"><b>${esc(label)}</b>` +
    `<span class="muted">${mdi(hint)}</span>` +
    `<textarea id="ld-${k}" rows="3" placeholder="not recorded">${esc(dir[k] || '')}</textarea>` +
    `</label>`).join('');

  const ab = dir.after_bar || d.default_after_bar;
  const opts = Object.entries(d.after_bar || {}).map(([k, v]) =>
    `<option value="${esc(k)}"${k === ab ? ' selected' : ''}>${esc(k)} — ${esc(v)}</option>`).join('');

  body.innerHTML = boxes +
    `<label class="ld-f"><b>When the bar clears</b>` +
    `<span class="muted">Only worth saying when it is not the default. Write it ` +
    `BEFORE the bar clears — afterwards there is a finished thing and an obvious ` +
    `momentum to keep going.</span>` +
    `<select id="ld-after_bar">${opts}</select></label>` +
    `<label class="ld-f"><b>Presents another lane</b>` +
    `<span class="muted">Only for a presentation surface. A commit here moves no ` +
    `rung on the lane named. A lane cannot present itself, and this is never ` +
    `drafted for you.</span>` +
    `<input id="ld-represents" value="${esc(dir.represents || '')}" placeholder="none">` +
    `</label>` +
    `<div class="ld-bar">` +
    `<button id="ld-save" class="primary">Save</button>` +
    `<button id="ld-draft"${d.architect ? '' : ' disabled'}>Draft from this lane\u2019s evidence</button>` +
    `<span class="muted" id="ld-status">${d.architect ? '' : esc(d.architect_off || '')}</span>` +
    `</div><div id="ld-why"></div>`;

  $('ld-save').addEventListener('click', saveLaneDirection);
  $('ld-draft').addEventListener('click', draftLaneDirection);
}

/** Read the boxes back. Empty fields are dropped rather than saved blank, which
 *  is what makes clearing one a real act: `lane_direction` renders an absent
 *  field as silence, and silence is honest where an empty string is a claim that
 *  somebody wrote nothing on purpose. */
function ldRead() {
  const out = {};
  for (const [k] of LD_FIELDS) {
    const v = ($(`ld-${k}`).value || '').trim();
    if (v) out[k] = v;
  }
  const ab = $('ld-after_bar').value;
  if (ab && ab !== (state.laneDir || {}).default_after_bar) out.after_bar = ab;
  const rp = ($('ld-represents').value || '').trim();
  if (rp) out.represents = rp;
  return out;
}

async function saveLaneDirection() {
  const st = $('ld-status');
  st.textContent = 'saving…';
  const r = await post('/api/lane/direction', { lane: state.sel, direction: ldRead() });
  if (!r.ok) { st.innerHTML = `<span class="err">${esc(r.error || 'refused')}</span>`; return; }
  // Reload first, then report. The reload rebuilds `ld-body`, so `st` is a
  // detached node by the time it returns and writing to it says nothing to
  // anyone - a save that worked read as a save that did nothing at all.
  await loadLaneDirection();
  $('ld-status').textContent = 'saved';
}

/** Fills the boxes in and saves nothing. The status line says so, because a
 *  button that rewrites what is on screen reads like a button that committed. */
async function draftLaneDirection() {
  const st = $('ld-status');
  st.textContent = 'reading this lane\u2019s spec, reviews, record and findings…';
  const r = await post('/api/lane/direction/draft', { lane: state.sel });
  if (!r.ok) { st.innerHTML = `<span class="err">${esc(r.error || 'failed')}</span>`; return; }

  const dr = r.draft || {};
  for (const [k] of LD_FIELDS) $(`ld-${k}`).value = dr[k] || '';
  if (dr.after_bar) $('ld-after_bar').value = dr.after_bar;

  st.innerHTML = `<b>${esc(r.note || 'drafted, not saved')}</b>` +
    ((r.moved || []).length
      ? ` — it changed ${esc((r.moved || []).join(', '))}`
      : ' — it changed nothing');
  $('ld-why').innerHTML =
    ((r.why || []).length
      ? `<div class="ld-why"><b>Why</b>` +
        (r.why || []).map((w) => `<div>${mdi(w)}</div>`).join('') + `</div>` : '') +
    ((r.kept || []).length
      ? `<div class="muted">left alone: ${esc((r.kept || []).join(', '))}</div>` : '');
}

async function loadLaneDirection() {
  try {
    renderLaneDirection(await api('/api/lane/directions'));
  } catch {
    $('ld-body').innerHTML = '<span class="err">could not read the lane directions</span>';
  }
}

/* --------------------------------------- what the whole workspace is held to
 *
 * DOCTRINE.md, editable. The lane block above is one lane; this is the file
 * every lane is judged against, and it is injected verbatim into every plan,
 * every review and every worker prompt.
 *
 * Three buttons and they are not the same kind of thing. Save writes the file.
 * Draft and Review are architect calls that write nothing. Ratify is the
 * operator saying the current core is the one that stands — and nothing else in
 * the system can do it, which is why a save deliberately leaves the change
 * unratified rather than pinning its own write.
 */

/** The numbers, which are the part that was missing entirely. Every one is
 *  counted off disk. There is no score: what a doctrine is worth is a
 *  judgement, and rule 6 says whose. */
function dcStats(s) {
  const pin = s.drifted
    ? `<span class="ld-rung none">the core has changed since you ratified it</span>`
    : s.unratified
      ? `<span class="ld-rung none">never ratified — nobody has said this core stands</span>`
      : s.ratified
        ? `<span class="ld-rung">ratified ${esc((s.ratified_at || '').slice(0, 10))}</span>`
        : `<span class="ld-rung none">no core is being injected at all</span>`;

  const cell = (n, label, warn) =>
    `<div class="dcs${warn ? ' warn' : ''}"><b>${esc(String(n))}</b>` +
    `<span>${esc(label)}</span></div>`;

  return `<div class="dc-stats">` +
    // Reach first: the file's own text cannot tell you how far it travels, and
    // a lane with no direction of its own is a lane this file is the only
    // standard for.
    cell(`${s.lanes_with_direction}/${s.lanes}`, 'lanes with a direction',
         s.lanes_with_direction < s.lanes) +
    cell(`${s.lanes_with_thesis}/${s.lanes}`, 'with a stated bet',
         s.lanes_with_thesis < s.lanes) +
    cell(`${s.lanes_with_unknown}/${s.lanes}`, 'that say what they do not know',
         s.lanes_with_unknown < s.lanes) +
    // The split that matters: the core is what every prompt carries, the rest
    // is commentary no model ever sees.
    cell(s.core_words, 'words injected into every prompt') +
    cell(s.bytes - s.core_bytes, 'bytes of commentary nothing reads') +
    cell(s.contradicted, 'findings that say something here is false', s.contradicted > 0) +
    cell(s.corrections, 'claims already found false') +
    cell(s.unread, 'findings you have not read', s.unread > 0) +
    `</div>` +
    `<div class="muted dc-pin">${pin} · core digest <code>` +
    `${esc(s.digest || '—')}</code> · <code>${esc(s.path || '')}</code></div>` +
    ((s.sections || []).length
      ? `<div class="muted dc-secs">` + s.sections.map((x) =>
          `<span>${esc(x.title)} <b>${x.words}</b>w</span>`).join('') + `</div>`
      : '');
}

function renderDoctrine(d) {
  state.doctrine = d;
  const s = d.stats || {};
  $('dc-sum').innerHTML = `· ${esc(d.workspace || '')} — ${s.core_words || 0} words ` +
    `in every prompt ` + (s.drifted || s.unratified
      ? `<span class="ld-rung none">unratified</span>`
      : `<span class="ld-rung">ratified</span>`);

  $('dc-body').innerHTML =
    dcStats(s) +
    `<label class="ld-f"><b>The file</b>` +
    `<span class="muted">Everything between <code>${esc(d.begin)}</code> and ` +
    `<code>${esc(d.end)}</code> is injected verbatim into every plan, every review ` +
    `and every worker prompt. The rest is commentary shown here and read by ` +
    `nobody else. Saving without those markers is refused — a file that lost them ` +
    `would inject nothing and nothing anywhere would report it.</span>` +
    `<textarea id="dc-text" rows="22" spellcheck="false">${esc(d.text || '')}</textarea>` +
    `</label>` +
    `<div class="ld-bar">` +
    `<button id="dc-save" class="primary">Save</button>` +
    `<button id="dc-ratify">It was me — this core stands</button>` +
    `<button id="dc-review"${d.architect ? '' : ' disabled'}>Review it against the evidence</button>` +
    `<button id="dc-draft"${d.architect ? '' : ' disabled'}>Redraft from the evidence</button>` +
    `<span class="muted" id="dc-status">${d.architect ? '' : esc(d.architect_off || '')}</span>` +
    `</div><div id="dc-out"></div>`;

  $('dc-save').addEventListener('click', saveDoctrine);
  $('dc-ratify').addEventListener('click', ratifyDoctrine);
  $('dc-review').addEventListener('click', reviewDoctrine);
  $('dc-draft').addEventListener('click', draftDoctrine);
}

async function saveDoctrine() {
  const st = $('dc-status');
  st.textContent = 'saving…';
  const r = await post('/api/doctrine/save', { text: $('dc-text').value });
  if (!r.ok) { st.innerHTML = `<span class="err">${esc(r.error || 'refused')}</span>`; return; }
  const text = r.note;
  await loadDoctrine();
  $('dc-status').textContent = text;
}

async function ratifyDoctrine() {
  const st = $('dc-status');
  st.textContent = 'pinning…';
  const r = await post('/api/doctrine/ratify', {});
  if (!r.ok) { st.innerHTML = `<span class="err">${esc(r.error || 'failed')}</span>`; return; }
  await loadDoctrine();
  $('dc-status').textContent = 'ratified — this core is the one that stands';
  loadBlockers && loadBlockers();
}

/** A reading, not a ruling. Quotes are checked against the file here as well as
 *  on the server, because the failure this call has is an entry that reads
 *  perfectly and is about a sentence nobody wrote. */
function dcItems(title, rows, cls) {
  if (!rows || !rows.length) return '';
  return `<h5>${esc(title)}</h5>` + rows.map((it) =>
    `<div class="dcq ${cls}">` +
    `<blockquote>${mdi(it.quote)}</blockquote>` +
    (it.in_file === false
      ? `<div class="err">this sentence is not in the file — the reviewer quoted ` +
        `something nobody wrote, so treat the item with suspicion</div>` : '') +
    (it.why ? `<div>${mdi(it.why)}</div>` : '') +
    (it.evidence ? `<div class="muted">cites: ${mdi(it.evidence)}</div>` : '') +
    `</div>`).join('');
}

async function reviewDoctrine() {
  const st = $('dc-status');
  st.textContent = 'reading it against every lane, finding and correction…';
  const r = await post('/api/doctrine/review', {});
  if (!r.ok) { st.innerHTML = `<span class="err">${esc(r.error || 'failed')}</span>`; return; }
  st.innerHTML = `<b>${esc(r.note || '')}</b>`;

  const word = { holds: 'it holds', stale: 'it is out of date',
                 contradicted: 'something in it is false' }[r.verdict] || 'no verdict';
  $('dc-out').innerHTML =
    `<div class="ld-why"><b>${esc(word)}</b>` +
    (r.summary ? `<div>${mdi(r.summary)}</div>` : '') +
    dcItems('Contradicted by the work', r.contradicted, 'bad') +
    dcItems('Claims a rung no review recorded', r.unearned, 'bad') +
    dcItems('Out of date', r.stale, 'warn') +
    dcItems('Believed by the work and not written down', r.missing, '') +
    ((r.well_earned || []).length
      ? `<h5>Actively supported by the evidence</h5>` +
        r.well_earned.map((w) => `<div class="dcq">${mdi(w)}</div>`).join('') : '') +
    `</div>`;
}

async function draftDoctrine() {
  const st = $('dc-status');
  st.textContent = 'redrafting against every lane, finding and correction…';
  const r = await post('/api/doctrine/draft', {});
  if (!r.ok) { st.innerHTML = `<span class="err">${esc(r.error || 'failed')}</span>`; return; }

  $('dc-text').value = r.draft || '';
  st.innerHTML = `<b>${esc(r.note || 'drafted, not saved')}</b>` +
    (r.same ? ' — it changed nothing' : '');
  $('dc-out').innerHTML =
    (r.markers_ok ? ''
      : `<div class="err">This draft is missing the core markers. Saving it is ` +
        `refused, and it is shown so you can see what it did rather than have ` +
        `it silently discarded.</div>`) +
    ((r.why || []).length
      ? `<div class="ld-why"><b>What it changed</b>` +
        r.why.map((w) => `<div>${mdi(w)}</div>`).join('') + `</div>` : '') +
    ((r.kept || []).length
      ? `<div class="ld-why"><b>What it left alone</b>` +
        r.kept.map((w) => `<div>${mdi(w)}</div>`).join('') + `</div>` : '');
}

async function loadDoctrine() {
  try {
    renderDoctrine(await api('/api/doctrine'));
  } catch {
    $('dc-body').innerHTML = '<span class="err">could not read the doctrine</span>';
  }
}

/** One spec run: what the reviewer asked for, what the writer did about it.
 *
 *  Both halves on screen, because the whole point of the loop is that neither
 *  side gets to be the only voice. A reviewer's demand that the writer refused
 *  and explained is a different situation from one it satisfied, and a run that
 *  ended `stalled` is telling you the two of them could not settle it. */
function specRound(rd, many, mine) {
  const w = rd.worker || {};
  const all = rd.reviews || [];
  // Only this row's document, when the run is drawn under a document. A run over
  // a set is drawn on every row it covers, so showing all of its reviews on each
  // of them prints the same round once per document - eleven documents would be
  // a hundred and twenty-one review blocks, and a demand about one file would
  // appear on ten rows it is not about.
  const shown = mine ? all.filter((rv) => rv.rel === mine) : all;
  const others = all.length - shown.length;
  // One review per document, because one reviewer cannot see all of several
  // documents in one call. The document's path is only printed when the run
  // covers more than one - on a single-document run it is the heading above.
  const reviews = shown.map((rv) => {
    const verdict = rv.verdict === 'SOLID'
      ? `<span class="sp-v ok">solid</span>`
      : `<span class="sp-v bad">needs work</span>`;
    // Per document, and only for the ones this round asked to move. A file with
    // no items was not asked for anything, so "did not change" would be a
    // complaint about obedience.
    const moved = rv.changed === null || rv.changed === undefined ? ''
      : rv.changed ? `<span class="sp-v ok">the file changed</span>`
                   : `<span class="sp-v bad">the file did not change</span>`;
    const defects = (rv.defects || []).length
      ? `<ol class="sp-defects">` + rv.defects.map((d) => `<li>${esc(d)}</li>`).join('') + `</ol>`
      : '';
    return `<div class="sp-review"><div class="sp-who">` +
      (many && !mine && rv.rel ? `<code>${esc(rv.rel)}</code> · ` : '') +
      `the reviewer ${verdict}` + (rv.why ? ` — ${esc(rv.why)}` : '') +
      ` ${moved}</div>` + defects + `</div>`;
  }).join('')
    // A document with no review in this round was already settled, or the round
    // never reached it. Said, rather than left as a gap under a writer reply
    // that would then read as an answer to nothing.
    || (mine ? `<div class="sp-review muted">this document was not reviewed ` +
               `in this round</div>` : '');
  // ONE writer for the whole round, so its reply sits under all of the reviews
  // rather than inside any one of them. It was given every item at once and
  // answers for every file it was given.
  const reply = w.text
    ? `<div class="sp-reply"><div class="sp-who">the writer` +
      (w.agreed === true ? ' — says they are already solid'
        : w.agreed === false ? ' — revised them' : '') +
      `</div><pre>${esc(w.text)}</pre></div>`
    : (rd.task_id ? `<div class="sp-reply muted">a writer is out on this round…</div>` : '');

  // The others are counted, not drawn: what they cost is part of what this round
  // was, and the writer's reply below covers all of them at once.
  return `<div class="sp-round"><div class="sp-who">round ${rd.n}` +
    (!many ? ''
      : mine ? (others ? ` · and ${others} other document(s) in the same round` : '')
             : ` · ${all.length} document(s) reviewed`) +
    `</div>` + reviews + reply + `</div>`;
}

const SP_STATE = {
  running: ['live', 'the reviewer and the writer are still going'],
  solid: ['ok', 'both of them say this document is solid'],
  stalled: ['warn', 'they disagree — two rounds changed nothing on disk'],
  capped: ['warn', 'stopped at the round cap without agreement'],
  stopped: ['bad', 'stopped'],
  closed: ['muted', 'closed by you'],
};

/** One run, folded. `r.rounds` is an array on the newest run and a count on the
 *  older ones - the server sends the transcript only for the one that renders
 *  open, and the rest are fetched by id the first time they are unfolded.
 *
 *  `mine` is the document whose row this is drawn on, and the reviews are
 *  narrowed to it: one run covers a set and appears on every row in the set.
 *
 *  `open` is only a DEFAULT. This pane redraws itself every four seconds while
 *  anything is live, and the default said "the newest run is open" every time -
 *  so folding it away lasted until the next tick and read as a button that does
 *  not work. What the operator did to a row outranks what the row opened as. */
function specRun(r, open, mine) {
  // Keyed on the row rather than on the run, because one run is drawn on every
  // document in its set - closing it on one row must not close the others.
  const key = `${r.id}|${mine || ''}`;
  if (key in state.specOpen) open = state.specOpen[key];
  const [cls, blurb] = SP_STATE[r.state] || ['muted', r.state];
  const rels = r.rels || [];
  const many = rels.length > 1;
  // Either the server sent the transcript, or this row was unfolded once before
  // and it was fetched then. The second case is why a repaint no longer drops an
  // open older run back to the placeholder.
  const rounds = Array.isArray(r.rounds) ? r.rounds : state.specRounds[r.id];
  const full = Array.isArray(rounds);
  const n = Array.isArray(r.rounds) ? r.rounds.length : (r.rounds || 0);
  const body = full
    ? rounds.map((rd) => specRound(rd, many, mine)).join('')
    : `<div class="sp-round muted">…</div>`;
  // A run over a set is drawn on every document's row, so the row on its own
  // does not say how much of the lane this run was about - and "6 rounds" over
  // eleven documents is a very different purchase from six rounds over one.
  const covers = many
    ? `<span class="sp-badge muted" title="${esc(rels.join('\n'))}">` +
      `${rels.length} documents</span> ` : '';
  // Documents the run had to set aside: the writer's worktree cannot reach them,
  // so reviewing them further would be buying rounds with no possible outcome.
  const dropped = Object.keys(r.dropped || {}).length
    ? `<span class="sp-badge warn" title="${esc(Object.entries(r.dropped)
        .map(([rel, why]) => `${rel} — ${why}`).join('\n'))}">` +
      `${Object.keys(r.dropped).length} unreachable</span> ` : '';
  // A run nobody is coming back to looks exactly like a live one from here -
  // same state, same badge - and it is the only thing on this tab that can hold
  // a whole campaign still. Named on the row, because the operator's next move
  // depends on knowing that waiting is not going to work.
  const stranded = r.stranded
    ? `<span class="sp-badge warn" title="${esc(r.stranded)}">abandoned</span> `
    : '';
  // The run's own reason, kept, and the correction under it. A stall says the
  // two sides disagree, and that reading needs the writer to have had the file;
  // when it did not, the sentence sends you to arbitrate a dispute that never
  // took place. Both are shown rather than one replacing the other, because
  // what the run recorded is the evidence for the correction.
  const misread = r.misread
    ? `<div class="sp-round sp-misread">${esc(r.misread)}</div>` : '';
  return `<details class="sp-run"${open ? ' open' : ''} data-id="${esc(r.id)}"` +
    ` data-key="${esc(key)}"` +
    // Carried on the element because the transcript of an older run is fetched
    // when it is unfolded, and by then the only thing that says which row it was
    // drawn on is the row itself.
    (mine ? ` data-mine="${esc(mine)}"` : '') +
    `${full ? '' : ' data-fetch="1"'}><summary>` +
    `<span class="sp-badge ${stranded || r.misread ? 'muted' : cls}">${esc(r.state)}</span> ` +
    stranded + covers + dropped +
    (r.misread ? `<span class="sp-badge warn" title="${esc(r.misread)}">not a stall</span> ` : '') +
    `${n} round${n === 1 ? '' : 's'} · ` +
    `${esc(r.stranded || r.why || blurb)}</summary>` + misread + body +
    (r.state === 'running'
      ? `<div class="sp-act">` +
        (r.stranded
          ? `<span class="muted">the heartbeat picks this back up on its own — ` +
            `stop it only if you want it dropped</span> ` : '') +
        `<button class="sp-stop" data-id="${esc(r.id)}">Stop this run</button></div>`
      : '') +
    `</details>`;
}

/** What the rater said about one document.
 *
 *  Three numbers and a derived fourth. `worth` is the only one that decides
 *  anything - it is what orders the queue - and the three it is derived from are
 *  shown next to it so that a document sitting at the bottom of the list can be
 *  argued with rather than just obeyed. */
function specRating(f) {
  const r = f.rating;
  if (!r) return `<div class="sp-rate muted">unrated — nothing has judged this document</div>`;
  if (r.error) return `<div class="sp-rate"><span class="err">${esc(r.error)}</span></div>`;
  const n = (label, v, why, cls) =>
    `<span class="sp-n ${cls || ''}" title="${esc(why || '')}">${label} ` +
    `<b>${pcs(v)}</b></span>`;
  // A rating is of a specific text. Once the document has moved, saying so is
  // the only honest thing to do with it: the numbers are still the numbers
  // somebody paid for, and they are no longer about the file on screen.
  const stale = r.stale
    ? `<span class="sp-n warn" title="the document has changed since it was rated">` +
      `rated an older version</span>`
    : '';
  const gaps = (r.gaps || []).length
    ? `<ul class="sp-gaps">` + r.gaps.map((g) => `<li>${esc(g)}</li>`).join('') + `</ul>`
    : '';
  return `<div class="sp-rate">` +
    n('solid', r.solidity, r.why_solidity, r.solidity >= 0.7 ? 'ok' : '') +
    n('needed', r.need, r.why_need) +
    n('headroom', r.headroom, r.why_headroom) +
    `<span class="sp-n worth" title="need × headroom — what one sharpen run on this ` +
    `document is expected to be worth. It is what orders the queue.">worth ` +
    `<b>${pcs(r.worth)}</b></span>` + stale +
    `</div>` + gaps;
}

/** One document, and the buttons that put it through the loop. */
function specFile(f, s) {
  const live = (f.runs || []).find((r) => r.state === 'running');
  const busy = state.specBusy === f.rel;
  const label = live ? `${live.waiting_on === 'worker' ? 'writer' : 'reviewer'} is working…`
    : busy ? 'starting…' : 'Sharpen until solid';
  const rating = f.rating && !f.rating.error;
  return `<section class="sp-file">` +
    `<div class="sp-file-head">` +
    (f.rank ? `<span class="sp-rank" title="where this sits in the sharpen order — ` +
      `the order the reviewer is asked in">${f.rank}</span>` : '') +
    `<code>${esc(f.rel)}</code>` +
    `<span class="muted">${f.lines} lines · ${Math.round(f.bytes / 100) / 10}k</span>` +
    // A document that only exists in the worktree is not in your repository
    // yet, and a tab that listed it the same way as the others would be telling
    // you the lane has a spec when what it has is a draft you have not read.
    (f.unmerged ? `<span class="sp-n warn" title="written by a worker in amp/${esc(s.lane)} ` +
      `and not merged — read it in the Diff tab">worktree only</span>` : '') +
    `<button class="sp-rate-go" data-rel="${esc(f.rel)}"` +
    `${state.specWork || !s.architect ? ' disabled' : ''}` +
    ` title="one architect call: how solid it is, how much the mission needs it, ` +
    `and whether sharpening would move it">${rating ? 'Re-rate' : 'Rate'}</button>` +
    `<button class="sp-go" data-rel="${esc(f.rel)}"` +
    `${live || busy || f.blocked || !s.architect ? ' disabled' : ''}` +
    ` title="${esc(f.blocked || (s.architect ? 'Codex reviews it, a Claude worker rewrites it in the worktree, until both agree' : s.architect_why || ''))}">` +
    `${esc(f.blocked ? 'no writer can reach it' : label)}</button></div>` +
    // Said on the row, not discovered by pressing the button. A document the
    // writer's worktree does not have cannot be sharpened at all: the reviewer
    // reads your checkout, the writer edits a tree without the file in it, and
    // every round comes back byte-identical until the run stalls.
    //
    // The remedy comes with it, and only half of it is a button. A worktree that
    // already exists never follows its branch, so committing the document is
    // necessary and not sufficient - and offering only the button would send
    // somebody to press it over and over on a file that is not committed yet.
    (f.blocked
      ? `<div class="sp-blocked">${esc(f.blocked)}` +
        (f.blocked_fix === 'refresh' || f.blocked_fix === 'commit+refresh'
          ? ` <button class="sp-refresh" data-lane="${esc(s.lane)}"` +
            `${state.specWork ? ' disabled' : ''}` +
            // `ahead` is null when git could not answer, and `|| 0` would read
            // that as "there is nothing in there to lose" - the one thing this
            // tooltip must not get wrong, since it is the reassurance the
            // button is asking to be trusted on.
            ` title="merge ${esc((s.worktree || {}).branch || 'the branch')} into ` +
            `amp/${esc(s.lane)} — a merge, never a reset, so ` +
            `${typeof (s.worktree || {}).ahead === 'number'
              ? `the ${s.worktree.ahead} unmerged worker commit(s) in there survive`
              : 'anything unmerged in there survives'}">` +
            `Refresh the worktree</button>`
          : '') +
        `</div>`
      : '') +
    specRating(f) +
    // Newest run open, the rest folded. Runs come back newest-first, so the one
    // worth reading is always the first one - and if it is live, the rounds
    // appearing under it are what "still going" looks like.
    ((f.runs || []).map((r, i) => specRun(r, i === 0, f.rel)).join('')) +
    `</section>`;
}

const SP_PLAN = {
  running: 'live',
  done: 'ok',
  stopped: 'bad',
};

/** The selection strip: what was chosen, what was skipped and why.
 *
 *  Not a queue any more. It chose a set of documents and opened ONE run over all
 *  of them, so there is no "next" and nothing to wait for - the run below is the
 *  whole of it, and its state is this strip's state. */
function specPlan(p) {
  if (!p) return '';
  const chose = (p.chose || []).length
    ? `<span class="sp-n ${p.state === 'running' ? 'live' : ''}" ` +
      `title="${esc((p.chose || []).join('\n'))}">` +
      `${p.chose.length} document(s) in one run</span>`
    : '';
  // Skipped is not the same as finished and is the number most likely to be
  // surprising - a selection that "did nothing" usually means the bar held
  // everything back, and that is a setting, not a fault.
  //
  // Split by reason, because the two answers are opposite: one is "raise the
  // bar", the other is "go and commit your specs", and one count covering both
  // sends people to change a setting that was never the problem.
  const skips = (kind, text) => {
    const xs = (p.skipped || []).filter((x) =>
      kind === 'bar' ? x.kind !== 'unreachable' : x.kind === 'unreachable');
    return xs.length
      ? `<span class="sp-n warn" title="${esc(xs.map((x) => `${x.rel} — ${x.why}`).join('\n'))}">` +
        `${xs.length} ${text}</span>`
      : '';
  };
  const skipped = skips('bar', `under the ${p.bar} bar`) +
                  skips('unreachable', 'no writer can reach');
  return `<div class="sp-plan">` +
    `<span class="sp-badge ${SP_PLAN[p.state] || 'muted'}">${esc(p.state)}</span> ` +
    chose + skipped +
    (p.why ? `<span class="muted">${esc(p.why)}</span>` : '') +
    // Stops the run, which is the only thing there is to stop. The old queue
    // could be halted without touching the run it had already started; there is
    // no "next" left for a button to be about.
    (p.state === 'running'
      ? `<button class="sp-stop" id="sp-plan-stop" title="closes the run this ` +
        `started — the same thing the run's own Stop button does">Stop this run</button>` : '') +
    `</div>`;
}

/** A lane with no documents of its own: what to do about it.
 *
 *  Two answers, because there are two problems wearing the same face. Either
 *  the design was never written down, or it was written down somewhere else -
 *  and the second is cheap to check, so it is checked first and the answer is
 *  shown before the button that would pay a worker to write a second copy. */
function specRecover(s) {
  const found = (s.candidates || []).length
    ? `<div class="sp-found"><div class="sp-who">` +
      `Design documents elsewhere in the repository that are mostly about this ` +
      `lane. Every <code>docs/spec/</code> was read, not only the ones that are ` +
      `lanes. The count is how often they name this lane, and it is what orders ` +
      `them.</div>` +
      s.candidates.map((c) =>
        `<div class="sp-cand"><code>${esc(c.path)}</code>` +
        `<span class="sp-n">${c.hits} mentions</span>` +
        (!c.lane
          ? `<span class="sp-n warn" title="no lane in this console covers that ` +
            `directory, so nothing here is watching it">no lane owns it</span>`
          : c.names_host ? ''
          : `<span class="sp-n warn" title="it never names the lane it is filed ` +
            `under">never names ${esc(c.lane)}</span>`) +
        `<span class="muted">${c.lines} lines${c.title ? ' · ' + esc(c.title) : ''}</span></div>`
      ).join('') +
      `<div class="sp-who">Moving one is a decision with a blast radius — it belongs ` +
      `to whichever lane you say it does, and nothing here will move it for you.</div>` +
      `</div>`
    : `<div class="sp-who">Nothing elsewhere in the repository reads as this lane's ` +
      `design either — every <code>docs/spec/</code> outside this lane was searched.</div>`;
  return `<div class="sp-gap">${esc(s.gap)} — this lane has no spec, so every ` +
    `<code>need</code> score it gets is judged against whatever the reader could find.</div>` +
    found + specDrafting(s);
}

/* The draft worker, while it is out.
 *
 *  Dispatch returns in about a second and the worker runs for minutes, so the
 *  button's own busy state is over almost immediately - which is what made this
 *  look like a button that did nothing. This reads the running task off the
 *  server instead, so it stays up for as long as the work does and is still
 *  there after a reload. */
function specDrafting(s) {
  const d = s.drafting;
  if (d) {
    const since = d.at ? ` · started ${ago(d.at)}` : '';
    return `<div class="sp-drafting">` +
      `<span class="dot"></span>` +
      (d.state === 'queued'
        ? `<b>queued</b> at position ${d.position} — a lane runs one worker at a ` +
          `time, so this starts when the one ahead of it finishes`
        : `<b>a worker is reading the code</b> and writing ` +
          `<code>docs/spec/SPEC.md</code> into the worktree`) +
      `<span class="muted">${since}</span>` +
      (d.task_id
        ? ` <button class="sp-watch" data-task="${esc(d.task_id)}" ` +
          `title="open this worker's transcript">watch it</button>`
        : '') +
      `</div>`;
  }
  return `<div class="sp-file-head"><button class="sp-go" id="sp-draft"` +
    `${state.specWork ? ' disabled' : ''}` +
    ` title="a worker reads the code and writes docs/spec/SPEC.md in the worktree. ` +
    `It is told to describe what the code already commits to, not to design.">` +
    `${state.specWork === 'draft' ? 'sending a worker…' : 'Draft one from the code'}` +
    `</button></div>`;
}

const SP_VERDICT = {
  missing: ['bad', 'no design document at all'],
  unrated: ['warn', 'not judged yet'],
  thin: ['warn', 'under the bar'],
  solid: ['ok', 'over the bar'],
};

/* The unattended loop: its switch, where the lane stands, and what it spent.
 *
 *  The verdict is shown whether or not the loop is on. It is the same fact
 *  either way, and a threshold you can only see while automation is running is
 *  a threshold nobody can check against. */
function specAuto(s) {
  const st = s.state || {};
  const a = s.auto || {};
  const [cls, said] = SP_VERDICT[st.verdict] || ['muted', st.verdict || '—'];
  // Capped, and the caps are shown as counts rather than as "on"/"off". A loop
  // that has stopped because it ran out of attempts looks exactly like a loop
  // that is switched off unless it says which.
  const spent = [];
  if (a.drafts) spent.push(`${a.drafts}/${a.max_drafts} draft${a.drafts > 1 ? 's' : ''}`);
  if (a.campaigns) spent.push(`${a.campaigns}/${a.max_campaigns} campaigns`);
  const stopped = a.on && (
    // Settled first: it is the only one of these that cost nothing, and reading
    // it as a spent budget would be exactly backwards.
    (a.settled
      ? 'it stopped without spending: these documents are under the bar, but none of ' +
        'them is judged worth a sharpen round — a round would not move them far ' +
        'enough, or the mission does not need them enough. Edit one, or re-rate it, ' +
        'and the loop picks the lane back up.'
      : st.verdict === 'missing' && a.drafts >= a.max_drafts
      ? 'it has used every draft attempt on this lane and stopped — a lane where drafting keeps producing nothing needs you, not another worker'
      : st.verdict === 'thin' && a.campaigns >= a.max_campaigns
      ? 'it has used every campaign on this lane and stopped — these documents did not converge and need a person'
      : ''));
  // Named for the lane it governs. It used to say "run this on its own" while
  // writing a stack-wide setting, so switching one lane on switched on eleven.
  return `<div class="sp-auto">` +
    `<label title="for ${esc(s.lane)} only: draft what is missing, rate what exists, ` +
    `sharpen what is under the bar, then ask Direction what to build from it. ` +
    `One step per tick. This spends — workers into worktrees, and architect calls.">` +
    `<input type="checkbox" id="sp-auto"${a.on ? ' checked' : ''}> ` +
    `run the loop on <b>${esc(s.lane)}</b>` +
    `</label>` +
    `<button id="sp-auto-all" class="sp-watch" title="turn the loop on for every ` +
    `lane at once. Spelled out as its own button because one lane's checkbox ` +
    `quietly doing this is the bug it replaces.">every lane</button>` +
    `<span class="sp-n ${cls}" title="${esc(said)}">${esc(st.verdict || '—')}</span>` +
    (st.solidity === null || st.solidity === undefined ? ''
      : `<span class="sp-n">lowest solidity ${pcs(st.solidity)}</span>`) +
    `<span class="muted">bar ${pcs(st.bar)}` +
    (spent.length ? ` · spent ${spent.join(', ')}` : '') + `</span>` +
    // What it is doing RIGHT NOW. An architect call takes tens of seconds on a
    // background thread, and without this the tab went from idle to a changed
    // verdict with nothing in between.
    (a.busy
      ? `<div class="sp-working"><span class="dot"></span>` +
        `${esc(a.busy.what)}<span class="muted"> · started ${ago(a.busy.at)}</span></div>`
      : '') +
    (stopped ? `<div class="sp-stopped">${esc(stopped)}</div>` : '') +
    specDirection(s) +
    ((a.log || []).length
      ? `<div class="muted sp-autolog">last: ${esc(a.log[0].what)} — ` +
        `${esc(a.log[0].why)} · ${ago(a.log[0].at)}</div>`
      : '') +
    `</div>`;
}

/* The hand-off into Direction.
 *
 *  Reaching the bar is not the end of the loop, it is the point of it - a
 *  document nothing ever reads changed nothing. The loop does this on its own
 *  once per version of the documents; the button is here because "once" is the
 *  right rule for something on a heartbeat and the wrong rule for the only way
 *  to get an answer, since the first answer can just be a bad one. */
function specDirection(s) {
  const a = s.auto || {};
  const busy = state.specWork === 'explore';
  const said = !a.explored_at
    ? 'Direction has not been asked about these documents'
    : a.explored_stale
    ? `asked ${ago(a.explored_at)}, but the documents have changed since`
    // "proposal(s)", not "goal(s)". They are two different things on two
    // different tabs, and calling the first one by the second one's name sends
    // people to Goals to look for something that is not there.
    : `asked ${ago(a.explored_at)} · ${a.proposed || 0} proposal(s) on Direction, ` +
      `awaiting adoption`;
  return `<div class="sp-dir">` +
    `<button id="sp-explore"${state.specWork ? ' disabled' : ''}` +
    ` title="one architect call. Reads this lane's documents, its scores and its ` +
    `named gaps, and writes PROPOSALS onto the Direction tab. They do not become ` +
    `goals and no worker starts until you adopt one there.">` +
    `${busy ? 'asking…' : a.explored_at ? 'Ask Direction again' : 'Propose goals from this spec'}` +
    `</button><span class="muted">${esc(said)}</span></div>`;
}

function renderSpec(s) {
  $('sp-lane').textContent = s.lane || '—';
  // Said once, at the top: the writer works in a worktree and you merge it.
  // A tab that starts workers without saying where they write is a tab that
  // looks like it is editing your files.
  const head = `<div class="sp-head">` +
    `Everything under <code>${esc(s.path)}/docs/spec/</code>. Sharpening sends the ` +
    `document to the reviewer, then to a writer in this lane's <code>amp/${esc(s.lane)}</code> ` +
    `worktree, and repeats until both of them say it is solid — or until it stops ` +
    `changing, or hits ${s.max_rounds} rounds. Your checkout is never written to; ` +
    `merge it yourself from the Diff tab.` +
    (s.architect ? '' : ` <span class="err">${esc(s.architect_why || '')}</span>`) +
    `</div>`;
  // The two whole-lane actions. Rating first, and left of sharpening, because
  // it is the cheap one and it is what decides the order the expensive one runs
  // in - a queue built from unrated documents is alphabetical wearing a ranking.
  const bar = (s.files || []).length
    ? `<div class="sp-bar">` +
      `<button id="sp-audit"${state.specWork || !s.architect ? ' disabled' : ''}` +
      ` title="one architect call per document that has changed since it was last ` +
      `rated. Already-rated documents are skipped.">` +
      `${state.specWork === 'audit' ? 'rating…' : 'Rate every document'}</button>` +
      `<button id="sp-all"${state.specWork || !s.architect ? ' disabled' : ''}` +
      ` title="one run over every document at once: each one is reviewed on its own ` +
      `— a reviewer has to see all of a document — and then a single writer is sent ` +
      `everything they asked for. Slow to come back: it is ${(s.files || []).length} ` +
      `architect calls before the first writer goes out.">` +
      `${state.specWork === 'all' ? 'starting…' : 'Sharpen every document'}</button>` +
      `<span class="muted">skips anything whose worth is under ${s.worth_bar}</span>` +
      `</div>` + specPlan(s.plan)
    : '';
  // The loop, and where this lane stands against its threshold. Rendered above
  // everything including the recovery panel, because "a worker is coming for
  // this on its own" changes what the buttons under it are for.
  const auto = specAuto(s);
  // A draft worker is shown either way. `specRecover` carries it while the lane
  // is still empty; once the worker has written the file the lane is no longer
  // empty but the worker is still running, and that is the moment the indicator
  // matters most - it is the difference between "finished" and "wrote a file".
  const body = s.gap
    ? specRecover(s)
    : (s.drafting ? specDrafting(s) : '') +
      (s.files || []).map((f) => specFile(f, s)).join('');
  // Runs whose document is gone. Kept visible so a rename cannot silently
  // swallow a review somebody paid for.
  const orphans = (s.orphans || []).length
    ? `<section class="sp-file"><div class="sp-file-head">` +
      `<code class="muted">documents that are no longer there</code></div>` +
      s.orphans.map((r) => specRun(
        { ...r, why: `${(r.rels || []).join(', ')} — ${r.why || ''}` }, false)).join('') +
      `</section>`
    : '';
  $('sp-out').innerHTML = head + auto + bar + body + orphans;

  if ($('sp-auto')) {
    $('sp-auto').onchange = async (e) => {
      const on = e.target.checked;
      $('sp-status').textContent = on
        ? `the loop will run on ${state.sel} — no other lane is affected`
        : `the loop is off for ${state.sel}`;
      await post('/api/spec/auto', { lane: state.sel, on });
      loadSpec();
    };
  }
  if ($('sp-auto-all')) {
    $('sp-auto-all').onclick = async () => {
      const on = !((s.auto || {}).on);
      if (!confirm(`Turn the spec loop ${on ? 'ON' : 'off'} for every lane?\n\n` +
                   (on ? 'It will draft missing specs, rate them, sharpen anything ' +
                         'under the bar, and ask Direction what to build. That means ' +
                         'real workers and real architect calls, one step per tick.'
                       : 'Nothing already running is stopped.'))) return;
      const r = await post('/api/spec/auto', { all: true, on });
      $('sp-status').textContent = `loop ${on ? 'on' : 'off'} for ${(r.lanes || []).length} lanes`;
      loadSpec();
    };
  }
  if ($('sp-explore')) {
    $('sp-explore').onclick = () =>
      specWork('explore', '/api/spec/explore', { lane: state.sel },
               'asking direction what to build from these documents…');
  }

  $('sp-out').querySelectorAll('.sp-go[data-rel]').forEach((b) => {
    b.onclick = () => startSpecRun(b.dataset.rel);
  });
  $('sp-out').querySelectorAll('.sp-watch').forEach((b) => {
    b.onclick = () => openWorkerLog(state.sel, b.dataset.task);
  });
  $('sp-out').querySelectorAll('.sp-refresh').forEach((b) => {
    b.onclick = () => specWork('refresh', '/api/lane/refresh', { lane: b.dataset.lane },
                               `bringing amp/${b.dataset.lane} forward…`);
  });
  $('sp-out').querySelectorAll('.sp-rate-go').forEach((b) => {
    b.onclick = () => specWork('rate', '/api/spec/rate',
                              { lane: state.sel, rel: b.dataset.rel },
                              `rating ${b.dataset.rel}…`);
  });
  if ($('sp-audit')) {
    $('sp-audit').onclick = () => specWork('audit', '/api/spec/rate', { lane: state.sel },
                                           'rating every document…');
  }
  if ($('sp-all')) {
    $('sp-all').onclick = () => specWork('all', '/api/spec/campaign', { lane: state.sel },
                                         'reviewing every document…');
  }
  if ($('sp-plan-stop')) {
    $('sp-plan-stop').onclick = () =>
      specWork('all', '/api/spec/campaign/stop', { lane: state.sel },
               'stopping the run…');
  }
  if ($('sp-draft')) {
    $('sp-draft').onclick = () => specWork('draft', '/api/spec/draft', { lane: state.sel },
                                           'sending a worker to draft one…');
  }
  $('sp-out').querySelectorAll('.sp-stop').forEach((b) => {
    b.onclick = async () => {
      b.disabled = true;
      await post('/api/spec/close', { id: b.dataset.id });
      loadSpec();
    };
  });
  // Every run row, not only the ones with a transcript still to fetch: the first
  // thing this does is record the fold, and a run whose transcript is already
  // here can be folded shut just the same.
  //
  // Older runs arrive as a summary. Their transcript is fetched the first time
  // they are unfolded, and replaces the placeholder in place - not by redrawing
  // the pane, which would close the thing that was just opened.
  const spFill = async (d) => {
    if (!d.open || d.dataset.fetch !== '1') return;
    d.dataset.fetch = '0';
    const got = await api('/api/spec/run?id=' + encodeURIComponent(d.dataset.id));
    // Kept so the next repaint draws it from here rather than asking again. Only
    // on success: caching an error would make one bad moment permanent.
    if (got.ok) state.specRounds[d.dataset.id] = got.run.rounds || [];
    const ph = d.querySelector('.sp-round');
    if (!ph) return;
    const many = ((got.run || {}).rels || []).length > 1;
    ph.outerHTML = got.ok
      ? ((got.run.rounds || []).map((rd) => specRound(rd, many, d.dataset.mine)).join('')
         || '<div class="sp-round muted">no rounds</div>')
      : `<div class="sp-round"><span class="err">${esc(got.error)}</span></div>`;
  };
  $('sp-out').querySelectorAll('.sp-run').forEach((d) => {
    d.ontoggle = () => {
      // Recorded before anything else, so that a fetch that fails still leaves
      // the fold where the operator put it.
      state.specOpen[d.dataset.key] = d.open;
      spFill(d);
    };
    // A row remembered as open is DRAWN open, and `toggle` does not fire for
    // something that was never closed - so a run unfolded before a repaint would
    // otherwise come back showing the placeholder and never fill it in.
    spFill(d);
  });

  // A run advances without anyone clicking: a writer finishes, the settle hook
  // sends the reviewer back in, and rounds appear. Poll only while one is live,
  // and only while this tab is the one being looked at - the whole reason the
  // old tab did not poll is that reading every spec on disk each tick is a cost
  // for nothing, and that is still true when nothing is running.
  clearTimeout(state.specPoll);
  // A draft worker is live too, and it is the one case where nothing on this
  // tab changes until it finishes - so without it the pane would show a running
  // worker forever and never notice the document it wrote.
  // The loop advances on the heartbeat thread with nothing clicked, so a tab
  // that only polls while a run is out would show a stale verdict for as long
  // as it was open. `busy` alone is not enough - most of the loop's time is
  // spent between steps, waiting for the next tick.
  const lp = s.auto || {};
  const live = (s.files || []).some((f) => (f.runs || []).some((r) => r.state === 'running'))
    || (s.plan || {}).state === 'running'
    || !!s.drafting
    || !!lp.busy
    // A settled lane is switched on and deliberately doing nothing, so polling
    // it every four seconds is asking a question that has already been answered.
    || (lp.on && !lp.settled && (s.state || {}).verdict !== 'solid');
  if (live && $('pane-specs').classList.contains('active')) {
    state.specPoll = setTimeout(loadSpec, 4000);
  }
}

/** One whole-lane action: disable everything, call, redraw.
 *
 *  `specWork` is a single string rather than a flag per button because these
 *  all end in an architect call or a worker, and two of them running at once is
 *  either a wasted call or a lane lock collision. One at a time is not a
 *  limitation being worked around - it is the actual constraint underneath. */
async function specWork(kind, path, body, status) {
  if (state.specWork) return;
  state.specWork = kind;
  $('sp-status').textContent = status;
  // Redraw first so the buttons read as busy for the whole call, not after it.
  await loadSpec();
  try {
    const r = await post(path, body);
    if (!r.ok) {
      $('sp-status').textContent = r.error || 'that did not work';
    } else if (kind === 'explore') {
      const n = r.proposed || 0;
      // Says PROPOSAL, and says adopt. The first version said they were "on the
      // Direction tab, waiting for you", which is true and was still read as
      // "there are new goals" - so the next place looked was the Goals tab,
      // where nothing had appeared and nothing was going to. A proposal is not a
      // goal until it is adopted, and the sentence that announces one is the
      // place to say so.
      $('sp-status').innerHTML = n
        ? `${n} <b>proposal(s)</b> on the Direction tab — not goals yet. ` +
          `Nothing starts until you adopt one. ` +
          `<button id="sp-goto-dir" class="sp-watch">open Direction</button>`
        : esc('nothing worth proposing from these documents: ' +
              ((r.why_exhausted || r.assessment || '').slice(0, 160) || 'no reason given'));
      const go = $('sp-goto-dir');
      if (go) go.onclick = () => showTab('direction');
    } else if (r.audit) {
      const a = r.audit;
      const failed = (a.failed || []).length ? ` · ${a.failed[0].error}` : '';
      $('sp-status').textContent =
        `rated ${a.rated.length}, skipped ${a.skipped.length} already current${failed}`;
    } else {
      $('sp-status').textContent = '';
    }
  } catch (e) {
    $('sp-status').textContent = String(e.message || e);
  } finally {
    state.specWork = null;
    loadSpec();
  }
}

async function loadSpec() {
  const lane = state.sel;
  if (!lane) { $('sp-out').innerHTML = '<span class="muted">pick a lane</span>'; return; }
  try {
    const r = await api('/api/spec?lane=' + encodeURIComponent(lane));
    if (!r.ok) throw new Error(r.error);
    renderSpec(r.spec || {});
  } catch (e) {
    $('sp-out').innerHTML = `<span class="err">${esc(String(e.message || e))}</span>`;
  }
}

/** The first architect turn runs inline, so this takes a while and can come
 *  back with a verdict already. `specBusy` exists so the button says so - a
 *  button that looks pressable during a 90-second call gets pressed again. */
async function startSpecRun(rel) {
  state.specBusy = rel;
  $('sp-status').textContent = `reviewing ${rel}…`;
  // Redraw first, so the button reads `starting…` for the whole call rather
  // than after it.
  await loadSpec();
  try {
    const r = await post('/api/spec/start', { lane: state.sel, rel });
    $('sp-status').textContent = r.ok ? '' : (r.error || 'could not start');
  } catch (e) {
    $('sp-status').textContent = String(e.message || e);
  } finally {
    state.specBusy = null;
    loadSpec();
  }
}

function wireSpecs() {
  $('btn-specs').onclick = loadSpec;
}

// ------------------------------------------------------------ lane modes
//
// One row per lane, and the row has to answer two questions the operator
// otherwise has to take on trust: what is this lane allowed to start, and what
// is that actually stopping right now. The second is the whole point. A mode is
// a claim about what will NOT happen, and a claim like that is unfalsifiable
// while nothing counts what it held - you set a lane to maintain and then have
// no way to tell it apart from a lane that had nothing to do anyway.

// Not `renderLanes`. That name already belongs to the lane list in the sidebar,
// and a second function declaration with the same name silently replaces the
// first - so every `renderLanes()` in `refresh()` reached this one instead,
// threw on `undefined.modes`, and left the sidebar empty with no lanes and no
// error anyone would see.
function renderLaneModes(v) {
  const modes = v.modes || [];
  const rows = (v.lanes || []).map((l) => {
    const opts = modes.map((m) =>
      `<option value="${esc(m)}"${m === l.mode ? ' selected' : ''}>${esc(m)}</option>`).join('');
    // Named goals rather than a count. "2 will finish first" is a number the
    // operator then has to go and look up; the objectives are what tells them
    // whether waiting is fine or whether they wanted to intervene.
    const flight = (l.running || []).length
      ? `<div class="lm-flight"><b>${l.running.length} already running</b>, and ` +
        `${l.running.length === 1 ? 'it will' : 'they will'} finish &mdash; the mode ` +
        `applies to what starts next.<ul>` +
        l.running.map((g) => `<li>${esc(g.objective)}</li>`).join('') + '</ul></div>'
      : '';
    // Only when it is holding something. A "held 0" on every building lane is
    // noise on ten rows to be informative on one.
    const held = l.held
      ? `<span class="lm-held" title="proposals that are scored, past both bars, and waiting on this mode alone">` +
        `holding ${l.held} proposal${l.held === 1 ? '' : 's'}</span>`
      : '';
    // Reported as held rather than as off, because they are different facts and
    // the combined boolean tells the operator they switched the loop off when
    // what is true is that they left it on and the mode is refusing it.
    const spec = l.auto_spec_held
      ? `<span class="lm-held">spec loop on, held by this mode</span>`
      : (l.auto_spec ? `<span class="muted">spec loop on</span>` : '');
    return `<div class="lm-row${l.mode === 'build' ? '' : ' lm-shut'}">` +
      `<div class="lm-name"><b>${esc(l.name)}</b>` +
      `<div class="muted">${esc(l.path || '')}` +
      (l.rung ? ` &middot; evidence at <b>${esc(l.rung)}</b>` : ' &middot; nothing judged past spec') +
      `</div></div>` +
      `<div class="lm-set"><select class="lm-mode" data-lane="${esc(l.name)}">${opts}</select>` +
      `<div class="muted">${esc(l.means || '')}</div></div>` +
      `<div class="lm-state">${held}${spec}` +
      `<span class="muted">${l.proposals} proposal${l.proposals === 1 ? '' : 's'} waiting</span>` +
      `</div>${flight}</div>`;
  }).join('');
  $('lm-out').innerHTML = rows || '<span class="muted">no lanes yet</span>';
  document.querySelectorAll('.lm-mode').forEach((s) => {
    s.onchange = () => setLaneMode(s.dataset.lane, s.value);
  });
}

async function loadLanes() {
  try {
    renderLaneModes(await api('/api/lanes'));
  } catch (e) {
    $('lm-out').innerHTML = `<span class="err">${esc(String(e.message || e))}</span>`;
  }
}

/** The reply says what is still in flight, and that is said out loud at the
 *  moment of the click. The one way this switch can lie is by looking
 *  instantaneous - the row goes grey, and an hour later a worker is still
 *  committing to that lane. */
async function setLaneMode(lane, mode) {
  $('lm-status').textContent = `setting ${lane}…`;
  const r = await post('/api/lane/mode', { lane, mode });
  if (!r.ok) {
    $('lm-status').innerHTML = `<span class="err">${esc(r.error || 'could not set it')}</span>`;
  } else {
    $('lm-status').textContent = `${lane}: ${r.means}` +
      ((r.in_flight || []).length && mode !== 'build'
        ? ` — ${r.in_flight.length} goal(s) already running will finish first` : '');
  }
  loadLanes();
  refresh();   // the board's own idea of the lanes is now one change behind
}

function wireLaneModes() {
  $('btn-lanes-reload').onclick = loadLanes;
  $('btn-set-open').onclick = openSettings;
}

// ------------------------------------------------------- pull requests
//
// Two lists that answer two different questions, and they are kept apart on
// purpose. The top one is what has been handed over and what the other side's
// automation says about it. The bottom one is what is finished here and has
// never been handed to anyone - which nothing else in this console can show,
// because the board stops caring about a goal the moment it reads `done`.
//
// The counts at the top are counted from goals and from GitHub's own replies.
// None of them is a model's summary of either, and none of them is stored.

/** One rollup, as a phrase that says what ran rather than a colour.
 *
 *  `skipped` is deliberately not folded into `passing`. A workflow whose every
 *  job was skipped reports a green tick on GitHub while having tested nothing,
 *  and that is the single most misleading state on this whole tab - it looks
 *  like the strongest evidence available and it is the absence of evidence. */
function prChecks(c) {
  if (!c) return '<span class="muted">not asked</span>';
  const n = c.total || 0;
  const cls = { passing: 'ok', failing: 'err', running: 'warn',
                skipped: 'warn', none: 'err' }[c.verdict] || 'muted';
  const words = {
    none: 'nothing is testing this',
    skipped: `all ${n} check${n === 1 ? '' : 's'} skipped &mdash; nothing actually ran`,
    failing: `${c.failed} of ${n} failing`,
    running: `${c.pending} of ${n} still running`,
    passing: `${c.passed} of ${n} passed`,
  }[c.verdict] || c.verdict;
  return `<span class="${cls}">${words}</span>`;
}

/** What one goal's definition of done is actually backed by.
 *
 *  Four numbers rather than a percentage. "no check written" and "check never
 *  run" are fixed by two different people doing two different things, and one
 *  blended score sends the operator to the wrong one half the time. */
function prTally(t) {
  if (!t) return '';
  const bits = [];
  if (t.passed) bits.push(`<span class="ok">${t.passed} proven</span>`);
  if (t.failed) bits.push(`<span class="err">${t.failed} failing</span>`);
  if (t.unrun) bits.push(`<span class="warn">${t.unrun} never run</span>`);
  if (t.unchecked) bits.push(`<span class="err">${t.unchecked} with no check</span>`);
  if (t.judgement) bits.push(`<span class="muted">${t.judgement} judgement</span>`);
  if (t.waived) bits.push(`<span class="warn">${t.waived} waived</span>`);
  return `<span class="pr-tally">${bits.join(' &middot; ')} of ${t.total}</span>`;
}

/** The one switch that runs the whole handoff without being pressed.
 *
 *  Per lane, and drawn only when a lane is on screen: this loop opens pull
 *  requests and merges them, and a control that sat over "all lanes" would be
 *  eleven of those decisions taken by one click on an unlabelled box.
 *
 *  `on` and `running` are drawn separately on purpose. The lane's mode and its
 *  stage ceiling both outrank this switch, so a ticked box next to a lane that
 *  stops at `code` would be reporting something that is not going to happen -
 *  and the sentence that says why is the useful half. */
function prAuto(v) {
  if (!v.lane) {
    const order = (v.order || []).filter((r) => r.on);
    return `<div class="pr-auto pr-auto-off"><span class="muted">` +
      `The unattended handoff is set per lane &mdash; stand on one to switch it ` +
      `on or off. ` +
      (order.length
        ? `It is running on <b>${order.map((r) => esc(r.lane)).join('</b>, <b>')}</b>.`
        : `It is not running on any lane.`) +
      `</span></div>`;
  }
  const a = v.auto || {};
  const log = (a.log || []).slice(0, 4).map((e) =>
    `<div class="pr-auto-line"><span class="muted">${esc((e.at || '').slice(0, 16))}</span> ` +
    `<b>${esc(e.what || '')}</b> ${esc(e.why || '')}</div>`).join('');
  const rank = (v.order || []).findIndex((r) => r.lane === v.lane);
  const mine = (v.order || [])[rank] || {};
  return `<div class="pr-auto">` +
    `<label><input type="checkbox" id="pr-auto-on"${a.on ? ' checked' : ''}> ` +
    `<b>Hand this lane over on its own</b></label>` +
    `<div class="muted">Takes the lanes fewest-blockers first, writes the checks that ` +
    `are missing, waives only what the architect ruled no command can decide, opens the ` +
    `pull request, and merges it once GitHub&rsquo;s own checks pass. A conflict sends a ` +
    `worker to merge the base branch in &mdash; never a force-push.</div>` +
    // Said plainly rather than implied by a greyed box. GitHub's rollup is the
    // gate, and it is not ours to write, which is the whole reason this is
    // allowed to run unattended at all.
    `<div class="muted">Our checks decide whether work may be OFFERED. GitHub&rsquo;s ` +
    `check rollup decides whether it may be MERGED, and a repository with no checks ` +
    `can never be merged into from here.</div>` +
    (a.on && !a.running
      ? `<div class="warn">Switched on, but nothing will run: ` +
        `${esc(a.why || 'the lane\u2019s mode or stage stops at an earlier rung')}.</div>`
      : '') +
    (a.busy
      ? `<div class="pr-auto-busy">working: ${esc(a.busy.what || '')} ` +
        `<span class="muted">since ${esc((a.busy.at || '').slice(0, 16))}</span></div>`
      : '') +
    `<div class="pr-auto-act">` +
    `<button id="pr-auto-step"${a.running ? '' : ' disabled'}>Run one step now</button> ` +
    `<span class="muted">${rank >= 0
      ? `${esc(v.lane)} has ${mine.blockers} blocker${mine.blockers === 1 ? '' : 's'} &mdash; ` +
        `${rank === 0 ? 'first' : `number ${rank + 1}`} of ${(v.order || []).length} ` +
        `in the order the loop takes them`
      : ''}</span></div>` +
    (log ? `<div class="pr-auto-log">${log}</div>` : '') +
    `</div>`;
}

function renderPrs(v) {
  const gh = v.github || {};
  const who = gh.ok
    ? `<span class="dep-ok">gh signed in as ${esc(gh.who || 'someone')}</span>`
    : !gh.installed
      ? `<span class="err">gh is not installed &mdash; nothing here can be asked</span>`
      : `<button class="dep-login" data-provider="github">Sign in to GitHub</button>`;

  // The headline. Nineteen finished goals and zero pull requests was the first
  // thing this view ever said, and it is the reason it exists: work that is
  // finished and has never left the machine is invisible everywhere else.
  const toll =
    `<div class="dep-toll"><b>${v.finished_unshipped} finished ` +
    `goal${v.finished_unshipped === 1 ? '' : 's'} ${v.finished_unshipped === 1 ? 'has' : 'have'} ` +
    `never been handed to anyone.</b> ` +
    // Nothing to say about how many of none could go. Narrowed to a lane this
    // is the ordinary case, and "None of them can go as things stand" read as
    // a report of trouble where there was none.
    (!v.finished_unshipped
      ? ''
      : v.handoff_ready
      ? `${v.handoff_ready} of them could go right now.`
      : `None of them can go as things stand &mdash; each row says what stops it.`) +
    (v.gaps
      ? ` ${v.gaps} done-condition${v.gaps === 1 ? '' : 's'} across them ` +
        `${v.gaps === 1 ? 'has' : 'have'} no command that decides ` +
        `${v.gaps === 1 ? 'it' : 'them'} at all.`
      : '') +
    `</div>` +
    (v.untested
      ? `<div class="dep-toll"><b>${v.untested} open pull ` +
        `request${v.untested === 1 ? '' : 's'} ${v.untested === 1 ? 'is' : 'are'} not being ` +
        `tested by anything.</b> Either no checks are configured, or every check that ` +
        `exists was skipped. A merge from here is a merge on somebody's word.</div>`
      : '');

  const open = (v.open || []).map((p) => {
    // The two actions are mutually exclusive by construction, because they are
    // answers to opposite states. `blocked` is computed on the server by the
    // same function the merge itself runs, so a row can never offer an Accept
    // that would be refused, and never hide one that would go through.
    const stuck = (p.blocked || []).length;
    const act =
      `<a class="pr-btn" href="${esc(p.url)}" target="_blank" rel="noreferrer">` +
      `View on GitHub</a>` +
      (stuck
        ? `<button class="pr-resolve" data-repo="${esc(p.repo)}" ` +
          `data-number="${esc(String(p.number))}">Resolve</button>`
        : `<button class="pr-accept" data-repo="${esc(p.repo)}" ` +
          `data-number="${esc(String(p.number))}">Accept</button>`);
    return `<div class="pr-row">` +
      `<div><a href="${esc(p.url)}" target="_blank">#${esc(String(p.number))}</a> ` +
      `<b>${esc(p.title || '')}</b>${p.draft ? ' <span class="muted">(draft)</span>' : ''}` +
      `<div class="muted">${esc(p.repo)} &middot; ${esc(p.head || '?')} &rarr; ` +
      `${esc(p.base || '?')} &middot; opened ${esc((p.at || '').slice(0, 10))}</div></div>` +
      // A pull request with no goal behind it is listed, and said out loud. It is
      // not ours to explain, but it is on a repository a lane owns, and leaving
      // it out would make this list look like the whole truth when it is not.
      `<div>${p.goal_id
        ? `<span class="muted">from ${esc(p.lane || '')} &middot; ${esc(p.goal_id)}</span>`
        : `<span class="muted">nothing here opened this</span>`}</div>` +
      `<div>${prChecks(p.checks)}</div>` +
      `<div class="pr-act">${act}</div>` +
      // Every sentence that stops the merge, on the row, before anybody presses
      // anything. The confirm quotes these too - this is so the operator does
      // not have to press a button to find out why they cannot.
      (stuck
        ? `<div class="pr-span pr-blocked">${p.blocked.map(esc).join('<br>')}</div>`
        : '') +
      `<div class="pr-span pr-log" data-prlog="${esc(prKey(p))}"></div>` +
      `</div>`;
  }).join('');

  // A repository GitHub would not answer about must never render like one with
  // nothing open. Same shape, opposite meaning.
  const quiet = (v.repos || []).filter((r) => r.why || !r.open).map((r) =>
    `<div class="pr-quiet"><b>${esc(r.repo)}</b> ` +
    `<span class="muted">${esc((r.lanes || []).join(', '))}</span> ` +
    (r.why ? `<span class="err">${esc(r.why)}</span>`
           : `<span class="muted">nothing open</span>`) + `</div>`).join('');

  const ready = (v.ready || []).map((r) => {
    const conds = (r.conditions || []).filter((c) => c.verdict !== 'passed').map((c) =>
      `<div class="pr-cond">` +
      `<div>${esc(c.text || '')}` +
      (c.check ? `<div class="muted"><code>${esc(c.check)}</code></div>` : '') +
      (c.waived
        ? `<div class="warn">waived by ${esc(c.waived.by || 'the operator')} &mdash; ` +
          `${esc(c.waived.why || 'no reason given')}</div>`
        : '') +
      `</div>` +
      `<div class="muted">${esc(c.verdict || '')}</div>` +
      `<div>${c.waived
        ? `<button class="pr-unwaive" data-goal="${esc(r.goal_id)}" ` +
          `data-text="${esc(c.text)}">Undo waiver</button>`
        : `<button class="pr-waive" data-goal="${esc(r.goal_id)}" ` +
          `data-text="${esc(c.text)}">Waive&hellip;</button>`}</div></div>`).join('');
    return `<div class="pr-goal${r.ok ? ' pr-go' : ''}">` +
      `<div class="pr-goal-head"><div><b>${esc(r.lane || '')}</b> ` +
      `<span class="muted">${esc(r.goal_id || '')}</span>` +
      `<div>${esc(r.objective || '')}</div>` +
      `<div class="muted">${r.ahead ? `${esc(String(r.ahead))} commit${r.ahead === 1 ? '' : 's'} ahead` : 'nothing ahead'}` +
      (r.stale_checks
        // Said on every row, because it is the difference between "this passed"
        // and "this passed at some point". The publish button re-runs for real.
        ? ' &middot; <span class="warn">these are stored exit codes, not a run just now</span>'
        : '') + `</div>` +
      `<div>${prTally(r.tally)}</div></div>` +
      `<div class="pr-goal-act">` +
      (r.pr_url
        ? `<a class="pr-btn" href="${esc(r.pr_url)}" target="_blank" rel="noreferrer">` +
          `View on GitHub</a>` +
          `<span class="muted">already handed over</span>`
        : (r.tally && r.tally.unchecked
            ? `<button class="pr-write" data-goal="${esc(r.goal_id)}">` +
              `Write the ${r.tally.unchecked} missing check${r.tally.unchecked === 1 ? '' : 's'}</button>`
            : '') +
          // Offered on every unshipped goal and not only the clear ones. The
          // press re-runs every done-condition for real before it pushes - so
          // a row whose stored exit codes say it is blocked can still turn out
          // to be ready, and one that looks ready can turn out not to be. A
          // button shown only on the green rows would be a button gated on the
          // stale numbers this row already warns about.
          `<button class="pr-open" data-goal="${esc(r.goal_id)}">` +
          `Open pull request</button>`) +
      `</div></div>` +
      (r.blocked && r.blocked.length
        ? `<div class="pr-blocked">${r.blocked.map((b) => esc(b)).join('<br>')}</div>`
        : `<div class="pr-blocked pr-clear">nothing is stopping this one</div>`) +
      (conds ? `<div class="pr-conds">${conds}</div>` : '') +
      `<div class="pr-log" data-prlog="${esc(r.goal_id || '')}"></div>` +
      `</div>`;
  }).join('');

  $('pr-out').innerHTML =
    `<div class="pr-who">${who}</div>` + prScope(v) + prAuto(v) + toll +
    `<h4>Open on GitHub</h4>` +
    (open ? `<div class="pr-list">${open}</div>`
          : `<span class="muted">${v.lane
              ? `nothing open belongs to ${esc(v.lane)}`
              : 'nothing is open on any repository a lane points at'}</span>`) +
    (quiet ? `<div class="pr-quiets">${quiet}</div>` : '') +
    `<h4>Finished here</h4>` +
    (ready ? ready
           : `<span class="muted">${v.lane
               ? `no ${esc(v.lane)} goal has reached done`
               : 'no goal has reached done'}</span>`);

  if ($('pr-auto-on')) {
    $('pr-auto-on').onchange = async (e) => {
      const r = await post('/api/pr/auto', { lane: v.lane, on: e.target.checked });
      // Reloaded rather than patched in place: the switch is not the only thing
      // that decides whether this runs, and the sentence saying so comes from
      // the server.
      const said = r.ok ? '' : (r.error || 'that did not take');
      // `loadPrs` clears `pr-status` when it finishes, so anything written
      // before it is wiped by the reload that was meant to confirm it. Said
      // after, or not said at all.
      await loadPrs();
      if (said) $('pr-status').textContent = said;
    };
  }
  if ($('pr-auto-step')) {
    $('pr-auto-step').onclick = async () => {
      $('pr-auto-step').disabled = true;
      $('pr-status').textContent = `running one handoff step on ${v.lane}…`;
      const r = await post('/api/pr/auto/step', { lane: v.lane });
      const said = r.ok
        ? (r.did ? `${r.did}${r.goal_id ? ` on ${r.goal_id}` : ''}`
                 : (r.note || 'nothing was waiting'))
        : (r.error || 'that step did not run');
      // After the reload, for the same reason as above. A step that did nothing
      // leaves no log line, so this sentence is the ONLY report there is.
      await loadPrs();
      $('pr-status').textContent = said;
    };
  }
  document.querySelectorAll('.dep-login').forEach((b) => {
    b.onclick = () => providerLogin(b.dataset.provider);
  });
  document.querySelectorAll('.pr-write').forEach((b) => {
    b.onclick = () => writeChecks(b.dataset.goal);
  });
  document.querySelectorAll('.pr-waive').forEach((b) => {
    b.onclick = () => waiveCondition(b.dataset.goal, b.dataset.text);
  });
  document.querySelectorAll('.pr-unwaive').forEach((b) => {
    b.onclick = () => waiveCondition(b.dataset.goal, b.dataset.text, true);
  });
  document.querySelectorAll('.pr-open').forEach((b) => {
    b.onclick = () => openPullRequest(b.dataset.goal);
  });
  document.querySelectorAll('.pr-accept').forEach((b) => {
    b.onclick = () => acceptPr(b.dataset.repo, Number(b.dataset.number));
  });
  document.querySelectorAll('.pr-resolve').forEach((b) => {
    b.onclick = () => resolvePr(b.dataset.repo, Number(b.dataset.number));
  });
}

/** What names one pull request's log line. Not the number on its own: two
 *  repositories both have a #1, and one of them would write into the other. */
const prKey = (p) => `${p.repo}#${p.number}`;

function setPrLog(gid, html) {
  const el = document.querySelector(`[data-prlog="${CSS.escape(gid)}"]`);
  if (el) el.innerHTML = html;
}

/** Ask the architect for the missing commands, show them, then keep them.
 *
 *  Two round trips and not one. The first stores nothing, which is what makes
 *  the confirm meaningful: the operator is approving the specific commands that
 *  are about to become this goal's evidence, not the idea of having some. A
 *  command that cannot fail is refused before it is ever offered, and is shown
 *  as refused so the gap is visibly still a gap. */
async function writeChecks(gid) {
  setPrLog(gid, 'asking the architect for the commands that are missing…');
  const r = await post('/api/pr/write-checks', { goal_id: gid, apply: false });
  if (!r.ok) { setPrLog(gid, `<span class="err">${esc(r.error)}</span>`); return; }
  const lines = (r.proposed || []).map((p) =>
    `${p.verdict === 'refused' ? 'REFUSED' : p.verdict === 'judgement' ? 'NO COMMAND' : 'CHECK'}` +
    `  ${p.text}\n    ${p.check || p.refused || p.why}`);
  const keep = (r.proposed || []).filter((p) => p.verdict !== 'refused').length;
  if (!keep) {
    setPrLog(gid, `<span class="err">nothing usable came back</span><pre class="dep-tail">` +
      `${esc(lines.join('\n'))}</pre>`);
    return;
  }
  setPrLog(gid, `<pre class="dep-tail">${esc(lines.join('\n'))}</pre>`);
  if (!confirm(lines.join('\n\n') +
      `\n\nStore these ${keep} and run them? They become this goal's evidence.`)) {
    setPrLog(gid, 'nothing was stored');
    return;
  }
  const a = await post('/api/pr/write-checks', { goal_id: gid, apply: true });
  if (!a.ok) { setPrLog(gid, `<span class="err">${esc(a.error)}</span>`); return; }
  const res = (a.results || []).map((x) => `exit ${x.exit}  ${x.text}`).join('\n');
  // Said out loud, because the alternative is a count that quietly does not add
  // up: the architect answered for four conditions, three were stored, and the
  // fourth named a program this machine does not have. It was tried, it never
  // ran, and it was taken back off - so that condition is still an open gap.
  const gone = (a.did_not_run || []).length
    ? `<div class="warn">${a.did_not_run.length} of them never ran on this machine ` +
      `and ${a.did_not_run.length === 1 ? 'was' : 'were'} taken back off &mdash; ` +
      `${a.did_not_run.map((b) => esc(b.check)).join(', ')}</div>`
    : '';
  setPrLog(gid, `<span class="ok">stored ${a.wrote}, ruled ${a.ruled_uncheckable} ` +
    `undecidable</span>${gone}<pre class="dep-tail">${esc(res)}</pre>`);
  loadPrs();
}

/** Sign for a condition that did not pass, or take the signature back.
 *
 *  This never marks the condition met and never changes its verdict. A waiver
 *  is a named person taking responsibility for shipping without the evidence,
 *  and it is printed in the pull request body next to the condition it is
 *  about - because the point is that whoever reads it downstream sees it too.
 *  The gate exists so that this is a decision somebody makes rather than a step
 *  they route around outside the tool, where nothing records it. */
async function waiveCondition(gid, text, undo) {
  if (undo) {
    const r = await post('/api/pr/waive', { goal_id: gid, text, undo: true });
    if (!r.ok) { setPrLog(gid, `<span class="err">${esc(r.error)}</span>`); return; }
    loadPrs();
    return;
  }
  const why = prompt(`Ship without proving this?\n\n${text}\n\n` +
    `Say why. It goes in the pull request, with your name on it.`);
  if (!why) return;
  const r = await post('/api/pr/waive', { goal_id: gid, text, why });
  if (!r.ok) { setPrLog(gid, `<span class="err">${esc(r.error)}</span>`); return; }
  loadPrs();
}

/** Hand one finished goal over: re-run every condition, then push and open it.
 *
 *  Two round trips, and the first one is not a formality. Every number on this
 *  row is a stored exit code from whenever the check last ran, which the row
 *  says out loud; the report call runs them all again in the worktree, so what
 *  gates the push is evidence from a minute ago rather than from a week ago. */
async function openPullRequest(gid) {
  setPrLog(gid, 're-running every done-condition before anything is pushed…');
  const r = await post('/api/goal/publish/report', { goal_id: gid });
  const rep = r.report || {};
  if (!rep.ok) {
    setPrLog(gid, `<span class="err">not handed over &mdash; ` +
      `${esc((rep.blocked || ['the gate refused it']).join('; '))}</span>`);
    loadPrs();
    return;
  }
  if (!confirm(`Push ${rep.branch} to ${rep.base} and open a pull request?\n\n` +
      `${rep.objective || gid}\n\n` +
      `${(rep.commits || []).length} commit(s). This leaves the machine.`)) {
    setPrLog(gid, 'nothing was pushed');
    return;
  }
  setPrLog(gid, 'pushing the branch and opening it…');
  const a = await post('/api/goal/publish', { goal_id: gid, confirm: true });
  const got = a.report || {};
  setPrLog(gid, got.pr_url
    ? `<span class="ok">opened</span> <a href="${esc(got.pr_url)}" target="_blank">${esc(got.pr_url)}</a>`
    : `<span class="err">${esc((got.blocked || ['it did not open']).join('; '))}</span>`);
  loadPrs();
}

/** Merge one pull request, on GitHub's answer about it rather than on ours.
 *
 *  The first call is a preflight and reaches GitHub for a fresh reading, which
 *  is the whole point: the row was painted when the tab loaded and every fact
 *  the gate reads - the rollup, the draft flag, whether it is still open - moves
 *  without anybody here doing anything. The confirm quotes that reading, so what
 *  is being agreed to is those specific checks and not the idea of merging. */
async function acceptPr(repo, number) {
  const key = `${repo}#${number}`;
  setPrLog(key, 'asking GitHub what it says about this one right now…');
  const p = await post('/api/pr/merge', { repo, number });
  if (!p.ok) { setPrLog(key, `<span class="err">${esc(p.error)}</span>`); return; }
  if ((p.blocked || []).length) {
    // Not a failure of the press. The state moved between the paint and the
    // click, and saying so is more use than a red row with no explanation.
    setPrLog(key, `<span class="err">will not merge: ${esc(p.blocked.join('; '))}</span>`);
    loadPrs();
    return;
  }
  const c = p.checks || {};
  if (!confirm(`Merge ${repo}#${number} (squash) and delete ${p.head}?\n\n` +
      `${p.title || ''}\n\n` +
      `GitHub says ${c.passed} of ${c.total} checks passed` +
      (c.skipped ? `, ${c.skipped} skipped` : '') + `.\n\n` +
      `This is not reversible from here.`)) {
    setPrLog(key, 'not merged');
    return;
  }
  setPrLog(key, 'merging…');
  const a = await post('/api/pr/merge', { repo, number, confirm: true, method: 'squash' });
  setPrLog(key, a.merged
    ? `<span class="ok">merged</span> <span class="muted">GitHub now says ` +
      `${esc(String(a.state_after || '').toLowerCase())}</span>`
    : `<span class="err">${esc((a.blocked || [a.error || 'it did not merge']).join('; '))}</span>` +
      (a.output ? `<pre class="dep-tail">${esc(a.output)}</pre>` : ''));
  loadPrs();
}

/** Try the cheapest thing that has not been tried on a stuck pull request.
 *
 *  One button and not three, because choosing the tier is the feature. A re-run
 *  is free and fixes a flake; a worker costs a worktree and a budget and is the
 *  answer when the branch is genuinely broken; the architect is a reading, and
 *  it is what is left when there is nobody to send. The server keeps the record
 *  of what has already been tried, so pressing this twice escalates instead of
 *  re-running the same dead workflow and reporting progress. */
async function resolvePr(repo, number) {
  const key = `${repo}#${number}`;
  setPrLog(key, 'working out the cheapest thing left to try…');
  const r = await post('/api/pr/resolve', { repo, number, tier: 'auto' });
  if (!r.ok) {
    setPrLog(key, `<span class="err">${esc(r.tier ? `${r.tier}: ` : '')}` +
      `${esc(r.error)}</span>`);
    return;
  }
  const tier = r.tier
    ? `<span class="pr-tier">${esc(r.tier)}</span> `
    : '';
  const steps = (r.steps || []).length
    ? `<pre class="dep-tail">${esc((r.steps || []).map((s) =>
        `run ${s.run} → exit ${s.exit}  ${s.said || ''}`).join('\n'))}</pre>`
    : '';
  // The architect's answer is shown in full and marked as a reading. Nothing
  // was stored and nothing ran, and a box that looked like a result would be
  // read as one.
  const g = r.reading;
  const read = g
    ? `<div class="pr-read"><b>${g.ours === false
        ? 'not this branch\u2019s fault' : 'cause'}</b>: ${esc(g.cause)}` +
      (g.fix ? `<div><b>fix</b>: ${esc(g.fix)}</div>` : '') +
      ((g.commands || []).length
        ? `<pre class="dep-tail">${esc(g.commands.join('\n'))}</pre>` : '') +
      `<div class="muted">a reading &mdash; nothing here was stored or run</div></div>`
    : '';
  setPrLog(key, tier + `<span class="muted">${esc(r.note || '')}</span>` + steps + read);
  // Only where something on GitHub or on the board actually moved. Reloading
  // after a reading would throw the reading away to repaint an identical row.
  if (r.tier === 'rerun' || r.goal_opened) loadPrs();
}

/** The lane this pane is asked about, or '' for the whole workspace. Narrowed
 *  by default and widened by the button, exactly as `depLane` is. */
function prLane() {
  return state.prAll ? '' : (state.sel || '');
}

/** What a narrowed handoff pane is NOT showing.
 *
 *  Two numbers rather than one, because a pull request withheld because it
 *  belongs to another lane and a finished goal withheld for the same reason are
 *  different things, and a single total would be a number about neither. */
function prScope(v) {
  if (!v.lane) return '';
  const loose = (v.open || []).filter((p) => !p.lane).length;
  const bits = [];
  if (v.hidden_open) {
    bits.push(`<b>${v.hidden_open}</b> open pull request` +
      `${v.hidden_open === 1 ? '' : 's'} on a shared repository belong` +
      `${v.hidden_open === 1 ? 's' : ''} to another lane`);
  }
  if (v.hidden_ready) {
    bits.push(`<b>${v.hidden_ready}</b> finished goal` +
      `${v.hidden_ready === 1 ? '' : 's'} elsewhere`);
  }
  return `<div class="dep-scope"><b>${esc(v.lane)}</b> only &mdash; every number below ` +
    `counts this lane and nothing else.` +
    (bits.length
      ? ` ${bits.join(', and ')} &mdash; <i>All lanes</i> shows ` +
        `${bits.length > 1 || v.hidden_open > 1 || v.hidden_ready > 1 ? 'them' : 'it'}.`
      : '') +
    // A pull request nothing here opened, on a branch that is not `amp/<lane>`,
    // cannot be attributed to a lane - so it is on this screen rather than
    // assigned to one by guesswork. Said only when one is actually here: as a
    // standing sentence it was a caveat about nothing on nearly every load.
    (loose
      ? ` ${loose} of the pull request${loose === 1 ? '' : 's'} below could not be ` +
        `attributed to any lane, so ${loose === 1 ? 'it is' : 'they are'} shown on ` +
        `every lane rather than assigned to one by guesswork.`
      : '') +
    `</div>`;
}

async function loadPrs() {
  const lane = prLane();
  $('btn-pr-scope').textContent = lane ? 'All lanes' : 'This lane';
  // With nothing selected there is nothing to narrow TO.
  $('btn-pr-scope').disabled = !state.sel;
  const q = lane ? `?lane=${encodeURIComponent(lane)}` : '';
  // Said out loud: this asks GitHub about every repository and reads every
  // finished goal's worktree, so the pause is real and a silent one looks broken.
  $('pr-status').textContent = lane
    ? `asking GitHub about ${lane}, and reading its finished goals…`
    : 'asking GitHub, and reading every finished goal…';
  try {
    renderPrs(await api(`/api/prs${q}`));
    $('pr-status').textContent = '';
  } catch (e) {
    $('pr-out').innerHTML = `<span class="err">${esc(String(e.message || e))}</span>`;
    $('pr-status').textContent = '';
  }
}

function wirePrs() {
  $('btn-pr-reload').onclick = loadPrs;
  $('btn-pr-scope').onclick = () => {
    state.prAll = !state.prAll;
    loadPrs();
  };
}

// ------------------------------------------------------------------- publish
//
// The providers go first and the services second, which is the opposite of what
// the data looks like and the right way round for the question. Ten services
// were listed as undeployable for two reasons between them; ten rows say "there
// are ten problems here" when there are two.

function renderDeploy(v) {
  const ids = (v.identities || []).map((i) => {
    const mine = (v.targets || []).filter((t) => t.provider === i.provider);
    // What the missing credential costs, in the only unit that matters here.
    // "not signed in" is a state; "not signed in, and nine services cannot
    // deploy because of it" is a reason to do something about it.
    const cost = mine.length
      ? `${mine.length} service${mine.length === 1 ? '' : 's'} ` +
        (i.ok ? 'can deploy' : 'cannot deploy without this')
      : 'nothing here needs it yet';
    // A tool that is not installed and a login that has not happened are
    // different problems with different fixes, and offering to sign in to
    // something that is not installed sends the operator nowhere.
    const act = i.ok
      ? `<span class="dep-ok">signed in as ${esc(i.who || 'someone')}</span>`
      : !i.installed
        ? `<span class="muted">not installed</span>`
        : `<button class="dep-login" data-provider="${esc(i.provider)}">Sign in</button>`;
    return `<div class="dep-id${i.ok ? '' : ' dep-bad'}">` +
      `<div><b>${esc(i.provider)}</b><div class="muted">${esc(cost)}</div></div>` +
      `<div class="dep-why">${i.why ? esc(i.why) : ''}` +
      (i.ok ? '' : `<div class="muted"><code>${esc(i.signin || '')}</code></div>`) +
      `</div><div>${act}</div></div>`;
  }).join('');

  const rows = (v.targets || []).map((t) =>
    `<div class="dep-row${t.signed_in ? '' : ' dep-shut'}" data-key="${esc(t.key)}">` +
    `<div><b>${esc(t.rel)}</b> ` +
    (t.package
      // Both numbers, side by side, rather than a verdict. Which of them is
      // ahead is a judgement about intent - a hotfix published from another
      // machine looks identical to a version waiting to go out - and the two
      // numbers are the fact.
      ? `<span class="muted">${esc(t.package)}@${esc(t.version)}</span>` +
        (t.unpublished
          ? ` <span class="dep-behind">not on the registry; its latest is ` +
            `${t.registry ? esc(t.registry) : 'nothing at all'}</span>`
          : '')
      : `<span class="muted">${esc(t.marker)}</span>`) +
    `<div class="dep-last">${lastPublish(t.last)}</div></div>` +
    `<div class="muted">${esc(t.provider)}</div>` +
    // A deployable service no lane owns is the case worth surfacing: it can be
    // deployed and no worker is ever going to look at it.
    `<div>${t.lane ? esc(t.lane) : '<span class="dep-orphan">no lane owns this</span>'}</div>` +
    `<div class="dep-act">` +
    `<button class="dep-check" data-key="${esc(t.key)}">Check</button>` +
    `<button class="dep-go" data-key="${esc(t.key)}">Publish</button></div>` +
    `<div class="dep-log" data-log="${esc(t.key)}"></div>` +
    `</div>`).join('');

  const head = v.stranded
    ? `<div class="dep-toll"><b>${v.stranded} of ${(v.targets || []).length} ` +
      `cannot be deployed at all.</b> The mission moves lanes onto ` +
      `<b>live_deployed</b>, and a rung moves when evidence moves it &mdash; so each ` +
      `of these is a service that cannot produce that evidence, no matter how many ` +
      `workers are pointed at it.` +
      ((v.lanes_stranded || []).length
        ? ` Lanes held by it: <b>${v.lanes_stranded.map(esc).join(', ')}</b>.` : '') +
      `</div>`
    : `<div class="dep-toll">Every deployable service has a credential that works.</div>`;

  // The other half of the toll, and the one nothing else in the console can
  // see: work that is finished, committed, and has never left this machine.
  const waiting = (v.waiting || []).length
    ? `<div class="dep-toll"><b>${v.waiting.length} package${v.waiting.length === 1 ? '' : 's'} ` +
      `${v.waiting.length === 1 ? 'carries a version' : 'carry versions'} the registry does not ` +
      `serve.</b> Nobody outside this machine can install that version. Some of these are a tree that is BEHIND the registry rather than ahead of it &mdash; the two numbers on the row say which.</div>`
    : '';

  // A lane with no deployable thing in it has no toll to report, and `head`
  // would say every service here has a working credential - of none.
  const empty = v.lane && !(v.targets || []).length;
  $('dep-out').innerHTML = depScope(v) + (empty ? '' : head + waiting + ids) +
    (rows ? `<div class="dep-list">${rows}</div>`
          : `<span class="muted">${v.lane
              ? esc(v.lane) + ' has nothing deployable in it'
              : 'nothing deployable found'}</span>`);
  document.querySelectorAll('.dep-login').forEach((b) => {
    b.onclick = () => providerLogin(b.dataset.provider);
  });
  document.querySelectorAll('.dep-check').forEach((b) => {
    b.onclick = () => runDeploy(b.dataset.key, false);
  });
  document.querySelectorAll('.dep-go').forEach((b) => {
    b.onclick = () => confirmPublish(b.dataset.key);
  });
}

/** The last publish of one target, in one line, with both halves kept apart.
 *
 *  `exit 0` and `GET … → 200` are two different claims and this never merges
 *  them into one word. A run that the tool called a success and that nothing
 *  outside the tool confirmed reads as `unverified`, which is the state that
 *  stops it being cited for `live_deployed`. */
function lastPublish(r) {
  if (!r) return '<span class="muted">never published from here</span>';
  const cls = { done: 'ok', unverified: 'warn', failed: 'err' }[r.state] || 'muted';
  const outside = r.verify
    ? `${esc(r.verify.how)} &rarr; ${esc(String(r.verify.answer))}`
    : 'nothing outside it was asked';
  return `<span class="${cls}">${esc(r.state)}</span> ` +
    `<span class="muted">${esc((r.at || '').slice(0, 16))} ` +
    `at ${esc(r.sha || 'an unnamed commit')} &middot; exit ${esc(String(r.exit))} ` +
    `&middot; ${outside}</span>`;
}

/** Ask what would stop this, show it, and make the operator say yes to THAT.
 *
 *  The confirm quotes the preflight rather than asking "are you sure?", because
 *  the thing worth confirming is not the intent, it is the specific facts: this
 *  commit, this directory, this provider, and - for npm - a version that the
 *  registry does not have yet. A dialog that says "are you sure?" is a dialog
 *  everybody clicks through. */
async function confirmPublish(key) {
  setDepLog(key, 'checking what would stop it…');
  const p = await post('/api/deploy/preflight', { key });
  if (!p.ok) { setDepLog(key, `<span class="err">${esc(p.error)}</span>`); return; }
  const pre = p.preflight;
  if (pre.blockers.length) {
    setDepLog(key, `<span class="err">will not publish: ${esc(pre.blockers.join('; '))}</span>`);
    return;
  }
  const lines = [`Publish ${key}`, `commit ${pre.sha || '(none)'}`]
    .concat(pre.notes).join('\n');
  if (!confirm(lines + '\n\nThis reaches production and costs money. Go?')) {
    setDepLog(key, 'not published');
    return;
  }
  runDeploy(key, true);
}

function setDepLog(key, html) {
  const el = document.querySelector(`[data-log="${CSS.escape(key)}"]`);
  if (el) el.innerHTML = html;
}

/** Start one run and follow it until it stops.
 *
 *  Polled rather than streamed because a Fly build takes minutes and a request
 *  held open that long is a request that dies to a proxy somewhere. Every line
 *  shown here is a line the provider printed - see `redact` on the way out. */
async function runDeploy(key, publish) {
  const r = await post('/api/deploy/run', { key, publish });
  if (!r.ok) { setDepLog(key, `<span class="err">${esc(r.error)}</span>`); return; }
  for (;;) {
    const s = await post('/api/deploy/status', { key });
    const run = s.run || {};
    const tail = (run.output || '').split('\n').slice(-8).join('\n');
    // A finished PUBLISH gets the full line, with the outside question on it. A
    // finished CHECK gets its command and its exit and nothing more, because a
    // dry run is not evidence that anything shipped and dressing it in the same
    // words would make it look like it was.
    const head = s.running
      ? `<span class="muted">${esc(run.cmd)} — running…</span>`
      : run.mode === 'publish'
        ? lastPublish(run)
        : `<span class="${run.exit === 0 ? 'ok' : 'err'}">${esc(run.cmd)} ` +
          `&rarr; exit ${esc(String(run.exit))}</span> ` +
          `<span class="muted">(a check, not a publish)</span>`;
    setDepLog(key, `${head}<pre class="dep-tail">${esc(tail)}</pre>`);
    if (!s.running) {
      // The row's own summary line is now stale, and reloading is how it gets
      // the new one - along with any npm version the registry has just started
      // serving.
      if (publish) loadDeploy();
      return;
    }
    await new Promise((k) => setTimeout(k, 1500));
  }
}

// ------------------------------------------------------ what is already live
//
// The rest of this tab answers "could this be deployed". This answers "what IS
// deployed", and it had been answering it from `wrangler.toml` - which found
// exactly one Cloudflare service on an account serving twenty-two sites out of
// this workspace, because every one of them is built by Cloudflare from a git
// push and none of them is deployed from this machine.
//
// So there is no button on these rows, deliberately. `wrangler pages deploy`
// would upload a build that no commit produced, straight past the integration
// that owns the site - a deployment nobody could trace back to a sha, which is
// the exact shape of the `live_deployed` claim this tab exists to make
// checkable. The row carries the two commits and says nothing else.

function renderPages(v) {
  if (!v.asked) {
    $('cf-out').innerHTML =
      `<div class="dep-toll">${esc(v.why || 'Cloudflare would not say')}</div>`;
    return;
  }
  // One repository often serves several sites - `WebHost.Systems` serves
  // `webhost.systems` and `app.webhost.systems`, `AmpersandBoxDesign` serves
  // two - and alphabetical rows put those nine apart. So each repository is a
  // headed group, and the header is what carries the directory: repeating it on
  // every row underneath a heading that says it is noise.
  let head_rel;
  const rows = (v.sites || []).map((s) => {
    const g = s.git_state || {};
    const live = s.live || {};
    let group = '';
    if (s.rel !== head_rel) {
      head_rel = s.rel;
      group = s.rel
        ? `<div class="cf-repo"><b>${esc(s.rel)}</b>` +
          `<span class="muted">${s.lane ? esc(s.lane) : 'no lane owns this'}</span>` +
          (s.siblings > 1
            ? `<span class="cf-many">${s.siblings} sites</span>`
            : '') +
          `</div>`
        // Not "unmatched" or "unknown": these are sites that exist and are
        // being served, and the only thing missing is a directory here.
        : `<div class="cf-repo cf-repo-out"><b>built outside this workspace</b>` +
          `<span class="muted">no directory here holds the commit they are serving` +
          `</span></div>`;
    }
    // Three states, and they are not degrees of the same thing. Level and
    // behind are both comparisons that happened; the third is the comparison
    // REFUSING to happen, and it must not read like agreement.
    const dist = !s.rel
      ? '<span class="dep-orphan">no directory here builds this</span>'
      : g.ahead === null || g.ahead === undefined
        ? `<span class="dep-unknown">${esc(g.why || 'never deployed')}</span>`
        : g.ahead === 0
          ? '<span class="dep-ok">serving HEAD</span>'
          : `<span class="dep-behind">${g.ahead} commit${g.ahead === 1 ? '' : 's'} behind</span>`;
    // A build that broke sits ABOVE the live line, not instead of it, because
    // they are two facts and the site is still serving the older one. Reading
    // the newest deployment as the live one is the mistake this section made
    // for an hour: `docs` was reported live at a commit its build never
    // finished, with a confident number beside it.
    const f = live.failed;
    const broke = f
      ? `<div class="dep-broke">last build <b>${esc(f.state || 'did not finish')}</b>` +
        (f.sha ? ` at <code>${esc(f.sha)}</code>` : '') +
        ` &mdash; so the site is still serving what is below</div>`
      : '';
    // Every custom domain, and the pages.dev one dropped: the first is what the
    // world types and the second is Cloudflare's plumbing.
    const doms = (s.domains || []).filter((d) => !d.endsWith('.pages.dev'));
    // The join that was tried and failed, kept on the row rather than dropped:
    // `zapp.bendscript.com` sits under "built outside this workspace" and the
    // obvious thing to do about that is join it to `bendscript.com/` by name.
    // This is the record of that having been tried, and refused by git.
    const no = s.refused
      ? `<div class="cf-refused">not <code>${esc(s.refused.rel)}</code> &mdash; ` +
        `that repository has no commit <code>${esc(s.refused.sha || '?')}</code>, ` +
        `so the name matching is a coincidence</div>`
      : '';
    return group + `<div class="dep-row${s.stale ? ' dep-stale' : ''}${s.broken ? ' dep-broken' : ''}">` +
      `<div><b>${esc(s.name)}</b> ` +
      `<span class="muted">${doms.length ? esc(doms.join(', ')) : 'no custom domain'}</span>` +
      broke + no +
      `<div class="dep-last">` +
      (live.sha
        ? `live <code>${esc(live.sha)}</code>` +
          (live.branch ? ` from ${esc(live.branch)}` : '') +
          (g.head ? ` &middot; here <code>${esc(g.head)}</code>` : '') +
          (live.age ? ` &middot; ${esc(live.age)}` : '')
        : '<span class="muted">no production deployment</span>') +
      `</div></div>` +
      // How this site found its repository, and only when that is worth
      // knowing. A name match is the ordinary case and says nothing; a commit
      // match is the pane asserting something git checked.
      //
      // Where the lane used to be. A lane owns the REPOSITORY, so with the
      // rows grouped it belongs in the header - printing it again on every
      // sibling underneath said "no lane owns this" three times about one
      // directory.
      `<div class="muted">${s.joined === 'commit'
        ? '<span class="cf-proved">found by its commit</span>' : ''}</div>` +
      `<div class="dep-act">${dist}</div>` +
      `</div>`;
  }).join('');

  // Above the stale count, because it is a different problem and it outranks
  // it: "push again" is the fix for behind, and it is exactly what has already
  // been tried on a site whose build is failing.
  const broken = v.broken
    ? `<div class="dep-toll dep-alarm"><b>${v.broken} site${v.broken === 1 ? '' : 's'} ` +
      `${v.broken === 1 ? 'has' : 'have'} a production build that did not finish.</b> ` +
      `Cloudflare kept serving the previous commit, so nothing is down &mdash; but the ` +
      `commit that was pushed is not live and pushing it again will not change that.` +
      ((v.lanes_broken || []).length
        ? ` Lanes holding one: <b>${v.lanes_broken.map(esc).join(', ')}</b>.` : '') +
      `</div>`
    : '';

  const head = v.stale
    ? `<div class="dep-toll"><b>${v.stale} site${v.stale === 1 ? '' : 's'} ` +
      `${v.stale === 1 ? 'is' : 'are'} serving a commit older than the tree.</b> ` +
      `That work is finished, committed, and nobody outside this machine can see it. ` +
      `These deploy on a push, so the fix is a push.` +
      ((v.lanes_stale || []).length
        ? ` Lanes holding one: <b>${v.lanes_stale.map(esc).join(', ')}</b>.` : '') +
      `</div>`
    : `<div class="dep-toll">Every site with a directory here is serving that directory&rsquo;s HEAD.</div>`;

  const orph = v.orphans
    ? `<div class="dep-toll muted">${v.orphans} of ${(v.sites || []).length} are built from ` +
      `somewhere other than this workspace. They are listed so that the number on this ` +
      `screen is the whole account rather than the part of it we recognise.` +
      (v.refused
        ? ` ${v.refused} of them ${v.refused === 1 ? 'has a name that matches' : 'have names that match'} ` +
          `a directory here and ${v.refused === 1 ? 'is' : 'are'} not built from it &mdash; ` +
          `said on the row, so the join is not tried again.`
        : '') +
      `</div>`
    : '';

  // The thing the account does that one row per site could not show: a single
  // repository is often serving several sites, so a push moves more than one of
  // them and any one of them can be behind or broken on its own.
  const many = v.multi
    ? `<div class="dep-toll muted">${v.multi} ${v.multi === 1 ? 'repository serves' : 'repositories serve'} ` +
      `more than one site &mdash; grouped below, because a commit that is live on one of them ` +
      `is not necessarily live on its neighbour.` +
      (v.by_commit
        ? ` ${v.by_commit} ${v.by_commit === 1 ? 'site found its' : 'sites found their'} repository ` +
          `by the commit it is serving rather than by its name; a subdomain never joins on the ` +
          `name alone.`
        : '') +
      `</div>`
    : '';

  // Under a lane filter there is nothing to say about levelness when the lane
  // owns no site: `head` would claim every site is serving HEAD, of none.
  const empty = v.lane && !(v.sites || []).length;
  $('cf-out').innerHTML = depScope(v) + (empty ? '' : broken + head + many + orph) +
    (rows ? `<div class="dep-list cf-list">${rows}</div>`
          : `<span class="muted">${v.lane
              ? 'no Pages site is built from ' + esc(v.lane)
              : 'no Pages projects on this account'}</span>`);
}

/** The lane this pane is asked about, or '' for the whole workspace.
 *
 *  Narrowed by default and widened by the button, never the other way round: an
 *  operator who has selected a lane is asking about that lane, and a pane that
 *  opened on thirty-eight sites to answer a question about one is why this
 *  exists. With no lane selected there is nothing to narrow TO, so it is the
 *  whole workspace regardless of what the toggle says. */
function depLane() {
  return state.depAll ? '' : (state.sel || '');
}

async function loadDeploy() {
  const lane = depLane();
  $('btn-dep-scope').textContent = lane ? 'All lanes' : 'This lane';
  // With nothing selected the button has nowhere to go, so it says so rather
  // than offering a narrowing that would empty the pane.
  $('btn-dep-scope').disabled = !state.sel;
  const q = lane ? `?lane=${encodeURIComponent(lane)}` : '';
  // Said out loud because it is slow and reaches the network: without this the
  // tab looks broken for the seconds each provider takes to answer.
  $('dep-status').textContent = lane
    ? `asking each provider ${lane} needs…` : 'asking each provider…';
  try {
    renderDeploy(await api(`/api/deploy${q}`));
    $('dep-status').textContent = '';
  } catch (e) {
    $('dep-out').innerHTML = `<span class="err">${esc(String(e.message || e))}</span>`;
    $('dep-status').textContent = '';
  }
  // After, not with. This is a wrangler start-up per project and there are
  // thirty-eight of them; making "which credential is missing" wait behind
  // "which commit is live" would put half a minute in front of the answer the
  // operator opened the tab for.
  $('cf-out').innerHTML =
    '<span class="muted">asking Cloudflare which commit each site is serving…</span>';
  try {
    renderPages(await api(`/api/deploy/pages${q}`));
  } catch (e) {
    $('cf-out').innerHTML = `<span class="err">${esc(String(e.message || e))}</span>`;
  }
}

/** What a narrowed pane is NOT showing, said in the pane.
 *
 *  Every count on this tab is a count of what was drawn, which is the only way
 *  a filtered total can be honest - but it also means a number here is smaller
 *  than the same number a moment ago, for a reason that is not progress. This
 *  line is that reason. */
function depScope(v) {
  if (!v.lane) return '';
  return `<div class="dep-scope"><b>${esc(v.lane)}</b> only &mdash; every number below ` +
    `counts this lane and nothing else.` +
    (v.hidden
      ? ` <b>${v.hidden}</b> elsewhere in the workspace ${v.hidden === 1 ? 'is' : 'are'} ` +
        `not on this screen; <i>All lanes</i> shows them.`
      : '') +
    `</div>`;
}

/** Start a browser sign-in and keep asking the PROVIDER whether it took.
 *
 *  Not the CLI. Both of these print something cheerful before the credential is
 *  usable, and a console that believed them would report a working sign-in and
 *  then watch every deploy fail. */
async function providerLogin(provider) {
  $('dep-status').textContent = `starting ${provider} sign-in — this can take a moment…`;
  const r = await post('/api/deploy/login', { provider });
  if (!r.ok) {
    $('dep-status').innerHTML = `<span class="err">${esc(r.error || 'could not start it')}</span>`;
    return;
  }
  $('dep-status').innerHTML =
    `approve it in the browser: <a href="${esc(r.url)}" target="_blank">${esc(r.url)}</a>`;
  for (let i = 0; i < 300; i++) {
    await new Promise((s) => setTimeout(s, 2000));
    const p = await post('/api/deploy/poll', {});
    if (p.state === 'connected') {
      $('dep-status').textContent = `${provider}: signed in`;
      loadDeploy();
      return;
    }
    if (p.state !== 'pending') {
      $('dep-status').innerHTML =
        `<span class="err">${esc(p.error || 'sign-in did not complete')}</span>`;
      return;
    }
  }
  $('dep-status').textContent = 'gave up waiting — press Sign in again when you are ready';
}

function wireDeploy() {
  $('btn-dep-reload').onclick = loadDeploy;
  $('btn-dep-scope').onclick = () => {
    state.depAll = !state.depAll;
    loadDeploy();
  };
}

function wireDirection() {
  $('dir-auto').onchange = async () => {
    const on = $('dir-auto').checked;
    const r = await post('/api/direction/auto', { on });
    $('dir-status').textContent = r.auto_adopt
      ? 'a review will now open the goals it proposes'
      : 'proposals wait for you';
  };
  // Fires by itself when a goal finishes; this is for the ones that finished
  // before there was anything to fire, and for asking again after a change.
  $('btn-dir-review').onclick = async () => {
    const d = state.direction || {};
    const g = (d.reviewable || [])[0];
    if (!g) { $('dir-status').textContent = 'every finished goal has been reviewed'; return; }
    const b = $('btn-dir-review');
    b.disabled = true; b.textContent = 'thinking…';
    $('dir-status').textContent = `reviewing ${g.lane}: ${g.objective.slice(0, 60)}…`;
    const r = await post('/api/direction/review', { goal_id: g.id });
    b.disabled = false; b.textContent = 'Review a finished goal';
    $('dir-status').innerHTML = r.ok ? 'reviewed' : `<span class="err">${esc(r.error || 'failed')}</span>`;
    loadDirection();
  };
}

// ------------------------------------------ workspace, mission, supervisor
//
// The header carries three things about this: which workspace you are in, what
// it is for, and whether the work still serves it. They are three because they
// fail separately - you can be in the right workspace with no mission, or with a
// mission the work stopped serving a month ago, and a single control would hide
// whichever of those was true.

const VERDICT_WORD = {
  aligned: 'on mission',
  drifting: 'drifting',
  off_mission: 'off mission',
  unknown: 'unclear',
};

function renderAlign(sup, ws) {
  const el = $('align');
  const last = (sup || {}).last;
  if (!ws || !ws.current) { el.textContent = ''; return; }
  // No mission is not a neutral state and does not get a neutral label. Nothing
  // in this console can tell you whether work is worth doing until there is one.
  if (!(ws.mission || '').trim()) {
    el.className = 'align drifting';
    el.innerHTML = '<b>no mission</b>';
    return;
  }
  if (!last) {
    el.className = 'align none';
    el.innerHTML = '<b>not read</b>';
    return;
  }
  const v = last.verdict || 'unknown';
  el.className = 'align ' + v + (sup.stale ? ' stale' : '');
  el.innerHTML = `<b>${esc(VERDICT_WORD[v] || v)}</b>` +
    (sup.stale ? ` <span class="muted">(${sup.since} change${sup.since === 1 ? '' : 's'} since)</span>` : '');
}

function renderWorkspacePick(ws) {
  const el = $('ws-pick');
  const html = (ws.list || [])
    .map((w) => `<option value="${esc(w.slug)}">${esc(w.name)}</option>`)
    .join('');
  // Same rule as the goal list: rewriting a <select> mid-interaction closes it
  // and throws away what the operator was pointing at.
  if (el._html !== html) { el._html = html; el.innerHTML = html; }
  if (el.value !== ws.current) el.value = ws.current;
  el.classList.toggle('blocked', !!ws.blocked);
  el.title = ws.blocked
    ? `cannot switch right now — ${ws.blocked}`
    : 'which set of lanes, goals and history you are looking at';
  // Standing reason, next to the control it is about. Shown BEFORE you try, not
  // only after: a select that silently springs back is indistinguishable from a
  // broken one, and that is exactly how this read.
  const why = $('ws-why');
  if (!why._flash) {
    why.textContent = ws.blocked ? `locked — ${ws.blocked}` : '';
    why.className = 'ws-why' + (ws.blocked ? ' locked' : '');
  }
}

// A refusal the operator caused, said loudly for a moment and then handed back
// to the standing reason above.
function wsWhyFlash(text) {
  const why = $('ws-why');
  why._flash = true;
  why.textContent = text;
  why.className = 'ws-why err';
  clearTimeout(why._t);
  why._t = setTimeout(() => {
    why._flash = false;
    if (state.workspace) renderWorkspacePick(state.workspace);
  }, 6000);
}

function svList(items, cls, extra) {
  return (items || [])
    .map((x) => `<div class="sv-item ${cls || ''}">` +
      (x.where ? `<span class="where">${esc(x.where)}</span> ` : '') +
      `<b>${mdi(String(x.what || ''))}</b>` +
      (x.why ? `<div class="muted">${mdi(String(x.why))}</div>` : '') +
      (extra && x[extra] ? `<div class="rec">${mdi(String(x[extra]))}</div>` : '') +
      `</div>`)
    .join('');
}

function renderSupervisor(sup) {
  const el = $('mi-sup');
  const last = (sup || {}).last;
  if (!last) {
    el.innerHTML = '<div class="muted">No reading yet. Nothing has been held against ' +
      'this mission.</div>';
    return;
  }
  const v = last.verdict || 'unknown';
  el.innerHTML =
    `<div class="verdict ${esc(v)}">${esc(VERDICT_WORD[v] || v)}` +
    `<span class="muted"> · ${esc(last.at || '')}` +
    (sup.stale ? ` · ${sup.since} change${sup.since === 1 ? '' : 's'} since` : '') +
    `</span></div>` +
    `<div class="md">${md(last.summary || '')}</div>` +
    // Drift first. It is the only part of a reading that says something here is
    // wrong, and burying it under what is going well is how it gets skimmed.
    (last.drift && last.drift.length
      ? `<h5>Not serving the mission</h5>${svList(last.drift, 'drift', 'recommend')}` : '') +
    (last.missing && last.missing.length
      ? `<h5>Called for and nobody carrying it</h5>${svList(last.missing)}` : '') +
    (last.aligned && last.aligned.length
      ? `<h5>On mission</h5>${svList(last.aligned)}` : '') +
    (last.mission_gap
      ? `<div class="sv-gap"><b>The mission does not say:</b> ${mdi(last.mission_gap)}` +
        `<div class="muted">That is a question for you. Nothing here may answer it.</div></div>`
      : '');
}

function renderWorkspaces(ws) {
  $('mi-ws-name').textContent =
    (ws.list.find((w) => w.current) || {}).name || ws.current || '—';
  $('mi-ws').innerHTML = (ws.list || [])
    .map((w) => `<div class="ws-row ${w.current ? 'cur' : ''}" data-slug="${esc(w.slug)}">` +
      `<span class="nm">${esc(w.name)}</span>` +
      (w.mission ? '' : '<span class="no-mission">no mission</span>') +
      (w.current ? '<span class="muted">here</span>'
                 : `<button class="ws-go">Switch</button>`) +
      `<button class="ws-rn">Rename</button>` +
      (w.current ? '' : `<button class="ws-rm">Remove</button>`) +
      `</div>`)
    .join('');
  $('mi-ws').querySelectorAll('.ws-row').forEach((n) => {
    const slug = n.dataset.slug;
    const go = n.querySelector('.ws-go');
    if (go) go.onclick = () => switchWorkspace(slug);
    n.querySelector('.ws-rn').onclick = async () => {
      const name = prompt('Name for this workspace', n.querySelector('.nm').textContent);
      if (!name) return;
      const r = await post('/api/workspace/rename', { slug, name });
      wsStatus(r, 'renamed');
      if (r.ok) applyWorkspace(r.workspace);
    };
    const rm = n.querySelector('.ws-rm');
    // Removing drops it from the list; the state stays on disk, and the button
    // says so rather than implying a delete that is not happening.
    if (rm) rm.onclick = async () => {
      if (!confirm(`Remove "${n.querySelector('.nm').textContent}" from the list?\n\n` +
                   'Its goals, rulings and transcripts stay on disk.')) return;
      const r = await post('/api/workspace/remove', { slug });
      wsStatus(r, 'removed from the list — its state is still on disk');
      if (r.ok) applyWorkspace(r.workspace);
    };
  });
}

function wsStatus(r, good) {
  const el = $('mi-ws-status');
  el.textContent = r.ok ? good : r.error || 'failed';
  el.className = r.ok ? 'good' : 'err';
}

function applyWorkspace(ws) {
  state.workspace = ws;
  renderWorkspacePick(ws);
  renderAlign(state.supervisor, ws);
  renderWorkspaces(ws);
  renderMove(ws);
  if ($('mi-text') !== document.activeElement) $('mi-text').value = ws.mission || '';
}

async function switchWorkspace(slug) {
  const r = await post('/api/workspace/use', { slug });
  if (!r.ok) {
    wsStatus(r, '');
    // Say it in the header too. The sheet's status line is invisible when the
    // switch was driven from the header select, which is the usual way, and a
    // refusal nobody can see is the same as no refusal at all.
    wsWhyFlash(r.error || 'could not switch');
    // The select is showing a workspace we are not in; put it back rather than
    // leaving a label that disagrees with every number under it.
    if (state.workspace) renderWorkspacePick(state.workspace);
    return;
  }
  wsStatus(r, 'switched');
  applyWorkspace(r.workspace);
  // Everything on the page belongs to the old workspace. Drop the selection and
  // reload rather than letting a stale lane, goal or thread survive the move.
  state.sel = null;
  state.goal = null;
  state.consult = null;
  // The Blueprint is workspace-scoped end to end - the layers, the triggers and
  // every prompt it builds - so its caches go too. A stack diagram left over
  // from another workspace is the exact kind of quietly-wrong this screen exists
  // to stop.
  state.bp.map = state.bp.trigs = state.bp.actions = state.bp.ctx = null;
  await refresh();
  await loadChat();
  if ($('pane-direction').classList.contains('active')) loadDirection();
  if (!$('blueprint').classList.contains('hidden')) bpShow(state.bp.view);
}

// Moving a lane is the only control in this console that deletes state in order
// to succeed: a lane leaves one config, some goal files leave one directory, and
// git relocates a checked-out worktree. So it is two buttons, not one. The first
// asks and changes nothing; the second only appears once there is an answer on
// screen to have read, and it goes away again the moment the question changes.

function renderMove(ws) {
  const lanes = (state.lanes || []).map((l) => l.name);
  const opt = (v, t) => `<option value="${esc(v)}">${esc(t)}</option>`;
  const l = $('mv-lane');
  const lh = lanes.length
    ? lanes.map((n) => opt(n, n)).join('')
    : opt('', 'no lanes in this workspace');
  if (l._html !== lh) { l._html = lh; l.innerHTML = lh; }
  const t = $('mv-to');
  const others = (ws.list || []).filter((w) => w.slug !== ws.current);
  const th = others.length
    ? others.map((w) => opt(w.slug, w.name)).join('')
    : opt('', 'there is nowhere else yet');
  if (t._html !== th) { t._html = th; t.innerHTML = th; }
  l.disabled = t.disabled = !lanes.length || !others.length;
  $('btn-mv-check').disabled = l.disabled;
}

// Any change to what is being asked invalidates the answer on screen, so the
// answer goes with it. Leaving a stale plan next to a live "Move it" is how you
// move the lane you were looking at a minute ago.
function moveReset() {
  const out = $('mv-out');
  if (out) out.innerHTML = '';
  const go = $('btn-mv-do');
  if (go) go.classList.add('hidden');
}

function wireMove() {
  $('mv-lane').onchange = moveReset;
  $('mv-to').onchange = moveReset;

  $('btn-mv-check').onclick = async () => {
    const lane = $('mv-lane').value;
    const to = $('mv-to').value;
    moveReset();
    if (!lane || !to) return;
    // The workspace this screen was showing when the choice was made, sent so
    // the harness can refuse if something else moved it in between. Another
    // console against this same state directory is a normal Wednesday here.
    const r = await post('/api/lane/move',
      { lane, to, from: (state.workspace || {}).current });
    const out = $('mv-out');
    if (!r.ok) {
      out.innerHTML = `<div class="mv-no">${esc(r.error || 'refused')}</div>`;
      return;
    }
    const left = Object.entries(r.left_behind || {});
    out.innerHTML =
      `<div class="mv-go"><b>${esc(lane)}</b> would move from ` +
      `<b>${esc(r.from)}</b> to <b>${esc(r.to)}</b>.</div>` +
      `<ul class="mv-list">` +
      `<li>${r.goals} goal${r.goals === 1 ? '' : 's'} move${r.goals === 1 ? 's' : ''} with it</li>` +
      `<li>${r.worktree
        ? 'its worktree moves too, with anything uncommitted in it'
        : 'no worktree — this lane has never been dispatched'}</li>` +
      (left.length
        ? `<li class="mv-stay">stays behind in ${esc(r.from)}: ` +
          left.map(([k, n]) => `${n} ${esc(k)}`).join(', ') + `</li>`
        : `<li>nothing else on record mentions it</li>`) +
      `</ul>`;
    $('btn-mv-do').classList.remove('hidden');
  };

  $('btn-mv-do').onclick = async () => {
    const lane = $('mv-lane').value;
    const to = $('mv-to').value;
    if (!lane || !to) return;
    const b = $('btn-mv-do');
    b.disabled = true;
    const r = await post('/api/lane/move',
      { lane, to, apply: true, from: (state.workspace || {}).current });
    b.disabled = false;
    b.classList.add('hidden');
    if (!r.ok || !r.moved) {
      $('mv-out').innerHTML =
        `<div class="mv-no">${esc(r.error || 'nothing moved')}</div>`;
      return;
    }
    $('mv-out').innerHTML =
      `<div class="mv-did"><b>${esc(lane)}</b> is now in <b>${esc(r.to)}</b>. ` +
      `Switch to that workspace to see it.</div>`;
    applyWorkspace(r.workspace);
    refresh();
  };
}

async function openMission() {
  $('mission').classList.remove('hidden');
  $('mi-status').textContent = '';
  $('mi-ws-status').textContent = '';
  moveReset();
  const r = await api('/api/workspaces');
  state.supervisor = r.supervisor;
  applyWorkspace(r.workspace);
  $('mi-text').value = r.workspace.mission || '';
  renderSupervisor(r.supervisor);
}

function wireMission() {
  $('btn-mission').onclick = openMission;
  $('btn-mi-close').onclick = () => $('mission').classList.add('hidden');
  $('ws-pick').onchange = () => switchWorkspace($('ws-pick').value);

  $('btn-mi-save').onclick = async () => {
    const st = $('mi-status');
    st.textContent = 'saving…';
    st.className = 'muted';
    const r = await post('/api/mission', { text: $('mi-text').value });
    st.textContent = r.ok
      ? 'saved — it goes out with the doctrine from the next prompt onward'
      : r.error || 'failed';
    st.className = r.ok ? 'good' : 'err';
    if (r.ok) { applyWorkspace(r.workspace); refresh(); }
  };

  $('btn-ws-add').onclick = async () => {
    const name = $('mi-new').value.trim();
    if (!name) { wsStatus({ error: 'name it first' }, ''); return; }
    const r = await post('/api/workspace/add', { name });
    wsStatus(r, `created — switch to it, then write what it is for`);
    if (r.ok) { $('mi-new').value = ''; applyWorkspace(r.workspace); }
  };

  // A reading is one architect call over the whole workspace, so it is a button
  // and not a timer. Same rule as a direction review.
  $('btn-supervise').onclick = async () => {
    const b = $('btn-supervise');
    const st = $('mi-sup-status');
    b.disabled = true;
    b.textContent = 'reading…';
    st.textContent = 'holding every goal, finding and obligation against the mission';
    st.className = 'muted';
    const r = await post('/api/supervise');
    b.disabled = false;
    b.textContent = 'Take a reading';
    if (!r.ok) {
      st.textContent = r.error || 'failed';
      st.className = 'err';
      return;
    }
    st.textContent = r.tokens ? `${r.tokens} tokens` : '';
    st.className = 'muted';
    const v = await api('/api/workspaces');
    state.supervisor = v.supervisor;
    renderSupervisor(v.supervisor);
    renderAlign(v.supervisor, v.workspace);
    loadChat();
  };
}

// ---------------------------------------------------------------- reports
//
// A report is a file, so this sheet is mostly about the file: what the next one
// would cover, and every one already taken. The page itself is not rendered
// here - it opens in its own tab, because it is meant to outlive the console.

/** What has moved since the last report, in the terms the report uses. */
function pendingLine(r) {
  const c = r.counts || {};
  const say = [
    [c.goals_closed, 'goal closed', 'goals closed'],
    [c.ladder, 'rung move', 'rung moves'],
    [c.findings, 'finding', 'findings'],
    [c.raised, 'objective proposed', 'objectives proposed'],
    [c.tasks, 'worker run', 'worker runs'],
  ].filter(([n]) => n > 0).map(([n, one, many]) => `${n} ${n === 1 ? one : many}`);
  if (!r.last) return 'No report has been taken for this workspace yet, so the first one covers everything on record.';
  if (!say.length) return `Nothing has moved since the last report, taken ${ago(r.last.at)}. Taking another would say exactly that.`;
  return `${say.join(', ')} since the last report, taken ${ago(r.last.at)}.`;
}

function renderReports(r) {
  const since = $('rp-since');
  since.textContent = pendingLine(r);
  since.className = 'rp-since' + (r.last && !r.pending ? ' quiet' : '');

  // The checkbox is only honest if the architect can actually search. Off and
  // disabled with the reason showing beats a tick that silently does nothing.
  $('rp-web').disabled = !r.can_web;
  if (!r.can_web) $('rp-web').checked = false;
  $('rp-web-why').textContent = r.can_web
    ? 'One architect turn with a web search. Every link it returns is fetched by the harness before it is printed, so an invented one shows up as unreachable rather than as a citation.'
    : 'The architect cannot search the web as configured, so this is off. Set the architect to codex in Settings to turn it on.';

  const list = r.reports || [];
  $('rp-list').innerHTML = list.length ? list.map((x) => {
    const bits = [
      x.quiet ? 'nothing moved' : Object.entries(x.counts || {})
        .filter(([, n]) => n > 0).map(([k, n]) => `${n} ${k.replace(/_/g, ' ')}`).join(', '),
      x.gates ? `${x.gates} gate(s) holding` : '',
      x.web ? 'with reading' : '',
    ].filter(Boolean);
    // Only a report that kept its own numbers can be solved. Re-deriving them
    // now would answer a question about today under an older report's heading.
    const solve = x.data_file
      ? `<button class="dm-open" data-solve="${esc(x.id)}">Solve</button>`
      : '<span class="muted" title="taken before reports kept their numbers on disk">—</span>';
    return `<div class="rp-row">
      <a href="/reports/${encodeURIComponent(x.file)}" target="_blank" rel="noreferrer noopener">#${x.nth}</a>
      <span class="muted">${esc(ago(x.at))}</span>
      <span class="rp-what">${esc(bits.join(' · '))}</span>
      ${solve}
    </div>`;
  }).join('') : '<div class="muted">none yet</div>';

  $('rp-list').querySelectorAll('[data-solve]').forEach((b) => {
    b.onclick = () => solveReport(b.dataset.solve, b);
  });
}

/** What the solver made of a report. Nothing here has been acted on. */
function renderSolved(r) {
  const box = $('rp-solved');
  const sec = (title, rows, line) => rows.length
    ? `<h4>${title}</h4><ul class="rp-sol">${rows.map(line).join('')}</ul>` : '';
  box.innerHTML = `
    <div class="rp-reading">${esc(r.assessment || '')}</div>
    ${sec('Proposed, and now waiting with the rest',
      (r.proposals || []).filter((p) => p.kind === 'goal'),
      (p) => `<li><b>${esc(p.lane)}</b> ${esc(p.text)}<br><span class="muted">${esc(p.why || '')}</span></li>`)}
    ${sec('Open questions it wants settled first',
      (r.proposals || []).filter((p) => p.kind === 'question'),
      (p) => `<li>${esc(p.text)}<br><span class="muted">${esc(p.why || '')}</span></li>`)}
    ${sec('Moves on goals already open &mdash; yours to make', r.goal_moves || [],
      (m) => `<li><button class="dm-open" data-gm="${esc(m.goal_id)}" data-lane="${esc(m.lane || '')}"
              >${esc(m.goal_id)}</button> <b>${esc(m.move)}</b> &mdash; ${esc(m.why)}</li>`)}
    ${sec('What it would change about the direction, if it could', r.direction || [],
      (x) => `<li>${esc(x.change)}<br><span class="muted">${esc(x.why)} — ${esc(x.evidence)}</span></li>`)}
    ${sec('What it would change about how goals are set', r.goal_setting || [],
      (x) => `<li>${esc(x.change)}<br><span class="muted">${esc(x.why)} — ${esc(x.evidence)}</span></li>`)}
    ${(r.misfiled || []).length
      ? `<p class="muted">Dropped, no such lane: ${r.misfiled.map((m) => esc(m.lane || '(none)')).join(', ')}</p>` : ''}
    ${r.exhausted ? `<p class="muted">Nothing to do: ${esc(r.why_exhausted || '')}</p>` : ''}`;
  box.querySelectorAll('[data-gm]').forEach((b) => {
    b.onclick = () => {
      $('report').classList.add('hidden');
      openGoal(b.dataset.lane || null, b.dataset.gm);
    };
  });
}

async function solveReport(rid, btn) {
  const st = $('rp-status');
  btn.disabled = true;
  st.textContent = 'reading the report back and working out what it asks for';
  st.className = 'muted';
  $('rp-solved').innerHTML = '';
  const r = await post('/api/report/solve', { report_id: rid });
  btn.disabled = false;
  if (!r.ok) {
    st.textContent = r.error || 'failed';
    st.className = 'err';
    return;
  }
  const n = (r.proposals || []).length;
  st.textContent = n ? `${n} proposal(s) written, none adopted` : 'nothing new to propose';
  st.className = n ? 'good' : 'muted';
  renderSolved(r);
  loadChat();
}

async function loadReports() {
  const r = await api('/api/reports');
  if (r.ok) renderReports(r);
}

async function openReports() {
  $('report').classList.remove('hidden');
  $('rp-status').textContent = '';
  $('rp-status').className = 'muted';
  await loadReports();
}

function wireReports() {
  $('btn-report').onclick = openReports;
  $('btn-rp-close').onclick = () => $('report').classList.add('hidden');
  $('btn-rp-take').onclick = async () => {
    const b = $('btn-rp-take');
    const st = $('rp-status');
    const web = $('rp-web').checked;
    b.disabled = true;
    b.textContent = 'taking…';
    st.textContent = web
      ? 'reading the workspace, then searching the web and checking every link'
      : 'reading the workspace';
    st.className = 'muted';
    const r = await post('/api/report', { web });
    b.disabled = false;
    b.textContent = 'Take a report';
    if (!r.ok) {
      st.textContent = r.error || 'failed';
      st.className = 'err';
      return;
    }
    st.textContent = `report #${r.nth} written`;
    st.className = 'good';
    await loadReports();
    window.open(r.url, '_blank', 'noreferrer');
  };
}

function openConsult(lane, cid) {
  if (lane !== state.sel) select(lane);
  state.consult = cid;          // what showTab's load will settle on
  showTab('ask');
}

/** Open a goal expanded, which is where its answer box is. */
function openGoal(lane, gid) {
  if (lane && lane !== state.sel) select(lane);
  state.goal = gid;
  showTab('goals');
}

async function sendChat() {
  const el = $('dock-text');
  const text = el.value.trim();
  if (!text) return;
  // Who is listening is decided by the mode, and only by the mode. In briefing
  // you are talking to the briefer, which reaches no lane and starts no work -
  // so this goes to a route that structurally cannot dispatch, rather than to
  // the board's own with a flag on it.
  if (state.dock === 'brief') {
    el.value = '';
    const b = await post('/api/brief', { text });
    if (!b.ok) {
      $('dock-sub').innerHTML = `<span class="err">${esc(b.error)}</span>`;
      el.value = text;
      return;
    }
    await loadChat();
    return;
  }
  const lane = $('dock-lane').value;
  el.value = '';
  const r = await post('/api/chat', { lane, text });
  if (!r.ok) {
    $('dock-sub').innerHTML = `<span class="err">${esc(r.error)}</span>`;
    el.value = text;
    return;
  }
  $('dock-sub').textContent =
    r.orchestrating ? 'thinking…' : lane === 'note' ? 'noted' : `dispatched to ${lane}`;
  await loadChat();
  if (r.dispatched) watchTask(lane, r.task_id);
}

function colorDiff(text) {
  return text
    .split('\n')
    .map((l) => {
      const e = esc(l);
      if (l.startsWith('+') && !l.startsWith('+++')) return `<span class="add">${e}</span>`;
      if (l.startsWith('-') && !l.startsWith('---')) return `<span class="del">${e}</span>`;
      if (l.startsWith('@@')) return `<span class="hunk">${e}</span>`;
      return e;
    })
    .join('\n');
}

// ---------------------------------------------------------------- data

async function refresh() {
  const s = await api('/api/state');
  state.lanes = s.lanes;
  state.health = s.health;
  state.models = s.consult_models;
  state.workers = s.workers || {};
  state.limits = s.limits || {};
  // The stage ladder is the same for every lane, so it rides once on the
  // payload rather than seven times per lane.
  state.stages = s.stages || [];
  // `stamp` carries the instant in the markup, so the 15s retimer keeps this
  // honest without another poll - and the exact time is still on hover.
  $('polled').innerHTML = s.polled_at ? `polled ${stamp(s.polled_at)}` : 'never polled';

  if (!$('d-budget').dataset.set) {
    $('d-budget').value = s.claude_budget_usd;
    $('d-model').placeholder = s.claude_model;
    $('d-budget').dataset.set = '1';
  }

  const sel = $('a-model');
  if (!sel.options.length) {
    Object.entries(s.consult_models).forEach(([k, v]) => {
      const o = document.createElement('option');
      o.value = k;
      o.textContent = `${k} — ${v.replace('openai/', '')}`;
      sel.appendChild(o);
    });
    sel.value = s.default_model;
  }

  // Blank first, and blank means the orchestrator: the default is to talk to
  // the thing that runs the board, not to pick who does the work yourself.
  // Rebuilt only when the lanes change - two overlapping refreshes would
  // otherwise reset the selection and send the next line somewhere else.
  const dl = $('dock-lane');
  const names = state.lanes.map((l) => l.name).join(',');
  if (dl.dataset.lanes !== names) {
    const keep = dl.value;
    dl.innerHTML = '<option value="">[&amp;] orchestrator</option>' +
      '<option value="note">note to self</option>' +
      state.lanes.map((l) => `<option value="${esc(l.name)}">&rarr; ${esc(l.name)}</option>`).join('');
    dl.dataset.lanes = names;
    dl.value = keep;
  }

  state.goals = s.goals || [];
  state.observations = s.observations || [];

  // Which workspace all of the above is about, and whether it is still the
  // mission. Both come free with the poll - neither costs a model call.
  if (s.workspace) {
    state.workspace = s.workspace;
    state.supervisor = s.supervisor;
    renderWorkspacePick(s.workspace);
    renderAlign(s.supervisor, s.workspace);
    // The lane list is one of the things a poll can change under this control -
    // a move, an `lane add`, another session - and until this line was here it
    // went on offering a lane that had already left. Picking it then failed with
    // "there is no lane called plugins in core", which reads like the operator
    // got it wrong when it was the screen that was out of date.
    renderMove(s.workspace);
  }

  renderHealth();
  renderSummary(s.summary || {});
  renderWatch(state.observations);
  // Fetched only when the count moves. The state poll already carries the
  // number, and the findings themselves are long enough not to want on every tick.
  const nf = (s.findings || {}).unread || 0;
  if (nf !== state.findingsSeen) {
    state.findingsSeen = nf;
    if (nf) loadFindings();
    else renderFindings((state.findings = []));
  }
  // Same rule for obligations: the summary carries the count, so the list is
  // only fetched when it changes.
  const nd = (s.summary || {}).drifted || 0;
  if (nd !== state.obligationsSeen) {
    state.obligationsSeen = nd;
    if (nd) loadObligations();
    else renderObligations((state.obligations = []));
  }
  renderRunning();
  renderLanes();
  renderGoals();
  if (!state.sel && state.lanes.length) select(state.lanes[0].name);
}

async function loadRulings() {
  const r = await api('/api/rulings');
  const el = $('r-list');
  if (!r.rulings.length) {
    el.innerHTML = '<div class="empty">No rulings yet.</div>';
    return;
  }
  el.innerHTML = r.rulings
    .map((x) => `<div class="ruling-item" data-n="${esc(x.name)}"><span>${esc(x.name)}</span><span class="muted">${x.size}b</span></div>`)
    .join('');
  el.querySelectorAll('.ruling-item').forEach((n) =>
    n.addEventListener('click', async () => {
      const d = await api('/api/ruling?name=' + encodeURIComponent(n.dataset.n));
      $('r-out').innerHTML = d.ok ? md(d.text) : `<span class="err">${esc(d.error)}</span>`;
    })
  );
}

// ---------------------------------------------------------------- actions

function busy(on, el, msg) {
  state.busy = on;
  $('btn-poll').disabled = on;
  if (el) el.innerHTML = on ? `<span class="spin">${msg}</span>` : '';
}

$('btn-poll').onclick = async () => {
  busy(true, null);
  $('polled').textContent = 'polling…';
  const r = await post('/api/poll', {});
  busy(false, null);
  if (!r.ok) {
    $('polled').innerHTML = `<span class="err">${esc(r.error)}</span>`;
    return;
  }
  await refresh();
};

async function dispatch(resume) {
  if (!state.sel) return;
  const prompt = $('d-prompt').value.trim();
  if (!prompt) {
    $('d-status').innerHTML = '<span class="err">prompt is empty</span>';
    return;
  }
  const lane = state.sel;
  // An armed session wins over "the last one", which is the whole point of
  // arming it - the session worth continuing is usually not the newest.
  if (resume && state.resumeOf && state.resumeOf.lane === lane) resume = state.resumeOf.session;
  busy(true, $('d-status'), resume ? 'replying…' : 'dispatching…');
  $('d-out').innerHTML = '';
  const r = await post('/api/dispatch', {
    lane,
    prompt,
    resume,
    backend: $('d-backend').value,
    branch: $('d-branch').value.trim(),
    budget: Number($('d-budget').value) || undefined,
    model: $('d-model').value.trim() || undefined,
    attempts: Number($('d-attempts').value) || 1,
  });
  if (!r.ok) {
    busy(false, $('d-status'));
    $('d-status').innerHTML = `<span class="err">${esc(r.error)}</span>`;
    return;
  }
  $('d-prompt').value = '';
  state.resumeOf = null;
  renderResumeChip();
  if (!r.running) {
    busy(false, $('d-status'));
    $('d-status').innerHTML = '<span class="good">dispatched</span>';
    setTimeout(() => $('btn-poll').click(), 1200);
    return;
  }
  $('d-status').innerHTML = `<span class="spin">worker running in ${esc(r.cwd)} · cap $${r.budget_usd}</span>`;
  watchTask(lane, r.task_id);
}

// Claude workers run for minutes on a server thread; poll until the board settles.
async function watchTask(lane, taskId) {
  const t0 = Date.now();
  const tick = async () => {
    const d = await api(`/api/task?lane=${encodeURIComponent(lane)}&task_id=${taskId}`);
    const task = d.task || {};
    if (task.status === 'running') {
      $('d-status').innerHTML = `<span class="spin">running ${Math.round((Date.now() - t0) / 1000)}s</span>`;
      return setTimeout(tick, 2000);
    }
    busy(false, $('d-status'));
    const ok = task.status === 'completed';
    const cost = task.cost_usd ? `$${task.cost_usd.toFixed(4)}` : '';
    $('d-status').innerHTML =
      `<span class="${ok ? 'good' : 'err'}">${esc(task.status)}</span> · ${cost} · ${task.num_turns || '?'} turns`;
    // A worker's final report is markdown - headings, a table of what changed,
    // fenced commands. It was being assigned as textContent, so every one of
    // those arrived as literal `##` and `**` in a wall of prose. This is the
    // same renderer the rulings and the architect thread already use, and it
    // escapes before it marks up, so a report still cannot become an element.
    $('d-out').innerHTML = md(task.result || task.error || '(no output)');
    refresh();
    loadChat();
  };
  tick();
}

$('btn-si-submit').onclick = submitCode;
$('btn-si-cancel').onclick = () => $('signin').classList.add('hidden');
$('si-code').onkeydown = (e) => { if (e.key === 'Enter') submitCode(); };
// Cancelling has to reach the server: the pty child is still sitting there
// polling OpenAI, and hiding the sheet would only hide it from you.
$('btn-settings').onclick = openSettings;
$('btn-notify').onclick = () => toggleNotify();
$('btn-notify-close').onclick = () => toggleNotify(false);
// Escape closes it, because the panel covers the right of the board and the
// close button is the far corner of a window this console is often only a
// third of.
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') toggleNotify(false);
});
$('btn-set-close').onclick = () => $('settings').classList.add('hidden');
$('set-openrouter').onchange = (e) => setFlag('openrouter_enabled', e.target.checked);
$('set-auto-escalate').onchange = (e) => setFlag('auto_escalate', e.target.checked);
// `input` only moves the label; the save is on `change`, which fires once when
// you let go. Saving on every pixel of a drag would be twenty writes and twenty
// re-renders for one decision.
$('set-bar').oninput = (e) => { $('set-bar-out').textContent = e.target.value + '%'; };
$('set-bar').onchange = (e) => setFlag('adopt_confidence', Number(e.target.value) / 100);
$('set-need').oninput = (e) => { $('set-need-out').textContent = e.target.value + '%'; };
$('set-need').onchange = (e) => setFlag('adopt_need', Number(e.target.value) / 100);
$('set-spec-rounds').onchange = (e) => setFlag('spec_max_rounds', Number(e.target.value));
$('set-db-mirror').onchange = (e) => dbSet('mirror', e.target.checked);
$('set-db-keep').onchange = (e) => dbSet('history_keep', e.target.value);
$('set-db-sweep').onchange = (e) => dbSet('sweep_min', e.target.value);
$('btn-db-backup').onclick = dbBackup;
$('btn-db-verify').onclick = dbVerify;
$('btn-db-prune').onclick = dbPrune;
// A plain navigation, not a fetch: the server sends it as an attachment and the
// browser saves it where the operator keeps things. Holding a 20 MB database in
// a blob first would only add a copy in memory.
$('btn-db-export').onclick = () => { window.location = '/api/db/export'; };
$('btn-cx-cancel').onclick = () => {
  cxStop();
  post('/api/codex-auth/cancel');
  $('cx-signin').classList.add('hidden');
};
$('btn-dispatch').onclick = () => dispatch(false);
$('btn-reply').onclick = () => dispatch(true);

$('btn-loaddiff').onclick = async () => {
  if (!state.sel) return;
  $('f-out').textContent = 'loading…';
  const a = $('f-attempt').value;
  const r = await api(`/api/diff?lane=${encodeURIComponent(state.sel)}${a ? '&attempt=' + a : ''}`);
  if (!r.ok) {
    $('f-out').innerHTML = `<span class="err">${esc(r.error || 'failed')}</span>`;
    return;
  }
  $('f-out').innerHTML = r.diff
    ? colorDiff(r.diff)
    : `<span class="muted">${esc(r.note || 'empty diff')}</span>`;
};

$('btn-apply').onclick = async () => {
  if (!state.sel) return;
  if (!confirm(`Apply the latest Codex diff into your local ${state.sel} worktree?`)) return;
  $('f-out').textContent = 'applying…';
  const a = $('f-attempt').value;
  const r = await post('/api/apply', { lane: state.sel, attempt: a ? Number(a) : null });
  $('f-out').innerHTML = (r.ok ? '<span class="good">applied</span>\n\n' : '<span class="err">failed</span>\n\n') + esc(r.output);
};

// ---------------------------------------------------------------- preview
//
// The frame points at a port this console started, not at a path on it: a page
// only resolves its own absolute paths - and a dev server only reaches its
// live-reload socket - at the root of an origin.

let _frameNonce = 0;

function previewActive() {
  return $('pane-preview').classList.contains('active');
}

function applyPreviewMode() {
  const cmd = $('p-mode').value === 'command';
  document.querySelectorAll('.p-cmd-only').forEach((n) => n.classList.toggle('hidden', !cmd));
}

function frameSrc(url) {
  // A distinct URL every time, because "reload" has to mean it even when the
  // browser has just been told the same address it is already showing.
  $('p-frame').src = url + (url.includes('?') ? '&' : '?') + '_amp=' + (++_frameNonce);
  $('p-frame').classList.remove('hidden');
  $('p-empty').classList.add('hidden');
}

function renderPreview(r) {
  const p = r.preview;
  state.preview = p;
  const dot = $('p-dot');
  const live = p && p.state === 'running' && p.url;
  dot.className = 'dot ' + (live ? 'ok' : p && p.state === 'starting' ? 'warn' : p && p.state === 'failed' ? 'bad' : '');
  $('p-url').textContent = live ? p.url : (p ? p.state : 'not running');
  $('p-url').href = live ? p.url : '#';
  // What is actually being served: the controls beside it describe what Start
  // would do next, which is not the same thing the moment you retarget them.
  $('p-url').title = p ? p.root + (p.cmd ? '\n$ ' + p.cmd : '') : '';
  $('p-out').textContent = p ? (p.log || []).join('\n') : '';
  if (p && p.log && p.log.length && !$('p-out').classList.contains('hidden')) {
    $('p-out').scrollTop = $('p-out').scrollHeight;
  }
  const note = (p && p.error) || r.note ||
    (r.detected ? `${r.detected.why}${r.detected.warn ? ' — ' + r.detected.warn : ''}` : '');
  $('p-status').innerHTML = (p && p.error) || r.note ? `<span class="err">${esc(note)}</span>` : esc(note);
  if (live && $('p-frame').src.indexOf(p.url) !== 0) frameSrc(p.url);
  if (!p || p.state === 'stopped') {
    $('p-frame').classList.add('hidden');
    $('p-frame').removeAttribute('src');
    $('p-empty').classList.remove('hidden');
  }
}

async function loadPreview() {
  if (!state.sel) return;
  const q = `lane=${encodeURIComponent(state.sel)}&source=${$('p-source').value}` +
    `&dir=${encodeURIComponent($('p-dir').value.trim())}`;
  const r = await api('/api/preview?' + q);
  // A running preview names its own settings; a stopped one takes the tree's
  // suggestion, so Start does the obvious thing without being configured.
  if (r.preview && r.preview.state !== 'stopped') {
    $('p-mode').value = r.preview.mode;
    $('p-cmd').value = r.preview.cmd || '';
  } else if (r.detected) {
    $('p-mode').value = r.detected.mode;
    $('p-cmd').value = r.detected.cmd || '';
    if (r.detected.dir && !$('p-dir').value.trim()) $('p-dir').value = r.detected.dir;
  }
  applyPreviewMode();
  renderPreview(r);
}

async function startPreview() {
  if (!state.sel) return;
  $('p-status').textContent = 'starting…';
  state.pstamp = null;
  const r = await post('/api/preview/start', {
    lane: state.sel, source: $('p-source').value, dir: $('p-dir').value.trim(),
    mode: $('p-mode').value, cmd: $('p-cmd').value,
  });
  if (!r.ok) {
    $('p-status').innerHTML = `<span class="err">${esc(r.error || 'failed')}</span>`;
    return;
  }
  renderPreview(r);
}

async function stopPreview() {
  if (!state.sel) return;
  await post('/api/preview/stop', { lane: state.sel });
  state.preview = null;
  state.pstamp = null;
  loadPreview();
}

/** Poll while the tab is open: a command takes a while to say where it landed,
 *  and a worker editing the tree is the whole reason to be looking. */
async function previewTick() {
  if (!previewActive() || !state.sel) return;
  const p = state.preview;
  if (!p || p.state === 'stopped') return;
  if (p.state !== 'running') return loadPreview();
  if (!$('p-auto').checked) return;
  const r = await api('/api/preview/stamp?lane=' + encodeURIComponent(state.sel));
  if (!r.ok) return loadPreview();
  if (r.state !== 'running') return loadPreview();
  if (state.pstamp && state.pstamp !== r.stamp && r.url) frameSrc(r.url);
  state.pstamp = r.stamp;
}

$('btn-p-start').onclick = startPreview;
$('btn-p-stop').onclick = stopPreview;
$('btn-p-reload').onclick = () => state.preview && state.preview.url && frameSrc(state.preview.url);
$('p-mode').onchange = applyPreviewMode;
$('p-source').onchange = () => { $('p-dir').value = ''; loadPreview(); };
$('p-dir').onchange = loadPreview;
$('btn-p-log').onclick = () => $('p-out').classList.toggle('hidden');
$('p-width').onchange = () => {
  const w = $('p-width').value;
  $('p-frame').style.width = w === '0' ? '100%' : w + 'px';
  $('p-stage').classList.toggle('narrow', w !== '0');
};

setInterval(previewTick, 1500);

// ---------------------------------------------------------------- consults
//
// A consult is one working conversation about one lane: the packet, every ruling,
// every answer you gave, and every report the worker brought back. Rounds accrue,
// so the architect keeps its own context for as long as it takes to settle.

const TURN_HEAD = {
  packet: 'packet',
  gpt: 'GPT-5.6',
  you: 'you',
  supplied: 'files supplied',
  worker: 'worker report',
  note: '',
};

function renderThread(c) {
  const el = $('a-thread');
  if (!c) {
    el.innerHTML = '<div class="empty">No thread open. Ask a question to start one.</div>';
    return;
  }
  el.innerHTML = c.turns
    .map((t) => {
      const at = (t.at || '').slice(11, 16);
      const tok = t.usage && t.usage.total_tokens ? ` · ${t.usage.total_tokens} tok` : '';
      const open = t.task_id
        ? ` <button class="dm-open" data-lane="${esc(c.lane)}" data-task="${esc(t.task_id)}">log</button>`
        : '';
      return (
        `<div class="turn ct-${esc(t.role)}">` +
        `<span class="who">${esc(TURN_HEAD[t.role] ?? t.role)} <span class="muted">${at}${tok}</span>${open}</span>` +
        `<div class="body md">${md(t.text)}</div></div>`
      );
    })
    .join('');
  el.querySelectorAll('.dm-open').forEach((b) =>
    b.addEventListener('click', () => openWorkerLog(b.dataset.lane, b.dataset.task))
  );
  el.scrollTop = el.scrollHeight;

  // What it says it is still missing, and why that is not moving by itself.
  // A thread only sits here when nobody left can answer it: files come off disk
  // and evidence is fetched by a worker, both without you. So the reason it
  // stopped is the useful half of this box, and it goes first.
  const needs = $('a-needs');
  const gathering = c.blocked_on === 'gathering';
  needs.classList.toggle('hidden', !c.needs.length && !gathering);
  needs.classList.toggle('busy', gathering);
  // Waiting on you is not something a button can fix, so only the thread's own
  // limits offer one.
  const kick = ['stalled', 'rounds', 'gather-failed', 'no-gather', null, undefined]
    .includes(c.blocked_on) && c.needs.length && !gathering;
  const why = gathering
    ? '<b>gathering</b><div>a worker is fetching this - the thread continues when it reports</div>'
    : c.blocked_why
      ? `<b>stopped</b><div>${esc(c.blocked_why)}</div>`
      : '';
  needs.innerHTML =
    why +
    (c.needs.length
      ? '<b>still needed</b>' + c.needs.map((n) => `<div>${esc(n)}</div>`).join('')
      : '') +
    (kick ? '<button id="a-continue">Continue it</button>' : '');
  const btn = $('a-continue');
  if (btn) btn.onclick = () => continueConsult(btn);
}

// Send the thread after what it is missing again: files off disk, a worker for
// anything observable. Threads opened before this existed have never been asked.
async function continueConsult(btn) {
  btn.disabled = true;
  btn.textContent = 'continuing…';
  const r = await post('/api/consult/continue', { consult_id: state.consult });
  if (!r.ok) {
    btn.textContent = r.error || 'could not continue it';
    return;
  }
  renderThread(r.consult);
  loadChat();
}

// Threads for a lane, newest first. Keeps whatever is selected if it survives.
// Loads can overlap (a lane click, a tab click and a settling worker all trigger
// one), so a later call always wins rather than whichever answers last.
let consultSeq = 0;

async function loadConsults(lane, pick) {
  const seq = ++consultSeq;
  const sel = $('a-consult');
  if (!lane) {
    sel.innerHTML = '';
    return renderThread(null);
  }
  const r = await api('/api/consults?lane=' + encodeURIComponent(lane));
  if (seq !== consultSeq) return;
  const list = r.consults || [];
  state.consults = list;
  sel.innerHTML =
    '<option value="">new thread</option>' +
    list
      .map(
        (c) =>
          `<option value="${esc(c.id)}">${(c.opened_at || '').slice(5, 16)} · ` +
          `${c.rounds} round${c.rounds === 1 ? '' : 's'}` +
          `${c.trigger === 'auto' ? ' · auto' : ''}${c.status === 'closed' ? ' · closed' : ''}` +
          ` · ${esc((c.question || '').slice(0, 60))}</option>`
      )
      .join('');
  const want = pick || state.consult || (list[0] && list[0].id) || '';
  sel.value = list.some((c) => c.id === want) ? want : '';
  await selectConsult(sel.value);
}

async function selectConsult(cid) {
  const seq = ++consultSeq;
  state.consult = cid || null;
  if (!cid) return renderThread(null);
  const r = await api('/api/consults?id=' + encodeURIComponent(cid));
  if (seq !== consultSeq) return;
  renderThread(r.ok ? r.consult : null);
}

$('a-consult').onchange = () => selectConsult($('a-consult').value);
$('btn-a-new').onclick = () => {
  $('a-consult').value = '';
  selectConsult('');
  $('a-question').focus();
};

$('btn-ask').onclick = async () => {
  if (!state.sel) return;
  const q = $('a-question').value.trim();
  if (!q) {
    $('a-status').innerHTML = '<span class="err">question is empty</span>';
    return;
  }
  const cid = state.consult;
  busy(true, $('a-status'), cid ? 'asking again…' : 'building packet, consulting GPT-5.6…');
  const r = await post('/api/ask',
    cid ? { consult_id: cid, question: q }
        : { lane: state.sel, question: q, model: $('a-model').value });
  busy(false, $('a-status'));
  if (!r.ok) {
    $('a-status').innerHTML = `<span class="err">${esc(r.error)}</span>`;
    return;
  }
  $('a-question').value = '';
  const c = r.consult;
  $('a-status').innerHTML =
    `<span class="good">${esc(c.model)}</span> · ${c.cost_tokens} tok · ${esc(c.id)}`;
  await loadConsults(state.sel, c.id);
  loadChat();
  loadRulings();
};

// The ruling goes to a worker only when you say so. The worker's report comes
// back to the architect on its own, so the next round is already waiting for you.
$('btn-relay').onclick = async () => {
  if (!state.consult) {
    $('a-status').innerHTML = '<span class="err">no thread selected</span>';
    return;
  }
  busy(true, $('a-status'), 'handing the ruling to the worker…');
  const r = await post('/api/consult/relay', {
    consult_id: state.consult,
    report_back: $('a-report').checked,
    budget: Number($('d-budget').value) || undefined,
    model: $('d-model').value.trim() || undefined,
  });
  busy(false, $('a-status'));
  if (!r.ok) {
    $('a-status').innerHTML = `<span class="err">${esc(r.error)}</span>`;
    return;
  }
  $('a-status').innerHTML = '<span class="good">worker is carrying it out</span>';
  refresh();
  loadChat();
  watchConsultTask(state.sel, r.task_id, state.consult);
};

// The worker reports back into the thread, so the thread is reloaded once it stops.
async function watchConsultTask(lane, taskId, cid) {
  const tick = async () => {
    const d = await api(`/api/task?lane=${encodeURIComponent(lane)}&task_id=${taskId}`);
    if ((d.task || {}).status === 'running') return setTimeout(tick, 3000);
    // The report and the next ruling are appended after the task settles.
    setTimeout(() => loadConsults(lane, cid), 4000);
    loadChat();
  };
  setTimeout(tick, 3000);
}

$('btn-a-close').onclick = async () => {
  if (!state.consult) return;
  await post('/api/consult/close', { consult_id: state.consult });
  $('a-status').innerHTML = '<span class="muted">thread closed</span>';
  loadConsults(state.sel);
};

// ---------------------------------------------------------------- earlier chats
//
// Most of the work this console supervises happened before it existed. These are
// the Claude Code sessions already on this disk: what each was for, where it got
// to, and what looks wrong with it.

function ago(iso) {
  const h = (Date.now() - Date.parse(iso)) / 3.6e6;
  if (!isFinite(h)) return '';
  if (h < 1) return `${Math.round(h * 60)}m ago`;
  if (h < 48) return `${Math.round(h)}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

function renderHistory() {
  const only = $('h-troubled').checked;
  const list = state.history.filter((s) => !only || s.symptoms.length);
  $('h-count').textContent = `${list.length} of ${state.history.length} shown`;
  const el = $('h-list');
  if (!list.length) {
    el.innerHTML = '<div class="empty">Nothing matched.</div>';
    return;
  }
  el.innerHTML = list
    .map(
      (s) =>
        `<div class="hist ${s.symptoms.length ? 'bad' : ''}" data-id="${esc(s.id)}">` +
        `<div class="h-top"><span class="dm-lane">${esc(s.project)}</span>` +
        `<span class="muted">${esc(ago(s.last_at))} · ${Math.round(s.size / 1000)} KB` +
        `${s.branch ? ' · ' + esc(s.branch) : ''}</span></div>` +
        `<div class="h-q">${esc(s.first_prompt || '(no opening prompt)')}</div>` +
        (s.symptoms.length
          ? `<div class="h-sym">${s.symptoms.map(esc).join(' · ')}</div>`
          : '') +
        `<div class="h-acts"><button class="h-open">Open</button>` +
        `<button class="h-note">To thread</button>` +
        `<button class="h-ask">Diagnose with GPT-5.6</button></div></div>`
    )
    .join('');

  el.querySelectorAll('.hist').forEach((n) => {
    const id = n.dataset.id;
    n.querySelector('.h-open').onclick = () => openSession(id);
    n.querySelector('.h-note').onclick = async () => {
      const r = await post('/api/history/note', { session_id: id });
      if (r.ok) loadChat();
    };
    // The architect needs a lane to read the repo from; the selected one is it.
    n.querySelector('.h-ask').onclick = async (e) => {
      const b = e.target;
      b.disabled = true;
      b.textContent = 'diagnosing…';
      const r = await post('/api/history/ask', { session_id: id, lane: state.sel });
      b.disabled = false;
      b.textContent = 'Diagnose with GPT-5.6';
      if (!r.ok) {
        $('h-out').innerHTML = `<div class="turn"><div class="body err">${esc(r.error)}</div></div>`;
        return;
      }
      loadChat();
      openConsult(r.consult.lane, r.consult.id);
    };
  });
}

async function openSession(id) {
  $('h-out').innerHTML = '<div class="empty">loading…</div>';
  const r = await api('/api/session?id=' + encodeURIComponent(id));
  if (!r.ok) {
    $('h-out').innerHTML = `<div class="empty err">${esc(r.error)}</div>`;
    return;
  }
  // Same renderer as a worker log, so an old chat reads like a new one.
  renderLog(r.events, 'h-out');
  $('h-out').scrollTop = 0;
}

async function loadHistory() {
  $('h-list').innerHTML = '<div class="empty">reading sessions…</div>';
  const r = await api('/api/history?limit=60' + ($('h-all').checked ? '&all=1' : ''));
  state.history = r.sessions || [];
  renderHistory();
}

$('btn-h-load').onclick = loadHistory;
$('h-all').onchange = loadHistory;
$('h-troubled').onchange = renderHistory;

// ---------------------------------------------------------------- blockers
//
// The pipeline, drawn as the one thing it is.
//
// Eleven panes each show one rung of this and none of them shows the joins, so
// the shape of the machine only exists in the head of whoever built it. This
// draws all seven rungs, who drives each, what state it is actually in, what is
// stopping it, and the feedback edges that make it a loop rather than a
// conveyor - which is the half that is invisible everywhere else.
//
// Two kinds of answer are useful here and they are deliberately not merged:
// `blocked` is work the harness would do if something were cleared, and `gap`
// is a rung nothing automates at all. A gap is not a fault - it is the next
// thing worth building, and calling it a fault would send you looking for a
// switch that was never wired.

const FLOW_TONE = {
  off: 'off', gap: 'gap', blocked: 'bad', running: 'run', ready: 'ready', clear: 'ok',
};

async function loadBlockers() {
  const out = $('b-out');
  if (!state.sel) {
    state.flow = null;
    out.innerHTML = '<div class="empty">Pick a lane. The flow is one lane\'s flow.</div>';
    return;
  }
  out.innerHTML = '<div class="empty">Reading&hellip;</div>';
  const r = await api('/api/flow?lane=' + encodeURIComponent(state.sel));
  if (!r || r.ok === false) {
    state.flow = null;
    out.innerHTML = `<div class="empty">${esc((r && r.error) || 'could not read the flow')}</div>`;
    return;
  }
  state.flow = r.flow;
  renderBlockers();
}

/** One rung's buttons. `post` does the thing and reloads; `tab` goes to where
 *  the thing is done. Nothing here is decorative: a button that only opened a
 *  pane says so by being drawn quieter than one that acts. */
function flowActions(f) {
  return (f.actions || []).map((a, i) =>
    `<button class="fa${a.primary ? ' primary' : ''}${a.tab ? ' goes' : ''}" ` +
    `data-stage="${esc(f.stage)}" data-i="${i}"` +
    (a.why ? ` title="${esc(a.why)}"` : '') + `>${esc(a.label)}</button>`
  ).join('');
}

function renderBlockers() {
  const out = $('b-out');
  const d = state.flow;
  if (!d) return;
  const only = $('b-only').checked;

  const actors = (d.actors || []).map((a) =>
    `<span class="ac ${a.on ? (a.ready ? 'on' : 'warn') : 'offr'}" title="${esc(a.why || '')}">` +
    `${esc(a.role)} ${a.on ? (a.ready ? '\u25cf' : '\u25cb') : 'off'}</span>`
  ).join('');

  const head =
    `<div class="flow-head">` +
    `<div class="fh-l">carried as far as <b>${esc(d.stage)}</b> in <b>${esc(d.lane)}</b>, ` +
    `which is set to <b>${esc(d.mode)}</b>` +
    (d.rung ? ` &middot; evidence judged to <b>${esc(d.rung)}</b>` : ' &middot; no evidence rung yet') +
    `</div><div class="fh-r">${actors}</div></div>` +
    (d.gates && d.gates.length
      ? `<div class="flow-gates"><b>${d.gates.length} gate(s) up across the fleet</b> &mdash; ` +
        d.gates.map((g) => `<span title="${esc(g.why)}">${esc(g.gate)}</span>`).join(', ') +
        `</div>`
      : '');

  const fb = (d.edges || []).filter((e) => e.kind === 'feedback');

  const rows = (d.flow || []).map((f, idx) => {
    const stuck = f.state === 'blocked' || f.state === 'gap' || f.state === 'off';
    if (only && !stuck) return '';
    const fwd = (d.edges || []).find((e) => e.kind !== 'feedback' && e.from === f.stage);
    const blockers = (f.blockers || []).map((b) =>
      `<div class="fb"><span class="who ${esc(b.whose)}">${esc(b.whose)}</span>` +
      `<span class="what">${esc(b.what)}</span><span class="fwhy">${esc(b.why)}</span></div>`
    ).join('');
    return (
      `<div class="frung ${FLOW_TONE[f.state] || ''}${f.is_ceiling ? ' ceiling' : ''}">` +
      `<div class="fr-head">` +
      `<span class="fst">${esc(f.label)}</span>` +
      `<span class="who ${esc(f.who)}">${esc(f.who)}</span>` +
      `<span class="fstate">${esc(f.state)}</span>` +
      `<span class="fmeans">${esc(f.means)}</span>` +
      (f.is_ceiling ? '<span class="fceil">stops here</span>' : '') +
      `</div>` +
      `<div class="fruns">${esc(f.runs)}</div>` +
      `<div class="fat">${esc(f.at)}</div>` +
      (blockers ? `<div class="fbs">${blockers}</div>` : '') +
      `<div class="facts">${flowActions(f)}</div>` +
      `</div>` +
      (fwd && idx < (d.flow || []).length - 1
        ? `<div class="fedge" title="${esc(fwd.what)}">\u2193 <span class="who ${esc(fwd.who)}">` +
          `${esc(fwd.who)}</span> ${esc(fwd.what)}</div>`
        : '')
    );
  }).join('');

  const back = fb.length
    ? `<div class="flow-back"><h4>and back round</h4>` +
      fb.map((e) =>
        `<div class="fbk"><span class="fbe">${esc(e.from)} \u21b0 ${esc(e.to)}</span>` +
        `<span class="who ${esc(e.who)}">${esc(e.who)}</span>` +
        `<span class="fwhy">${esc(e.what)}</span></div>`).join('') +
      `</div>`
    : '';

  out.innerHTML = head + `<div class="flow">${rows || '<div class="empty">Nothing is stuck.</div>'}</div>` + back;

  out.querySelectorAll('.fa').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const f = (state.flow.flow || []).find((x) => x.stage === btn.dataset.stage);
      const a = f && f.actions[Number(btn.dataset.i)];
      if (!a) return;
      if (a.tab) return showTab(a.tab);
      btn.disabled = true;
      btn.textContent = 'working\u2026';
      const r = await post(a.post, a.body);
      if (r && r.ok === false) alert(r.error || 'that did not go through');
      await refresh();
      loadBlockers();
    });
  });
}

$('btn-flow').onclick = loadBlockers;
$('b-only').onchange = renderBlockers;

function showTab(name) {
  document.querySelectorAll('.tab').forEach((x) => x.classList.toggle('active', x.dataset.tab === name));
  document.querySelectorAll('.pane').forEach((x) => x.classList.remove('active'));
  $('pane-' + name).classList.add('active');
  if (name === 'rulings') loadRulings();
  if (name === 'log' && state.sel) fillSessions(state.sel);
  if (name === 'ask' && state.sel) loadConsults(state.sel);
  if (name === 'goals') renderGoals();   // which reloads the open goal itself now
  // Loaded on open, not on every poll: it reads a file and a store off disk, and
  // nothing in it changes between ticks except when a goal finishes.
  if (name === 'direction') loadDirection();
  // On open, not on the poll. It reads only what is already on disk, so it is
  // cheap - but the buttons on it act, and redrawing them under a hand that is
  // reaching for one is its own kind of wrong.
  if (name === 'blockers') loadBlockers();
  if (name === 'specs') loadSpec();
  if (name === 'history' && !state.history.length) loadHistory();
  if (name === 'preview' && state.sel) loadPreview();
  // Every time, not once: the counts are the point, and a stale `holding 3` is
  // worse than no number at all.
  if (name === 'settings') loadLanes();
  // On open and nowhere else. Every row here costs a subprocess that reaches the
  // network, and none of it changes on its own - a credential changes when the
  // operator signs in, which is the moment they are looking at this tab.
  if (name === 'publish') loadDeploy();
  // Same rule, and for a stronger reason: this one asks GitHub about every
  // repository AND shells into every finished goal's worktree. Polling it on a
  // timer would put that load on the network and on other lanes' trees forever,
  // to answer a question nobody is currently reading the answer to.
  if (name === 'prs') loadPrs();
}

document.querySelectorAll('.tab').forEach((t) =>
  t.addEventListener('click', () => showTab(t.dataset.tab))
);

$('btn-loadlog').onclick = () => loadLog();
$('l-task').onchange = () => loadLog();
$('l-thinking').onchange = () => state.log && renderLog(state.log);

$('dock-send').onclick = sendChat;
$('dock-text').onkeydown = (e) => { if (e.key === 'Enter') sendChat(); };
$('dock-toggle').onclick = () => {
  const collapsed = $('dock').classList.toggle('collapsed');
  $('dock-toggle').textContent = collapsed ? '+' : '\u2212';
};

// `reload` is false for the one call that only dresses the switch to match what
// was restored: boot repaints the feed a few lines further down anyway, and a
// second load in briefing mode is a second walk of the board for one answer.
function setDockMode(mode, reload = true) {
  state.dock = mode;
  localStorage.setItem('amp.dock', mode);
  $('dock-mode').querySelectorAll('button').forEach((b) =>
    b.classList.toggle('on', b.dataset.mode === mode));
  // No caption here, on purpose. `#dock-sub` belongs to the BOARD - it is
  // rewritten on every poll with what is waiting on you - so a mode label
  // written into it is erased within seconds, and until it is, it is sitting on
  // top of live state. The switch already shows which mode is on, and it is on
  // screen whether the dock is open or shut.
  //
  // The composer changes hands with the mode, so it has to SAY so. A lane
  // picker in briefing mode would be a control that quietly does nothing: the
  // briefer reaches no lane, and offering the choice is a promise the mode
  // cannot keep.
  $('dock-lane').classList.toggle('hidden', mode === 'brief');
  $('dock-text').placeholder = mode === 'brief'
    ? 'ask about what\u2019s going on'
    : 'ask, or say what you want done';
  // Switching while the dock is shut asks for something you cannot see. Open
  // it: the press said "show me", and the only reading that honours it is to
  // show it.
  if ($('dock').classList.contains('collapsed')) $('dock-toggle').click();
  if (reload) loadChat();
}

$('dock-mode').querySelectorAll('button').forEach((b) =>
  b.addEventListener('click', () => setDockMode(b.dataset.mode)));
setDockMode(state.dock, false);

// ------------------------------------------------------------ lane column width
//
// Kept in the browser, like the dock mode: which column is wider is what this
// person wants to look at, not a fact about the workspace. Clamped rather than
// free, because a column dragged to zero cannot be dragged back - the grip goes
// with it.

const LANE_W_MIN = 200;
const LANE_W_MAX = 720;
const LANE_W_DEFAULT = 340;

// The width actually in force, so the drag never has to read it back out of a
// computed style and re-parse its own output.
let laneW = LANE_W_DEFAULT;

function setLaneWidth(px, save) {
  laneW = Math.round(Math.min(LANE_W_MAX, Math.max(LANE_W_MIN, px)));
  document.documentElement.style.setProperty('--lane-w', `${laneW}px`);
  if (save) localStorage.setItem('amp.lanew', String(laneW));
}

function wireLaneGrip() {
  const saved = parseInt(localStorage.getItem('amp.lanew') || '', 10);
  setLaneWidth(isFinite(saved) ? saved : LANE_W_DEFAULT, false);

  const grip = $('lane-grip');
  // Whether a drag is under way, held here rather than asked of the pointer.
  // `hasPointerCapture` would be the tidier gate, but capture is a request the
  // browser can decline - and a declined capture would leave a grip that looks
  // draggable and silently is not.
  let dragging = false;

  grip.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    dragging = true;
    // Capture is still asked for: the pointer spends the whole drag over the
    // pane to the right, and with it the moves keep arriving here anyway.
    try { grip.setPointerCapture(e.pointerId); } catch (_) { /* moves still land below */ }
    document.body.classList.add('resizing');
  });
  // Listened for on the window, not the grip: if capture was declined the
  // moves land on whatever is under the cursor, and the drag has to survive it.
  window.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    setLaneWidth(e.clientX - document.querySelector('main').getBoundingClientRect().left, false);
  });
  const end = (e) => {
    if (!dragging) return;
    dragging = false;
    try { grip.releasePointerCapture(e.pointerId); } catch (_) { /* never held it */ }
    document.body.classList.remove('resizing');
    // Written once, at the end. Saving on every move would put a few hundred
    // writes through localStorage for one drag.
    setLaneWidth(laneW, true);
  };
  window.addEventListener('pointerup', end);
  window.addEventListener('pointercancel', end);
  grip.addEventListener('dblclick', () => setLaneWidth(LANE_W_DEFAULT, true));
}

// ============================================================== blueprint ===
//
// Three views over one question: does the thing that is about to run actually
// know what it needs to know? The Map answers it structurally, the Triggers
// answer it causally, and the Context answers it literally - by building the
// real prompt and taking it apart.
//
// Nothing in here dispatches. The one control that writes anything at all
// writes an UNSCORED proposal into the same queue every other proposer writes
// into, which is why it can be a diagram and not a foot-gun.

state.bp = {
  view: 'flow', map: null, trigs: null, actions: null, ctx: null,
  node: null, open: new Set(), sel: new Set(),
  flow: { lens: 'agents', level: 'lane', cy: null, panels: {}, pick: null, land: null },
};

function openBlueprint() {
  $('blueprint').classList.remove('hidden');
  document.querySelector('main').classList.add('hidden');
  bpShow(state.bp.view);
}

function closeBlueprint() {
  $('blueprint').classList.add('hidden');
  document.querySelector('main').classList.remove('hidden');
}

function bpShow(view) {
  state.bp.view = view;
  document.querySelectorAll('.bp-tab').forEach((t) =>
    t.classList.toggle('active', t.dataset.bp === view));
  document.querySelectorAll('.bp-pane').forEach((p) =>
    p.classList.toggle('active', p.id === 'bp-pane-' + view));
  if (view === 'flow') loadBpFlow();
  if (view === 'map' && !state.bp.map) loadBpMap();
  if (view === 'triggers' && !state.bp.trigs) loadBpTriggers();
  if (view === 'context' && !state.bp.actions) loadBpActions();
}

// ------------------------------------------------------------------- flow
//
// One canvas, three lenses, and they are deliberately the same three things
// the panes beside them list. This is a LENS, not a fourth source of truth:
// the server builds the elements out of `map_view`, `triggers_view` and
// `context_view`, the very functions the lists render, so a picture that
// disagreed with a list would be a bug in one place rather than a difference
// of opinion between two.
//
// Nothing here writes. Clicking an element says what it is; that is all.

const BP_FLOW_STYLE = [
  { selector: 'node', style: {
    'background-color': '#1d2430', 'border-width': 1, 'border-color': '#33405a',
    label: 'data(label)', color: '#c9d4e4', 'font-size': 11,
    'text-valign': 'center', 'text-halign': 'center', 'text-wrap': 'wrap',
    'text-max-width': '120px', shape: 'round-rectangle',
    width: 'label', height: 24, padding: '8px',
  } },
  { selector: 'node.layer', style: {
    'background-color': '#141922', 'background-opacity': 0.6,
    'border-color': '#3b4d6e', 'border-width': 1, 'text-valign': 'top',
    color: '#8fa4c2', 'font-size': 12, 'font-weight': 'bold', padding: '18px',
    shape: 'round-rectangle',
  } },
  { selector: 'node.layer.loose', style: { 'border-style': 'dashed', color: '#a0761f' } },
  // Amber, and only here: a lane no review has ever ruled on is the single most
  // useful thing on this canvas, and it is invisible if it looks like the rest.
  { selector: 'node.unjudged', style: { 'border-color': '#a0761f', 'border-style': 'dashed' } },
  { selector: 'node.port', style: {
    'background-color': '#22303f', 'border-color': '#4b7fb5', 'border-width': 2,
    shape: 'round-diamond', color: '#9ec6ee', padding: '14px',
  } },
  { selector: 'node.call', style: {
    'background-color': '#243024', 'border-color': '#4f7a4f', 'border-width': 2,
    color: '#bfe0bf', padding: '12px',
  } },
  { selector: 'node.block.inline', style: { 'border-style': 'dotted' } },
  { selector: 'node.block.empty', style: {
    'border-color': '#a0761f', 'background-color': '#2a2317', color: '#e0b357',
  } },
  { selector: 'edge', style: {
    width: 1.4, 'line-color': '#3a465c', 'target-arrow-color': '#3a465c',
    'target-arrow-shape': 'triangle', 'curve-style': 'bezier',
    label: 'data(label)', 'font-size': 9, color: '#7d8ba1',
    'text-background-color': '#0f131a', 'text-background-opacity': 0.85,
    'text-background-padding': '2px', 'arrow-scale': 0.8,
  } },
  { selector: 'edge.contains', style: { 'line-style': 'dashed', 'line-color': '#4a5a74' } },
  { selector: 'edge.represents', style: { 'line-color': '#6a5a8a', 'target-arrow-color': '#6a5a8a' } },
  // Armed and off must not be a shade apart. An armed trigger fires on the next
  // tick; an off one does nothing, ever, and telling them apart at a glance is
  // the entire reason to draw this.
  { selector: 'edge.trig.armed', style: {
    'line-color': '#4f9d5f', 'target-arrow-color': '#4f9d5f', width: 2.4,
  } },
  { selector: 'edge.trig.off', style: {
    'line-color': '#4a5468', 'target-arrow-color': '#4a5468', 'line-style': 'dashed',
  } },
  { selector: 'edge.trig.broken', style: {
    'line-color': '#b4574f', 'target-arrow-color': '#b4574f',
  } },
  { selector: 'edge.waiting', style: { 'line-style': 'dotted', width: 2.4 } },
  { selector: 'edge.feeds.empty', style: {
    'line-color': '#a0761f', 'target-arrow-color': '#a0761f', 'line-style': 'dashed',
  } },

  // --- the agents lens ---
  // Two shapes and one rule: an actor is a rounded box with its model under its
  // name, a rung is a squarer one with the actor holding it under its name. The
  // second line is the whole point of the lens, so it is drawn IN the node
  // rather than left to the panel.
  { selector: 'node.actor', style: {
    'background-color': '#1b2733', 'border-color': '#41607e', 'border-width': 2,
    color: '#cfe2f2', shape: 'round-rectangle', height: 'label', padding: '12px',
    label: 'data(label)', 'font-size': 12, 'font-weight': 'bold',
  } },
  { selector: 'node.actor[sub]', style: {
    label: (n) => `${n.data('label')}\n${n.data('sub')}`, 'line-height': 1.35,
  } },
  { selector: 'node.actor.off', style: {
    'border-color': '#4a5468', 'border-style': 'dashed', color: '#6f7b8c',
    'background-color': '#151a21',
  } },
  // Switched on and unable to answer is not the same as switched off, and it is
  // the failure that otherwise looks like nothing happening at all.
  { selector: 'node.actor.broken', style: {
    'border-color': '#b4574f', 'border-style': 'solid', color: '#e6a49e',
  } },
  { selector: 'node.subject', style: {
    'background-color': '#241f2e', 'border-color': '#6a5a8a', 'border-width': 2,
    color: '#d3c6ea', height: 'label', padding: '12px', 'font-weight': 'bold',
    label: (n) => `${n.data('label')}\n${n.data('sub')}`, 'line-height': 1.35,
  } },
  { selector: 'node.stage', style: {
    'background-color': '#141a22', 'border-color': '#33405a', shape: 'rectangle',
    height: 'label', padding: '10px', 'font-size': 11,
    label: (n) => `${n.data('label')}\n${n.data('sub')}`, 'line-height': 1.35,
  } },
  { selector: 'node.stage.running', style: { 'border-color': '#4f9d5f', 'border-width': 2 } },
  { selector: 'node.stage.ready', style: { 'border-color': '#4f9d5f' } },
  { selector: 'node.stage.blocked', style: { 'border-color': '#b4574f', 'border-width': 2 } },
  { selector: 'node.stage.gap', style: { 'border-color': '#a0761f', 'border-style': 'dashed' } },
  // Above the ceiling. Faded rather than hidden: the rungs still exist and the
  // work still has to pass through them, it just will not start here.
  { selector: 'node.stage.out', style: { opacity: 0.42, 'border-style': 'dashed' } },
  { selector: 'node.stage.ceiling', style: {
    'border-color': '#e0b357', 'border-width': 3, color: '#f0ddb0',
  } },
  { selector: 'edge.performs', style: {
    'line-style': 'dashed', 'line-color': '#41607e', 'target-arrow-color': '#41607e',
    width: 1.2, 'arrow-scale': 0.6, opacity: 0.75,
  } },
  { selector: 'edge.hand', style: {
    'line-color': '#5b7091', 'target-arrow-color': '#5b7091', width: 2,
  } },
  // The half that makes it a loop. Curved away from the forward chain on
  // purpose - drawn straight it lands underneath the arrow it is answering.
  { selector: 'edge.hand.feedback', style: {
    'line-color': '#6a5a8a', 'target-arrow-color': '#6a5a8a', 'line-style': 'dashed',
    'curve-style': 'unbundled-bezier', 'control-point-distances': [-90],
    'control-point-weights': [0.5], width: 1.6,
  } },
  { selector: 'edge.subject-edge', style: {
    'line-color': '#6a5a8a', 'target-arrow-color': '#6a5a8a', width: 1.6,
  } },
  { selector: 'edge.watches', style: {
    'line-style': 'dotted', 'line-color': '#6a5a8a', 'target-arrow-color': '#6a5a8a',
    width: 1.2, 'arrow-scale': 0.6, opacity: 0.8,
  } },
  // Advice is drawn thinner and dashed than a hand-off on purpose. The two are
  // the claim this screen is most able to overstate: a hand-off moves the work,
  // and advice moves nothing at all until somebody acts on it.
  { selector: 'edge.advises', style: {
    'line-style': 'dashed', 'line-color': '#8a7ab0', 'target-arrow-color': '#8a7ab0',
    width: 1.2, 'arrow-scale': 0.7, opacity: 0.85,
  } },

  // --- the levels above one lane ---
  // A loop node is the same rounded box as an actor, because at these levels the
  // thing that runs IS a call rather than somebody holding a rung.
  { selector: 'node.loop', style: {
    'background-color': '#141d26', 'border-color': '#41607e', 'border-width': 2,
    color: '#cfe2f2', shape: 'round-rectangle', height: 'label', padding: '11px',
    label: (n) => `${n.data('label')}\n${n.data('sub')}`, 'line-height': 1.35,
    'font-size': 12,
  } },
  { selector: 'node.loop.measure', style: {
    'background-color': '#1e2418', 'border-color': '#5f7d3f', color: '#cfe0bb',
  } },
  { selector: 'node.loop.lanes, node.loop.work', style: {
    'background-color': '#241f2e', 'border-color': '#6a5a8a', color: '#d3c6ea',
  } },
  { selector: 'node.loop.advisory', style: {
    'border-color': '#6a5a8a', 'border-style': 'dashed',
  } },
  // Drafts write nothing until they are accepted, and the border says so before
  // the panel is opened.
  { selector: 'node.loop.draft', style: {
    'border-style': 'dashed', 'border-color': '#a0761f', color: '#e8d4a8',
  } },
  { selector: 'node.written', style: {
    'background-color': '#14181f', 'border-color': '#3b4756', shape: 'rectangle',
    height: 'label', padding: '9px', 'font-size': 11, color: '#aab6c5',
    label: (n) => `${n.data('label')}\n${n.data('sub')}`, 'line-height': 1.3,
  } },
  { selector: 'node.ws', style: {
    'background-color': '#171c23', 'border-color': '#3b4756', 'border-width': 2,
    color: '#c3cddb', shape: 'round-rectangle', height: 'label', padding: '13px',
    label: (n) => `${n.data('label')}\n${n.data('sub')}`, 'line-height': 1.35,
    'font-weight': 'bold',
  } },
  { selector: 'node.ws.current', style: {
    'border-color': '#e0b357', 'border-width': 3, color: '#f0ddb0',
  } },
  { selector: 'edge.port', style: {
    'line-color': '#5b7091', 'target-arrow-color': '#5b7091', width: 2,
  } },
  // Waiting is not failing - the far end simply has not admitted it. Amber
  // rather than red, and it is the count that is on the label.
  { selector: 'edge.port.waiting', style: {
    'line-color': '#a0761f', 'target-arrow-color': '#a0761f', 'line-style': 'dashed',
  } },

  { selector: '.picked', style: { 'border-color': '#e0e6ef', 'border-width': 3 } },
  // An edge has no border, so the node rule above did nothing to it and a picked
  // arrow looked identical to an unpicked one - which now matters, because
  // arrows carry panels too.
  { selector: 'edge.picked', style: {
    'line-color': '#e0e6ef', 'target-arrow-color': '#e0e6ef', 'line-style': 'solid',
    width: 3, opacity: 1,
  } },
];

const BP_FLOW_LAYOUT = {
  // Both hand-laid lenses use `preset`, for the same reason and a different one.
  // Agents is a pipeline: it reads left to right or it does not read, and there
  // is no layout that can be told that.
  agents: { name: 'preset', padding: 30 },
  // The map lens is laid out by hand - see `bpMapPositions`. Every core
  // cytoscape layout treats a compound parent as one more node to push around,
  // so the layers land on top of each other and the graph reads as a smear.
  map: { name: 'preset', padding: 30 },
  triggers: { name: 'breadthfirst', directed: true, animate: false, padding: 30,
    spacingFactor: 1.3 },
  context: { name: 'breadthfirst', directed: true, animate: false, padding: 20,
    spacingFactor: 1.1 },
};

// The Agents lens is four pictures, not one, and they do not want the same
// layout. Three are hand-laid for the same reason the pipeline is - each says
// something about order or about sides - and `workspaces` is the one that is
// genuinely a graph of peers, so a circle is the honest shape for it.
const BP_LEVEL_LAYOUT = {
  lane: { name: 'preset', padding: 30 },
  workspace: { name: 'preset', padding: 30 },
  workspaces: { name: 'circle', animate: false, padding: 40 },
  harness: { name: 'preset', padding: 30 },
};

/** Put every lane inside its own layer, in reading order, without overlap.
 *
 * Written out rather than left to a layout because the layers are the point of
 * this lens: a run of boxes laid side by side is a picture of how the work
 * stacks, and a force layout that happens to overlap two of them is a picture
 * of nothing. Positions are for the child nodes only - cytoscape sizes a
 * compound parent around whatever is inside it.
 */
function bpMapPositions(elements) {
  const COL = 200, ROW = 46, PAD = 60;
  const kids = new Map();
  const loose = [];
  for (const e of elements) {
    if (e.data.source) continue;
    if (e.data.parent) {
      if (!kids.has(e.data.parent)) kids.set(e.data.parent, []);
      kids.get(e.data.parent).push(e.data.id);
    } else if (!(e.classes || '').includes('layer')) {
      loose.push(e.data.id);
    }
  }
  const groups = [...kids.entries()];
  if (loose.length) groups.push([null, loose]);

  const pos = {};
  let x = 0, y = 0, rowTall = 0;
  for (const [, ids] of groups) {
    const cols = Math.min(3, Math.max(1, Math.ceil(Math.sqrt(ids.length))));
    const rows = Math.ceil(ids.length / cols);
    // Wrap onto a new band once a row of layers is wider than a comfortable
    // canvas. `fit` will scale it afterwards; this only decides the shape.
    if (x && x + cols * COL > 1100) { x = 0; y += rowTall + PAD; rowTall = 0; }
    ids.forEach((id, i) => {
      pos[id] = { x: x + (i % cols) * COL, y: y + Math.floor(i / cols) * ROW };
    });
    x += cols * COL + PAD;
    rowTall = Math.max(rowTall, rows * ROW);
  }
  return elements.map((e) => (pos[e.data.id] ? { ...e, position: pos[e.data.id] } : e));
}

/** The pipeline, left to right, with the actors above the rungs they hold.
 *
 * Positions rather than a layout because this lens is a claim about order:
 * direction comes before code, and code comes before review, and a graph that
 * puts them in a ring because that packed better has thrown away the only thing
 * it was drawn to say. An actor sits over the AVERAGE of its own rungs, so who
 * is holding what is readable without following a dashed line to its end.
 */
function bpAgentPositions(elements) {
  const COL = 150, LANE_Y = 210, ACTOR_Y = 40;
  const stages = [], actors = [];
  let subject = null;
  for (const e of elements) {
    if (e.data.source) continue;
    const c = e.classes || '';
    if (c.includes('stage')) stages.push(e.data.id);
    else if (c.includes('actor')) actors.push(e.data.id);
    else if (c.includes('subject')) subject = e.data.id;
  }
  const pos = {};
  if (subject) pos[subject] = { x: -COL, y: LANE_Y };
  stages.forEach((id, i) => { pos[id] = { x: i * COL, y: LANE_Y }; });

  // Where each actor's own lines end. Read off the edges rather than from a
  // second copy of STAGE_WHO here - the edges are what the picture draws, so an
  // actor floating away from its own lines would be this file disagreeing with
  // itself.
  //
  // Two passes, because one actor watches the others. `performs` always ends on
  // a rung, which is placed before any actor is; `watches` ends on another
  // ACTOR, which has no position yet on the first pass. Averaging both together
  // silently dropped every unplaced target, so the supervisor came out over the
  // lane - the exact reading the three-edge fix was made to stop.
  const avg = (xs) => xs.reduce((s, v) => s + v, 0) / xs.length;
  const ends = (a, kind) => elements
    .filter((e) => (e.classes || '').includes(kind) && e.data.source === a)
    .map((e) => pos[e.data.target]).filter(Boolean).map((p) => p.x);

  const held = actors.filter((a) => ends(a, 'performs').length);
  held.forEach((a) => { pos[a] = { y: ACTOR_Y, x: avg(ends(a, 'performs')) }; });

  // Whoever holds no rung is placed by what it watches, a row further up: it is
  // above the pipeline in the picture because it is above it in the machine -
  // it reads what the others did and recommends, and starts nothing.
  // An actor attached to nothing at all is parked past the end rather than at
  // zero, where it would sit on top of whoever holds the first rung.
  let spare = stages.length;
  actors.filter((a) => !pos[a]).forEach((a) => {
    const xs = ends(a, 'watches');
    pos[a] = xs.length ? { y: ACTOR_Y - 110, x: avg(xs) }
      : { y: ACTOR_Y, x: (spare++) * COL };
  });
  return elements.map((e) => (pos[e.data.id] ? { ...e, position: pos[e.data.id] } : e));
}

/** The workspace level: the report loop as a ring, the advice down the side.
 *
 * A ring rather than a line because it IS one - the last arrow of the report
 * loop is what the next report measures, and a layout that laid it out
 * left-to-right would end with an arrow flying back over the top of everything,
 * which reads as an exception rather than as the point.
 *
 * The three calls that only advise are put on their own side, away from the
 * ring, because none of them is a step in it. Mixing them in would make the
 * loop look like it has seven stations when it has four.
 */
const BP_WS_POS = {
  'K:report': { x: 60, y: -160 }, 'K:solve': { x: 380, y: -160 },
  'K:lanes': { x: 380, y: 70 }, 'K:work': { x: 60, y: 70 },
  'K:supervise': { x: -300, y: -20 }, 'K:doctrine': { x: -300, y: 110 },
  'K:settle': { x: -300, y: 240 },
};

function bpWorkspacePositions(elements) {
  const pos = { ...BP_WS_POS };
  for (const e of elements) {
    // The subject carries the workspace's own slug in its id, so it is found by
    // its class rather than named here.
    if (!e.data.source && (e.classes || '').includes('subject')) {
      pos[e.data.id] = { x: -300, y: -180 };
    }
  }
  return elements.map((e) => (pos[e.data.id] ? { ...e, position: pos[e.data.id] } : e));
}

/** The harness level: each call beside the one thing it writes.
 *
 * The right column is placed off the EDGES rather than off its own order, so a
 * call added to the list lands next to what it actually writes even if the two
 * lists ever stop being parallel. Two lists that have to stay in step are a
 * thing that eventually does not.
 */
function bpHarnessPositions(elements) {
  const ROW = 96;
  const pos = {};
  const calls = elements.filter((e) => !e.data.source && (e.classes || '').includes('loop'));
  calls.forEach((e, i) => { pos[e.data.id] = { x: 0, y: i * ROW }; });
  for (const e of elements) {
    // Only the outbound arrow - the feedback edge runs the other way, and using
    // it would place a call at the row of what it reads instead.
    if (!e.data.source || !e.data.id.startsWith('HE:')) continue;
    const at = pos[e.data.source];
    if (at) pos[e.data.target] = { x: 380, y: at.y };
  }
  let spare = calls.length;
  for (const e of elements) {
    if (!e.data.source && !pos[e.data.id]) pos[e.data.id] = { x: 380, y: (spare++) * ROW };
  }
  return elements.map((e) => (pos[e.data.id] ? { ...e, position: pos[e.data.id] } : e));
}

const BP_LEVEL_POS = {
  lane: bpAgentPositions, workspace: bpWorkspacePositions,
  harness: bpHarnessPositions,
};

async function loadBpFlow() {
  const lens = state.bp.flow.lens;
  // The Context lens needs a call picked before it can draw one, and the list
  // of calls is the same one the Context pane loads. Fetched here rather than
  // duplicated.
  // Both Context and Agents are drawn ABOUT something and need it picked first.
  // Agents wants only the lane - a rung is held by the same actor whichever call
  // is being built - so the call picker is hidden rather than offered and
  // ignored.
  const needs = lens === 'context' || lens === 'agents';
  if (needs) {
    if (!state.bp.actions) await loadBpActions();
    if (!state.bp.map) await loadBpMap();
    bpFlowArgs();
  }
  $('bp-flow-args').classList.toggle('hidden', !needs);
  $('bp-flow-act-l').classList.toggle('hidden', lens !== 'context');
  $('bp-flow-lvl-l').classList.toggle('hidden', lens !== 'agents');
  // Which of the four levels is asking is not knowable until the answer comes
  // back, so the lane picker is left as it was and corrected in `renderBpFlow`.
  // Hiding it here on a guess would flicker it on every redraw.

  const q = new URLSearchParams({ lens });
  if (needs) {
    if (lens === 'context') q.set('action', $('bp-flow-act').value || '');
    if (lens === 'agents') q.set('level', state.bp.flow.level || 'lane');
    q.set('lane', $('bp-flow-lane').value || '');
  }
  $('bp-flow-note').innerHTML = '<span class="spin">drawing&hellip;</span>';
  const r = await api('/api/blueprint/flow?' + q.toString());
  $('bp-flow-note').textContent = '';
  if (!r.ok) {
    $('bp-flow-empty').textContent = r.error || 'it refused';
    $('bp-flow-empty').classList.remove('hidden');
    return;
  }
  renderBpFlow(r);
}

// Refill a picker only when its list actually changed, keeping the selection if
// it survives. `if (!options.length)` filled these ONCE, which is wrong for the
// lane picker: switching workspace nulls `state.bp.map` but leaves the old
// workspace's lanes sitting in the DOM, and every lane-scoped call then refuses
// with "pick a lane" about a lane the picker itself offered.
function bpFill(id, want, fallback) {
  const el = $(id);
  const have = [...el.options].map((o) => o.value);
  if (have.length === want.length && have.every((v, i) => v === want[i].value)) return;
  const keep = want.some((w) => w.value === el.value) ? el.value : fallback;
  el.innerHTML = want.map((w) =>
    `<option value="${esc(w.value)}" title="${esc(w.title || '')}"` +
    `${w.value === keep ? ' selected' : ''}>${esc(w.label)}</option>`).join('');
  el.value = keep;
}

function bpFlowArgs() {
  const acts = state.bp.actions || [];
  const lanes = ((state.bp.map || {}).nodes || []).map((n) => n.lane);
  bpFill('bp-flow-act', acts.map((a) => ({ value: a.id, label: a.title, title: a.what })),
    (acts[0] || {}).id);
  bpFill('bp-flow-lane', lanes.map((l) => ({ value: l, label: l })),
    lanes.includes(state.sel) ? state.sel : lanes[0]);
}

function renderBpFlow(r) {
  const box = $('bp-flow');
  $('bp-flow-empty').classList.toggle('hidden', !r.empty);
  $('bp-flow-empty').textContent = r.empty || '';
  $('bp-flow-legend').innerHTML = (r.legend || []).map((l) =>
    `<span class="bp-leg bp-leg-${esc(l.k)}">${esc(l.k)}<span class="muted"> &mdash; ${esc(l.t)}</span></span>`).join('')
    + (r.note ? `<span class="bp-leg bp-leg-note muted">${esc(r.note)}</span>` : '');
  $('bp-flow-tip').classList.add('hidden');
  state.bp.flow.panels = r.panels || {};

  // The levels come from the server so this file holds no second copy of what
  // they are, and the picker is corrected from the answer rather than from what
  // was asked - a level the server declined to draw must not be left showing as
  // the one you are on.
  if (r.levels) {
    if (!$('bp-flow-lvl').options.length) {
      $('bp-flow-lvl').innerHTML = r.levels.map((l) =>
        `<option value="${esc(l.id)}" title="${esc(l.what)}">${esc(l.label)}</option>`).join('');
    }
    state.bp.flow.level = r.level;
    $('bp-flow-lvl').value = r.level;
    $('bp-flow-lvl').title = r.level_what || '';
  }
  // A lane picker on a level that is not about one lane is a control that looks
  // broken, so the server says whether this picture takes one.
  if (r.lens === 'agents') {
    $('bp-flow-lane-l').classList.toggle('hidden', !r.needs_lane);
  } else {
    $('bp-flow-lane-l').classList.remove('hidden');
  }

  if (state.bp.flow.cy) { state.bp.flow.cy.destroy(); state.bp.flow.cy = null; }
  if (!r.elements.length) {
    // The reason goes where the picture would have been. Left in the strip
    // under a blank canvas it reads as a diagram that failed to load, which is
    // a different claim from "this call has nothing to be built about yet".
    box.innerHTML = `<div class="bp-flow-none">${esc(r.empty || 'nothing to draw')}</div>`;
    $('bp-flow-empty').classList.add('hidden');
    return;
  }
  box.innerHTML = '';

  const level = r.lens === 'agents' ? (r.level || 'lane') : null;
  const laid = r.lens === 'map' ? bpMapPositions(r.elements)
    : level && BP_LEVEL_POS[level] ? BP_LEVEL_POS[level](r.elements) : r.elements;
  const cy = cytoscape({
    container: box, style: BP_FLOW_STYLE, elements: laid,
    layout: (level ? BP_LEVEL_LAYOUT[level] : BP_FLOW_LAYOUT[r.lens]) || { name: 'grid' },
    wheelSensitivity: 0.25,
  });
  state.bp.flow.cy = cy;
  // A layout fits what it laid out, so four lanes fill the canvas at 300% and a
  // lane name reads like a headline. Zooming OUT to fit is always right; zooming
  // in past life size only makes a small graph look like a big one.
  cy.ready(() => {
    if (cy.zoom() > 1) { cy.zoom(1); cy.center(); }
  });

  cy.on('tap', 'node, edge', (ev) => {
    cy.elements().removeClass('picked');
    ev.target.addClass('picked');
    const d = ev.target.data();
    const panel = state.bp.flow.panels[d.id];
    if (r.lens === 'agents' && panel) bpAgentPanel(d.id, panel);
    else bpFlowTip(d, r.lens);
  });
  // A tap on the background is how you put the panel away, which is the only
  // gesture people try without being told.
  cy.on('tap', (ev) => {
    if (ev.target === cy) {
      cy.elements().removeClass('picked');
      state.bp.flow.pick = null;
      $('bp-flow-tip').classList.add('hidden');
    }
  });
  // A jump names the node it is jumping to, and this is where it arrives. Doing
  // it here rather than in the click handler is what makes it survive the round
  // trip: the graph the jump lands on has not been drawn yet at the moment the
  // jump is made.
  //
  // A jump that names nothing is a jump to the level itself, which is a real
  // thing to want - `open the pipeline` has no one rung it means.
  const land = state.bp.flow.land;
  state.bp.flow.land = null;
  if (land) {
    const at = cy.getElementById(land);
    if (at.nonempty()) {
      cy.elements().removeClass('picked');
      at.addClass('picked');
      cy.center(at);
      if (state.bp.flow.panels[land]) bpAgentPanel(land, state.bp.flow.panels[land]);
      return;
    }
    // Said out loud rather than silently ignored. A jump landing on nothing is
    // a wiring mistake in the lens, and the only person who can ever see it is
    // whoever pressed the button.
    $('bp-flow-note').textContent = `nothing called ${land} on this level`;
    state.bp.flow.pick = null;
  }
  // Re-open whatever was open before a write redrew the canvas. Without this
  // every change closes the panel you changed it from, so a second change means
  // finding the node again - and the whole point of putting the switches on the
  // graph was that they are where the thing they govern is.
  if (state.bp.flow.pick && state.bp.flow.panels[state.bp.flow.pick]) {
    const back = cy.getElementById(state.bp.flow.pick);
    if (back.nonempty()) back.addClass('picked');
    bpAgentPanel(state.bp.flow.pick, state.bp.flow.panels[state.bp.flow.pick]);
  }
}

/** One node's real controls, floated over the graph.
 *
 * Every row that writes posts to an endpoint that already existed - the role
 * switches from Settings, the mode and the ceiling from Lanes, the two loops
 * from Specs and Pull requests. This panel builds the body from `body` plus the
 * one `field` the control owns, so there is no second copy of what any of those
 * calls take, and nothing here can send a shape the server has not always
 * accepted.
 */
function bpAgentPanel(id, p) {
  const tip = $('bp-flow-tip');
  state.bp.flow.pick = id;
  const bits = [`<b>${esc(p.title)}</b>`, `<div>${esc(p.what)}</div>`];
  (p.rows || []).forEach((row, i) => {
    const key = `bpa-${i}`;
    if (row.kind === 'fact') {
      bits.push(`<div class="bp-row"><span class="muted">${esc(row.label)}</span> ` +
        `${esc(row.value)}${row.why ? `<div class="muted">${esc(row.why)}</div>` : ''}</div>`);
    } else if (row.kind === 'toggle') {
      bits.push(`<div class="bp-row"><label><input type="checkbox" id="${key}"` +
        `${row.value ? ' checked' : ''}> ${esc(row.label)}</label>` +
        `<div class="muted">${esc(row.why || '')}</div></div>`);
    } else if (row.kind === 'select') {
      const opts = (row.options || []).map((o) =>
        `<option value="${esc(o.value)}"${o.value === row.value ? ' selected' : ''}` +
        `${o.cannot ? ' disabled' : ''} title="${esc(o.cannot || o.note || '')}">` +
        `${esc(o.value)}${o.cannot ? ' \u2014 cannot' : ''}</option>`).join('');
      const note = (row.options || []).find((o) => o.value === row.value);
      bits.push(`<div class="bp-row"><label>${esc(row.label)} ` +
        `<select id="${key}">${opts}</select></label>` +
        `<div class="muted">${esc((note && note.note) || row.why || '')}</div></div>`);
    } else if (row.kind === 'button') {
      bits.push(`<div class="bp-row"><button id="${key}">${esc(row.label)}</button>` +
        `<div class="muted">${esc(row.why || '')}</div></div>`);
    } else if (row.kind === 'goto') {
      // Drawn apart from the buttons that write, because they are not the same
      // kind of thing at all: one changes the harness, the other only moves you
      // to where you can read what a call is actually sent.
      bits.push(`<div class="bp-row bp-goto"><button id="${key}" class="bp-gotob">` +
        `${esc(row.label)}</button>` +
        `<div class="muted">${esc(row.why || '')}</div></div>`);
    }
  });
  tip.innerHTML = bits.join('');
  tip.classList.remove('hidden');

  (p.rows || []).forEach((row, i) => {
    const el = $(`bpa-${i}`);
    if (!el) return;
    const send = async (value) => {
      el.disabled = true;
      $('bp-flow-note').innerHTML = '<span class="spin">saving&hellip;</span>';
      const r = await post(row.post, { ...row.body, [row.field]: value });
      el.disabled = false;
      // A refusal is reported AND the redraw still happens - the server refusing
      // is exactly the case where the panel must not be left showing the value
      // it just failed to set. The message is put back afterwards because the
      // redraw clears this line on its way past.
      const err = (r && r.ok === false) ? (r.error || 'it refused') : '';
      // Redrawn from the server rather than patched in place: a mode outranks a
      // loop, a ceiling outranks both, and the only thing that knows what a
      // change actually did is the thing that just did it.
      await loadBpFlow();
      $('bp-flow-note').textContent = err;
    };
    if (row.kind === 'toggle') el.onchange = () => send(el.checked);
    else if (row.kind === 'select') el.onchange = () => send(el.value);
    else if (row.kind === 'button') el.onclick = () => send(row.value);
    else if (row.kind === 'goto') el.onclick = () => bpFlowGoto(row);
  });
}

/** Follow a jump: to another level of this lens, or to the exact prompt.
 *
 * This is the whole reason the lenses share one canvas. `the architect reviews
 * it` on one screen and `here is what the architect is sent` on another are the
 * same fact twice, and the second was unreachable from the first until
 * something carried the address across. The address is what is set here: a lens
 * plus, depending, a level and a node, or an action and a lane.
 */
function bpFlowGoto(row) {
  if (row.lens === 'agents') {
    state.bp.flow.level = row.level;
    // Empty means the level itself, and cytoscape would happily hand back an
    // empty collection for `''` and report a landing that never happened.
    state.bp.flow.land = row.node || null;
    // Cleared on purpose: the panel that is open belongs to the level being
    // left, and the arriving level either lands on something or on nothing.
    state.bp.flow.pick = null;
    loadBpFlow();
    return;
  }
  // Into the Context lens, at the build this node makes. The lane comes from
  // the jump when it carries one - a workspace-scoped call has no lane, and
  // forcing the picker to one would be inventing an argument the call does not
  // take.
  state.bp.flow.lens = row.lens;
  state.bp.flow.pick = state.bp.flow.land = null;
  document.querySelectorAll('.bp-lensb').forEach((o) =>
    o.classList.toggle('active', o.dataset.lens === row.lens));
  if (row.action) $('bp-flow-act').value = row.action;
  if (row.lane) $('bp-flow-lane').value = row.lane;
  loadBpFlow();
}

function bpFlowTip(d, lens) {
  const tip = $('bp-flow-tip');
  const bits = [`<b>${esc(d.label || d.id)}</b>`];
  if (d.tip) bits.push(`<div>${esc(d.tip)}</div>`);
  if (d.rung) bits.push(`<div class="muted">judged rung: ${esc(d.rung)}</div>`);
  if (d.mode) bits.push(`<div class="muted">${esc(d.mode)} &middot; ${esc(d.stage)} &middot; ` +
    `${d.open} open, ${d.blocked} blocked, ${d.findings} unread</div>`);
  if (d.reads) bits.push(`<div class="muted">reads: ${esc(d.reads)}</div>`);
  if (d.chars !== undefined) bits.push(`<div class="muted">${d.chars.toLocaleString()} chars</div>`);
  if (d.would !== undefined) {
    bits.push(`<div class="muted">${d.would
      ? `<b>would fire now</b> on ${d.would} occurrence(s)`
      : 'nothing matches it right now'}</div>`);
  }
  // The lens picked it, so the lens says where to go to act on it. Nothing on
  // this canvas edits anything.
  bits.push('<div class="muted">' + esc(
    lens === 'triggers' ? 'arm, edit or remove it on the Triggers pane'
      : lens === 'context' ? 'the exact bytes are on the Context pane'
      : 'lane details are on the Map pane') + '</div>');
  tip.innerHTML = bits.join('');
  tip.classList.remove('hidden');
}

// ------------------------------------------------------------------ map

const RUNG_MEANS = {
  spec: 'written down and argued for',
  in_tree: 'it exists in the repository',
  live_local: 'it runs here',
  live_deployed: 'it runs where someone else can reach it',
  external: 'someone outside has used it',
};

async function loadBpMap() {
  const r = await api('/api/blueprint');
  if (!r.ok) {
    $('bp-map').innerHTML = `<div class="err">${esc(r.error)}</div>`;
    return;
  }
  state.bp.map = r;
  renderBpMap();
}

function renderBpMap() {
  const m = state.bp.map;
  // The button stays available once layers exist: the drafter is shown the
  // layers already drawn and asked to argue with them, which is a different and
  // more useful call than the one-shot seeding it replaced.
  $('bp-seed').textContent = m.suggest ? 'Suggest layers' : 'Redraft layers';
  // The unplaced column is always drawn, and always last. A lane nobody has
  // filed is the single most useful thing on this screen - it is a lane whose
  // place in the stack no one has had to say out loud yet.
  const cols = m.stacks.concat([{ id: '', name: 'not filed', note:
    'lanes nobody has put in a layer yet' }]);
  // Rows top-down: the furthest evidence at the top, so the stack reads the way
  // people describe it. The last row is for lanes no review has ruled on, which
  // is a different fact from "it only has a spec".
  const rows = m.rungs.slice().reverse().concat([null]);
  const at = (rung, sid) => m.nodes.filter((n) =>
    (n.rung || null) === rung && (n.stack || '') === sid);

  let h = '<table class="bp-grid"><thead><tr><th class="bp-rung"></th>';
  for (const c of cols) {
    h += `<th title="${esc(c.note || '')}">${esc(c.name)}` +
      (c.id ? `<button class="bp-x" data-stack-del="${esc(c.id)}" title="remove this layer">&times;</button>` : '') +
      '</th>';
  }
  h += '</tr></thead><tbody>';
  for (const rung of rows) {
    const label = rung || 'not judged yet';
    const why = rung ? RUNG_MEANS[rung] || ''
      : 'no review has awarded this lane a rung';
    h += `<tr><th class="bp-rung" title="${esc(why)}">${esc(label)}` +
      `<span class="bp-why">${esc(why)}</span></th>`;
    for (const c of cols) {
      const here = at(rung, c.id);
      h += '<td>' + (here.length ? here.map(bpChip).join('') : '<span class="bp-none">&middot;</span>') + '</td>';
    }
    h += '</tr>';
  }
  h += '</tbody></table>';

  // The edges the harness already knows about, drawn as sentences rather than
  // lines. A line between two boxes says they are related; this says how, and
  // `represents` in particular carries a rule - a commit on the surface moves
  // no rung on the system it presents - that an arrow could never carry.
  if (m.edges.length) {
    h += '<h4 class="bp-h">How they are already wired</h4><ul class="bp-edges">';
    for (const e of m.edges) {
      h += `<li><b>${esc(e.from)}</b> <span class="bp-kind">${esc(e.kind)}</span> ` +
        `<b>${esc(e.to)}</b> <span class="muted">&mdash; ${esc(e.why)}</span></li>`;
    }
    h += '</ul>';
  }
  $('bp-map').innerHTML = h;

  $('bp-map').querySelectorAll('[data-lane]').forEach((el) =>
    el.onclick = () => showBpNode(el.dataset.lane));
  $('bp-map').querySelectorAll('[data-stack-del]').forEach((el) =>
    el.onclick = async (ev) => {
      ev.stopPropagation();
      await post('/api/blueprint/stack', { op: 'remove', id: el.dataset.stackDel });
      state.bp.trigs = null;
      loadBpMap();
    });
}

// ---------------------------------------------------------------- draft layers
//
// The draft is held here, in the page, and nowhere else until it is accepted.
// That is the whole point of the two steps: what comes back from the architect
// is a proposal about how this portfolio is shaped, and it gets to be wrong in
// front of the operator before it gets to be the thing triggers are wired to.

async function bpDraftStacks() {
  const box = $('bp-draft');
  box.classList.remove('hidden');
  box.innerHTML = '<div class="bp-draft-head"><span class="spin">reading every ' +
    'lane\u2019s direction\u2026</span></div>';
  const r = await post('/api/blueprint/stack', { op: 'draft' });
  if (!r.ok) {
    box.innerHTML = `<div class="err">${esc(r.error)}</div>` +
      (r.raw ? `<pre class="bp-raw">${esc(r.raw)}</pre>` : '') +
      '<div class="row"><button data-bpd="close">Close</button></div>';
    bpDraftWire();
    return;
  }
  state.bp.draft = r;
  renderBpDraft();
}

function bpDraftClose() {
  state.bp.draft = null;
  $('bp-draft').classList.add('hidden');
  $('bp-draft').innerHTML = '';
}

/** Read whatever the operator has typed back into the draft, before anything
 *  re-renders. A structural edit that silently discarded a renamed layer would
 *  be the most annoying possible bug on this screen. */
function bpDraftSync() {
  const d = state.bp.draft;
  if (!d) return;
  $('bp-draft').querySelectorAll('[data-bpd-name]').forEach((el) => {
    const l = d.layers[+el.dataset.bpdName];
    if (l) l.name = el.value;
  });
  $('bp-draft').querySelectorAll('[data-bpd-note]').forEach((el) => {
    const l = d.layers[+el.dataset.bpdNote];
    if (l) l.note = el.value;
  });
}

/** Move one lane to a layer index, or to -1 for left out. */
function bpDraftMove(lane, to) {
  const d = state.bp.draft;
  let row = null;
  for (const l of d.layers) {
    const i = l.lanes.findIndex((x) => x.lane === lane);
    if (i >= 0) row = l.lanes.splice(i, 1)[0];
  }
  const j = d.unplaced.findIndex((x) => x.lane === lane);
  if (j >= 0) row = d.unplaced.splice(j, 1)[0];
  if (!row) return;
  if (to >= 0 && d.layers[to]) d.layers[to].lanes.push({ lane, why: row.why || '' });
  // A lane the operator moves out by hand is out because they said so, and
  // saying that is more honest than leaving the drafter's reason attached to a
  // decision the drafter did not make.
  else d.unplaced.push({ lane, why: row.why || '', missed: false, moved: true });
  renderBpDraft();
}

function renderBpDraft() {
  const d = state.bp.draft;
  const opts = (sel) => d.layers.map((l, i) =>
    `<option value="${i}"${i === sel ? ' selected' : ''}>${esc(l.name || `layer ${i + 1}`)}</option>`)
    .join('') + `<option value="-1"${sel < 0 ? ' selected' : ''}>\u2014 leave out</option>`;

  let h = '<div class="bp-draft-head"><h4>Suggested layers</h4>';
  h += d.source === 'architect'
    ? `<span class="muted">drafted by ${esc(d.model || 'the architect')} off what each ` +
      'lane says it is for. Nothing is saved until you accept.</span>'
    : `<span class="warn">${esc(d.note || 'the architect is off')}</span>`;
  h += '<button data-bpd="close" class="bp-x" title="discard">&times;</button></div>';

  // Two things the drafter can get wrong about lanes that exist are shown as
  // its errors, not silently repaired, because they say how much to trust the
  // rest of what it said.
  if ((d.invented || []).length) {
    h += `<div class="err">it named ${d.invented.map((x) => `<code>${esc(x)}</code>`).join(', ')}` +
      ', which are not lanes in this workspace. Those were dropped.</div>';
  }
  if ((d.duplicated || []).length) {
    h += `<div class="err">it put ${d.duplicated.map((x) => `<code>${esc(x)}</code>`).join(', ')}` +
      ' in more than one layer. Only the first was kept.</div>';
  }

  d.layers.forEach((l, i) => {
    h += '<div class="bp-draft-layer"><div class="bp-draft-top">' +
      `<input data-bpd-name="${i}" value="${esc(l.name)}" placeholder="layer name">` +
      `<button data-bpd="del" data-i="${i}" class="bp-x" title="drop this layer">&times;</button></div>` +
      `<input class="bp-draft-note" data-bpd-note="${i}" value="${esc(l.note || '')}" ` +
      'placeholder="what belongs in this layer, and what does not">';
    h += l.lanes.length ? '<ul class="bp-draft-lanes">' : '<p class="bp-none">no lane here yet</p>';
    for (const row of l.lanes) {
      h += `<li><b>${esc(row.lane)}</b> <span class="muted">${esc(row.why || '')}</span>` +
        `<select data-bpd-move="${esc(row.lane)}">${opts(i)}</select></li>`;
    }
    if (l.lanes.length) h += '</ul>';
    h += '</div>';
  });

  h += '<div class="bp-draft-layer out"><div class="bp-draft-top"><b>Left out</b>' +
    '<span class="muted">these stay unfiled &mdash; the map draws them in its own column</span></div>';
  h += d.unplaced.length ? '<ul class="bp-draft-lanes">' : '<p class="bp-none">nothing</p>';
  for (const row of d.unplaced) {
    const why = row.missed
      ? 'the drafter did not mention this lane at all'
      : (row.moved ? 'you moved it out' : row.why || '');
    h += `<li><b>${esc(row.lane)}</b> <span class="${row.missed ? 'warn' : 'muted'}">${esc(why)}</span>` +
      `<select data-bpd-move="${esc(row.lane)}">${opts(-1)}</select></li>`;
  }
  if (d.unplaced.length) h += '</ul>';
  h += '</div>';

  h += '<div class="row bp-draft-act"><button data-bpd="accept" class="primary">Accept these layers</button>' +
    '<button data-bpd="again">Draft it again</button>' +
    '<button data-bpd="add">+ empty layer</button>' +
    '<button data-bpd="close">Discard</button>' +
    '<span class="muted">accepting replaces the layers you have now, and any ' +
    'trigger pointing at one of them is switched off and told why.</span></div>';
  $('bp-draft').innerHTML = h;
  bpDraftWire();
}

function bpDraftWire() {
  const box = $('bp-draft');
  box.querySelectorAll('[data-bpd-move]').forEach((el) =>
    el.onchange = () => { bpDraftSync(); bpDraftMove(el.dataset.bpdMove, +el.value); });
  box.querySelectorAll('[data-bpd]').forEach((el) => el.onclick = async () => {
    const d = state.bp.draft;
    switch (el.dataset.bpd) {
      case 'close': return bpDraftClose();
      case 'again': return bpDraftStacks();
      case 'add':
        bpDraftSync();
        d.layers.push({ name: '', note: '', lanes: [] });
        return renderBpDraft();
      case 'del': {
        bpDraftSync();
        const [gone] = d.layers.splice(+el.dataset.i, 1);
        for (const row of gone.lanes) d.unplaced.push({ ...row, missed: false, moved: true });
        return renderBpDraft();
      }
      case 'accept': {
        bpDraftSync();
        const r = await post('/api/blueprint/stack',
          { op: 'apply', layers: d.layers.filter((l) => (l.name || '').trim()) });
        if (!r.ok) return alert(r.error);
        bpDraftClose();
        state.bp.trigs = null;
        return loadBpMap();
      }
      default:
    }
  });
}

function bpChip(n) {
  const flags = [];
  if (n.mode !== 'build') flags.push(n.mode);
  if (n.contradictions) flags.push(`${n.contradictions} contradiction${n.contradictions > 1 ? 's' : ''}`);
  if (!n.specs) flags.push('no spec');
  return `<span class="bp-lane${n.contradictions ? ' bad' : ''}" data-lane="${esc(n.lane)}"` +
    ` title="${esc(n.for || '')}">${esc(n.lane)}` +
    (flags.length ? `<span class="bp-flag">${esc(flags.join(' · '))}</span>` : '') +
    '</span>';
}

function showBpNode(lane) {
  const n = (state.bp.map.nodes || []).find((x) => x.lane === lane);
  const box = $('bp-node');
  if (!n) return box.classList.add('hidden');
  state.bp.node = lane;
  const field = (k, label) => n[k]
    ? `<div class="bp-field"><b>${esc(label)}</b><p>${esc(n[k])}</p></div>` : '';
  box.innerHTML =
    `<div class="bp-node-head"><h3>${esc(n.lane)}</h3>` +
    `<span class="muted">${esc(n.path)} &middot; ${esc(n.repo || 'no repo')} &middot; ${esc(n.mode)} &middot; ${esc(n.stage)}</span>` +
    '<button id="bp-node-x" title="close">&times;</button></div>' +
    '<div class="bp-place"><label>layer <select id="bp-place">' +
    '<option value="">not filed</option>' +
    (state.bp.map.stacks || []).map((s) =>
      `<option value="${esc(s.id)}"${s.id === n.stack ? ' selected' : ''}>${esc(s.name)}</option>`).join('') +
    '</select></label>' +
    `<span class="muted">rung <b>${esc(n.rung || 'not judged yet')}</b> &mdash; read from the reviews, not set here</span></div>` +
    field('for', 'What it is for') +
    field('thesis', 'The bet') +
    field('claim', 'What it claims to have shown') +
    field('bar', 'What would settle it') +
    field('unknown', 'What it does not know') +
    (n.represents ? `<div class="bp-field"><b>Presents</b><p>${esc(n.represents)} &mdash; a commit here moves no rung on that.</p></div>` : '') +
    `<div class="bp-counts">${n.goals.open} open goal(s) &middot; ${n.goals.blocked} blocked &middot; ` +
    `${n.goals.done} done &middot; ${n.specs} design document(s) &middot; ` +
    `${n.findings} unread finding(s)</div>` +
    '<div class="row"><button id="bp-node-ctx">See what a worker in this lane is sent</button></div>';
  box.classList.remove('hidden');
  $('bp-node-x').onclick = () => box.classList.add('hidden');
  $('bp-place').onchange = async () => {
    const r = await post('/api/blueprint/place', { lane, stack: $('bp-place').value });
    if (!r.ok) return alert(r.error);
    await loadBpMap();
    showBpNode(lane);
  };
  $('bp-node-ctx').onclick = async () => {
    bpShow('context');
    await loadBpActions();
    $('bp-act').value = 'worker';
    $('bp-lane').value = lane;
    runBpContext();
  };
}

// -------------------------------------------------------------- triggers

async function loadBpTriggers() {
  const r = await api('/api/blueprint/triggers');
  if (!r.ok) {
    $('bp-trigs').innerHTML = `<div class="err">${esc(r.error)}</div>`;
    return;
  }
  state.bp.trigs = r;
  renderBpTriggers();
}

/** What this workspace has taken in from the others, most recent first.
 *
 *  Drawn even when nothing is wired here, because it is the far end of a wire
 *  somebody drew somewhere else - and a proposal arriving from another
 *  workspace with nothing on this screen explaining it would be the one thing
 *  on the Blueprint that could surprise you. */
function bpInboxHtml(rows) {
  if (!rows || !rows.length) return '';
  return '<details class="bp-hist bp-inbox"><summary>Admitted from other ' +
    `workspaces (${rows.length})</summary><ul>` +
    rows.map((r) => `<li>${esc(r.at)} &middot; from <b>${esc(r.from_ws)}</b> ` +
      `&middot; ${esc(r.lane || '?')} &middot; <b>${esc(r.state)}</b>` +
      (r.proposal ? ` <code>${esc(r.proposal)}</code>` : '') +
      (r.why ? ` <span class="muted">&mdash; ${esc(r.why)}</span>` : '') +
      `<div class="muted">${esc((r.objective || '').slice(0, 160))}</div></li>`).join('') +
    '</ul></details>';
}

function renderBpTriggers() {
  const t = state.bp.trigs;
  if (!t.triggers.length) {
    $('bp-trigs').innerHTML = '<div class="empty">Nothing is wired up. A trigger ' +
      'watches one side of the stack for a condition the harness can already ' +
      'check on disk, and when it holds it proposes a goal in another lane.</div>' +
      bpInboxHtml(t.inbox);
    return;
  }
  $('bp-trigs').innerHTML = bpInboxHtml(t.inbox) + t.triggers.map((x) => {
    const ev = t.events[x.when] || {};
    const from = x.from_lanes.length
      ? x.from_lanes.join(', ')
      : '<span class="err">nothing &mdash; this watches no lane</span>';
    return `<div class="bp-trig${x.on ? '' : ' off'}${x.broken ? ' broken' : ''}">` +
      '<div class="bp-trig-head">' +
      `<label class="bp-arm"><input type="checkbox" data-arm="${esc(x.id)}"${x.on ? ' checked' : ''}> armed</label>` +
      `<span class="bp-when">when <b>${esc(from)}</b> ${esc(ev.label || x.when)}</span>` +
      `<button class="bp-x" data-trig-del="${esc(x.id)}" title="remove">&times;</button></div>` +
      (x.broken ? `<div class="err">${esc(x.broken)}</div>` : '') +
      `<div class="bp-trig-body"><span class="muted">${esc(ev.what || '')}</span>` +
      '<div class="bp-arrow">&darr; ' + (x.port
        ? `request <b>${esc(x.to.id)}</b> in the workspace ` +
          `<b>${esc(x.port.name)}</b> <span class="muted">&mdash; it is written here ` +
          'and admitted over there, against that workspace&rsquo;s own bars' +
          (x.port.waiting ? `; ${x.port.waiting} still waiting` : '') + '</span>'
        : `propose in <b>${esc(x.to.id)}</b>`) + '</div>' +
      `<pre class="bp-obj">${esc(x.objective)}</pre>` +
      (x.why ? `<div class="muted">${esc(x.why)}</div>` : '') +
      `<div class="bp-trig-foot">fired ${x.fires || 0} time(s)` +
      (x.would && x.would.length
        ? ` &middot; <b>would fire now on ${esc(x.would.map((w) => w.lane).join(', '))}</b>`
        : ' &middot; nothing matches right now') +
      '</div>' +
      (x.history && x.history.length
        ? '<details class="bp-hist"><summary>what it has done</summary><ul>' +
          x.history.map((h) => `<li>${esc(h.at)} &middot; ${esc(h.lane)} &middot; ` +
            (h.proposal ? `proposed <code>${esc(h.proposal)}</code>`
              : h.request ? `requested <code>${esc(h.request)}</code> in ${esc(h.ws)}`
              : esc(h.note)) + '</li>').join('') +
          '</ul></details>'
        : '') +
      '</div></div>';
  }).join('');

  $('bp-trigs').querySelectorAll('[data-arm]').forEach((el) =>
    el.onchange = async () => {
      await post('/api/blueprint/trigger',
        { op: 'set', id: el.dataset.arm, patch: { on: el.checked } });
      loadBpTriggers();
    });
  $('bp-trigs').querySelectorAll('[data-trig-del]').forEach((el) =>
    el.onclick = async () => {
      await post('/api/blueprint/trigger', { op: 'remove', id: el.dataset.trigDel });
      loadBpTriggers();
    });
}

function bpTriggerForm() {
  const m = state.bp.map || { stacks: [], nodes: [], rungs: [] };
  const t = state.bp.trigs || { events: {} };
  const lanes = m.nodes.map((n) => n.lane);
  const box = $('bp-trig-form');
  box.innerHTML =
    '<div class="row"><label>when <select id="bt-when">' +
    Object.entries(t.events).map(([k, v]) =>
      `<option value="${esc(k)}">${esc(v.label)}</option>`).join('') +
    '</select></label>' +
    '<label>on <select id="bt-kind">' +
    '<option value="lane">one lane</option>' +
    '<option value="stack">a whole layer</option>' +
    '<option value="rung">every lane at a rung</option></select></label>' +
    '<span id="bt-who"></span>' +
    '<span id="bt-arg"></span></div>' +
    '<div class="row"><label>propose in <select id="bt-ws">' +
    (t.workspaces || []).map((w) =>
      `<option value="${esc(w.slug)}"${w.slug === t.current_ws ? ' selected' : ''}>` +
      `${esc(w.name)}${w.slug === t.current_ws ? ' (here)' : ''}</option>`).join('') +
    '</select> <select id="bt-to"></select></label>' +
    '<span class="muted">one lane, because the proposal is held to that lane\'s mode, stage and bars</span></div>' +
    '<div id="bt-port" class="muted hidden">Another workspace: this writes a ' +
    'REQUEST here and stops. That workspace admits it the next time it is ' +
    'opened, against its own bars.</div>' +
    '<label>objective <textarea id="bt-obj" rows="3" placeholder="what should be proposed, written as the goal itself"></textarea></label>' +
    '<label>why <input id="bt-why" placeholder="optional — the reason, carried onto every proposal it writes"></label>' +
    '<div class="row"><button id="bt-save" class="primary">Add it</button>' +
    '<button id="bt-cancel">Cancel</button><span id="bt-msg" class="msg"></span></div>';

  const who = () => {
    const k = $('bt-kind').value;
    const opts = k === 'lane' ? lanes
      : k === 'stack' ? m.stacks.map((s) => s.id)
      : m.rungs;
    const label = k === 'stack' ? m.stacks.map((s) => s.name) : opts;
    $('bt-who').innerHTML = '<select id="bt-who-v">' +
      opts.map((o, i) => `<option value="${esc(o)}">${esc(label[i])}</option>`).join('') + '</select>';
  };
  const arg = () => {
    const ev = t.events[$('bt-when').value] || {};
    $('bt-arg').innerHTML = ev.arg === 'rung'
      ? 'reaching <select id="bt-arg-v">' +
        m.rungs.map((r) => `<option value="${esc(r)}">${esc(r)}</option>`).join('') + '</select>'
      : '';
  };
  // The lanes on offer are the TARGET workspace's own, not this one's. A port
  // aimed at a lane that does not exist over there is refused by `add_trigger`
  // anyway; not offering it is the cheaper half of the same check.
  const to = () => {
    const slug = $('bt-ws').value;
    const w = (t.workspaces || []).find((x) => x.slug === slug) || { lanes: [] };
    $('bt-to').innerHTML = (w.lanes || []).map((l) =>
      `<option value="${esc(l)}">${esc(l)}</option>`).join('');
    $('bt-port').classList.toggle('hidden', slug === t.current_ws);
  };
  $('bt-kind').onchange = who;
  $('bt-when').onchange = arg;
  $('bt-ws').onchange = to;
  who(); arg(); to();

  $('bt-cancel').onclick = () => box.classList.add('hidden');
  $('bt-save').onclick = async () => {
    const body = {
      op: 'add', when: $('bt-when').value,
      from: { kind: $('bt-kind').value, id: $('bt-who-v').value },
      to: {
        kind: 'lane', id: $('bt-to').value,
        ws: $('bt-ws').value === t.current_ws ? '' : $('bt-ws').value,
      },
      objective: $('bt-obj').value.trim(), why: $('bt-why').value.trim(),
    };
    if ($('bt-arg-v')) body.arg = $('bt-arg-v').value;
    const r = await post('/api/blueprint/trigger', body);
    if (!r.ok) {
      $('bt-msg').innerHTML = `<span class="err">${esc(r.error)}</span>`;
      return;
    }
    box.classList.add('hidden');
    loadBpTriggers();
  };
  box.classList.remove('hidden');
}

// --------------------------------------------------------------- draft triggers
//
// Same two steps as the layers, held to the same rule: the draft lives in this
// page and nowhere else until it is accepted, and accepting it writes triggers
// that are OFF. Nothing drafted here can fire without a separate press made
// after reading what it would propose.

async function bpDraftTriggers() {
  const box = $('bp-trig-draft-box');
  box.classList.remove('hidden');
  box.innerHTML = '<div class="bp-draft-head"><span class="spin">reading the lanes, ' +
    'the layers and what is already wired\u2026</span></div>';
  const r = await post('/api/blueprint/trigger', { op: 'draft' });
  if (!r.ok) {
    box.innerHTML = `<div class="err">${esc(r.error)}</div>` +
      (r.raw ? `<pre class="bp-raw">${esc(r.raw)}</pre>` : '') +
      '<div class="row"><button data-btd="close">Close</button></div>';
    bpTrigDraftWire();
    return;
  }
  state.bp.tdraft = r;
  renderBpTrigDraft();
}

function bpTrigDraftClose() {
  state.bp.tdraft = null;
  $('bp-trig-draft-box').classList.add('hidden');
  $('bp-trig-draft-box').innerHTML = '';
}

/** Read the edited objectives back before anything re-renders. */
function bpTrigDraftSync() {
  const d = state.bp.tdraft;
  if (!d) return;
  $('bp-trig-draft-box').querySelectorAll('[data-btd-obj]').forEach((el) => {
    const t = d.triggers[+el.dataset.btdObj];
    if (t) t.objective = el.value;
  });
}

function renderBpTrigDraft() {
  const d = state.bp.tdraft;
  const box = $('bp-trig-draft-box');
  let h = '<div class="bp-draft-head">drafted by <b>' + esc(d.model || d.source) +
    '</b> &mdash; nothing is written yet, and everything accepted arrives ' +
    '<b>disarmed</b></div>';
  if (d.note) h += `<div class="muted">${esc(d.note)}</div>`;

  h += d.triggers.map((t, i) => {
    const port = t.to.ws
      ? ` <span class="bp-port">in workspace ${esc(t.to.ws)}</span>` : '';
    return '<div class="bp-draft-layer"><div class="bp-draft-top">' +
      `<label class="bp-arm"><input type="checkbox" data-btd-take="${i}" checked> take it</label>` +
      `<span class="bp-when">when <b>${esc(t.from.name || t.from.id)}</b> ` +
      `${esc(t.event)}${t.arg ? ' ' + esc(t.arg) : ''} &rarr; propose in ` +
      `<b>${esc(t.to.id)}</b>${port}</span></div>` +
      `<textarea class="bp-draft-note" rows="2" data-btd-obj="${i}">${esc(t.objective)}</textarea>` +
      (t.why ? `<div class="muted">${esc(t.why)}</div>` : '') +
      `<div class="muted">watches: ${esc((t.from_lanes || []).join(', ') || 'nothing')}</div>` +
      '</div>';
  }).join('');

  // Rejected rows are SHOWN, not dropped. Four of six coming back silently
  // looks exactly like a drafter that only found four, and the two it got
  // wrong are the ones worth reading.
  if ((d.rejected || []).length) {
    h += '<div class="bp-draft-layer out"><b>Could not be wired</b>' +
      d.rejected.map((t) =>
        `<div class="muted">&ldquo;${esc((t.objective || '').slice(0, 90))}&rdquo; ` +
        `&mdash; <span class="err">${esc(t.rejected)}</span></div>`).join('') +
      '</div>';
  }
  if (!d.triggers.length) {
    h += '<div class="empty">Nothing here can be wired as written.</div>';
  }
  h += '<div class="bp-draft-act row">' +
    (d.triggers.length
      ? '<button data-btd="accept" class="primary">Add them, disarmed</button>' : '') +
    '<button data-btd="again">Draft again</button>' +
    '<button data-btd="close">Close</button><span id="btd-msg" class="msg"></span></div>';
  box.innerHTML = h;
  bpTrigDraftWire();
}

function bpTrigDraftWire() {
  const box = $('bp-trig-draft-box');
  box.querySelectorAll('[data-btd]').forEach((el) => {
    el.onclick = async () => {
      const act = el.dataset.btd;
      if (act === 'close') return bpTrigDraftClose();
      if (act === 'again') return bpDraftTriggers();
      if (act !== 'accept') return;
      bpTrigDraftSync();
      const take = [...box.querySelectorAll('[data-btd-take]')]
        .filter((c) => c.checked).map((c) => +c.dataset.btdTake);
      const rows = take.map((i) => {
        const t = state.bp.tdraft.triggers[i];
        return { when: t.when, arg: t.arg || '', from: t.from,
          to: t.to, objective: (t.objective || '').trim(), why: t.why };
      }).filter((t) => t.objective);
      if (!rows.length) {
        $('btd-msg').innerHTML = '<span class="err">nothing is ticked</span>';
        return;
      }
      const r = await post('/api/blueprint/trigger', { op: 'apply', triggers: rows });
      if (!r.ok) {
        $('btd-msg').innerHTML = `<span class="err">${esc(r.error)}</span>`;
        return;
      }
      bpTrigDraftClose();
      $('bp-note').innerHTML = `<span class="good">added ${r.added}, all disarmed` +
        (r.failed && r.failed.length ? ` &middot; ${r.failed.length} refused` : '') +
        '</span>';
      loadBpTriggers();
    };
  });
}

// --------------------------------------------------------------- context

async function loadBpActions() {
  if (!state.bp.map) await loadBpMap();
  const r = await api('/api/blueprint/actions');
  if (!r.ok) return;
  state.bp.actions = r.actions;
  $('bp-act').innerHTML = r.actions.map((a) =>
    `<option value="${esc(a.id)}" title="${esc(a.what)}">${esc(a.title)}</option>`).join('');
  const lanes = (state.bp.map.nodes || []).map((n) => n.lane);
  $('bp-lane').innerHTML = lanes.map((l) =>
    `<option value="${esc(l)}"${l === state.sel ? ' selected' : ''}>${esc(l)}</option>`).join('');
  bpActNote();
}

function bpActNote() {
  const a = (state.bp.actions || []).find((x) => x.id === $('bp-act').value);
  $('bp-ctx-sub').textContent = a ? a.what : '';
}

async function runBpContext() {
  const action = $('bp-act').value;
  const lane = $('bp-lane').value;
  $('bp-ctx').innerHTML = '<div class="spin">building it&hellip;</div>';
  state.bp.sel = new Set();
  const r = await api(`/api/blueprint/context?action=${encodeURIComponent(action)}` +
    `&lane=${encodeURIComponent(lane)}`);
  state.bp.ctx = r;
  renderBpContext();
}

function renderBpContext() {
  const c = state.bp.ctx;
  if (!c.ok) {
    // Not an error page. "There is no open proposal in this lane to sharpen" is
    // an answer about the workspace, and the operator asked a fair question.
    $('bp-ctx').innerHTML =
      `<div class="bp-nosubject">${esc(c.error || 'the server did not answer')}</div>`;
    return;
  }
  let h = `<div class="bp-ctx-head"><h3>${esc(c.title)}</h3>` +
    `<span class="muted">${esc(c.what)}</span>` +
    `<div class="bp-ctx-sum">about <b>${esc(c.subject)}</b> &middot; ` +
    `${c.chars.toLocaleString()} chars &middot; ~${c.tokens_est.toLocaleString()} tokens &middot; ` +
    `${c.blocks.length} block(s)</div></div>`;

  if (c.empty.length) {
    // The most valuable thing on the page. A block that was built and came back
    // with nothing is a hole in what the agent knows, and it is invisible in
    // the prompt itself precisely because there is nothing there to see.
    h += '<div class="bp-empty-blocks"><b>Built, but had nothing to say &mdash; ' +
      'so it is not in this prompt at all:</b><ul>' +
      c.empty.map((e) => `<li><code>${esc(e.fn)}</code> &mdash; ${esc(e.why)}` +
        (e.reads.length ? `<span class="muted"> (reads ${esc(e.reads.join('; '))})</span>` : '') +
        '</li>').join('') + '</ul></div>';
  }

  h += '<div class="bp-blocks">' + c.blocks.map((b, i) => {
    const share = Math.round((b.chars / Math.max(c.chars, 1)) * 100);
    return `<div class="bp-block" data-i="${i}">` +
      '<div class="bp-block-head">' +
      `<label class="bp-pick"><input type="checkbox" data-pick="${i}"></label>` +
      `<span class="bp-block-name">${esc(b.heading)}</span>` +
      (b.headings > 1 ? `<span class="bp-sub">${b.headings} documents</span>` : '') +
      `<span class="bp-size" style="--w:${share}%">${b.chars.toLocaleString()} chars ` +
      `&middot; ~${b.tokens_est.toLocaleString()} tok &middot; ${share}%</span></div>` +
      `<div class="bp-block-from"><code>${esc(b.from)}</code>` +
      (b.shared ? '<span class="bp-shared">shared with other calls</span>' : '') +
      `<span class="muted"> &mdash; ${esc(b.why)}</span></div>` +
      (b.reads.length
        ? '<div class="bp-reads">reads: ' + b.reads.map((x) => `<code>${esc(x)}</code>`).join(' ') + '</div>'
        : '') +
      `<details class="bp-body"><summary>exact bytes</summary><pre>${esc(b.text)}</pre></details>` +
      '</div>';
  }).join('') + '</div>';

  h += '<div class="bp-drill hidden" id="bp-drill"></div>';
  $('bp-ctx').innerHTML = h;
  $('bp-ctx').querySelectorAll('[data-pick]').forEach((el) =>
    el.onchange = () => {
      const i = Number(el.dataset.pick);
      if (el.checked) state.bp.sel.add(i); else state.bp.sel.delete(i);
      renderBpDrill();
    });
}

function renderBpDrill() {
  const box = $('bp-drill');
  const n = state.bp.sel.size;
  if (!n) return box.classList.add('hidden');
  const c = state.bp.ctx;
  const picked = [...state.bp.sel].sort((a, b) => a - b).map((i) => c.blocks[i]);
  box.innerHTML =
    `<b>${n} block(s) picked</b> <span class="muted">${esc(picked.map((b) => b.heading).join(' · '))}</span>` +
    '<textarea id="bp-drill-q" rows="2" placeholder="what do you want to ask about these?"></textarea>' +
    '<div class="row"><button id="bp-drill-dock">Ask the orchestrator</button>' +
    '<button id="bp-drill-arch">Escalate to the architect</button>' +
    '<span class="muted">the orchestrator is fast and cheap; the architect is GPT-5.6 and its answer is recorded as a ruling on this lane</span>' +
    '<span id="bp-drill-msg" class="msg"></span></div>';
  box.classList.remove('hidden');

  // The excerpt is capped per block rather than in total, so picking two blocks
  // cannot silently drop the second one - which would be a question asked about
  // something the answerer never saw.
  const packet = () => {
    const q = $('bp-drill-q').value.trim();
    return `I am looking at the context the "${c.title}" call is sent about ` +
      `${c.subject} in lane ${c.lane}. These parts of it:\n\n` +
      picked.map((b) => `## ${b.heading}  (from ${b.from}, ${b.chars} chars)\n` +
        b.text.slice(0, 4000) + (b.text.length > 4000 ? '\n…[trimmed]' : '')).join('\n\n') +
      `\n\n---\n\n${q}`;
  };
  const need = () => {
    const q = $('bp-drill-q').value.trim();
    if (!q) $('bp-drill-msg').innerHTML = '<span class="err">ask something first</span>';
    return q;
  };
  $('bp-drill-dock').onclick = async () => {
    if (!need()) return;
    const r = await post('/api/chat', { lane: '', text: packet() });
    $('bp-drill-msg').innerHTML = r.ok
      ? '<span class="good">sent — the answer lands in the dock</span>'
      : `<span class="err">${esc(r.error)}</span>`;
    if (r.ok) loadChat();
  };
  $('bp-drill-arch').onclick = async () => {
    if (!need()) return;
    $('bp-drill-msg').innerHTML = '<span class="spin">consulting GPT-5.6…</span>';
    const r = await post('/api/ask', { lane: c.lane, question: packet() });
    $('bp-drill-msg').innerHTML = r.ok
      ? `<span class="good">${esc(r.consult.id)} — open it in Escalate</span>`
      : `<span class="err">${esc(r.error)}</span>`;
    if (r.ok) { loadRulings(); loadChat(); }
  };
}

function wireBlueprint() {
  $('btn-blueprint').onclick = openBlueprint;
  $('bp-close').onclick = closeBlueprint;
  document.querySelectorAll('.bp-tab').forEach((t) =>
    t.onclick = () => bpShow(t.dataset.bp));
  $('bp-reload').onclick = () => {
    state.bp.map = state.bp.trigs = state.bp.actions = null;
    if (state.bp.view === 'flow') loadBpFlow();
    if (state.bp.view === 'map') loadBpMap();
    if (state.bp.view === 'triggers') { loadBpMap(); loadBpTriggers(); }
    if (state.bp.view === 'context') loadBpActions();
  };
  document.querySelectorAll('.bp-lensb').forEach((b) =>
    b.onclick = () => {
      state.bp.flow.lens = b.dataset.lens;
      document.querySelectorAll('.bp-lensb').forEach((o) =>
        o.classList.toggle('active', o === b));
      loadBpFlow();
    });
  $('bp-flow-fit').onclick = () => {
    if (state.bp.flow.cy) state.bp.flow.cy.fit(undefined, 30);
  };
  $('bp-flow-act').onchange = loadBpFlow;
  $('bp-flow-lane').onchange = loadBpFlow;
  $('bp-flow-lvl').onchange = () => {
    state.bp.flow.level = $('bp-flow-lvl').value;
    // Changing level by hand is not arriving from a jump, so whatever a jump
    // asked to land on is dropped: landing on last level's node would either
    // miss or, worse, hit a node of the same name meaning something else.
    state.bp.flow.land = null;
    loadBpFlow();
  };
  $('bp-seed').onclick = bpDraftStacks;
  $('bp-stack-add').onclick = async () => {
    const name = prompt('What is this layer called?');
    if (!name) return;
    const r = await post('/api/blueprint/stack', { op: 'add', name });
    if (!r.ok) return alert(r.error);
    loadBpMap();
  };
  $('bp-trig-new').onclick = async () => {
    if (!state.bp.map) await loadBpMap();
    if (!state.bp.trigs) await loadBpTriggers();
    bpTriggerForm();
  };
  $('bp-trig-draft').onclick = bpDraftTriggers;
  $('bp-trig-step').onclick = async () => {
    $('bp-note').innerHTML = '<span class="spin">running…</span>';
    const r = await post('/api/blueprint/trigger', { op: 'step' });
    if (!r.ok) {
      $('bp-note').innerHTML = `<span class="err">${esc(r.error)}</span>`;
      return;
    }
    // Three different things can come out of one pass, and saying "wrote N
    // proposals" for a request that was only queued for another workspace
    // would claim something this pass did not do.
    const took = r.fired.filter((f) => f.admitted).length;
    const sent = r.fired.filter((f) => f.request).length;
    const made = r.fired.filter((f) => f.proposal).length;
    const said = [];
    if (made) said.push(`wrote ${made} proposal(s) here — in Direction, unscored`);
    if (sent) said.push(`queued ${sent} request(s) for another workspace`);
    if (took) said.push(`admitted ${took} request(s) sent to this one`);
    $('bp-note').innerHTML = said.length
      ? `<span class="good">${said.join('; ')}</span>` : 'nothing matched';
    loadBpTriggers();
  };
  $('bp-act').onchange = bpActNote;
  $('bp-ctx-go').onclick = runBpContext;
}

wireBlueprint();
wireLaneGrip();
wireLaneAdd();
wireGoals();
wireDirection();
wireSpecs();
wireLaneModes();
wireDeploy();
wirePrs();
wireMission();
wireMove();
wireReports();
refresh();
loadChat();
setInterval(retimeStamps, 15000);
