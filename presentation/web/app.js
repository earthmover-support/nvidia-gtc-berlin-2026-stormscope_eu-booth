/* GTC Berlin booth page: live StormScope-EU forecast from Arraylake.
 *
 * Data flow
 *   /api/config  -> static settings (config.json)
 *   /api/state   -> recent Icechunk commits grouped into runs (polled every 4 s)
 *   /api/sql     -> latest Zax-SQL result (polled every 10 s, 1 s while a query runs)
 *                   POST {query} runs what is in the panel 06 editor; only Run sends it
 *                   Hosted page (`sql_editable: false`): the editor is read-only and Run
 *                   sends GET ?place=&init=, answered once the query is done (CDN-cached)
 * Tiles are fetched by MapLibre straight from the public Flux tiles service,
 * one raster layer per (init time, lead time) selected with the `time` and
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
    sqlPlace: null, // city chip that produced the query, null once hand-edited
    sqlDirty: false, // textarea edited but not run yet
    doneInit: null,
  };

  // seconds each forecast frame stays on screen
  const SPEEDS = { slow: 0.75, normal: 0.5, fast: 0.25 };

  // ------------------------------------------------------------------ helpers
  const pad = (n) => String(n).padStart(2, "0");
  const initDate = (init) => new Date(init + ":00Z"); // inits are stored in UTC
  // Times show in the viewer's own zone, or Berlin's when the browser reports none.
  const TZ = (() => {
    try {
      const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      new Intl.DateTimeFormat("en", { timeZone: zone }); // throws on a zone it doesn't know
      return zone || "Europe/Berlin";
    } catch {
      return "Europe/Berlin";
    }
  })();
  const HM = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const HMS = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
  const fmtTime = (d) => HM.format(d);
  const fmtTimeSec = (d) => HMS.format(d);
  const hhmm = (init) => (init ? fmtTime(initDate(init)) : "--:--");
  // Zone abbreviation for `d` (CEST vs CET depends on the date). en-GB names the European
  // zones, en-US the American ones; anywhere else this is an offset like "GMT+3".
  const tzName = (d) => {
    const name = (locale) =>
      new Intl.DateTimeFormat(locale, { timeZone: TZ, timeZoneName: "short" })
        .formatToParts(d).find((p) => p.type === "timeZoneName").value;
    const gb = name("en-GB");
    return /^(GMT|UTC)[+-]/.test(gb) ? name("en-US") : gb;
  };
  const ago = (iso) => {
    const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (s < 60) return `${s} s`;
    if (s < 3600) return `${Math.floor(s / 60)} min`;
    return `${Math.floor(s / 3600)} h`;
  };
  const selectableRuns = () => S.runs.slice(0, S.cfg.max_runs);
  // "Latest" = newest init; the pipeline commits each init whole, so every run has all leads
  const latestShown = () => selectableRuns()[0] || null;
  const selectedRun = () =>
    (S.selInit && selectableRuns().find((r) => r.init === S.selInit)) || latestShown();

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
    // the group goes in the path; a `group=` query parameter still opens the whole (unaligned) repo
    const g = c.group ? `/${c.group}` : "";
    return `${c.tile_base}/${encodeURIComponent(r)}${g}/tiles/WebMercatorQuad/{z}/{y}/{x}?${p}`;
  }

  // ------------------------------------------------------------------ globe
  const BASEMAP = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";
  const FORECAST_ATTR = "EUMETSAT MTG-I1 FCI · StormScope-EU · Arraylake Flux";
  const TILE_SIZE = 512;

  let map = null;
  let mapReady = false;
  let labelsLayer; // forecast frames go under the basemap's first label layer
  let layerKey = "";
  const layers = new Map(); // lead -> MapLibre source/layer id
  const loaded = new Set(); // leads whose source has finished loading once
  const LOAD_TIMEOUT_MS = 20000; // a stuck lead stops blocking the next one after this
  let addedAt = 0;

  // Smallest zoom at which the whole panel, swayed and pitched, stays inside
  // camera.cover [west, south, east, north]: the part of Europe the forecast always fills.
  function coverZoom(cam, box) {
    if (!cam.cover) return cam.zoom;
    const mx = (lon) => (lon + 180) / 360;
    const my = (lat) => (1 - Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360)) / Math.PI) / 2;
    const [w, s, e, n] = cam.cover;
    const cx = mx(cam.lon), cy = my(cam.lat);
    const rot = (cam.sway_deg * Math.PI) / 180;
    const W = box.clientWidth / 2, H = box.clientHeight / 2;
    const halfX = W * Math.cos(rot) + H * Math.sin(rot);
    const halfY = W * Math.sin(rot) + H * Math.cos(rot);
    const far = 1 / Math.cos((cam.pitch * Math.PI) / 180) ** 2; // pitch stretches the top half
    const zx = Math.log2(halfX / (512 * Math.min(cx - mx(w), mx(e) - cx)));
    const zn = Math.log2((halfY * far) / (512 * (cy - my(n))));
    const zs = Math.log2(halfY / (512 * (my(s) - cy)));
    return Math.max(zx, zn, zs);
  }

  function initGlobe() {
    const c = S.cfg;
    try {
      map = new maplibregl.Map({
        container: "globe",
        style: BASEMAP,
        center: [c.camera.lon, c.camera.lat],
        zoom: c.camera.zoom,
        pitch: c.camera.pitch,
        // visitors may zoom and pan; the sway owns the bearing, pitch stays fixed
        dragRotate: false,
        pitchWithRotate: false,
        touchPitch: false,
        keyboard: false,
        boxZoom: false,
        maxZoom: c.camera.max_zoom ?? 8.5,
        attributionControl: { compact: true, customAttribution: FORECAST_ATTR },
        fadeDuration: 0,
      });
    } catch (e) {
      showNotice(`Globe failed to start (WebGL?): ${e.message}`);
      return;
    }
    map.on("style.load", () => {
      map.setProjection({ type: "globe" });
      labelsLayer = map.getStyle().layers.find((l) => l.type === "symbol")?.id;
    });
    map.on("load", () => {
      mapReady = true;
      syncLayers();
      showFrame();
    });
    map.on("sourcedata", (e) => {
      const lead = Number((e.sourceId || "").replace("lead-", ""));
      if (!layers.has(lead) || loaded.has(lead) || !map.isSourceLoaded(e.sourceId)) return;
      loaded.add(lead);
      syncLayers();
    });

    map.touchZoomRotate.disableRotation();

    // Camera: always looking at Europe, gentle sway of the bearing around a fixed centre.
    // Visitors can zoom (out to camera.min_zoom) and pan; after camera.idle_reset_s without input it eases home.
    const cam = c.camera;
    const center = [cam.lon, cam.lat];
    let home = cam.zoom;
    const fitZoom = () => {
      home = Math.max(cam.zoom, coverZoom(cam, map.getContainer()));
      map.setMinZoom(Math.min(cam.min_zoom ?? home, home)); // visitors may zoom out to camera.min_zoom
      map.jumpTo({ center, zoom: home });
    };
    fitZoom();
    map.on("resize", fitZoom);

    let idle;
    const touched = () => {
      clearTimeout(idle);
      idle = setTimeout(() => map.easeTo({ center, zoom: home, duration: 1500 }), (cam.idle_reset_s ?? 30) * 1000);
    };
    let held = false; // a finger or mouse button is down on the globe
    let wheelAt = 0; // a wheel zoom is queued until MapLibre's next frame
    const box = map.getCanvasContainer();
    box.addEventListener("pointerdown", () => { held = true; touched(); }, { passive: true });
    box.addEventListener("wheel", () => { wheelAt = performance.now(); touched(); }, { passive: true });
    for (const type of ["pointerup", "pointercancel"]) {
      window.addEventListener(type, () => { held = false; touched(); }, { passive: true });
    }

    // jumpTo() resets gesture handlers (dropping queued wheel zooms) and stops eases, so the
    // sway waits while a pointer is down, just after a wheel event, or while the map moves;
    // its clock pauses too, so the bearing picks up where it left off.
    let t = 0, last = performance.now();
    const sway = (now) => {
      if (!held && now - wheelAt > 300 && !map.isMoving()) {
        t += (now - last) / 1000;
        map.jumpTo({ bearing: cam.sway_deg * Math.sin((2 * Math.PI * t) / cam.sway_period_s) });
      }
      last = now;
      requestAnimationFrame(sway);
    };
    requestAnimationFrame(sway);

    // Station marker
    const st = c.station;
    const m = el("div", "station");
    m.append(el("i"), el("span", null, `${st.name}\nyou are here`));
    new maplibregl.Marker({ element: m, anchor: "left", offset: [-11, 0] }).setLngLat([st.lon, st.lat]).addTo(map);
  }

  function clearLayers() {
    for (const id of layers.values()) {
      map.removeLayer(id);
      map.removeSource(id);
    }
    layers.clear();
    loaded.clear();
  }

  // One raster source per lead time at opacity 0, so the loop never waits on tiles once loaded.
  // Sources are added one at a time in playback order: the tiles service renders about one
  // tile per second, so requesting all leads at once leaves every frame half drawn for minutes.
  function syncLayers() {
    if (!mapReady) return;
    const run = selectedRun();
    if (!run) return;
    const key = `${run.init}|${S.variable}`;
    if (key !== layerKey) {
      clearLayers();
      layerKey = key;
      S.frame = 0;
    }
    const busy = [...layers.keys()].some((l) => !loaded.has(l));
    if (busy && Date.now() - addedAt < LOAD_TIMEOUT_MS) return;
    const lead = S.cfg.leads.find((l) => !layers.has(l));
    if (lead != null) {
      const id = `lead-${lead}`;
      map.addSource(id, {
        type: "raster",
        tiles: [tileTemplate(run, S.variable, lead, TILE_SIZE)],
        tileSize: TILE_SIZE,
        bounds: S.cfg.data_bounds,
        maxzoom: S.cfg.max_tile_level,
      });
      map.addLayer(
        { id, type: "raster", source: id, paint: { "raster-opacity": 0, "raster-fade-duration": 0 } },
        labelsLayer
      );
      layers.set(lead, id);
      addedAt = Date.now();
    }
  }

  function showFrame() {
    const run = selectedRun();
    const leads = run ? S.cfg.leads : [];
    if (S.frame >= leads.length) S.frame = 0;
    const lead = leads[S.frame];
    for (const [l, id] of layers) map.setPaintProperty(id, "raster-opacity", l === lead ? 1 : 0);
    renderTicks(run, lead);
    renderValid(run, lead);
  }

  // One pending timer; restarting it makes play/pause and speed changes take effect at once.
  let animTimer = null;
  function scheduleAnim() {
    clearTimeout(animTimer);
    if (S.animPaused) return;
    const run = selectedRun();
    const last = run && S.frame === S.cfg.leads.length - 1;
    animTimer = setTimeout(animate, SPEEDS[S.speed] * 1000 + (last ? S.cfg.hold_ms : 0));
  }

  function animate() {
    const run = selectedRun();
    const n = run ? S.cfg.leads.length : 0;
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
      const text = `${k === 0 ? "Latest " : ""}${hhmm(run.init)}`;
      const b = el("button", null, text);
      b.type = "button";
      b.setAttribute("aria-pressed", String(sel && run.init === sel.init));
      b.addEventListener("click", () => selectInit(k));
      box.append(b);
    });
    const leads = S.cfg.leads;
    $("#loopLabel span").textContent = sel
      ? `+${leads[0]}→+${leads.at(-1)}${S.animPaused ? " · paused" : ""}`
      : "";
    $("#initTitle").textContent = sel ? `${hhmm(sel.init)} ${tzName(initDate(sel.init))}` : "--:--";
  }

  function selectInit(k) {
    const runs = selectableRuns();
    if (!runs[k]) return;
    S.selInit = runs[k] === latestShown() ? null : runs[k].init;
    renderInits();
    syncLayers();
    showFrame();
  }

  function renderTicks(run, current) {
    const box = $("#ticks");
    box.replaceChildren();
    for (const lead of S.cfg.leads) {
      const d = el("div", null, `+${lead}`);
      if (lead === current) d.classList.add("now");
      box.append(d);
    }
  }

  function renderValid(run, lead) {
    if (!run || lead == null) { $("#validLabel").textContent = "waiting for the first frame…"; return; }
    const t = new Date(initDate(run.init).getTime() + lead * 60000);
    $("#validLabel").textContent = `valid ${fmtTime(t)} ${tzName(t)} · +${lead} min forecast`;
  }

  function showNotice(text) {
    const n = $("#notice");
    n.hidden = !text;
    n.textContent = text || "";
  }

  // ------------------------------------------------------------------ strip
  const mmss = (s) => `${pad(Math.floor(s / 60))}:${pad(Math.floor(s % 60))}`;

  // A run's phases as the strip draws them, each with its estimated length (config
  // `run_phases_s`). The pipeline only commits once per init, so nothing reports
  // progress while it runs: the strip times these phases from the last commit instead.
  function runPhases() {
    const p = S.cfg.run_phases_s;
    return [
      { label: "download initial conditions", busy: "downloading initial conditions", s: p.ic },
      ...S.cfg.leads.map((lead) => ({ label: `+${lead}`, busy: `inference +${lead} min`, s: p.step })),
      { label: "commit", busy: "committing to Arraylake", s: p.commit, cls: "commit" },
    ];
  }

  function renderStrip() {
    const c = S.cfg;
    const now = new Date();
    $("#clock").textContent = `${S.status === "demo" ? "DEMO" : "LIVE"} · ${fmtTimeSec(now)} ${tzName(now)}`;
    $("#live").classList.toggle("stale", S.status === "error");

    const head = S.commits[0];
    $("#lastCommit").textContent = head ? `${head.id.slice(0, 8)} · ${ago(head.written_at)} ago` : "—";

    const phases = runPhases();
    const cells = $("#cells");
    if (cells.children.length !== phases.length) {
      cells.replaceChildren();
      for (const ph of phases) {
        const d = el("div", ph.cls || null);
        d.style.flexGrow = ph.s; // width proportional to the phase's estimated length
        d.append(el("i"), el("span", null, ph.label));
        cells.append(d);
      }
    }

    const latest = S.runs[0];
    if (!latest || !head) {
      $("#countdownLabel").textContent = "NEXT RUN IN";
      $("#countdown").textContent = "--:--";
      $("#ingestLabel").textContent = "Waiting for Arraylake…";
      return;
    }
    // One commit per init: a new init lands with every lead at once, so flash them together.
    if (S.doneInit !== latest.init) {
      S.doneInit = latest.init;
      for (const d of cells.children) { d.classList.remove("flash"); void d.offsetWidth; d.classList.add("flash"); }
    }

    // Steady state: one commit per `run_interval_min`, so the next run is taken to end one
    // interval after the last commit and to start one run-length before that.
    const total = phases.reduce((sum, ph) => sum + ph.s, 0);
    const start = new Date(head.written_at).getTime() + c.run_interval_min * 60000 - total * 1000;
    const elapsed = (now.getTime() - start) / 1000;

    if (elapsed < 0) {
      // Between runs: the last committed run stays complete until the next one starts.
      $("#countdownLabel").textContent = "NEXT RUN IN";
      $("#countdown").textContent = mmss(-elapsed);
      for (const d of cells.children) {
        d.classList.remove("past", "active");
        d.classList.add("done");
        d.firstChild.style.width = "";
      }
      $("#ingestLabel").textContent =
        `Run ${hhmm(latest.init)} · complete in Arraylake · ${c.lead_steps} steps in one commit`;
      return;
    }

    // A run in progress, estimated. Past the estimate it holds on a pulsing commit cell
    // until the real commit lands.
    let end = 0;
    const ends = phases.map((ph) => (end += ph.s));
    const active = elapsed < total ? ends.findIndex((e) => elapsed < e) : phases.length - 1;
    phases.forEach((ph, k) => {
      const d = cells.children[k];
      d.classList.remove("done", "flash");
      d.classList.toggle("past", k < active);
      d.classList.toggle("active", k === active);
      const frac = Math.min(1, Math.max(0, (elapsed - ends[k] + ph.s) / ph.s));
      d.firstChild.style.width = `${frac * 100}%`;
    });
    const next = new Date(initDate(latest.init).getTime() + c.run_interval_min * 60000);
    $("#countdownLabel").textContent = "COMMIT IN";
    $("#countdown").textContent = mmss(Math.max(0, total - elapsed));
    $("#ingestLabel").textContent = `Run start ${fmtTime(next)} · ${phases[active].busy}`;
  }

  // ------------------------------------------------------------------ panels
  const panels = () => [...document.querySelectorAll(".panel")];

  function initPanels() {
    $("#stepSeconds").textContent = S.cfg.run_phases_s.step; // same estimate the strip uses
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
      const lead = c.leads.at(-1);
      const q = c.qr_tile;
      const url = tileTemplate(latest, "true_color", lead, q.size)
        .replace("{z}", q.z).replace("{y}", q.y).replace("{x}", q.x);
      setQR("qrTiles", url);
    }
    setQR("qrViewer", c.viewer_url);
    $("#viewerUrl").textContent = c.viewer_url || "[VIEWER URL]";
  }

  // ------------------------------------------------------------------ SQL
  const sqlBox = () => $("#sqlQuery");
  const holdPanel = () => { S.manualUntil = Math.max(S.manualUntil, Date.now() + 120000); };

  // server-filled query per city chip, {init} left in; the first one is the default
  const sqlFor = (name) => S.cfg.sql_places[name];
  const defaultSql = () => Object.values(S.cfg.sql_places)[0];

  // which city chip produced this server-side query (null = hand-written)
  const placeOf = (query) => {
    const q = (query || "").replace(/TIMESTAMP '[^']*'/, "TIMESTAMP '{init}'");
    return Object.keys(S.cfg.sql_places).find((name) => sqlFor(name) === q) || null;
  };

  function renderSqlPlaces() {
    $("#sqlPlaces").replaceChildren(...Object.keys(S.cfg.sql_places).map((name) => {
      const b = el("button", name === S.sqlPlace ? "on" : "", name);
      b.type = "button";
      b.addEventListener("click", () => {
        S.sqlPlace = name;
        S.sqlDirty = true; // filled in, not run: only Run sends a query
        sqlBox().value = sqlFor(name);
        renderSqlPlaces();
      });
      return b;
    }));
    $("#sqlFoot").textContent = S.sqlPlace
      ? `How cloudy over ${S.sqlPlace}, next 2 h · PostgreSQL wire protocol`
      : "Your query · PostgreSQL wire protocol";
  }

  // the box shows the init the server filled in last; send it back as {init} so Run
  // always queries the newest run (UTC) instead of pinning an old one
  const withInitSlot = (query) => {
    const m = /TIMESTAMP '([^']*)'/.exec((S.sql && S.sql.query) || "");
    return m ? query.split(`TIMESTAMP '${m[1]}'`).join("TIMESTAMP '{init}'") : query;
  };

  async function runSql(query) {
    holdPanel();
    S.sqlDirty = false;
    if (!S.cfg.sql_editable) return runPlaceSql();
    try {
      const res = await fetch("/api/sql", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: withInitSlot(query) }),
      });
      const out = await res.json();
      if (!res.ok) { S.sql = { status: "error", query, error: out.error }; renderSql(); return; }
    } catch (e) {
      S.sql = { status: "error", query, error: String(e) };
      renderSql();
      return;
    }
    S.sql = { ...(S.sql || {}), status: "running", ran_at: null };
    renderSql();
    pollSqlFast();
  }

  // hosted page: the server runs only a city chip's query, for the newest init
  async function runPlaceSql() {
    const run = latestShown();
    if (!run || !S.sqlPlace) return;
    S.sql = { ...(S.sql || {}), status: "running", ran_at: null };
    renderSql();
    try {
      const res = await fetch(`/api/sql?${new URLSearchParams({ place: S.sqlPlace, init: run.time })}`);
      const cache = res.headers.get("x-vercel-cache");
      S.sql = { ...(await res.json()), cached: cache === "HIT" || cache === "STALE" };
    } catch (e) {
      S.sql = { status: "error", error: String(e) };
    }
    renderSql();
  }

  // poll every second until the query we just sent has finished
  let fastTimer = null;
  function pollSqlFast() {
    clearTimeout(fastTimer);
    const started = Date.now();
    const tick = async () => {
      await refreshSql();
      const busy = !S.sql || S.sql.status === "running";
      if ((busy || Date.now() - started < 4000) && Date.now() - started < 180000) fastTimer = setTimeout(tick, 1000);
    };
    fastTimer = setTimeout(tick, 1000);
  }

  function renderSql() {
    const r = S.sql;
    const status = $("#sqlStatus");
    const bars = $("#sqlBars");
    const box = sqlBox();
    const running = r && r.status === "running";
    status.classList.remove("err");
    status.title = "";
    $(".sql").classList.toggle("running", !!running);
    $("#sqlRun").disabled = !!running;
    if (running) holdPanel();
    // never overwrite what someone is typing
    if (r && r.query && !S.sqlDirty && document.activeElement !== box) {
      box.value = r.query;
      S.sqlPlace = placeOf(r.query);
    }
    const empty = () => { bars.replaceChildren(); for (const _ of S.cfg.leads) bars.append(el("div", "empty")); };
    if (!r) { status.textContent = "connecting…"; empty(); return; }
    if (r.status === "idle") { status.textContent = "press Run ▸"; empty(); return; }
    if (r.status === "disabled") { status.textContent = "SQL service off"; status.classList.add("err"); empty(); return; }
    if (r.status === "error") {
      status.textContent = "query failed";
      status.classList.add("err");
      status.title = r.error || "";
      $("#sqlFoot").textContent = (r.error || "query failed").split("\n")[0];
      empty();
      return;
    }
    if (running) { status.textContent = "running… (a cold start takes ~30 s)"; return; } // keep the old bars, dimmed
    renderSqlPlaces(); // restores the foot text after an error
    // first numeric column after the x column, so hand-written queries still plot
    const yi = (r.rows && r.rows[0] ? r.rows[0].findIndex((v, i) => i > 0 && typeof v === "number") : -1);
    const rows = yi > 0 ? r.rows.filter((row) => typeof row[yi] === "number") : [];
    status.textContent = `${r.status === "demo" ? "demo · " : ""}ran ${ago(r.ran_at)} ago · ${r.seconds} s · ${r.cached ? "cached · " : ""}${(r.rows || []).length} rows`;
    if (!rows.length) { empty(); return; }
    const ys = rows.map((row) => row[yi]);
    const lo = Math.min(...ys), hi = Math.max(...ys);
    bars.replaceChildren();
    for (const row of rows.slice(0, 60)) {
      const y = row[yi];
      const d = el("div");
      d.style.height = `${hi > lo ? 15 + (85 * (y - lo)) / (hi - lo) : 60}%`;
      d.title = `${row[0]}: ${y.toFixed(2)}`;
      bars.append(d);
    }
  }

  function initSqlEditor() {
    const box = sqlBox();
    S.sqlPlace = placeOf(defaultSql());
    box.value = defaultSql();
    renderSqlPlaces();
    if (!S.cfg.sql_editable) {
      box.readOnly = true; // hosted page: the chips pick the query, nobody types one
      $("#sqlReset").hidden = true;
    }
    box.addEventListener("focus", holdPanel);
    box.addEventListener("input", () => {
      holdPanel();
      S.sqlDirty = true;
      if (S.sqlPlace) { S.sqlPlace = null; renderSqlPlaces(); }
    });
    box.addEventListener("keydown", (e) => {
      e.stopPropagation(); // typing must not trigger the booth shortcuts
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); runSql(box.value); }
      else if (e.key === "Escape") box.blur();
    });
    $("#sqlRun").addEventListener("click", () => runSql(box.value));
    $("#sqlReset").addEventListener("click", () => {
      S.sqlPlace = placeOf(defaultSql());
      S.sqlDirty = true;
      box.value = defaultSql();
      renderSqlPlaces();
    });
  }

  // ------------------------------------------------------------------ polling
  async function refreshState() {
    try {
      const res = await fetch("/api/state", { cache: "no-store" });
      const st = await res.json();
      S.status = st.status;
      // the hosted page answers a failed read with no runs: keep showing the last ones
      if (st.status !== "error" || (st.runs || []).length) {
        S.commits = st.commits || [];
        S.runs = st.runs || [];
      }
      showNotice(st.status === "error" ? `Arraylake unreachable, showing last known data · ${st.error}` : "");
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
      if (e.target.closest && e.target.closest("textarea, input, button")) return;
      const k = e.key.toLowerCase();
      if (k === "arrowright" || k === "pagedown") { S.manualUntil = Date.now() + 60000; showPanel(S.panel + 1); }
      else if (k === "arrowleft" || k === "pageup") { S.manualUntil = Date.now() + 60000; showPanel(S.panel - 1); }
      else if (k === " ") { S.rotatePaused = !S.rotatePaused; showPanel(S.panel); e.preventDefault(); }
      else if (k === "p") togglePlay();
      else if (k === "s") { const names = Object.keys(SPEEDS); setSpeed(names[(names.indexOf(S.speed) + 1) % names.length]); }
      else if (k === "t") setVariable("true_color");
      else if (k === "a") setVariable("airmass");
      else if (k === "l") selectInit(0);
      else if (/^[1-9]$/.test(k)) selectInit(Number(k) - 1);
      else return;
    });
  }

  // ------------------------------------------------------------------ fit
  // the page is laid out for the 1920 x 1080 booth TV; smaller windows get it zoomed down
  function fitStage() {
    const app = $(".app");
    const fit = Math.min(1, window.innerWidth / 1920, window.innerHeight / 1080);
    app.classList.toggle("fit", fit < 1);
    app.style.setProperty("--fit", fit);
    // centre it vertically; margins are in the zoomed element's own pixels
    app.style.marginTop = fit < 1 ? `${(window.innerHeight / fit - 1080) / 2}px` : "";
  }

  // ------------------------------------------------------------------ boot
  async function boot() {
    fitStage();
    window.addEventListener("resize", fitStage);
    S.cfg = await (await fetch("/api/config")).json();
    if (S.cfg.playback_speed in SPEEDS) S.speed = S.cfg.playback_speed;
    initSqlEditor();
    renderVarToggle();
    renderPlayback();
    $("#playBtn").addEventListener("click", togglePlay);
    initGlobe();
    initPanels();
    initKeys();
    await refreshState();
    setInterval(refreshState, 4000);
    if (S.cfg.sql_editable) {
      refreshSql();
      setInterval(refreshSql, 10000);
    } else {
      S.sql = { status: "idle" }; // nothing to poll: Run waits for its own answer
      renderSql();
    }
    setInterval(() => { renderStrip(); renderCommits(); }, 1000);
    scheduleAnim();
  }

  boot().catch((e) => showNotice(`Startup failed: ${e.message}`));
})();
