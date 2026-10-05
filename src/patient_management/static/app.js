// Fixed dashboard behaviour: layout, navigation, charts, tooltips, copy, SSE updates.
// Server HTML is already escaped; data text is only ever written with textContent.
(function () {
  "use strict";
  var pid = document.body.dataset.patient;
  var grid = document.getElementById("grid");
  document.body.classList.add("has-js");
  var ORDER = ["overview", "examinations", "tasks", "knowledge"];
  var current = null;
  var section = "overview";
  var examMode = "labs", detail = null;
  var overviewTab = "diagnoses";
  var displayPreference = null;
  try {
    var savedMode = localStorage.getItem('pm-display');
    if (savedMode === 'mobile' || savedMode === 'desktop') displayPreference = savedMode;
    document.body.classList.toggle('sidebar-collapsed', localStorage.getItem('pm-sidebar-collapsed') === 'true');
  } catch (e) {} // Preferences must not block the dashboard.

  function card(key) { return document.getElementById("m-" + key); }

  function layout() {
    var mode = displayPreference || (window.innerWidth < 768 ? 'mobile' : 'desktop');
    if (mode === current) return;
    current = mode;
    document.body.dataset.display = mode;
    document.body.classList.remove('menu-open');
    document.querySelector('.menu-backdrop').hidden = true;
    document.querySelectorAll('[data-display-mode]').forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.displayMode === mode)); });
    updateSidebar();
    setSection(section, false);
    initSelectors(document);
    showOverview();
    showExams();
    drawAll(document);
  }

  function setSection(key, scroll) {
    section = key === "knowledge" ? "knowledge" : key === "tasks" ? "tasks" : ["examinations", "lab_trends", "investigations"].indexOf(key) >= 0 ? "examinations" : "overview";
    if (key === "diagnoses") overviewTab = "diagnoses";
    if (key === "lab_trends" || key === "investigations") { examMode = key === "lab_trends" ? "labs" : "investigations"; detail = null; }
    document.body.dataset.section = section;
    ORDER.forEach(function (k) {
      var c = card(k);
      if (c) c.hidden = false;
    });
    document.querySelectorAll("[data-nav]").forEach(function (a) {
      var active = a.dataset.nav === section || (section === "examinations" &&
                   a.dataset.nav === (examMode === "labs" ? "lab_trends" : "investigations"));
      a.classList.toggle("active", active);
      if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    if (scroll) {
      var target = card(key) || card(section);
      if (target) { target.closest('details.major-section').open = true; target.scrollIntoView({behavior:"smooth", block:"start"}); }
    }
    showExams();
    showOverview();
    drawAll(document);
  }

  function updateSidebar() {
    var open = current === 'mobile' ? document.body.classList.contains('menu-open') : !document.body.classList.contains('sidebar-collapsed');
    document.querySelector('.sidebar').inert = current === 'mobile' && !open;
    var toggle = document.querySelector('.sidebar-toggle');
    toggle.setAttribute('aria-expanded', String(open));
    toggle.setAttribute('aria-label', open ? '收起导航' : '展开导航');
    toggle.innerHTML = (open ? '‹' : '›') + '<span>' + (open ? '收起导航' : '展开导航') + '</span>';
    document.querySelector('.menu-toggle').setAttribute('aria-expanded', String(open));
    document.querySelector('.menu-toggle').setAttribute('aria-label', open ? '收起导航' : '展开导航');
  }

  function showOverview() {
    var root = card('overview');
    root.querySelectorAll('[data-overview-tab]').forEach(function (b) {
      var active = b.dataset.overviewTab === overviewTab;
      b.setAttribute('aria-selected', String(active)); b.tabIndex = active ? 0 : -1;
    });
    root.querySelectorAll('[data-overview-pane]').forEach(function (p) { p.hidden = p.dataset.overviewPane !== overviewTab; });
  }

  function showExams() {
    var root = card("examinations");
    if (!root) return;
    if (detail && !Array.from(root.querySelectorAll('[data-detail]')).some(function (p) { return p.dataset.detail === detail; })) detail = null;
    root.querySelectorAll('[data-exam-mode]').forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.examMode === examMode)); });
    root.querySelectorAll('[data-exam-kind]').forEach(function (p) {
      p.hidden = p.dataset.examKind !== examMode;
      p.querySelector('[data-exam-list]').hidden = !!detail;
      p.querySelectorAll('[data-detail]').forEach(function (d) { d.hidden = d.dataset.detail !== detail; });
    });
    drawAll(root);
  }

  function initSelectors(root) {
    root.querySelectorAll(".selector").forEach(function (list) {
      var selected = list.querySelector('[aria-selected="true"]') || list.querySelector("button");
      if (selected) selectItem(selected);
    });
  }

  function selectItem(button) {
    var panel = button.closest(".card");
    var lab = button.hasAttribute("data-select-lab");
    var assessment = button.hasAttribute("data-select-assessment");
    var interpretation = button.hasAttribute("data-select-interp");
    var key = lab ? "series" : assessment ? "assessment" : interpretation ? "interpretation" : "investigation";
    var value = lab ? button.dataset.selectLab : assessment ? button.dataset.selectAssessment : interpretation ? button.dataset.selectInterp : button.dataset.selectInv;
    panel.querySelectorAll('.selector button').forEach(function (b) {
      b.setAttribute("aria-selected", b === button ? "true" : "false");
      b.tabIndex = b === button ? 0 : -1;
    });
    panel.querySelectorAll(lab ? "details.lab" : assessment ? ".assessment-panel" : interpretation ? ".interpretation" : ".investigation").forEach(function (p) {
      p.hidden = p.dataset[key] !== value;
      if (lab && !p.hidden) p.open = true;
    });
    drawAll(panel);
  }

  // ----- charts (inline SVG, objective values only; colour marks out-of-range, never "good/bad") -----
  var NS = "http://www.w3.org/2000/svg";
  function el(name, attrs) { var e = document.createElementNS(NS, name); for (var k in attrs) e.setAttribute(k, attrs[k]); return e; }

  function draw(box) {
    if (!box.getBoundingClientRect().width) return;
    var spec;
    try { spec = JSON.parse(box.dataset.chart); } catch (e) { return; }
    var pts = spec.points.filter(function (p) { return typeof p.v === "number"; });
    if (pts.length < 2) return;
    var W = box.clientWidth || 300, H = box.clientHeight || 150, L = 44, R = 12, T = 10, B = 24;
    var ts = pts.map(function (p) { return Date.parse(p.t); });
    var t0 = Math.min.apply(null, ts), t1 = Math.max.apply(null, ts);
    var vals = pts.map(function (p) { return p.v; });
    var lo = spec.ref && typeof spec.ref.low === "number" ? spec.ref.low : null;
    var hi = spec.ref && typeof spec.ref.high === "number" ? spec.ref.high : null;
    var ymin = Math.min.apply(null, vals.concat(lo === null ? [] : [lo]));
    var ymax = Math.max.apply(null, vals.concat(hi === null ? [] : [hi]));
    if (ymax === ymin) { ymax += 1; ymin -= 1; }
    var pad = (ymax - ymin) * 0.12; ymin -= pad; ymax += pad;
    var x = function (t) { return t1 === t0 ? (L + W - R) / 2 : L + (t - t0) / (t1 - t0) * (W - L - R); };
    var y = function (v) { return T + (ymax - v) / (ymax - ymin) * (H - T - B); };
    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, preserveAspectRatio: "none" });
    if (lo !== null || hi !== null) {
      var top = y(hi === null ? ymax : hi), bot = y(lo === null ? ymin : lo);
      svg.appendChild(el("rect", { x: L, y: top, width: W - L - R, height: Math.max(0, bot - top), fill: "#eef3f9" }));
    }
    [ymin + pad, (ymin + ymax) / 2, ymax - pad].forEach(function (v) {
      svg.appendChild(el("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: "#e6eaf0", "stroke-width": 1 }));
      var lab = el("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end", "font-size": 11, fill: "#8a95a3" });
      lab.textContent = (+v.toPrecision(3)).toString(); svg.appendChild(lab);
    });
    [pts[0], pts[pts.length - 1]].forEach(function (p, i) {
      var d = el("text", { x: x(Date.parse(p.t)), y: H - 6, "text-anchor": i ? "end" : "start", "font-size": 11, fill: "#8a95a3" });
      d.textContent = p.t.slice(5, 10); svg.appendChild(d);
    });
    svg.appendChild(el("polyline", { points: pts.map(function (p) { return x(Date.parse(p.t)) + "," + y(p.v); }).join(" "),
      fill: "none", stroke: "#0878ff", "stroke-width": 2.5, "stroke-linejoin": "round" }));
    pts.forEach(function (p) {
      var cx = x(Date.parse(p.t)), cy = y(p.v), out = p.flag && p.flag !== "正常";
      svg.appendChild(el("circle", { cx: cx, cy: cy, r: 4, fill: out ? "#b25e00" : "#1a5fb4", stroke: "#fff", "stroke-width": 1.5 }));
      var hit = el("circle", { cx: cx, cy: cy, r: 22, fill: "transparent", tabindex: 0, role: "button" });
      hit.setAttribute("aria-label", p.t + " " + p.raw + " " + (spec.unit || "") + " " + (p.flag || ""));
      hit.addEventListener("click", function (ev) { ev.stopPropagation(); show(ev, p, spec.unit); });
      hit.addEventListener('pointermove', function (ev) { if (ev.pointerType !== 'touch') show(ev, p, spec.unit); });
      hit.addEventListener('pointerleave', hideTip);
      hit.addEventListener('blur', hideTip);
      hit.addEventListener("keydown", function (ev) { if (ev.key === "Enter") show(ev, p, spec.unit); });
      svg.appendChild(hit);
    });
    box.textContent = "";
    box.appendChild(svg);
  }

  var tipTimer;
  function hideTip() {
    clearTimeout(tipTimer);
    document.getElementById('tip').hidden = true;
  }
  function show(ev, p, unit) {
    clearTimeout(tipTimer);
    var tip = document.getElementById('tip');
    tip.textContent =
      p.t.replace("T", " ") + "  " + p.raw + " " + (unit || "") + (p.flag ? "  " + p.flag : "");
    tip.hidden = false;
    var r = ev.target.getBoundingClientRect();
    var x = ev.clientX || r.left + r.width / 2, y = ev.clientY || r.top + r.height / 2;
    tip.style.left = Math.max(8, Math.min(window.innerWidth - tip.offsetWidth - 8, x + 12)) + 'px';
    tip.style.top = Math.max(8, Math.min(window.innerHeight - tip.offsetHeight - 8, y - tip.offsetHeight - 12)) + 'px';
    if (ev.type === 'click') tipTimer = setTimeout(hideTip, 2500);
  }
  document.addEventListener('click', hideTip);
  document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') hideTip(); });
  window.addEventListener('scroll', hideTip, {passive:true});

  function drawAll(root) {
    root.querySelectorAll("details.lab[open] .chart").forEach(function (box) { if (box.getBoundingClientRect().width) draw(box); });
  }
  document.addEventListener("toggle", function (ev) {
    if (ev.target.matches && ev.target.matches("details.lab") && ev.target.open) ev.target.querySelectorAll(".chart").forEach(draw);
    if (ev.target.matches && ev.target.matches('details.major-section') && ev.target.open) drawAll(ev.target);
  }, true);
  var rt;
  window.addEventListener("resize", function () { clearTimeout(rt); rt = setTimeout(function () { layout(); drawAll(document); }, 150); });

  // ----- navigation -----
  document.addEventListener("click", function (ev) {
    var nav = ev.target.closest("[data-nav]");
    if (nav) {
      ev.preventDefault();
      var home = nav.hasAttribute('data-home');
      setSection(nav.dataset.nav, !home);
      if (home) window.scrollTo({top:0, behavior:'auto'});
      document.body.classList.remove("menu-open");
      document.querySelector('.menu-backdrop').hidden = true;
      updateSidebar();
    }
    var select = ev.target.closest("[data-select-lab], [data-select-inv], [data-select-assessment], [data-select-interp]");
    if (select) selectItem(select);
    var overview = ev.target.closest('[data-overview-tab]');
    if (overview) { overviewTab = overview.dataset.overviewTab; showOverview(); }
    var mode = ev.target.closest('[data-exam-mode]'), entry = ev.target.closest('[data-open-detail]');
    if (mode) { examMode = mode.dataset.examMode; detail = null; showExams(); }
    if (entry) { detail = entry.dataset.openDetail; showExams(); card('examinations').querySelector('[data-detail="' + detail + '"] [data-back-list]').focus(); }
    if (ev.target.closest('[data-back-list]')) { var oldDetail = detail; detail = null; showExams(); card('examinations').querySelector('[data-open-detail="' + oldDetail + '"]').focus(); }
    if (ev.target.closest(".menu-toggle, .menu-backdrop, .sidebar-toggle")) {
      if (current === 'mobile') {
        var open = !document.body.classList.contains('menu-open');
        document.body.classList.toggle('menu-open', open);
        document.querySelector('.menu-backdrop').hidden = !open;
      } else {
        document.body.classList.toggle('sidebar-collapsed');
        try { localStorage.setItem('pm-sidebar-collapsed', String(document.body.classList.contains('sidebar-collapsed'))); } catch (e) {}
      }
      updateSidebar();
    }
    var display = ev.target.closest('[data-display-mode]');
    if (display) { displayPreference = display.dataset.displayMode; try { localStorage.setItem('pm-display', displayPreference); } catch (e) {} current = null; layout(); }
  });

  document.addEventListener('change', function (ev) {
    var box = ev.target;
    if (!box.matches('input[data-task]')) return;
    var checked = box.checked, feedback = card('tasks').querySelector('.task-feedback');
    box.disabled = true;
    feedback.textContent = '保存中…';
    fetch('/api/patient/' + encodeURIComponent(pid) + '/task-completion', {
      method:'POST', headers:{'Content-Type':'application/json', 'X-PM-Action':'task-completion'},
      body:JSON.stringify({task_id:box.dataset.task, completed:checked})
    }).then(function (r) { if (!r.ok) throw new Error(); return refresh(); })
      .then(function () { var f = card('tasks').querySelector('.task-feedback'); if (f) f.textContent = ''; })
      .catch(function () { box.checked = !checked; box.disabled = false; feedback.textContent = '保存失败，请重试'; });
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") { document.body.classList.remove("menu-open"); document.querySelector('.menu-backdrop').hidden = true; updateSidebar(); }
    if (ev.target.matches('[role="tab"]') && (ev.key === "ArrowRight" || ev.key === "ArrowLeft")) {
      var buttons = Array.from(ev.target.parentElement.querySelectorAll("button"));
      var next = (buttons.indexOf(ev.target) + (ev.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
      ev.preventDefault();
      if (buttons[next].dataset.overviewTab) { overviewTab = buttons[next].dataset.overviewTab; showOverview(); }
      else selectItem(buttons[next]);
      buttons[next].focus();
    }
  });

  // ----- handover copy -----
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest && ev.target.closest("[data-copy]");
    if (!b) return;
    var lines = Array.prototype.map.call(b.closest(".card").querySelectorAll(".kv"), function (kv) {
      return kv.querySelector(".k").textContent + "：" + kv.querySelector(".v").textContent.replace(/\s*展开\s*/g, " ").trim();
    });
    if (navigator.clipboard) navigator.clipboard.writeText(lines.join("\n")).then(function () { b.textContent = "已复制"; });
  });

  // ----- live updates (SSE -> refetch server-rendered fragments, replace only changed modules) -----
  function openLabs(c) { return Array.prototype.map.call(c.querySelectorAll("details.lab[open]"), function (d) { return d.dataset.series; }); }
  var previousHTML = {};
  function apply(f) {
    var h = document.getElementById("m-header");
    if (h && h.outerHTML !== f.header) h.outerHTML = f.header;
    Object.keys(f.modules).forEach(function (k) {
      var old = card(k).closest('details.major-section');
      if (!old || previousHTML[k] === f.modules[k]) return;
      previousHTML[k] = f.modules[k];
      var active = old.querySelector('.selector [aria-selected="true"]');
      var selected = active && (active.dataset.selectLab || active.dataset.selectInv || active.dataset.selectAssessment || active.dataset.selectInterp);
      var expanded = Array.from(old.querySelectorAll('details[open]')).map(function (d) { return d.querySelector('summary').textContent; });
      var keep = openLabs(old), tmp = document.createElement("div");
      tmp.innerHTML = f.modules[k];  // server-rendered, escaped HTML from our own fixed renderer
      var neu = tmp.firstElementChild;
      neu.open = old.open;
      if (keep.length) neu.querySelectorAll("details.lab").forEach(function (d) { d.open = keep.indexOf(d.dataset.series) >= 0; });
      neu.querySelectorAll('details').forEach(function (d) { if (expanded.indexOf(d.querySelector('summary').textContent) >= 0) d.open = true; });
      old.replaceWith(neu);
      initSelectors(neu);
      if (selected) {
        var choice = Array.from(neu.querySelectorAll(".selector button")).find(function (b) {
          return (b.dataset.selectLab || b.dataset.selectInv || b.dataset.selectAssessment || b.dataset.selectInterp) === selected;
        });
        if (choice) selectItem(choice);
      }
      drawAll(neu);
    });
    setSection(section, false);
    document.body.dataset.fingerprint = f.fingerprint || "";
  }
  function refresh() {
    return fetch("/api/patient/" + encodeURIComponent(pid) + "/fragments" + window.location.search, { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); }).then(apply);
  }
  if ("EventSource" in window) {
    var es = new EventSource("/api/patient/" + encodeURIComponent(pid) + "/events");
    es.addEventListener("update", function () { refresh().catch(function () {}); });
  }

  layout();
})();
