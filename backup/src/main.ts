import { MapboxOverlay } from "@deck.gl/mapbox";
import type { MinimalTileData } from "@developmentseed/deck.gl-raster";
import type { GetTileDataOptions } from "@developmentseed/deck.gl-zarr";
import { ZarrLayer } from "@developmentseed/deck.gl-zarr";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import * as zarr from "zarrita";
import "./geos.js";
import "./style.css";
import type { Snapshot, Variable } from "./store.js";
import { openSnapshot, VARIABLES } from "./store.js";

const BASEMAP = "https://basemaps.cartocdn.com/gl/dark-matter-nolabels-gl-style/style.json";
const BELOW = "boundary_country_outline"; // imagery goes under the basemap's country borders
const FRAME_MS = 400; // playback: one lead step
const HOLD_MS = 2000; // pause on the last lead before looping
const POLL_MS = 10_000; // re-read the `main` ref this often
const CACHE_TILES = 120; // ~2.7 MB each as RGBA8; one run of one variable is 13 leads x 4 chunks

/** One decoded chunk of one frame. */
type Tile = { image: ImageData; empty: boolean };
/** What deck.gl-raster holds per tile: just its chunk index; pixels come from `ready`. */
type TileRef = MinimalTileData & { x: number; y: number };
type Frame = { variable: Variable; init: number; lead: number };

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const ui = {
  variable: $<HTMLSelectElement>("variable"),
  init: $<HTMLSelectElement>("init"),
  lead: $<HTMLInputElement>("lead"),
  play: $<HTMLButtonElement>("play"),
  label: $("label"),
  status: $("status"),
};

let snap: Snapshot | null = null;
let frame: Frame | null = null; // requested
let shown: Frame | null = null; // on screen; only ever a frame whose chunks are all loaded
let playing = true;

/* ------------------------------------------------------------------ tiles */

// Decoded chunks, keyed by frame + chunk index. Keyed by init *timestamp*, not `time`
// index: the repo keeps a rolling window, so index 0 is a different run after each new init.
const loading = new Map<string, Promise<Tile>>();
const ready = new Map<string, Tile>();
const BLANK = new ImageData(1, 1);
const frameKey = (f: Frame) => `${f.variable}/${f.init}/${f.lead}`;
const tileKey = (f: Frame, y: number, x: number) => `${frameKey(f)}/${y}/${x}`;

/** float32 RGB in 0-1, NaN off the satellite disk -> RGBA8, transparent where NaN. */
function toImage(rgb: Float32Array, width: number, height: number): Tile {
  const out = new Uint8ClampedArray(width * height * 4);
  let written = false;
  for (let i = 0, j = 0; i < rgb.length; i += 3, j += 4) {
    const r = rgb[i]!;
    if (Number.isNaN(r)) continue;
    // Chunks never written (a lead still running) come back as fill = 0 everywhere.
    if (r !== 0 || rgb[i + 1] !== 0 || rgb[i + 2] !== 0) written = true;
    out[j] = r * 255;
    out[j + 1] = rgb[i + 1]! * 255;
    out[j + 2] = rgb[i + 2]! * 255;
    out[j + 3] = 255;
  }
  if (!written) out.fill(0);
  return { image: new ImageData(out, width, height), empty: !written };
}

/** Chunk grid of the spatial dims: [rows, cols]. */
function grid(s: Snapshot, v: Variable) {
  const { shape, chunks } = s.arrays[v];
  return [Math.ceil(shape[3]! / chunks[3]!), Math.ceil(shape[4]! / chunks[4]!)] as const;
}

function loadTile(s: Snapshot, f: Frame, y: number, x: number): Promise<Tile> {
  const key = tileKey(f, y, x);
  const hit = loading.get(key);
  if (hit) {
    loading.delete(key); // LRU: re-insert at the end
    loading.set(key, hit);
    return hit;
  }
  const arr = s.arrays[f.variable];
  const [, , , ch, cw] = arr.chunks as number[];
  const [, , , h, w] = arr.shape as number[];
  const y0 = y * ch!, x0 = x * cw!;
  const y1 = Math.min(y0 + ch!, h!), x1 = Math.min(x0 + cw!, w!);
  const t = s.inits.indexOf(f.init), l = s.leads.indexOf(f.lead);
  const p = zarr
    .get(arr, [0, t, l, zarr.slice(y0, y1), zarr.slice(x0, x1), null])
    .then((c) => toImage(c.data as Float32Array, x1 - x0, y1 - y0));
  loading.set(key, p);
  p.then(
    (tile) => {
      // Not-yet-written chunks are dropped so they are fetched again later.
      if (tile.empty) loading.delete(key);
      else if (loading.get(key) === p) ready.set(key, tile);
    },
    () => loading.delete(key),
  );
  for (const old of loading.keys()) {
    if (loading.size <= CACHE_TILES) break;
    loading.delete(old);
    ready.delete(old);
  }
  return p;
}

/** All chunks of a frame; true when the frame has data (some chunk was written). */
async function loadFrame(s: Snapshot, f: Frame) {
  const [ny, nx] = grid(s, f.variable);
  const jobs = [];
  for (let y = 0; y < ny; y++) for (let x = 0; x < nx; x++) jobs.push(loadTile(s, f, y, x));
  return (await Promise.all(jobs)).some((t) => !t.empty);
}

/** Warm the cache for every lead of the selected run, so the loop plays from memory. */
async function prefetch(s: Snapshot, variable: Variable, init: number) {
  for (const lead of s.leads) {
    if (snap !== s || frame?.variable !== variable || frame.init !== init) return; // selection moved on
    await loadFrame(s, { variable, init, lead }).catch(() => {});
  }
}

/* ------------------------------------------------------------------ layer */

/**
 * One layer per (variable, run). deck.gl-raster builds each chunk's reprojection mesh
 * once per layer; stepping through leads only re-runs `renderTile`, which swaps the
 * image, so playback costs a texture upload per chunk instead of a mesh rebuild.
 */
function render() {
  const s = snap, f = shown;
  if (!s || !f) return;
  const layer = new ZarrLayer<zarr.Readable, "float32", TileRef>({
    id: `${f.variable}/${f.init}`,
    node: withoutRgb(s.arrays[f.variable]),
    metadata: s.attrs, // GeoZarr `proj:projjson` + `spatial:*` live on the root group
    // Required by ZarrLayer's validation; the actual reads happen in `loadTile`.
    selection: { ensemble: 0, time: s.inits.indexOf(f.init), lead_time: 0 },
    getTileData: async (_arr, o: GetTileDataOptions) => {
      await loadTile(s, f, o.y, o.x);
      return { x: o.x, y: o.y, width: o.width, height: o.height };
    },
    // A chunk with no data in this frame (all fill) draws nothing.
    renderTile: (t) => ({ image: ready.get(tileKey(shown!, t.y, t.x))?.image ?? BLANK }),
    updateTriggers: { renderTile: [frameKey(f)] },
    // Interleaved-mode prop read by @deck.gl/mapbox; ZarrLayer's prop type doesn't list it.
    ...({ beforeId: BELOW } as object),
  });
  overlay.setProps({ layers: [layer] });
}

/**
 * The arrays are [ensemble, time, lead_time, y, x, rgb]: RGB is the *last* dim, while
 * ZarrLayer wants y, x last (it tiles on the last two dims). Hand the layer a view
 * without `rgb`; `loadTile` does the real read with `null` (all three bands) appended.
 */
function withoutRgb(arr: zarr.Array<"float32", zarr.Readable>) {
  // Memoized: a new `node` object makes ZarrLayer drop its tiles and re-parse.
  let view = views.get(arr);
  if (!view) {
    view = Object.create(arr, {
      shape: { value: arr.shape.slice(0, -1) },
      chunks: { value: arr.chunks.slice(0, -1) },
      dimensionNames: { value: arr.dimensionNames!.slice(0, -1) },
    }) as zarr.Array<"float32", zarr.Readable>;
    views.set(arr, view);
  }
  return view;
}
const views = new WeakMap<object, zarr.Array<"float32", zarr.Readable>>();

/* --------------------------------------------------------------------- ui */

const fmt = (ms: number) => new Date(ms).toISOString().slice(0, 16).replace("T", " ") + " UTC";

function label(f: Frame, note = "") {
  ui.label.textContent =
    `${f.lead === 0 ? "observed" : `+${f.lead} min forecast`} · valid ${fmt(f.init + f.lead * 60e3)}` +
    (note ? ` · ${note}` : "");
}

/** Request a frame; it goes on screen once all its chunks are decoded. */
async function select(next: Partial<Frame>) {
  const s = snap;
  if (!s) return;
  const prev = frame;
  const f: Frame = {
    variable: next.variable ?? prev?.variable ?? "true_color",
    init: next.init ?? prev?.init ?? s.inits.at(-1)!,
    lead: next.lead ?? prev?.lead ?? 0,
  };
  frame = f;
  ui.variable.value = f.variable;
  ui.init.value = String(f.init);
  ui.lead.value = String(s.leads.indexOf(f.lead));
  if (!prev || prev.variable !== f.variable || prev.init !== f.init) void prefetch(s, f.variable, f.init);
  label(f, "loading…");
  const written = await loadFrame(s, f).catch((e) => {
    console.error(e);
    return false;
  });
  if (frame !== f || snap !== s) return; // superseded while loading
  if (!written) return label(f, "not written yet"); // keep the previous frame on screen
  shown = f;
  label(f);
  render();
}

function fillInits(s: Snapshot) {
  ui.init.replaceChildren(
    ...s.inits
      .map((t, i) => new Option(`${fmt(t)}${i === s.inits.length - 1 ? " (latest)" : ""}`, String(t)))
      .reverse(),
  );
  ui.lead.max = String(s.leads.length - 1);
}

ui.variable.replaceChildren(...VARIABLES.map((v) => new Option(v.replace("_", " "), v)));
ui.variable.onchange = () => select({ variable: ui.variable.value as Variable });
ui.init.onchange = () => select({ init: Number(ui.init.value) });
ui.lead.oninput = () => {
  setPlaying(false);
  void select({ lead: snap!.leads[Number(ui.lead.value)]! });
};
ui.play.onclick = () => setPlaying(!playing);

function setPlaying(on: boolean) {
  playing = on;
  ui.play.textContent = on ? "Pause" : "Play";
}

addEventListener("keydown", (e) => {
  if (e.target instanceof HTMLSelectElement) return;
  if (e.key === " " || e.key === "p") setPlaying(!playing);
  else if (e.key === "t") void select({ variable: "true_color" });
  else if (e.key === "a") void select({ variable: "airmass" });
  else if (e.key === "l") void select({ init: snap?.inits.at(-1) });
  else return;
  e.preventDefault();
});

async function tick() {
  const s = snap, f = frame;
  if (playing && s && f && shown === f) {
    const next = (s.leads.indexOf(f.lead) + 1) % s.leads.length;
    // Stop at the last lead that already has data, then loop back to the analysis frame.
    const atEnd = next === 0 || !(await loadFrame(s, { ...f, lead: s.leads[next]! }).catch(() => false));
    if (atEnd) await new Promise((r) => setTimeout(r, HOLD_MS));
    if (playing && frame === f) await select({ lead: s.leads[atEnd ? 0 : next]! });
  }
  setTimeout(tick, FRAME_MS);
}

/* ------------------------------------------------------------------- data */

async function poll() {
  try {
    const s = await openSnapshot();
    if (s.id !== snap?.id) {
      const followLatest = !frame || frame.init === snap?.inits.at(-1);
      snap = s;
      fillInits(s);
      ui.status.textContent = `snapshot ${s.id.slice(0, 8)} · ${s.inits.length} runs`;
      // Stay on the viewed run unless it rolled out of the window or we were on the latest.
      const init = followLatest || !frame || !s.inits.includes(frame.init) ? s.inits.at(-1)! : frame.init;
      if (shown && !s.inits.includes(shown.init)) shown = null;
      await select({ init });
      void prefetch(s, frame!.variable, frame!.init); // new leads may have landed; cached ones are reused
    }
  } catch (e) {
    console.error(e);
    ui.status.textContent = `read failed: ${(e as Error).message}`;
  }
  setTimeout(poll, POLL_MS);
}

/* -------------------------------------------------------------------- map */

const map = new maplibregl.Map({
  container: "map",
  style: BASEMAP,
  center: [12, 50],
  zoom: 3.6,
  attributionControl: { compact: true },
});
const overlay = new MapboxOverlay({ interleaved: true, layers: [] });
map.addControl(overlay);
map.on("load", () => {
  void poll();
  void tick();
});
