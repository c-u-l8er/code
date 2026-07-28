'use strict';

const $ = (id) => document.getElementById(id);
const state = {
  lanes: [], sel: null, health: {}, models: {}, busy: false, log: null,
  workers: {}, limits: {}, consults: [], consult: null, history: [],
  goals: [], goal: null, observations: [], dismissed: new Set(), resumeOf: null,
  findings: [], findingsAll: false, findingsSeen: -1, direction: null,
  obligations: [], obligationsSeen: -1, workspace: null, supervisor: null,
  settings: null,
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
      : 'saved.';
  st.className = 'good';
  await refresh();
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
function renderLanes() {
  const el = $('lane-list');
  if (!state.lanes.length) {
    paintLanes(el, '<div class="empty">No lanes configured.</div>');
    return;
  }
  const html = state.lanes
    .map((l) => {
      const tasks = (l.tasks || [])
        .map(
          (t) =>
            `<div class="t"><span class="pill ${esc((t.status || '').toLowerCase())}">${esc(t.status)}</span>` +
            (t.cost_usd ? `<span class="cost">$${t.cost_usd.toFixed(3)}</span>` : '') +
            `<span>${esc((t.title || '').replace(/\n/g, ' ').slice(0, 30))}</span></div>`
        )
        .join('');
      return (
        `<div class="lane ${state.sel === l.name ? 'sel' : ''}" data-lane="${esc(l.name)}">` +
        `<div class="n"><span class="name">${esc(l.name)}</span>` +
        `<span class="pill be-${esc(l.backend)}">${esc(l.backend)}</span>` +
        (l.bound ? '' : '<span class="pill unbound">no env</span>') +
        `</div><div class="repo">${esc(l.repo)} &middot; ${esc(l.branch)}</div>` +
        `<div class="tasks">${tasks}</div></div>`
      );
    })
    .join('');

  if (!paintLanes(el, html)) return;   // unchanged: listeners are still on the
                                       // nodes that are still there
  el.querySelectorAll('.lane').forEach((n) =>
    n.addEventListener('click', () => select(n.dataset.lane))
  );
}

/** Write `html` into `el` only if it differs. Returns whether it wrote. */
function paintLanes(el, html) {
  if (el.dataset.painted === html) return false;
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
  ['d-lane', 'f-lane', 'a-lane', 'l-lane'].forEach((id) => ($(id).textContent = name || '—'));
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
    if ($('pane-ask').classList.contains('active')) loadConsults(name);
    if ($('pane-direction').classList.contains('active')) loadDirection();
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

function timeAgo(iso) {
  const t = Date.parse(iso || '');
  if (!t) return '';
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 10) return 'just now';
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
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
  // The orchestrator's own conversation, rendered in full: this one is not a
  // summary of something you can open elsewhere, it IS the thing.
  if (m.kind === 'you')
    return `<div class="dm said"><span class="dm-at">${at}</span>` +
           `<span class="dm-body">${esc(m.text)}</span></div>`;
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

async function loadChat() {
  const r = await api('/api/chat');
  const feed = $('dock-feed');
  const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
  feed.innerHTML = r.messages.length
    ? r.messages.map(dockLine).join('')
    : '<div class="empty">Ask the orchestrator anything about this workspace, ' +
      'or tell it what you want done.</div>';
  // A turn takes as long as it takes. Keep looking until it lands, then stop:
  // the rest of the feed is derived from the board and refreshes with it.
  clearTimeout(loadChat.timer);
  if (r.messages.some((m) => m.kind === 'amp' && m.status === 'running'))
    loadChat.timer = setTimeout(loadChat, 2000);
  feed.querySelectorAll('.dm-open').forEach((b) =>
    b.addEventListener('click', () =>
      b.dataset.consult
        ? openConsult(b.dataset.lane, b.dataset.consult)
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
  el.classList.toggle('hidden', !all.length);
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
      : '');
  const more = $('fi-more');
  if (more) more.onclick = () => { state.findingsAll = !state.findingsAll; renderFindings(all); };
  el.querySelectorAll('.fi-x').forEach((b) =>
    b.addEventListener('click', async () => {
      await post('/api/findings/ack', { ids: [b.dataset.id] });
      loadFindings();
    })
  );
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
  el.classList.toggle('hidden', !bad.length);
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
  el.classList.toggle('hidden', !all.length);
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
    el.innerHTML = '<div class="empty">No goals in this lane. A goal is an objective with a ' +
      'definition of done — the architect plans it, workers carry it out one task at a time, ' +
      'and it runs itself until it is finished or it needs you.</div>';
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
      `<div class="row"><input id="ga-text" placeholder="Answer it, and it carries on"><button id="ga-go" class="primary">Answer</button></div></div>`
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
    (g.state === 'blocked' && !(g.questions || []).length
      ? `<button id="gp-go">Carry on anyway</button>` : '') +
    (g.pr_url
      ? `<a class="prlink" href="${esc(g.pr_url)}" target="_blank" rel="noreferrer">pull request open &rarr;</a>`
      : g.state === 'done' ? `<button id="gpub-go">Open pull request</button>` : '') +
    `<button id="gc-go" class="danger">Abandon</button></div>` +
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
  const gp = $('gp-go');
  if (gp) gp.onclick = async () => {
    gp.disabled = true; gp.textContent = 'going…';
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

function dirSection(s, cls) {
  if (!s) return '';
  return `<section class="dsec ${cls || ''}"><h4>${esc(s.title)}</h4>` +
    `<div class="md">${md(s.body)}</div></section>`;
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

  const props = (d.proposals || []).map((p) =>
    `<div class="dprop"><b>${mdi(p.text)}</b>` +
    (p.why ? `<div class="muted">${mdi(p.why)}</div>` : '') +
    `<div class="row"><span class="tag">${esc(p.lane || '')}</span>` +
    `<button class="dp-go primary" data-id="${esc(p.id)}">Adopt as a goal</button>` +
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
    `<section class="dsec"><h4>What we still do not know</h4>` +
    (d.open_theses ? `<div class="md">${md(d.open_theses.body)}</div>` : '') +
    (qs ? `<div class="dqs"><div class="muted">Proposed by the work, not yet in the doctrine ` +
      `— only you can put one there:</div>${qs}</div>` : '') + `</section>` +
    `<section class="dsec"><h4>Where there is left to go</h4>` +
    (props || '<div class="empty">No proposals. One appears when a goal finishes and the ' +
      'architect judges the lane has somewhere left to travel.</div>') +
    (revs ? `<h5>Recent reviews</h5>${revs}` : '') + `</section>` +
    dirSection(d.ladder, '') + dirSection(d.values, '') + dirSection(d.owed, '') +
    `<div class="muted dfoot">The two sections above are read from <code>` +
    `${esc(d.doctrine_path || 'DOCTRINE.md')}</code> and are injected verbatim into every ` +
    `plan, every review, and every worker prompt.</div>`;

  el.querySelectorAll('.fi-x').forEach((b) =>
    b.addEventListener('click', async () => {
      await post('/api/findings/ack', { ids: [b.dataset.id] });
      loadFindings(); loadDirection();
    }));
  el.querySelectorAll('.dp-go').forEach((b) =>
    b.addEventListener('click', async () => {
      b.disabled = true; b.textContent = 'planning…';
      const r = await post('/api/direction/proposal', { id: b.dataset.id, action: 'adopt' });
      $('dir-status').innerHTML = r.ok
        ? `opened goal ${esc(r.goal.id)}`
        : `<span class="err">${esc(r.error)}</span>`;
      await refresh(); loadDirection();
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
  await refresh();
  await loadChat();
  if ($('pane-direction').classList.contains('active')) loadDirection();
}

async function openMission() {
  $('mission').classList.remove('hidden');
  $('mi-status').textContent = '';
  $('mi-ws-status').textContent = '';
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

function openConsult(lane, cid) {
  if (lane !== state.sel) select(lane);
  state.consult = cid;          // what showTab's load will settle on
  showTab('ask');
}

async function sendChat() {
  const el = $('dock-text');
  const text = el.value.trim();
  if (!text) return;
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
  $('polled').textContent = s.polled_at ? `polled ${s.polled_at}` : 'never polled';

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

async function loadCredits() {
  const c = await api('/api/credits');
  $('credit').textContent = c.ok ? `$${c.remaining} left` : '';
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
$('btn-set-close').onclick = () => $('settings').classList.add('hidden');
$('set-openrouter').onchange = (e) => setFlag('openrouter_enabled', e.target.checked);
$('set-auto-escalate').onchange = (e) => setFlag('auto_escalate', e.target.checked);
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
  loadCredits();
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
  if (name === 'history' && !state.history.length) loadHistory();
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

wireLaneAdd();
wireGoals();
wireDirection();
wireMission();
refresh();
loadCredits();
loadChat();
setInterval(retimeStamps, 15000);
