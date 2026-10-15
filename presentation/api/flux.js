// Proxy to the Flux tiles service so each tile is rendered once for every visitor.
//
// Two caches: Vercel's CDN (per edge region, keyed by hostname too) and the Runtime Cache
// here. The function always runs in dub1, so the Runtime Cache is one copy shared by every
// region and hostname. Concurrent misses on one instance share a single Flux request.
// Only successful responses are cached: a 504 from a busy service must not stick.
// Tiles of a listed init never change. Copy of site/api/flux.js; the booth page pins each
// run to its snapshot, so tile paths carry a snapshot id instead of `main`.
// Clear a rerun with `vercel cache invalidate --tag init-<YYYYMMDDHHMM>` (or `tiles`).
import { getCache } from "@vercel/functions";

const FLUX = "https://tiles-edf5beae0d2afdc4.eu-west-1.compute.arraylake.app/v1/services/tiles-eu/gtc-berlin";
const REPO = "stormscope-eu_gtc_berlin_2026_EU_region";
// Only what the page requests: an unchecked path with `..` would reach other services on the host.
const ALLOWED = new RegExp(`^${REPO}/(main|[0-9A-Z]{20})/tiles/WebMercatorQuad/\\d+/\\d+/\\d+$`);

const cache = getCache({ namespace: "flux" });
const inflight = new Map(); // key -> Promise of { status, type, body (base64) }

async function fromFlux(key, ttl, tags) {
  const res = await fetch(`${FLUX}/${key}`);
  const out = {
    status: res.status,
    type: res.headers.get("content-type"),
    body: Buffer.from(await res.arrayBuffer()).toString("base64"), // the cache stores JSON
  };
  if (res.ok) await cache.set(key, out, { ttl, tags }).catch((e) => console.warn("cache set:", e.message));
  return out;
}

export async function GET(req) {
  const url = new URL(req.url);
  const path = url.searchParams.get("path") ?? "";
  url.searchParams.delete("path");
  if (!ALLOWED.test(path)) return new Response("not found", { status: 404 });

  const key = path + url.search;
  const ttl = 86400;
  const init = (url.searchParams.get("time") ?? "").replace(/\D/g, "").slice(0, 12);
  const tags = ["tiles", `init-${init}`];

  let state = "hit";
  let out = await cache.get(key).catch(() => null);
  if (!out) {
    state = inflight.has(key) ? "joined" : "miss";
    if (state === "miss") inflight.set(key, fromFlux(key, ttl, tags).finally(() => inflight.delete(key)));
    try {
      out = await inflight.get(key);
    } catch (e) {
      return new Response(`flux: ${e.message}`, { status: 502 });
    }
  }

  const headers = { "content-type": out.type, "cache-control": "public, max-age=0", "x-flux-proxy-cache": state };
  if (out.status < 300) {
    headers["Vercel-CDN-Cache-Control"] = `max-age=${ttl}`;
    headers["Vercel-Cache-Tag"] = tags.join(",");
  }
  return new Response(Buffer.from(out.body, "base64"), { status: out.status, headers });
}
