/* GTC Berlin booth page: live StormScope-EU forecast from Arraylake.
 *
 * Data flow
 *   /api/config  -> static settings (config.json)
 *   /api/state   -> recent Icechunk commits grouped into runs (polled every 4 s)
 *   /api/sql     -> latest live Zax-SQL result (polled every 10 s)
 * Tiles are fetched by Cesium straight from the public Flux tiles service,
 * one imagery layer per (init time, lead time) selected with the `time` and
 * `lead_time` query parameters on the branch (or pinned to a snapshot id).
 */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  const S = {
    cfg: null,
    status: "starting",
    error: null,
    commits: [],
    runs: [],
    selInit: null, // null = follow latest run
    variable: "true_color",
    frame: 0,
    animPaused: false,
    speed: "normal",
    panel: 0,
    rotatePaused: false,
    manualUntil: 0,
    sql: null,
    doneLeads: new Set(),
    doneInit: null,
  };

  // seconds each forecast frame stays on screen
  const SPEEDS = { slow: 3, normal: 2, fast: 1 };

  // ------------------------------------------------------------------ helpers
  const pad = (n) => String(n).padStart(2, "0");
  const hhmm = (init) => (init || "").slice(11, 16);
  const initDate = (init) => new Date(init + ":00Z");
  const fmtUTC = (d) => `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  const ago = (iso) => {
    const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (s < 60) return `${s} s`;
    if (s < 3600) return `${Math.floor(s / 60)} min`;
    return `${Math.floor(s / 3600)} h`;
  };
  const forecastCount = (run) => (run ? run.leads.filter((l) => l > 0).length : 0);
  const selectableRuns = () => S.runs.slice(0, S.cfg.max_runs);
  // "Latest" = newest init that already has at least one forecast frame
  const latestShown = () => selectableRuns().find((r) => r.leads.length) || null;
  const selectedRun = () => {
    const runs = selectableRuns();
    if (!runs.length) return null;
    if (S.selInit) return runs.find((r) => r.init === S.selInit) || latestShown();
    return latestShown();
  };

  function tileTemplate(run, variable, lead, size = 256) {
    const c = S.cfg;
    const v = c.variables[variable];
    const p = new URLSearchParams({
      variables: variable,
      style: v.style,
      colorscalerange: v.colorscalerange,
      time: run.time,
      lead_time: `${lead} minutes`,
      ensemble: "0",
      f: "png",
      width: String(size),
      height: String(size),
    });
    const r = c.tile_ref_mode === "snapshot" && run.ref ? run.ref : c.branch;
    return `${c.tile_base}/${encodeURIComponent(r)}/tiles/WebMercatorQuad/{z}/{y}/{x}?${p}`;
  }

  // ------------------------------------------------------------------ globe
  let viewer = null;
  let layerKey = "";
  const layers = new Map(); // lead -> ImageryLayer

  function initGlobe() {
    const c = S.cfg;
    try {
      viewer = new Cesium.Viewer("globe", {
        baseLayer: Cesium.ImageryLayer.fromProviderAsync(
          Cesium.TileMapServiceImageryProvider.fromUrl(Cesium.buildModuleUrl("Assets/Textures/NaturalEarthII"))
        ),
        animation: false,
        timeline: false,
        baseLayerPicker: false,
        geocoder: false,
        homeButton: false,
        sceneModePicker: false,
        navigationHelpButton: false,
        fullscreenButton: false,
        infoBox: false,
        selectionIndicator: false,
        creditContainer: el("div"),
      });
    } catch (e) {
      showNotice(`Globe failed to start (WebGL?): ${e.message}`);
      return;
    }
    const scene = viewer.scene;
    scene.skyBox.show = false;
    if (scene.sun) scene.sun.show = false;
    if (scene.moon) scene.moon.show = false;
    scene.backgroundColor = Cesium.Color.fromCssColorString("#05070A");
    scene.globe.baseColor = Cesium.Color.fromCssColorString("#0B0F14");
    scene.globe.enableLighting = false;
    scene.globe.showGroundAtmosphere = false;
    scene.screenSpaceCameraController.enableInputs = false; // booth: nobody can drag it away
    const base = viewer.imageryLayers.get(0);
    if (base) { base.brightness = 0.55; base.saturation = 0.6; }

    // Camera: always looking at Europe, gentle sway around a fixed centre.
    // lookAt() every frame means the view can never drift towards the pole.
    const cam = c.camera;
    const centre = Cesium.Cartesian3.fromDegrees(cam.lon, cam.lat);
    const pitch = Cesium.Math.toRadians(cam.pitch_deg);
    const t0 = performance.now();
    scene.preRender.addEventListener(() => {
      const t = (performance.now() - t0) / 1000;
      const heading = Cesium.Math.toRadians(cam.sway_deg * Math.sin((2 * Math.PI * t) / cam.sway_period_s));
      viewer.camera.lookAt(centre, new Cesium.HeadingPitchRange(heading, pitch, cam.range_km * 1000));
    });

    // Station marker
    const st = c.station;
    viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(st.lon, st.lat),
      point: {
        pixelSize: 11,
        color: Cesium.Color.fromCssColorString("#B7E400"),
        outlineColor: Cesium.Color.BLACK,
        outlineWidth: 2,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      label: {
        text: `${st.name}\nyou are here`,
        font: "600 15px Geist, system-ui, sans-serif",
        fillColor: Cesium.Color.fromCssColorString("#EDEDED"),
        showBackground: true,
        backgroundColor: Cesium.Color.fromCssColorString("#0A0A0C").withAlpha(0.8),
        backgroundPadding: new Cesium.Cartesian2(8, 5),
        horizontalOrigin: Cesium.HorizontalOrigin.LEFT,
        verticalOrigin: Cesium.VerticalOrigin.CENTER,
        pixelOffset: new Cesium.Cartesian2(14, 0),
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
    });
  }

  function clearLayers() {
    for (const layer of layers.values()) viewer.imageryLayers.remove(layer, true);
    layers.clear();
  }

  function syncLayers() {
    if (!viewer) return;
    const run = selectedRun();
    if (!run) return;
    const key = `${run.init}|${S.variable}`;
    if (key !== layerKey) {
      clearLayers();
      layerKey = key;
      S.frame = 0;
    }
    const b = S.cfg.data_bounds;
    const rect = Cesium.Rectangle.fromDegrees(b[0], b[1], b[2], b[3]);
    for (const lead of run.leads) {
      if (layers.has(lead)) continue;
      const provider = new Cesium.UrlTemplateImageryProvider({
        url: tileTemplate(run, S.variable, lead),
        rectangle: rect,
        maximumLevel: S.cfg.max_tile_level,
        credit: "EUMETSAT MTG-I1 FCI · StormScope-EU · Arraylake Flux",
      });
      const layer = viewer.imageryLayers.addImageryProvider(provider);
      layer.alpha = 0;
      layers.set(lead, layer);
    }
  }

  function showFrame() {
    const run = selectedRun();
    const leads = run ? run.leads : [];
    if (S.frame >= leads.length) S.frame = 0;
    const lead = leads[S.frame];
    for (const [l, layer] of layers) layer.alpha = l === lead ? 1 : 0;
    renderTicks(run, lead);
    renderValid(run, lead);
  }

  // One pending timer; restarting it makes play/pause and speed changes take effect at once.
  let animTimer = null;
  function scheduleAnim() {
    clearTimeout(animTimer);
    if (S.animPaused) return;
    const run = selectedRun();
    const last = run && run.leads.length && S.frame === run.leads.length - 1;
    animTimer = setTimeout(animate, SPEEDS[S.speed] * 1000 + (last ? S.cfg.hold_ms : 0));
  }

  function animate() {
    const run = selectedRun();
    const n = run ? run.leads.length : 0;
    if (n) {
      S.frame = (S.frame + 1) % n;
      showFrame();
    }
    scheduleAnim();
  }

  // ------------------------------------------------------------------ playback controls
  const ICON_PAUSE = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="5" y="4" width="5" height="16" rx="1"/><rect x="14" y="4" width="5" height="16" rx="1"/></svg>';
  const ICON_PLAY = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7 4.5v15a1 1 0 0 0 1.5.86l12-7.5a1 1 0 0 0 0-1.72l-12-7.5A1 1 0 0 0 7 4.5z"/></svg>';

  function renderPlayback() {
    const b = $("#playBtn");
    b.innerHTML = S.animPaused ? ICON_PLAY : ICON_PAUSE;
    b.setAttribute("aria-label", S.animPaused ? "Play" : "Pause");
    b.setAttribute("aria-pressed", String(S.animPaused));
    const box = $("#speedToggle");
    box.replaceChildren();
    for (const [name, sec] of Object.entries(SPEEDS)) {
      const s = el("button", null, name);
      s.type = "button";
      s.title = `${sec} s per frame`;
      s.setAttribute("aria-pressed", String(name === S.speed));
      s.addEventListener("click", () => setSpeed(name));
      box.append(s);
    }
  }

  function togglePlay() {
    S.animPaused = !S.animPaused;
    renderPlayback();
    renderInits();
    scheduleAnim();
  }

  function setSpeed(name) {
    if (!(name in SPEEDS) || name === S.speed) return;
    S.speed = name;
    renderPlayback();
    renderInits();
    scheduleAnim();
  }

  // ------------------------------------------------------------------ viz chrome
  function renderVarToggle() {
    const box = $("#varToggle");
    box.replaceChildren();
    for (const [name, v] of Object.entries(S.cfg.variables)) {
      const b = el("button", null, name);
      b.type = "button";
      b.setAttribute("aria-pressed", String(name === S.variable));
      if (!v.available) { b.disabled = true; b.title = "coming soon"; }
      b.addEventListener("click", () => setVariable(name));
      box.append(b);
    }
  }

  function setVariable(name) {
    const v = S.cfg.variables[name];
    if (!v || !v.available || name === S.variable) return;
    S.variable = name;
    renderVarToggle();
    syncLayers();
    showFrame();
  }

  function renderInits() {
    const box = $("#inits");
    box.replaceChildren();
    const runs = selectableRuns();
    const sel = selectedRun();
    runs.forEach((run, k) => {
      const n = forecastCount(run);
      const status = run.complete ? "" : ` · ${n}/${S.cfg.lead_steps}`;
      const text = `${k === 0 ? "Latest " : ""}${hhmm(run.init)}${status}`;
      const b = el("button", null, text);
      b.type = "button";
      b.setAttribute("aria-pressed", String(sel && run.init === sel.init));
      if (!run.leads.length) b.disabled = true;
      b.addEventListener("click", () => selectInit(k));
      box.append(b);
    });
    const run = sel;
    const maxLead = run && run.leads.length ? Math.max(...run.leads) : 0;
    const following = !S.selInit;
    const minLead = run && run.leads.length ? Math.min(...run.leads) : 0;
    $("#loopLabel span").textContent = run
      ? `+${minLead}→+${maxLead}${following && !run.complete ? " · live" : ""}${S.animPaused ? " · paused" : ""}`
      : "";
    $("#initTitle").textContent = run ? `${hhmm(run.init)} UTC` : "--:-- UTC";
  }

  function selectInit(k) {
    const runs = selectableRuns();
    if (!runs[k]) return;
    if (!runs[k].leads.length) return;
    S.selInit = runs[k] === latestShown() ? null : runs[k].init;
    renderInits();
    syncLayers();
    showFrame();
  }

  function renderTicks(run, current) {
    const box = $("#ticks");
    box.replaceChildren();
    const have = new Set(run ? run.leads : []);
    for (let i = 1; i <= S.cfg.lead_steps; i++) {
      const lead = i * 10;
      const d = el("div", null, `+${lead}`);
      if (lead === current) d.classList.add("now");
      else if (!have.has(lead)) d.classList.add("pending");
      box.append(d);
    }
  }

  function renderValid(run, lead) {
    if (!run || lead == null) { $("#validLabel").textContent = "waiting for the first frame…"; return; }
    const t = new Date(initDate(run.init).getTime() + lead * 60000);
    $("#validLabel").textContent = lead === 0
      ? `observed ${fmtUTC(t)} UTC`
      : `valid ${fmtUTC(t)} UTC · +${lead} min forecast`;
  }

  function showNotice(text) {
    const n = $("#notice");
    n.hidden = !text;
    n.textContent = text || "";
  }

  // ------------------------------------------------------------------ strip
  function renderStrip() {
    const c = S.cfg;
    const now = new Date();
    const ms = c.run_interval_min * 60000;
    const left = ms - (now.getTime() % ms);
    $("#countdown").textContent = `${pad(Math.floor(left / 60000))}:${pad(Math.floor((left % 60000) / 1000))}`;
    $("#clock").textContent = `${S.status === "demo" ? "DEMO" : "LIVE"} · ${pad(now.getUTCHours())}:${pad(now.getUTCMinutes())}:${pad(now.getUTCSeconds())} UTC`;
    $("#live").classList.toggle("stale", S.status === "error");

    const head = S.commits[0];
    $("#lastCommit").textContent = head ? `${head.id.slice(0, 8)} · ${ago(head.written_at)} ago` : "—";

    const latest = S.runs[0];
    const cells = $("#cells");
    if (cells.children.length !== c.lead_steps) {
      cells.replaceChildren();
      for (let i = 1; i <= c.lead_steps; i++) cells.append(el("div", null, `+${i * 10}`));
    }
    if (!latest) { $("#ingestLabel").textContent = "Waiting for Arraylake…"; return; }
    if (S.doneInit !== latest.init) { S.doneInit = latest.init; S.doneLeads = new Set(); }
    const have = new Set(latest.leads);
    const maxLead = latest.leads.length ? Math.max(...latest.leads) : 0;
    const recent = head && Date.now() - new Date(head.written_at).getTime() < 5 * 60000;
    [...cells.children].forEach((d, idx) => {
      const lead = (idx + 1) * 10;
      const done = have.has(lead);
      d.classList.toggle("done", done);
      d.classList.toggle("writing", !done && !latest.complete && recent && lead === maxLead + 10);
      if (done && !S.doneLeads.has(lead)) {
        S.doneLeads.add(lead);
        d.classList.remove("flash"); void d.offsetWidth; d.classList.add("flash");
      }
    });
    const n = forecastCount(latest);
    $("#ingestLabel").textContent =
      `Run ${hhmm(latest.init)} · ${latest.complete ? "complete in Arraylake" : "ingesting to Arraylake"} · ${n} of ${c.lead_steps} steps committed`;
  }

  // ------------------------------------------------------------------ panels
  const panels = () => [...document.querySelectorAll(".panel")];

  function initPanels() {
    const prog = $("#progress");
    prog.replaceChildren(...panels().map(() => el("span")));
    showPanel(0);
    setInterval(() => {
      if (S.rotatePaused || Date.now() < S.manualUntil) return;
      showPanel(S.panel + 1);
    }, S.cfg.panel_seconds * 1000);
  }

  function showPanel(i) {
    const ps = panels();
    S.panel = (i + ps.length) % ps.length;
    ps.forEach((p, k) => p.classList.toggle("active", k === S.panel));
    $("#panels").classList.toggle("em-theme", ps[S.panel].classList.contains("em"));
    [...$("#progress").children].forEach((s, k) => {
      s.className = k === S.panel ? "on" : "";
      s.replaceChildren();
      if (k === S.panel) {
        const bar = el("i");
        s.append(bar);
        const dur = S.rotatePaused ? 0 : S.cfg.panel_seconds;
        requestAnimationFrame(() => {
          bar.style.transition = dur ? `width ${dur}s linear` : "none";
          bar.style.width = "100%";
        });
      }
    });
  }

  function renderCommits() {
    const box = $("#commitList");
    box.replaceChildren();
    S.commits.slice(0, 4).forEach((c, k) => {
      const row = el("div", "commit" + (k === 0 ? " fresh" : ""));
      row.append(
        el("span", "id", c.id.slice(0, 8)),
        el("span", "msg", (c.message || "").replace(/(\d{4}-\d{2}-\d{2})T/, "")),
        el("span", "ago", ago(c.written_at))
      );
      box.append(row);
    });
  }

  // ------------------------------------------------------------------ QR codes
  const qrCache = {};
  function setQR(id, text) {
    const box = document.getElementById(id);
    if (!text) { if (qrCache[id] !== "") { box.textContent = "[QR]"; qrCache[id] = ""; } return; }
    if (qrCache[id] === text) return;
    qrCache[id] = text;
    const qr = qrcode(0, "M");
    qr.addData(text);
    qr.make();
    box.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 2, scalable: true });
  }

  function renderQRs() {
    const c = S.cfg;
    const latest = latestShown();
    if (latest) {
      const lead = Math.max(...latest.leads);
      const q = c.qr_tile;
      const url = tileTemplate(latest, "true_color", lead, q.size)
        .replace("{z}", q.z).replace("{y}", q.y).replace("{x}", q.x);
      setQR("qrTiles", url);
    }
    setQR("qrViewer", c.viewer_url);
    $("#viewerUrl").textContent = c.viewer_url || "[VIEWER URL]";
  }

  // ------------------------------------------------------------------ SQL
  function renderSql() {
    const r = S.sql;
    const status = $("#sqlStatus");
    const bars = $("#sqlBars");
    status.classList.remove("err");
    bars.replaceChildren();
    const empty = () => { for (let i = 1; i <= S.cfg.lead_steps; i++) bars.append(el("div", "empty")); };
    if (!r || r.status === "starting") { status.textContent = "connecting…"; empty(); return; }
    if (r.status === "disabled") { status.textContent = "SQL service off"; status.classList.add("err"); empty(); return; }
    if (r.status === "error") { status.textContent = "query failed"; status.classList.add("err"); status.title = r.error || ""; empty(); return; }
    if (r.query) $("#sqlQuery").textContent = r.query;
    const rows = (r.rows || []).filter((row) => typeof row[1] === "number");
    status.textContent = `${r.status === "demo" ? "demo · " : ""}ran ${ago(r.ran_at)} ago · ${r.seconds} s · ${rows.length} rows`;
    if (!rows.length) { empty(); return; }
    const ys = rows.map((row) => row[1]);
    const lo = Math.min(...ys), hi = Math.max(...ys);
    for (const [x, y] of rows) {
      const d = el("div");
      d.style.height = `${hi > lo ? 15 + (85 * (y - lo)) / (hi - lo) : 60}%`;
      d.title = `${x}: ${y.toFixed(2)}`;
      bars.append(d);
    }
  }

  // ------------------------------------------------------------------ polling
  async function refreshState() {
    try {
      const res = await fetch("/api/state", { cache: "no-store" });
      const st = await res.json();
      S.status = st.status;
      S.error = st.error || null;
      S.commits = st.commits || [];
      S.runs = st.runs || [];
      showNotice(st.status === "error" ? `Arraylake unreachable, showing last known data · ${S.error}` : "");
    } catch (e) {
      S.status = "error";
      showNotice("Booth server unreachable");
    }
    renderInits();
    syncLayers();
    showFrame();
    renderCommits();
    renderQRs();
    renderStrip();
  }

  async function refreshSql() {
    try {
      S.sql = await (await fetch("/api/sql", { cache: "no-store" })).json();
    } catch (e) {
      S.sql = { status: "error", error: String(e) };
    }
    renderSql();
  }

  // ------------------------------------------------------------------ keys
  function initKeys() {
    window.addEventListener("keydown", (e) => {
      if (e.altKey || e.ctrlKey || e.metaKey) return;
      const k = e.key.toLowerCase();
      if (k === "arrowright" || k === "pagedown") { S.manualUntil = Date.now() + 60000; showPanel(S.panel + 1); }
      else if (k === "arrowleft" || k === "pageup") { S.manualUntil = Date.now() + 60000; showPanel(S.panel - 1); }
      else if (k === " ") { S.rotatePaused = !S.rotatePaused; showPanel(S.panel); e.preventDefault(); }
      else if (k === "p") togglePlay();
      else if (k === "s") { const names = Object.keys(SPEEDS); setSpeed(names[(names.indexOf(S.speed) + 1) % names.length]); }
      else if (k === "t") setVariable("true_color");
      else if (k === "c") setVariable("cloud_mass");
      else if (k === "l") selectInit(0);
      else if (/^[1-9]$/.test(k)) selectInit(Number(k) - 1);
      else return;
    });
  }

  // ------------------------------------------------------------------ boot
  async function boot() {
    S.cfg = await (await fetch("/api/config")).json();
    if (S.cfg.playback_speed in SPEEDS) S.speed = S.cfg.playback_speed;
    $("#sqlQuery").textContent = S.cfg.sql_query;
    renderVarToggle();
    renderPlayback();
    $("#playBtn").addEventListener("click", togglePlay);
    initGlobe();
    initPanels();
    initKeys();
    await refreshState();
    setInterval(refreshState, 4000);
    refreshSql();
    setInterval(refreshSql, 10000);
    setInterval(() => { renderStrip(); renderCommits(); }, 1000);
    scheduleAnim();
  }

  boot().catch((e) => showNotice(`Startup failed: ${e.message}`));
})();
