// Proxy to the Flux tiles service so Vercel's CDN keeps one copy of each tile for every
// visitor. Only successful responses get a CDN lifetime: a 504 from a busy service must not
// stick. Tiles of a listed init never change; the init list (`_meta`) changes every 10 min.
export const config = { runtime: "edge" };

const FLUX = "https://tiles-edf5beae0d2afdc4.eu-west-1.compute.arraylake.app/v1/services/tiles-eu/gtc-berlin";

export default async function handler(req) {
  const url = new URL(req.url);
  const path = url.searchParams.get("path");
  url.searchParams.delete("path");
  const res = await fetch(`${FLUX}/${path}${url.search}`);
  const headers = new Headers(res.headers);
  headers.delete("content-encoding"); // fetch already decoded the body
  headers.delete("content-length");
  if (res.ok) headers.set("Vercel-CDN-Cache-Control", `max-age=${path.startsWith("_meta/") ? 30 : 86400}`);
  return new Response(res.body, { status: res.status, headers });
}
