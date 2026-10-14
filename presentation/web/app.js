/* GTC Berlin booth page: live StormScope-EU forecast from Arraylake.
 *
 * Data flow
 *   /api/config  -> static settings (config.json)
 *   /api/state   -> recent Icechunk commits grouped into runs (polled every 4 s)
 *   /api/sql     -> latest Zax-SQL result (polled every 10 s, 1 s while a query runs)
 *                   POST {query} runs what is in the panel 06 editor; chips and
 *                   ⌘/Ctrl+Enter send it (there is no Run button)
 *                   (the server also runs the Berlin query once for each new init)
 *                   Hosted page (`sql_editable: false`): the editor is read-only and Run
 *                   sends GET ?place=&init=, answered once the query is done (CDN-cached)
 * Tiles are fetched by MapLibre straight from the public Earthmover Compute tiles service,
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
    loop: true, // false: play to the last lead, then pause
    speed: "slow",
    panel: 0,
    rotatePaused: false,
    manualUntil: 0,
    sql: null,
    sqlPlace: null, // city chip (or My location) that produced the query, null once hand-edited
    sqlDirty: false, // textarea edited but not run yet
    myCoords: null, // {lat, lon} from geolocation once inside data_bounds
    locating: false, // geolocation lookup in flight
    doneInit: null,
  };

  // seconds each forecast frame stays on screen
  const SPEEDS = { slow: 1, medium: 0.5, fast: 0.25 };

  // ------------------------------------------------------------------ helpers
  const pad = (n) => String(n).padStart(2, "0");
  const initDate = (init) => new Date(init + ":00Z"); // inits are stored in UTC
  // All screen timestamps use config.json `timezone`, with UTC as the fallback.
  // Set in boot() once the config has loaded.
  let TZ = "UTC", HM, YMD;
  function setTimezone(zone) {
    for (const z of [zone, "UTC"]) {
      try {
        if (!z) continue;
        new Intl.DateTimeFormat("en", { timeZone: z }); // throws on a zone it doesn't know
        TZ = z;
        break;
      } catch {}
    }
    HM = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
    YMD = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" }); // YYYY-MM-DD
  }
  setTimezone();
  const hhmm = (init) => (init ? HM.format(initDate(init)) : "--:--");
  const ymdhm = (d) => `${YMD.format(d)} ${HM.format(d)}`;
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
    const r = run.ref || c.branch; // a snapshot id makes the tile URL immutable, so it caches
    return `${c.tile_base}/${encodeURIComponent(r)}/tiles/WebMercatorQuad/{z}/{y}/{x}?${p}`;
  }

  // ------------------------------------------------------------------ globe
  const BASEMAP = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";
  const FORECAST_ATTR = "EUMETSAT MTG-I1 FCI · StormScope-EU · Earthmover Compute";
  const TILE_SIZE = 512;

  let map = null;
  let mapReady = false;
  let labelsLayer; // forecast frames go under the basemap's first label layer
  // A frame set is one run and variable: { id, run, variable, layers, loaded, addedAt },
  // where `layers` maps lead -> MapLibre source/layer id and `loaded` holds the leads whose
  // source has finished loading once.
  let shown = null; // on screen
  let pending = null; // a newer run loading in the background; replaces `shown` once complete
  let sets = 0;
  const LOAD_TIMEOUT_MS = 20000; // a stuck lead stops blocking the next one after this

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
        style: c.basemap ?? BASEMAP, // server.py --offline serves its saved copy
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
      // compact attribution opens expanded until the first drag, which a booth screen never
      // gets: fold it to the (i) button so the credits stop covering the lead-time ticks
      document.querySelector(".maplibregl-ctrl-attrib")?.classList.remove("maplibregl-compact-show");
      syncLayers();
      showFrame();
    });
    map.on("sourcedata", (e) => {
      const [, id, lead] = e.sourceId?.match(/^lead-(\d+)-(\d+)$/) ?? []; // none for the basemap's sources
      const set = [shown, pending].find((s) => s?.id === Number(id));
      if (!e.tile || !set || set.loaded.has(+lead) || !map.isSourceLoaded(e.sourceId)) return;
      set.loaded.add(+lead);
      syncLayers();
      if (set === shown) showFrame();
      else renderInits();
    });

    map.touchZoomRotate.disableRotation();

    // Camera: always looking at Europe, gentle sway of the bearing around a fixed centre.
    // Visitors can zoom (out to camera.min_zoom) and pan; after camera.idle_reset_s without input it eases home.
    const cam = c.camera;
    const center = [cam.lon, cam.lat];
    let home = cam.zoom;
    const fitZoom = () => {
      const atHome = Math.abs(map.getZoom() - home) < 0.01; // a visitor's pan/zoom survives a resize
      home = Math.max(cam.zoom, coverZoom(cam, map.getContainer()));
      map.setMinZoom(Math.min(cam.min_zoom ?? home, home)); // visitors may zoom out to camera.min_zoom
      if (atHome) map.jumpTo({ center, zoom: home });
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

  function dropSet(set) {
    for (const id of set.layers.values()) {
      map.removeLayer(id);
      map.removeSource(id);
    }
  }

  // frame indices whose lead has loaded in `set`, in playback order
  const readyFrames = (set) => (set ? S.cfg.leads.flatMap((l, k) => (set.loaded.has(l) ? [k] : [])) : []);

  // Every lead has been added, and the last one has loaded or timed out.
  const complete = (set) =>
    set.layers.size === S.cfg.leads.length &&
    (set.loaded.has(S.cfg.leads.at(-1)) || Date.now() - set.addedAt >= LOAD_TIMEOUT_MS);

  // One raster source per lead time at opacity 0, so the loop never waits on tiles once loaded.
  // Sources are added one at a time in playback order: the tiles service renders about one
  // tile per second, so requesting all leads at once leaves every frame half drawn for minutes.
  // A run or variable the visitor picks (`pick`) replaces the frames on screen at once; a new
  // run found by polling loads behind them, so the loop keeps going until it is complete.
  function syncLayers(pick = false) {
    if (!mapReady) return;
    const run = selectedRun();
    if (!run) return;
    const same = (set) => set?.run.init === run.init && set.variable === S.variable;
    if (!same(pending)) {
      if (pending) dropSet(pending);
      pending = null;
    }
    if (!pending && !same(shown)) {
      pending = { id: sets++, run, variable: S.variable, layers: new Map(), loaded: new Set(), addedAt: 0 };
    }
    // a pick goes on screen at once, keeping any frames the set loaded behind
    if (pending && (pick || !shown)) promote(pending, pick);
    addNext(shown);
    // the background set only loads once the one on screen is complete, so the two never
    // compete for the tiles service
    if (pending && complete(shown)) {
      addNext(pending);
      if (complete(pending)) promote(pending);
    }
  }

  function promote(set, pick = false) {
    if (shown) dropSet(shown);
    // a visitor pick (run or variable) keeps the time step so views of the same step can be
    // compared; a complete run swapped in the background keeps the loop's place if that lead
    // loaded, otherwise holds on +0
    if (pick) S.frame = S.cfg.leads.length ? Math.min(Math.max(S.frame, 0), S.cfg.leads.length - 1) : 0;
    else if (!complete(set) || !set.loaded.has(S.cfg.leads[S.frame])) S.frame = 0;
    shown = set;
    pending = null;
    renderInits();
  }

  function addNext(set) {
    const busy = [...set.layers.keys()].some((l) => !set.loaded.has(l));
    if (busy && Date.now() - set.addedAt < LOAD_TIMEOUT_MS) return;
    const lead = S.cfg.leads.find((l) => !set.layers.has(l));
    if (lead == null) return;
    const id = `lead-${set.id}-${lead}`;
    map.addSource(id, {
      type: "raster",
      tiles: [tileTemplate(set.run, set.variable, lead, TILE_SIZE)],
      tileSize: TILE_SIZE,
      bounds: S.cfg.data_bounds,
      maxzoom: S.cfg.max_tile_level,
    });
    map.addLayer(
      { id, type: "raster", source: id, paint: { "raster-opacity": 0, "raster-opacity-transition": { duration: 0 }, "raster-fade-duration": 0 } },
      labelsLayer
    );
    set.layers.set(lead, id);
    set.addedAt = Date.now();
  }

  function showFrame() {
    const run = shown?.run;
    const leads = run ? S.cfg.leads : [];
    if (S.frame >= leads.length) S.frame = 0;
    const lead = leads[S.frame];
    for (const [l, id] of shown?.layers ?? []) map.setPaintProperty(id, "raster-opacity", l === lead ? 1 : 0);
    renderTicks(lead);
    renderClock();
    renderValid(run, lead, shown && !complete(shown) ? ` · loading ${shown.loaded.size}/${leads.length}` : "");
  }

  // One pending timer; restarting it makes play/pause and speed changes take effect at once.
  let animTimer = null;
  function scheduleAnim() {
    clearTimeout(animTimer);
    if (S.animPaused) return;
    const last = shown && readyFrames(shown).find((k) => k > S.frame) == null;
    animTimer = setTimeout(animate, SPEEDS[S.speed] * 1000 + (last ? S.cfg.hold_ms : 0));
  }

  // Holds the first frame until every lead has loaded (or timed out), then loops over the
  // leads that loaded (a lead that timed out is skipped).
  function animate() {
    if (shown && complete(shown) && !scrubbing) {
      const ready = readyFrames(shown);
      const next = ready.find((k) => k > S.frame);
      if (next == null && !S.loop && ready.length) { // play once: stop on the last lead
        S.animPaused = true;
        renderPlayback();
        renderInits();
        return;
      }
      if (ready.length) S.frame = next ?? ready[0];
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

  function toggleLoop() {
    S.loop = !S.loop;
    $("#loopLabel").setAttribute("aria-pressed", String(S.loop));
    renderInits();
  }

  function togglePlay() {
    S.animPaused = !S.animPaused;
    // resuming a finished single run starts it over
    if (!S.animPaused && !S.loop && shown) {
      const ready = readyFrames(shown);
      if (ready.length && ready.find((k) => k > S.frame) == null) { S.frame = ready[0]; showFrame(); }
    }
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
  // display names for the variable keys in config.json
  const VAR_LABELS = { true_color: "True Color", airmass: "Air Mass" };

  // Observation cards highlight the variable on the globe
  function renderVarCards() {
    for (const c of document.querySelectorAll(".card[data-var]")) {
      c.classList.toggle("on", c.dataset.var === S.variable);
      c.setAttribute("aria-pressed", String(c.dataset.var === S.variable));
    }
  }

  // Real +0 composite tiles, never invented observed-history thumbnails.
  function renderCompositePreviews() {
    const run = shown?.run || selectedRun();
    for (const img of document.querySelectorAll("[data-composite]")) {
      if (!run) { img.hidden = true; continue; }
      const tile = S.cfg.qr_tile;
      const src = tileTemplate(run, img.dataset.composite, 0, tile.size)
        .replace("{z}", tile.z).replace("{x}", tile.x).replace("{y}", tile.y);
      if (img.dataset.source === src) continue;
      img.dataset.source = src;
      img.hidden = true;
      img.onload = () => { img.hidden = false; };
      img.onerror = () => { img.hidden = true; };
      img.src = src;
    }
    $("#compositeCaption").textContent = run
      ? `Observed +0 · ${hhmm(run.init)} UTC · Berlin Region`
      : "Observed +0 Frame · Waiting for Data";
  }

  // Matched, snapshot-pinned thumbnails; never substitute a forecast for an observation.
  const publicationForRun = (run) => run && S.commits.find((c) => c.id === run.ref && (c.message || "").includes(run.init));
  const thumbnailUrl = (run, variable, lead) => {
    const q = S.cfg.qr_tile;
    return tileTemplate(run, variable, lead, 128).replace("{z}", q.z).replace("{x}", q.x).replace("{y}", q.y);
  };
  let contextExample = 0;

  function setFrameImage(group, source) {
    const img = group.querySelector("image");
    if (img.dataset.source === (source || "")) return;
    img.dataset.source = source || "";
    img.setAttribute("visibility", "hidden");
    img.removeAttribute("href");
    group.classList.remove("has-image");
    if (!source) return;
    const loader = new Image();
    loader.onload = () => {
      if (img.dataset.source !== source) return;
      img.setAttribute("href", source);
      img.setAttribute("visibility", "visible");
      group.classList.add("has-image");
      updateThumbnailAvailability();
    };
    loader.onerror = () => { if (img.dataset.source === source) updateThumbnailAvailability(); };
    loader.src = source;
  }

  function updateThumbnailAvailability() {
    const observed = [...document.querySelectorAll('[data-frame-group="initial"]')];
    const count = observed.filter((g) => g.classList.contains("has-image")).length;
    const predicted = document.querySelectorAll("[data-output-index].has-image").length;
    $("#modelImageState").textContent = `${VAR_LABELS[S.variable]} · ${count}/6 observed · ${predicted}/12 forecast thumbnails`;
  }

  function renderModelSequence() {
    if (panels()[S.panel]?.dataset.panel !== "nowcasting") return;
    const run = shown?.run || selectedRun();
    const getSource = (offset) => {
      if (!run) return null;
      if (offset > 0) return thumbnailUrl(run, S.variable, offset);
      const time = initDate(run.init).getTime() + offset * 60000;
      const observation = S.runs.find((r) => initDate(r.init).getTime() === time);
      return observation ? thumbnailUrl(observation, S.variable, 0) : null;
    };
    for (const group of document.querySelectorAll("[data-frame-group]")) {
      const rolling = group.dataset.frameGroup === "rolling";
      const offset = (rolling ? -40 + contextExample * 10 : -50) + Number(group.dataset.frameIndex) * 10;
      const generated = offset > 0;
      group.classList.toggle("generated", generated);
      group.querySelector("rect").setAttribute("class", generated ? "diagram-predicted" : "diagram-observed");
      group.querySelector(".frame-label").textContent = offset < 0 ? `−${-offset}` : offset > 0 ? `+${offset}` : "0";
      setFrameImage(group, getSource(offset));
    }
    for (const group of document.querySelectorAll("[data-output-index]")) {
      const lead = S.cfg.leads[Number(group.dataset.outputIndex) + 1];
      setFrameImage(group, run ? thumbnailUrl(run, S.variable, lead) : null);
    }
    $("#modelNextLead").textContent = `+${(contextExample + 1) * 10} min`;
    $("#modelContextState").textContent = contextExample === 11 ? "Illustrative step 12/12 · final +120 min forecast" : `Illustrative step ${contextExample + 1}/12 · context for +${(contextExample + 2) * 10} min`;
    updateThumbnailAvailability();
  }

  function renderPublishedCycles() {
    const rows = [...document.querySelectorAll("[data-cycle-row]")];
    const runs = S.runs.slice(0, 3).reverse();
    $("#cycleHistoryCaption").textContent = {
      demo: "Simulated Forecast Cycles · UTC", offline: "Replay Forecast Cycles · UTC",
      error: "Last Known Published Cycles · UTC",
    }[S.status] || "Published Forecast Cycles · UTC";
    rows.forEach((row, index) => {
      const run = runs[index];
      const fields = ["window", "init", "horizon", "publication"];
      if (!run) { for (const f of fields) row.querySelector(`[data-cycle-${f}]`).textContent = "—"; return; }
      const init = initDate(run.init);
      row.querySelector("[data-cycle-window]").textContent = `${HM.format(new Date(init.getTime() - 50 * 60000))}–${HM.format(init)}`;
      row.querySelector("[data-cycle-init]").textContent = HM.format(init);
      row.querySelector("[data-cycle-horizon]").textContent = HM.format(new Date(init.getTime() + S.cfg.leads.at(-1) * 60000));
      const pub = publicationForRun(run);
      row.querySelector("[data-cycle-publication]").textContent = pub ? HM.format(new Date(pub.written_at)) : "Unknown";
      row.setAttribute("aria-label", `Forecast initialized ${ymdhm(init)} UTC, published ${pub ? ymdhm(new Date(pub.written_at)) + " UTC" : "time unavailable"}`);
    });
  }

  function renderComputeTile() {
    if (panels()[S.panel]?.dataset.panel !== "compute") return;
    const run = shown?.run || selectedRun();
    const img = $("#computeTile");
    if (!run) { img.hidden = true; $("#computeTileCaption").textContent = "Waiting for forecast metadata"; return; }
    const lead = S.cfg.leads[S.frame] ?? 0;
    const source = thumbnailUrl(run, S.variable, lead);
    $("#computeTileCaption").textContent = `${VAR_LABELS[S.variable]} · +${lead} min · Init ${hhmm(run.init)} UTC`;
    if (img.dataset.source === source) return;
    img.dataset.source = source;
    img.hidden = true;
    img.onload = () => { if (img.dataset.source === source) img.hidden = false; };
    img.onerror = () => { img.hidden = true; };
    img.src = source;
  }

  function initVisuals() {
    const editor = $("#sqlEditor");
    editor.addEventListener("toggle", () => { if (editor.open) holdPanel(); });
    $("#sqlEditorClose").addEventListener("click", () => { editor.open = false; });
    $("#sqlQuery").addEventListener("keydown", (e) => { if (e.key === "Escape") editor.open = false; });
    if (!S.cfg.sql_editable) editor.querySelector("summary").textContent = "Inspect SQL";
    setInterval(() => {
      if (document.hidden || S.rotatePaused || window.matchMedia("(prefers-reduced-motion: reduce)").matches || panels()[S.panel]?.dataset.panel !== "nowcasting") return;
      contextExample = (contextExample + 1) % 12;
      renderModelSequence();
    }, 5000);
  }

  function renderVarToggle() {
    const box = $("#varToggle");
    box.replaceChildren();
    for (const name of Object.keys(S.cfg.variables)) {
      const b = el("button", null, VAR_LABELS[name] ?? name);
      b.type = "button";
      b.setAttribute("aria-pressed", String(name === S.variable));
      b.addEventListener("click", () => setVariable(name));
      box.append(b);
    }
  }

  function setVariable(name) {
    if (!(name in S.cfg.variables) || name === S.variable) return;
    S.variable = name;
    renderVarToggle();
    renderVarCards();
    syncLayers(true);
    showFrame();
  }

  function renderInits() {
    const box = $("#inits");
    box.replaceChildren();
    const runs = selectableRuns();
    const on = shown?.run || selectedRun(); // a newer run loading in the background isn't on screen yet
    runs.forEach((run, k) => {
      const loading = pending?.run.init === run.init ? ` · ${pending.loaded.size}/${S.cfg.leads.length}` : "";
      const b = el("button", null, `${k === 0 ? "Latest " : ""}${hhmm(run.init)}${loading}`);
      b.type = "button";
      b.setAttribute("aria-pressed", String(on && run.init === on.init));
      b.addEventListener("click", () => selectInit(k));
      box.append(b);
    });
    const leads = S.cfg.leads;
    $("#loopLabel span").textContent = on
      ? `+${leads[0]}→+${leads.at(-1)} · ${S.loop ? "loop" : "once"}`
      : "";
    $("#initTitle").textContent = on ? `${ymdhm(initDate(on.init))} ${tzName(initDate(on.init))}` : "--:--";
  }

  function selectInit(k) {
    const runs = selectableRuns();
    if (!runs[k]) return;
    S.selInit = runs[k] === latestShown() ? null : runs[k].init;
    renderInits();
    syncLayers(true);
    showFrame();
  }

  function renderTicks(current) {
    const box = $("#ticks");
    box.replaceChildren();
    S.cfg.leads.forEach((lead, k) => {
      const d = el("div", lead ? null : "obs", lead ? `+${lead}` : "+0 · sat");
      if (lead === current) d.classList.add("now");
      d.addEventListener("click", () => scrubTo(k));
      box.append(d);
    });
    const r = $("#scrub");
    r.max = String(S.cfg.leads.length - 1);
    if (!scrubbing) r.value = String(S.frame);
  }

  // slider / tick click: jump to the nearest lead that has loaded; the loop carries on from there
  let scrubbing = false; // pointer down on the slider: no auto-advance, no thumb writes
  function scrubTo(k) {
    if (!shown) return;
    const ready = readyFrames(shown);
    if (!ready.length) return;
    const dir = Math.sign(k - S.frame); // ties go the way the visitor is moving
    S.frame = ready.reduce((best, i) => {
      const di = Math.abs(i - k), db = Math.abs(best - k);
      return di < db || (di === db && Math.sign(i - S.frame) === dir) ? i : best;
    }, ready[0]);
    showFrame();
    scheduleAnim();
  }

  // top left: the valid time of the frame on screen, big; the init line below is renderInits'
  function renderValid(run, lead, suffix) {
    if (!run || lead == null) {
      $("#validEyebrow").textContent = "Forecast";
      $("#validTitle").textContent = "waiting for the first frame…";
      return;
    }
    const t = new Date(initDate(run.init).getTime() + lead * 60000);
    $("#validEyebrow").textContent = (lead ? `Forecast · +${lead} min` : "Observed · Satellite") + suffix;
    $("#validTitle").textContent = `${ymdhm(t)} ${tzName(t)}`;
    renderCompositePreviews();
    renderModelSequence();
    renderComputeTile();
  }

  function showNotice(text) {
    const n = $("#notice");
    n.hidden = !text;
    n.textContent = text || "";
  }

  // ------------------------------------------------------------------ strip
  // Header pill: wall clock, separate from the map’s forecast valid time.
  function renderClock() {
    const now = new Date();
    const tag = { demo: "Demo", offline: "Replay", error: "Disconnected" }[S.status] ?? "Live Feed";
    $("#clock").textContent = `${tag} · Now ${HM.format(now)} ${tzName(now)}`;
  }

  const mmss = (s) => `${pad(Math.floor(s / 60))}:${pad(Math.floor(s % 60))}`;

  // A run's phases as the strip draws them, each with its estimated length (config
  // `run_phases_s`). The pipeline only commits once per init, so nothing reports
  // progress while it runs: the strip times these phases from the last commit instead.
  function runPhases() {
    const p = S.cfg.run_phases_s;
    return [
      // +0 is the newest observed frame, part of the initial conditions, not an inference step
      { label: "+0 · download satellite", busy: "downloading initial conditions (satellite −50…+0 min)", s: p.ic },
      ...S.cfg.leads.slice(1).map((lead) => ({ label: `+${lead}`, busy: `inference +${lead} min`, s: p.step })),
      { label: "commit", busy: "committing to Arraylake", s: p.commit, cls: "commit" },
    ];
  }

  function renderStrip() {
    const c = S.cfg;
    const now = new Date();
    renderClock();
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
    if (S.status === "error") {
      $("#countdownLabel").textContent = "Cycle Unavailable";
      $("#countdown").textContent = "--:--";
      $("#ingestLabel").textContent = "Disconnected · last known publication retained";
      for (const d of cells.children) {
        d.classList.remove("past", "active", "done", "flash");
        d.firstChild.style.width = "";
      }
      return;
    }
    if (!latest || !head) {
      $("#countdownLabel").textContent = "Next Run · Estimate";
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
      $("#countdownLabel").textContent = "Next Run · Estimate";
      $("#countdown").textContent = mmss(-elapsed);
      for (const d of cells.children) {
        d.classList.remove("past", "active");
        d.classList.add("done");
        d.firstChild.style.width = "";
      }
      $("#ingestLabel").textContent =
        `Run ${hhmm(latest.init)} · complete in Arraylake`;
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
    const next = new Date(start);
    $("#countdownLabel").textContent = "Commit · Estimate";
    $("#countdown").textContent = mmss(Math.max(0, total - elapsed));
    $("#ingestLabel").textContent = `Estimated cycle start ${HM.format(next)} UTC · ${phases[active].busy} (not live telemetry)`;
  }

  // ------------------------------------------------------------------ panels
  const panels = () => [...document.querySelectorAll(".panel")];

  let panelTimer = null;
  function schedulePanelRotation() {
    clearTimeout(panelTimer);
    if (S.rotatePaused) return;
    const wait = Math.max(S.cfg.panel_seconds * 1000, S.manualUntil - Date.now());
    panelTimer = setTimeout(() => showPanel(S.panel + 1), wait);
  }

  function togglePanelPause() {
    S.rotatePaused = !S.rotatePaused;
    if (!S.rotatePaused) S.manualUntil = 0;
    showPanel(S.panel);
  }

  function initPanels() {
    const prog = $("#progress");
    prog.replaceChildren(...panels().map((panel, index) => {
      const button = el("button");
      button.type = "button";
      button.setAttribute("aria-label", `Go to ${panel.querySelector(".kicker").textContent}`);
      button.title = panel.querySelector(".kicker").textContent;
      button.addEventListener("click", () => {
        S.manualUntil = Date.now() + 60000;
        showPanel(index);
      });
      return button;
    }));
    $("#panelPause").addEventListener("click", togglePanelPause);
    const step = (d) => { S.manualUntil = Date.now() + 60000; showPanel(S.panel + d); };
    $("#panelPrev").addEventListener("click", () => step(-1));
    $("#panelNext").addEventListener("click", () => step(1));
    showPanel(0);
  }

  function showPanel(i) {
    const ps = panels();
    S.panel = (i + ps.length) % ps.length;
    ps.forEach((p, k) => {
      const active = k === S.panel;
      p.classList.toggle("active", active);
      p.inert = !active;
      p.setAttribute("aria-hidden", String(!active));
    });
    $("#panels").classList.toggle("em-theme", ps[S.panel].classList.contains("em"));
    $("#panelCount").textContent = `${S.panel + 1} / ${ps.length}`;
    $("#panelPause").textContent = S.rotatePaused ? "Resume" : "Pause";
    $("#panelPause").setAttribute("aria-pressed", String(S.rotatePaused));
    $("#panelPause").title = S.rotatePaused ? "Resume panel rotation" : "Pause panel rotation";
    $("#panelPause").setAttribute("aria-label", S.rotatePaused ? "Resume panel rotation" : "Pause panel rotation");
    [...$("#progress").children].forEach((s, k) => {
      s.className = k === S.panel ? "on" : "";
      if (k === S.panel) s.setAttribute("aria-current", "step");
      else s.removeAttribute("aria-current");
      s.replaceChildren();
      if (k === S.panel) {
        const bar = el("i");
        s.append(bar);
        const dur = S.rotatePaused ? 0 : Math.max(S.cfg.panel_seconds, (S.manualUntil - Date.now()) / 1000);
        requestAnimationFrame(() => {
          bar.style.transition = dur ? `width ${dur}s linear` : "none";
          bar.style.width = "100%";
        });
      }
    });
    schedulePanelRotation();
    renderModelSequence();
    renderPublishedCycles();
    renderComputeTile();
  }

  let lastPublicationId = null;
  function renderCommits() {
    const box = $("#commitList");
    box.replaceChildren();
    $("#historyFeedState").textContent = { demo: "Demo", offline: "Replay", error: "Disconnected · last known" }[S.status] || "Live";
    const forecasts = S.commits.filter(c => /stormscope-eu\s+\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(c.message || "") && c.id && c.written_at);
    const head = forecasts[0]?.id;
    const isNew = !!(lastPublicationId && head && head !== lastPublicationId);
    lastPublicationId = head || lastPublicationId;
    // Each node corresponds to a real published forecast snapshot. Gaps are not durations.
    const history = $("#publicationTimeline");
    history.replaceChildren();
    forecasts.slice(0, 4).reverse().forEach((c, i, a) => {
      const node = el("span", "publication-event" + (i === a.length - 1 ? " newest" : ""));
      node.title = `Published ${ymdhm(new Date(c.written_at))} UTC · snapshot ${c.id}`;
      node.setAttribute("aria-label", node.title);
      history.append(node);
    });
    if (!forecasts.length) history.textContent = "No verified forecast publications";
    const columns = el("div", "commit-heading");
    columns.append(el("span", null, "Init · UTC"), el("span", null, "Published · UTC"), el("span", null, "Snapshot"), el("span", null, "Status"), el("span", null, "Age"));
    box.append(columns);
    if (!forecasts.length) box.append(el("div", "commit", "No forecast publications available"));
    forecasts.slice(0, 4).forEach((c, k) => {
      const row = el("div", "commit" + (k === 0 ? " fresh" : ""));
      if (k === 0 && isNew) row.classList.add("new-publication");
      row.title = c.id;
      row.append(
        el("span", "init", (c.message || "").match(/stormscope-eu\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})/)?.[1].replace("T", " ")),
        el("span", "published-at", ymdhm(new Date(c.written_at))),
        el("span", "id", c.id.slice(0, 8)),
        el("span", "publication-status", "Published"),
        el("span", "ago", `${ago(c.written_at)} ago`)
      );
      box.append(row);
    });
  }

  // ------------------------------------------------------------------ QR codes
  // Level H leaves room for the logo. Each URL uses its smallest QR grid.
  const qrCache = {};
  function setQR(id, text, type = 0) {
    const box = document.getElementById(id);
    if (!text) { if (qrCache[id]?.key !== "") { box.textContent = "[QR]"; qrCache[id] = { key: "", type: 0 }; } return 0; }
    const key = `${type} ${text}`;
    if (qrCache[id]?.key === key) return qrCache[id].type;
    const qr = qrcode(type, "H");
    qr.addData(text);
    qr.make();
    box.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 16, scalable: true }) +
      '<img src="img/earthmover-mark.svg" alt="">';
    qrCache[id] = { key, type: (qr.getModuleCount() - 17) / 4 };
    return qrCache[id].type;
  }

  function renderQRs() {
    const c = S.cfg;
    const latest = latestShown();
    if (latest) {
      const q = c.qr_tile;
      const url = tileTemplate(latest, "true_color", c.leads[1], q.size) // leads[0] is the observed frame
        .replace("{z}", q.z).replace("{y}", q.y).replace("{x}", q.x);
      setQR("qrTiles", url);
    }
    setQR("qrViewer", c.viewer_url);
    $("#viewerLink").href = c.viewer_url;
  }

  // ------------------------------------------------------------------ SQL
  const sqlBox = () => $("#sqlQuery");
  const holdPanel = () => {
    S.manualUntil = Math.max(S.manualUntil, Date.now() + 120000);
    showPanel(S.panel);
  };

  // server-filled query per city chip, {init} left in; the first one is the default
  const sqlFor = (name) => S.cfg.sql_places[name];
  const defaultSql = () => Object.values(S.cfg.sql_places)[0];
  const MY = "My location";

  // coords embedded in a built query, e.g. `lat BETWEEN 52.5000 - 0.05 AND ...`
  const coordsOf = (query) => {
    const mLat = /lat BETWEEN\s+(-?[\d.]+)\s*-\s*[\d.]+\s+AND\s+(-?[\d.]+)\s*\+\s*[\d.]+/i.exec(query || "");
    const mLon = /lon BETWEEN\s+(-?[\d.]+)\s*-\s*[\d.]+\s+AND\s+(-?[\d.]+)\s*\+\s*[\d.]+/i.exec(query || "");
    if (!mLat || !mLon) return null;
    return { lat: parseFloat(mLat[1]), lon: parseFloat(mLon[1]) };
  };

  // same query shape as the server's place_queries, but for an arbitrary point
  const sqlForCoords = (lat, lon) => {
    const box = S.cfg.sql_box ?? 0.05;
    const rLat = lat.toFixed(4), rLon = lon.toFixed(4);
    return defaultSql()
      .replace(/lat BETWEEN\s+[^\s]+\s*-\s*[^\s]+\s+AND\s+[^\s]+\s*\+\s*[^\s]+/i, `lat BETWEEN ${rLat} - ${box} AND ${rLat} + ${box}`)
      .replace(/lon BETWEEN\s+[^\s]+\s*-\s*[^\s]+\s+AND\s+[^\s]+\s*\+\s*[^\s]+/i, `lon BETWEEN ${rLon} - ${box} AND ${rLon} + ${box}`);
  };

  const inBounds = (lat, lon) => {
    const [w, s, e, n] = S.cfg.data_bounds; // west, south, east, north
    return lon >= w && lon <= e && lat >= s && lat <= n;
  };
  const fmtCoords = (lat, lon) =>
    `${Math.abs(lat).toFixed(2)}°${lat >= 0 ? "N" : "S"}, ${Math.abs(lon).toFixed(2)}°${lon >= 0 ? "E" : "W"}`;
  const boundsText = () => {
    const [w, s, e, n] = S.cfg.data_bounds;
    return `lon ${w}…${e}, lat ${s}…${n}`;
  };

  // which city chip produced this server-side query (null = hand-written)
  const placeOf = (query) => {
    const q = (query || "").replace(/TIMESTAMP '[^']*'/, "TIMESTAMP '{init}'");
    const city = Object.keys(S.cfg.sql_places).find((name) => sqlFor(name) === q);
    if (city) return city;
    if (S.myCoords) {
      const c = coordsOf(q);
      const want = sqlForCoords(S.myCoords.lat, S.myCoords.lon);
      if (c && q === want) return MY;
    }
    return null;
  };

  function renderSqlPlaces() {
    const places = $("#sqlPlaces");
    const running = S.sql && S.sql.status === "running";
    places.replaceChildren(...[
      ...Object.keys(S.cfg.sql_places).map((name) => {
        const b = el("button", name === S.sqlPlace ? "on" : "", name);
        b.type = "button";
        b.disabled = !!running;
        b.addEventListener("click", () => {
          if (S.sql && S.sql.status === "running") return; // ignore clicks while a query runs
          S.sqlPlace = name;
          S.myCoords = null;
          const query = sqlFor(name);
          sqlBox().value = query;
          renderSqlPlaces();
          runSql(query); // no Run button: chips run immediately
        });
        return b;
      }),
      (() => {
        const b = el("button", S.sqlPlace === MY ? "on" : "", S.locating ? "Locating…" : `📍 ${MY}`);
        b.type = "button";
        b.title = "Use browser geolocation";
        b.disabled = !!S.locating || !!running;
        b.setAttribute("aria-pressed", String(S.sqlPlace === MY));
        b.addEventListener("click", () => useMyLocation());
        return b;
      })(),
    ]);
    $("#sqlFoot").textContent =
      S.sqlPlace === MY && S.myCoords
        ? `Mean composite intensity over ${MY} (${fmtCoords(S.myCoords.lat, S.myCoords.lon)}), 0–2 h from init · PostgreSQL wire protocol`
        : S.sqlPlace
          ? `Mean composite intensity over ${S.sqlPlace}, 0–2 h from init · PostgreSQL wire protocol`
          : "Your query · PostgreSQL wire protocol";
  }

  function myLocationError(msg) {
    S.locating = false;
    S.sql = { status: "error", query: sqlBox().value, error: msg };
    renderSqlPlaces(); // re-enable the button first; renderSql then overwrites the foot with the error
    renderSql();
  }

  function useMyLocation() {
    holdPanel();
    if (S.sql && S.sql.status === "running") return; // ignore clicks while a query runs
    if (!("geolocation" in navigator)) {
      myLocationError("Geolocation is not available in this browser – try Berlin, Paris or London.");
      return;
    }
    S.locating = true;
    renderSqlPlaces();
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const lat = +pos.coords.latitude.toFixed(4);
        const lon = +pos.coords.longitude.toFixed(4);
        S.locating = false;
        if (!inBounds(lat, lon)) {
          myLocationError(
            `My location (${fmtCoords(lat, lon)}) is outside the StormScope-EU domain (${boundsText()}) – try Berlin, Paris or London.`
          );
          return;
        }
        S.myCoords = { lat, lon };
        S.sqlPlace = MY;
        const query = sqlForCoords(lat, lon);
        sqlBox().value = query;
        renderSqlPlaces();
        runSql(query); // booth POSTs the query; hosted GETs lat/lon (see runPlaceSql)
      },
      (err) => {
        const hint = " – try Berlin, Paris or London.";
        if (err && err.code === 1) myLocationError(`Location permission denied – allow location access or pick a city.${hint}`);
        else if (err && err.code === 3) myLocationError(`Location request timed out${hint}`);
        else myLocationError(`Location unavailable${err && err.message ? `: ${err.message}` : hint}`);
      },
      { timeout: 10000, maximumAge: 60000 }
    );
  }

  // the box shows the init the server filled in last; send it back as {init} so Run
  // always queries the newest run (UTC) instead of pinning an old one
  const withInitSlot = (query) => {
    const m = /TIMESTAMP '([^']*)'/.exec((S.sql && S.sql.query) || "");
    return m ? query.split(`TIMESTAMP '${m[1]}'`).join("TIMESTAMP '{init}'") : query;
  };

  async function runSql(query) {
    if (S.sql && S.sql.status === "running") return; // ignore clicks while a query runs
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

  // hosted page: the server runs only a city chip's query (or a coordinate inside
  // data_bounds for My location), for the newest init
  async function runPlaceSql() {
    const run = latestShown();
    if (!run) return;
    const params =
      S.sqlPlace === MY && S.myCoords
        ? { lat: String(S.myCoords.lat), lon: String(S.myCoords.lon), init: run.time }
        : S.sqlPlace
          ? { place: S.sqlPlace, init: run.time }
          : null;
    if (!params) return;
    S.sql = { ...(S.sql || {}), status: "running", ran_at: null };
    renderSql();
    try {
      const res = await fetch(`/api/sql?${new URLSearchParams(params)}`);
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
    const metric = /AVG\(\s*(true_color|airmass)\s*\)/i.exec(r?.query || box.value);
    $("#sqlMetric").textContent = metric ? `${VAR_LABELS[metric[1].toLowerCase()]} · SQL` : "SQL Result";
    const running = r && r.status === "running";
    status.classList.remove("err");
    status.title = "";
    $(".sql").classList.toggle("running", !!running);
    if (running) holdPanel();
    // never overwrite what someone is typing
    if (r && r.query && !S.sqlDirty && document.activeElement !== box) {
      box.value = r.query;
      S.sqlPlace = placeOf(r.query);
    }
    const empty = () => { bars.replaceChildren(); for (const _ of S.cfg.leads) bars.append(el("div", "empty")); };
    if (!r) { status.textContent = "connecting…"; empty(); return; }
    if (r.status === "idle") { status.textContent = "pick a place"; empty(); return; }
    if (r.status === "disabled") { status.textContent = "SQL service off"; status.classList.add("err"); empty(); return; }
    if (r.status === "error") {
      status.textContent = "query failed";
      status.classList.add("err");
      status.title = r.error || "";
      $("#sqlFoot").textContent = (r.error || "query failed").split("\n")[0];
      empty();
      return;
    }
    if (running) { status.textContent = "running…"; return; } // keep the old bars, dimmed
    renderSqlPlaces(); // restores the foot text after an error
    // first numeric column after the x column, so hand-written queries still plot
    const yi = (r.rows && r.rows[0] ? r.rows[0].findIndex((v, i) => i > 0 && typeof v === "number") : -1);
    const rows = yi > 0 ? r.rows.filter((row) => typeof row[yi] === "number") : [];
    status.textContent = `${{ demo: "demo · ", offline: "saved · " }[r.status] ?? ""}ran ${ago(r.ran_at)} ago · ${r.seconds} s · ${r.cached ? "cached · " : ""}${(r.rows || []).length} rows`;
    if (!rows.length) { empty(); return; }
    bars.replaceChildren();
    for (const row of rows.slice(0, 60)) {
      const y = row[yi];
      const d = el("div");
      d.style.height = `${Math.min(100, Math.max(0, (100 * y) / 255))}%`; // fixed 0-255 true_color scale, so heights read as absolute brightness
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
      if (S.sqlPlace) { S.sqlPlace = null; S.myCoords = null; renderSqlPlaces(); }
    });
    box.addEventListener("keydown", (e) => {
      e.stopPropagation(); // typing must not trigger the booth shortcuts
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); runSql(box.value); }
      else if (e.key === "Escape") box.blur();
    });
    $("#sqlReset").addEventListener("click", () => {
      if (S.sql && S.sql.status === "running") return; // ignore clicks while a query runs
      S.sqlPlace = placeOf(defaultSql());
      S.myCoords = null;
      S.sqlDirty = true; // filled in, not run
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
    renderCompositePreviews();
    renderModelSequence();
    renderPublishedCycles();
    renderComputeTile();
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

  // ------------------------------------------------------------------ ingest strip collapse
  function setStripCollapsed(on) {
    $(".app").classList.toggle("strip-collapsed", on);
    $("#stripToggle").setAttribute("aria-expanded", String(!on));
    map?.resize(); // the globe's row changed height
  }
  // not persisted: the booth must always start with the live strip showing
  function initStrip() {
    setStripCollapsed(false);
    $("#stripToggle").addEventListener("click", () => setStripCollapsed(!$(".app").classList.contains("strip-collapsed")));
  }

  // ------------------------------------------------------------------ attract reset
  // Anything a visitor or staffer toggles (loop, pause, speed, variable, init, rotation,
  // strip) goes back to the defaults after attract_reset_s without input, like the camera.
  let lastInput = Date.now();
  function initAttractReset() {
    const touch = () => { lastInput = Date.now(); };
    for (const type of ["keydown", "pointerdown", "wheel"]) window.addEventListener(type, touch, { passive: true, capture: true });
    const secs = S.cfg.attract_reset_s;
    if (!secs) return;
    setInterval(() => {
      if (Date.now() - lastInput < secs * 1000) return;
      const dirty = !S.loop || S.animPaused || S.rotatePaused || S.selInit || S.variable !== "true_color" ||
        S.speed !== (S.cfg.playback_speed in SPEEDS ? S.cfg.playback_speed : "slow") || $(".app").classList.contains("strip-collapsed");
      if (!dirty) return;
      lastInput = Date.now();
      S.loop = true; $("#loopLabel").setAttribute("aria-pressed", "true");
      S.animPaused = false; S.rotatePaused = false; S.selInit = null;
      S.speed = S.cfg.playback_speed in SPEEDS ? S.cfg.playback_speed : "slow";
      setStripCollapsed(false);
      if (S.variable !== "true_color") setVariable("true_color"); else syncLayers(true);
      renderPlayback(); renderInits(); showFrame(); scheduleAnim(); showPanel(S.panel);
    }, 1000);
  }

  // ------------------------------------------------------------------ keys
  function initKeys() {
    window.addEventListener("keydown", (e) => {
      if (e.altKey || e.ctrlKey || e.metaKey) return;
      if (e.target.closest && e.target.closest("textarea, input, button")) return;
      const k = e.key.toLowerCase();
      if (k === "arrowright" || k === "pagedown") { S.manualUntil = Date.now() + 60000; showPanel(S.panel + 1); }
      else if (k === "arrowleft" || k === "pageup") { S.manualUntil = Date.now() + 60000; showPanel(S.panel - 1); }
      else if (k === " ") { togglePanelPause(); e.preventDefault(); }
      else if (k === "p") togglePlay();
      else if (k === "o") toggleLoop();
      else if (k === "s") { const names = Object.keys(SPEEDS); setSpeed(names[(names.indexOf(S.speed) + 1) % names.length]); }
      else if (k === "t") setVariable("true_color");
      else if (k === "a") setVariable("airmass");
      else if (k === "l") selectInit(0);
      else if (k === "c") setStripCollapsed(!$(".app").classList.contains("strip-collapsed"));
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
    setTimezone(S.cfg.timezone);
    if (S.cfg.playback_speed in SPEEDS) S.speed = S.cfg.playback_speed;
    initSqlEditor();
    renderVarToggle();
    renderVarCards();
    renderPlayback();
    $("#playBtn").addEventListener("click", togglePlay);
    $("#loopLabel").addEventListener("click", toggleLoop);
    const scrub = $("#scrub");
    scrub.addEventListener("input", (e) => scrubTo(Number(e.target.value)));
    scrub.addEventListener("pointerdown", () => { scrubbing = true; });
    const released = () => { if (scrubbing) { scrubbing = false; scrub.blur(); showFrame(); scheduleAnim(); } };
    for (const type of ["pointerup", "pointercancel"]) window.addEventListener(type, released);
    window.addEventListener("blur", released); // pointer let go outside the window
    scrub.addEventListener("change", () => scrub.blur()); // keyboard nudges must not swallow the booth shortcuts
    // clicked buttons must not keep focus either: Space would re-fire them and the shortcuts would go dead
    document.addEventListener("click", (e) => e.target.closest?.("button")?.blur());
    initGlobe();
    initPanels();
    initVisuals();
    for (const card of document.querySelectorAll(".card[data-var]")) {
      card.addEventListener("click", () => setVariable(card.dataset.var));
    }
    initStrip();
    initKeys();
    initAttractReset();
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
