/* ---------------------------------------------------------------
   code — marketing site
   No framework, no build step. Same rule as the app it advertises.
   --------------------------------------------------------------- */
(function () {
  'use strict';

  var $  = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ------------------------------------------------------------ nav */

  var nav = $('#nav');
  var onScroll = function () { nav.classList.toggle('stuck', window.scrollY > 8); };
  addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  /* ------------------------------------------------------------ reveal */

  var revealTargets = [
    '.roles > *', '.split > *', '.rung', '.rule', '.owed',
    '.subcard', '.spendbar', '.g8', '.alertrow', '.st', '.codeblock.big', '.cmds'
  ].join(',');

  if (!reduced && 'IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        e.target.classList.add('in');
        io.unobserve(e.target);
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.06 });

    $$(revealTargets).forEach(function (el, i) {
      el.classList.add('rv');
      el.style.transitionDelay = (Math.min(i % 8, 5) * 45) + 'ms';
      io.observe(el);
    });
  }

  /* ------------------------------------------------------------ copy buttons */

  $$('.copybtn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var src = $(btn.dataset.copy);
      if (!src) return;
      var text = src.innerText.replace(/^\s*\$\s?/gm, '');
      navigator.clipboard.writeText(text).then(function () {
        var was = btn.textContent;
        btn.textContent = 'copied';
        btn.classList.add('done');
        setTimeout(function () { btn.textContent = was; btn.classList.remove('done'); }, 1400);
      }).catch(function () { btn.textContent = 'ctrl-c'; });
    });
  });

  /* ------------------------------------------------------------ ladder */

  var rungs = $$('#ladder .rung');
  var setRung = function (n) {
    rungs.forEach(function (r, i) { r.classList.toggle('on', i <= n); });
  };
  rungs.forEach(function (r, i) {
    r.addEventListener('mouseenter', function () { setRung(i); });
    r.addEventListener('focus', function () { setRung(i); });
    r.addEventListener('click', function () { setRung(i); });
  });
  setRung(2); // live_local — where the console honestly is

  /* ------------------------------------------------------------ autonomy slider */

  var sl = $('#sl'), slOut = $('#sl-out'), slNote = $('#sl-note');
  var noteFor = function (v) {
    if (v === 0)   return '0% — anything that has been scored at all starts on its own. Nothing waits for you.';
    if (v <= 25)   return 'Almost everything starts itself. You find out what the fleet decided by reading Direction.';
    if (v <= 55)   return 'Confident work runs; anything the architect thinks will stop and ask you waits instead.';
    if (v <= 85)   return 'Only goals judged very likely to finish unattended start on their own. The rest queue with their reason showing.';
    if (v < 100)   return 'Near-manual. A goal has to look nearly certain before it moves without you.';
    return '100% — nothing starts without you. Every proposed goal waits in Direction. This is the manual setting.';
  };
  var paint = function () {
    var v = +sl.value;
    sl.style.setProperty('--pct', v + '%');
    slOut.textContent = v + '%';
    slNote.textContent = noteFor(v);
  };
  sl.addEventListener('input', paint);
  paint();

  /* ------------------------------------------------------------ console preview

     A structural replica of the app shell — lane rail, eight tabs, detail pane,
     orchestrator dock — with every string rendered as a bar rather than as text.

     Two reasons it is drawn this way rather than filled with sample copy:
       1. The layout is the claim. What a given lane is working on is not.
       2. Sample copy in a hero is still copy. The version this replaced asserted
          specific rung movements, spend figures and test counts as decoration,
          none of which the page can stand behind. Bars assert nothing.

     Deterministic PRNG, so the ragged edges are identical on every load. A posed
     set, not a slot machine. */

  var rnd = (function (seed) {
    return function () {
      seed = (seed * 1664525 + 1013904223) % 4294967296;
      return seed / 4294967296;
    };
  })(20260728);

  var pick = function (lo, hi) { return lo + Math.floor(rnd() * (hi - lo + 1)); };

  /* one "word" */
  function gl(w, cls) {
    return '<i class="gl' + (cls ? ' ' + cls : '') + '" style="width:' + w + 'px"></i>';
  }
  /* one "line", filled to roughly `total` px with word-sized bars */
  function glline(total, cls) {
    var out = '', left = total;
    while (left > 16) {
      var w = Math.min(left, pick(15, 58));
      out += gl(w, cls);
      left -= w + 7;
    }
    return '<div class="glrow">' + out + '</div>';
  }
  /* a "paragraph" — last line short, the way prose actually wraps */
  function glpara(lines, total, cls) {
    var out = '';
    for (var i = 0; i < lines; i++) {
      out += glline(i === lines - 1 ? Math.round(total * (0.4 + rnd() * 0.35)) : total, cls);
    }
    return out;
  }
  /* key → value row, as in the real Dispatch pane */
  function glkv(kw, vw) {
    return '<div class="glkv">' + gl(kw, 'dim') + gl(vw) + '</div>';
  }
  /* a ticked checklist row */
  function gltick(tone, w) {
    return '<div class="gltick"><i class="tk ' + tone + '"></i>' + gl(w) + '</div>';
  }
  function box(label, inner) {
    return '<div class="vbox">' + (label ? '<div class="vlabel">' + gl(label, 'dim') + '</div>' : '') + inner + '</div>';
  }

  var LANES = [
    { state: 'running', w: 54, spent: 1.31, cap: 2.00 },
    { state: 'running', w: 82, spent: 0.42, cap: 1.00 },
    { state: 'blocked', w: 66, spent: 0.94, cap: 1.00 },
    { state: 'waiting', w: 38, spent: 0,    cap: 0 },
    { state: 'idle',    w: 48, spent: 0,    cap: 0 }
  ];

  var laneEl = $('#c-lanes');
  var viewEl = $('#c-view');
  var tabEl  = $('#c-tabs');
  var sel = 0, view = 0;

  /* the eight tabs the app actually carries, as label widths only */
  var TABS = [62, 42, 58, 30, 34, 66, 52, 54];

  function renderLanes() {
    laneEl.innerHTML = LANES.map(function (l, i) {
      var pct = l.cap ? Math.min(100, (l.spent / l.cap) * 100) : 0;
      return '<li class="clane' + (i === sel ? ' sel' : '') + '" data-i="' + i + '">' +
        '<div class="cl-top">' + gl(l.w, 'lead') +
          '<i class="lst ' + l.state + '"></i></div>' +
        '<div class="glrow tight">' + gl(pick(26, 40), 'dim') + gl(pick(30, 48), 'dim') + '</div>' +
        '<div class="glrow tight">' + gl(pick(70, 130), 'dim') + '</div>' +
        (l.cap
          ? '<div class="cl-bar"><span style="width:' + pct + '%"></span></div>' +
            '<div class="glrow tight">' + gl(pick(38, 56), 'dim') + '</div>'
          : '') +
        '</li>';
    }).join('');
  }

  function renderTabs() {
    tabEl.innerHTML = TABS.map(function (w, i) {
      return '<div class="ctab' + (i === view ? ' active' : '') + '" data-i="' + i + '">' +
        gl(w) + '</div>';
    }).join('');
  }

  /* Eight panes, each with a visibly different density and rhythm, so moving
     across the tab row reads as depth rather than as one template restyled. */
  var VIEWS = [

    /* dispatch — worker, branch, worktree, cap, then the task itself */
    function () {
      return box(0, glkv(46, 74) + glkv(38, 96) + glkv(56, 128) + glkv(24, 40)) +
        box(26, glpara(4, 470)) +
        '<div class="vfoot">' + gl(44, 'go') + gl(58, 'dim') + gl(36, 'dim') + '</div>';
    },

    /* goals — objective, definition of done, odds of finishing unattended */
    function () {
      return box(52, glpara(2, 430)) +
        box(88, gltick('ok', 168) + gltick('ok', 208) + gltick('warn', 186) + gltick('mut', 232)) +
        box(76, '<div class="cl-bar tall"><span style="width:78%"></span></div>' +
          '<div class="glrow tight">' + gl(212, 'dim') + '</div>');
    },

    /* direction — proposed goals, each scored, each waiting or not */
    function () {
      var rows = '';
      for (var i = 0; i < 5; i++) {
        rows += '<div class="glprop">' +
          '<i class="lst ' + (i < 2 ? 'running' : i === 2 ? 'waiting' : 'idle') + '"></i>' +
          '<div class="gp-body">' + gl(pick(150, 260)) +
            '<div class="cl-bar"><span style="width:' + pick(24, 92) + '%"></span></div></div>' +
          gl(28, 'dim') + '</div>';
      }
      return box(64, rows);
    },

    /* log — the worker transcript, densest pane in the app */
    function () {
      var out = '';
      for (var i = 0; i < 13; i++) {
        var lead = i % 4 === 0;
        out += '<div class="glrow log' + (lead ? ' lead' : '') + '">' +
          gl(pick(26, 40), 'dim') +
          gl(pick(lead ? 180 : 90, lead ? 380 : 300), lead ? 'accent' : '') + '</div>';
      }
      return box(30, '<div class="stream">' + out + '</div>');
    },

    /* diff — files touched on the worker branch, with churn */
    function () {
      var out = '';
      for (var i = 0; i < 6; i++) {
        out += '<div class="gldiff">' + gl(pick(90, 210), 'dim') +
          '<span class="pm"><i class="add" style="width:' + pick(8, 46) + 'px"></i>' +
          '<i class="del" style="width:' + pick(4, 26) + 'px"></i></span></div>';
      }
      return box(34, out) + box(40, glpara(3, 440, 'dim'));
    },

    /* ask — the escalation packet, assembled not pasted */
    function () {
      var out = '';
      for (var i = 0; i < 6; i++) {
        out += '<div class="glpack">' + gl(pick(80, 150), 'accent') +
          '<i class="lead"></i>' + gl(pick(20, 44), 'dim') + '</div>';
      }
      return box(96, out) + box(44, glpara(3, 460)) +
        '<div class="vfoot">' + gl(28, 'go') + gl(92, 'dim') + '</div>';
    },

    /* history — every dispatch this lane has ever taken */
    function () {
      var out = '';
      for (var i = 0; i < 7; i++) {
        out += '<div class="glhist">' + gl(52, 'dim') +
          '<i class="lst ' + (i === 1 ? 'blocked' : i === 4 ? 'waiting' : 'running') + '"></i>' +
          gl(pick(120, 250)) + gl(30, 'dim') + '</div>';
      }
      return box(48, out);
    },

    /* rulings — what the architect decided, kept verbatim */
    function () {
      return box(46, '<div class="glquote">' + glpara(3, 420) + '</div>') +
        box(58, '<div class="glquote">' + glpara(2, 420) + '</div>') +
        '<div class="vfoot">' + gl(72, 'dim') + '</div>';
    }
  ];

  /* Memoised per (tab, lane).

     Two reasons, one cosmetic and one structural. The PRNG is stateful, so
     re-running a pane builder yields different bar widths — hovering away and
     back would visibly reshuffle the "text", which real text never does and
     which reads as flicker. Caching also means each lane keeps its own pane
     content, so moving down the rail actually shows you something rather than
     redrawing one template five times. */
  var viewCache = {};

  function renderView() {
    var key = view + ':' + sel;
    if (!(key in viewCache)) viewCache[key] = VIEWS[view]();
    viewEl.innerHTML = viewCache[key];
  }

  /* Hover is enough to explore it. No auto-advance: a hero that reshuffles
     itself while you are reading the paragraph beside it is a distraction,
     not a demo. */
  function selectLane(i) {
    if (i === sel) return;
    sel = i; renderLanes(); renderView();
  }
  function selectTab(i) {
    if (i === view) return;
    view = i; renderTabs(); renderView();
  }

  laneEl.addEventListener('mouseover', function (e) {
    var li = e.target.closest('.clane');
    if (li) selectLane(+li.dataset.i);
  });
  laneEl.addEventListener('click', function (e) {
    var li = e.target.closest('.clane');
    if (li) selectLane(+li.dataset.i);
  });
  tabEl.addEventListener('mouseover', function (e) {
    var t = e.target.closest('.ctab');
    if (t) selectTab(+t.dataset.i);
  });
  tabEl.addEventListener('click', function (e) {
    var t = e.target.closest('.ctab');
    if (t) selectTab(+t.dataset.i);
  });

  /* dock: a short back-and-forth, rendered as bubbles */
  var dock = $('#c-dockfeed');
  if (dock) {
    var bubbles = '';
    [['in', 108], ['out', 82], ['in', 124]].forEach(function (b) {
      bubbles += '<div class="dk-msg ' + b[0] + '">' + gl(b[1]) + '</div>';
    });
    dock.innerHTML = bubbles;
  }

  renderLanes();
  renderTabs();
  renderView();

  /* One slow-moving element so the panel reads as live rather than posed.
     Ten seconds a step, one bar, no colour change — below the threshold that
     pulls the eye off the copy. */
  if (!reduced) {
    var credit = $('#c-credit > span');
    setInterval(function () {
      var l = LANES[0];
      if (l.spent >= l.cap - 0.02) return;
      l.spent = Math.round((l.spent + 0.03) * 100) / 100;
      var bar = laneEl.querySelector('.clane .cl-bar span');
      if (bar) bar.style.width = Math.min(100, (l.spent / l.cap) * 100) + '%';
      if (credit) credit.style.width = Math.max(8, 62 - (l.spent / l.cap) * 30) + '%';
    }, 10000);
  }

})();
