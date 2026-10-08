// Cache-first store for Flux tile responses. The tiles service sends branch tiles with
// `max-age=0`, so without this every repeat tile goes back to the (slow) service.
// A published init never changes: the pipeline adds it to `time` and writes all its leads
// in a single commit, so a tile URL (branch, init, lead, variable, z/y/x) is immutable.
// Bump CACHE when tiles change under the same URL: a rerun of an init, a branch reset, or
// a tiles service upgrade that renders differently.
const CACHE = "tiles-v1";
const KEEP_INITS = 12; // the Cache API has no eviction; older inits are dropped
const PRUNE_EVERY = 50; // stores between prunes; listing the keys gets slow with many tiles

let stored = 0;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) =>
  e.waitUntil(Promise.all([
    caches.keys().then((names) => Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n)))),
    self.clients.claim(),
    prune(),
  ])),
);

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || !url.pathname.includes("/tiles/WebMercatorQuad/") || !url.searchParams.has("time")) return;
  e.respondWith(cacheFirst(e));
});

async function cacheFirst(e) {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(e.request);
  if (hit) return hit;
  const res = await fetch(e.request);
  // Errors (504 when the service queue is full, 422, cold starts) must not stick.
  if (res.ok) e.waitUntil(store(cache, e.request, res.clone()));
  return res;
}

async function store(cache, req, res) {
  await cache.put(req, res);
  if (++stored % PRUNE_EVERY === 0) await prune();
}

const timeOf = (req) => new URL(req.url).searchParams.get("time");

async function prune() {
  const cache = await caches.open(CACHE);
  const reqs = await cache.keys();
  const keep = new Set([...new Set(reqs.map(timeOf))].sort().reverse().slice(0, KEEP_INITS));
  await Promise.all(reqs.filter((r) => !keep.has(timeOf(r))).map((r) => cache.delete(r)));
}
