'use strict';

const $ = (id) => document.getElementById(id);
const state = { lanes: [], sel: null, health: {}, models: {}, busy: false };

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

function renderHealth() {
  const h = state.health;
  const dot = (ok, label) =>
    `<span class="h"><span class="dot ${ok ? 'ok' : 'bad'}"></span>${label}</span>`;
  $('health').innerHTML =
    dot(h.codex_installed, 'codex') +
    dot(h.codex_logged_in, 'auth') +
    dot(h.openrouter_key, 'gpt-5.6');

  const problems = [];
  if (!h.codex_installed) problems.push('Codex CLI missing &mdash; <code>npm i -g @openai/codex</code>');
  else if (!h.codex_logged_in) problems.push('Codex not logged in &mdash; run <code>codex login</code> in a terminal');
  if ((h.unbound_lanes || []).length)
    problems.push(
      `${h.unbound_lanes.length} lane(s) have no Codex env id &mdash; run <code>codex cloud</code>, copy the id, paste it on the lane`
    );
  if (!h.openrouter_key) problems.push('No OpenRouter key &mdash; escalation disabled');

  const b = $('banner');
  if (problems.length) {
    b.innerHTML = problems.join(' &nbsp;·&nbsp; ');
    b.classList.remove('hidden');
    b.classList.toggle('bad', !h.codex_logged_in || !h.codex_installed);
  } else {
    b.classList.add('hidden');
  }
}

function renderLanes() {
  const el = $('lane-list');
  if (!state.lanes.length) {
    el.innerHTML = '<div class="empty">No lanes configured.</div>';
    return;
  }
  el.innerHTML = state.lanes
    .map((l) => {
      const tasks = (l.tasks || [])
        .map(
          (t) =>
            `<div class="t"><span class="pill ${esc((t.status || '').toLowerCase())}">${esc(t.status)}</span>` +
            `<span>${esc((t.title || '').slice(0, 34))}</span></div>`
        )
        .join('');
      return (
        `<div class="lane ${state.sel === l.name ? 'sel' : ''}" data-lane="${esc(l.name)}">` +
        `<div class="n"><span class="name">${esc(l.name)}</span>` +
        (l.bound ? '' : '<span class="pill unbound">no env</span>') +
        `</div><div class="repo">${esc(l.repo)} &middot; ${esc(l.branch)}</div>` +
        `<div class="tasks">${tasks}</div></div>`
      );
    })
    .join('');

  el.querySelectorAll('.lane').forEach((n) =>
    n.addEventListener('click', () => select(n.dataset.lane))
  );
}

function select(name) {
  state.sel = name;
  const lane = state.lanes.find((l) => l.name === name);
  ['d-lane', 'f-lane', 'a-lane'].forEach((id) => ($(id).textContent = name || '—'));
  if (lane) $('d-branch').value = lane.branch || 'main';
  renderLanes();
  bindEnvPromptIfNeeded(lane);
}

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
  $('polled').textContent = s.polled_at ? `polled ${s.polled_at}` : 'never polled';

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

  renderHealth();
  renderLanes();
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
      $('r-out').textContent = d.ok ? d.text : d.error;
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

$('btn-dispatch').onclick = async () => {
  if (!state.sel) return;
  const prompt = $('d-prompt').value.trim();
  if (!prompt) {
    $('d-status').innerHTML = '<span class="err">prompt is empty</span>';
    return;
  }
  busy(true, $('d-status'), 'dispatching…');
  const r = await post('/api/dispatch', {
    lane: state.sel,
    prompt,
    branch: $('d-branch').value.trim(),
    attempts: Number($('d-attempts').value) || 1,
  });
  busy(false, $('d-status'));
  $('d-status').innerHTML = r.ok
    ? '<span class="good">dispatched</span>'
    : `<span class="err">${esc(r.error)}</span>`;
  if (r.ok) {
    $('d-prompt').value = '';
    setTimeout(() => $('btn-poll').click(), 1200);
  }
};

$('btn-loaddiff').onclick = async () => {
  if (!state.sel) return;
  $('f-out').textContent = 'loading…';
  const a = $('f-attempt').value;
  const r = await api(`/api/diff?lane=${encodeURIComponent(state.sel)}${a ? '&attempt=' + a : ''}`);
  if (!r.ok) {
    $('f-out').innerHTML = `<span class="err">${esc(r.error || 'failed')}</span>`;
    return;
  }
  $('f-out').innerHTML = r.diff ? colorDiff(r.diff) : '(empty diff)';
};

$('btn-apply').onclick = async () => {
  if (!state.sel) return;
  if (!confirm(`Apply the latest Codex diff into your local ${state.sel} worktree?`)) return;
  $('f-out').textContent = 'applying…';
  const a = $('f-attempt').value;
  const r = await post('/api/apply', { lane: state.sel, attempt: a ? Number(a) : null });
  $('f-out').innerHTML = (r.ok ? '<span class="good">applied</span>\n\n' : '<span class="err">failed</span>\n\n') + esc(r.output);
};

$('btn-ask').onclick = async () => {
  if (!state.sel) return;
  const q = $('a-question').value.trim();
  if (!q) {
    $('a-status').innerHTML = '<span class="err">question is empty</span>';
    return;
  }
  busy(true, $('a-status'), 'building packet, consulting GPT-5.6…');
  $('a-out').textContent = '';
  const r = await post('/api/ask', { lane: state.sel, question: q, model: $('a-model').value });
  busy(false, $('a-status'));
  if (!r.ok) {
    $('a-status').innerHTML = `<span class="err">${esc(r.error)}</span>`;
    return;
  }
  const u = r.usage || {};
  $('a-status').innerHTML = `<span class="good">${esc(r.model)}</span> · ${u.total_tokens || '?'} tok · saved ${esc(r.saved)}`;
  $('a-out').textContent = r.ruling;
  loadCredits();
  loadRulings();
};

document.querySelectorAll('.tab').forEach((t) =>
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((x) => x.classList.remove('active'));
    document.querySelectorAll('.pane').forEach((x) => x.classList.remove('active'));
    t.classList.add('active');
    $('pane-' + t.dataset.tab).classList.add('active');
    if (t.dataset.tab === 'rulings') loadRulings();
  })
);

refresh();
loadCredits();
