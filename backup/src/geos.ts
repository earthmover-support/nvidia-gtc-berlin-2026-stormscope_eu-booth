import proj4 from "proj4";

/**
 * Geostationary projection for the repo's CRS, registered with proj4js.
 *
 * 1. The CRS (pyproj `proj:projjson` / `proj:wkt2`) names its method "Geostationary
 *    Satellite (Sweep Y)". proj4js implements the projection as `geos` but knows
 *    neither that name nor wkt-parser's `satellite_height` key, so it would not parse.
 *
 * 2. The FCI crop is a rectangle cut from the round Earth disk, so its northern corners
 *    look past the limb into space, where `geos` returns NaN. deck.gl-raster needs
 *    finite tile corners to pick tiles, and its mesh refinement needs exact round trips
 *    (pixel -> lon/lat -> pixel). So scan angles beyond the limb are folded onto the
 *    far side of the Earth: a ray that misses by d is mapped to the hidden surface point
 *    on the ray d *inside* the limb (its second, far intersection). That is continuous
 *    at the limb and exactly invertible. The pixels out there are NaN in the data and
 *    render transparent, so only the geometry has to be well behaved.
 *
 * Formulas follow proj4js `lib/projections/geos.js` (ellipse branch; it reduces to the
 * sphere for es = 0), with sweep axis y.
 */
type Point = { x: number; y: number };
type Geos = {
  a: number;
  long0: number;
  h: number;
  satellite_height?: number;
  sweep: string;
  radius_g: number;
  radius_g_1: number;
  radius_p: number;
  radius_p2: number;
  radius_p_inv2: number;
  C: number;
};
type ProjImpl = {
  names: string[];
  init(this: Geos): void;
  forward(this: Geos, p: Point): Point;
  inverse(this: Geos, p: Point): Point;
};
// proj4's bundled typings mis-declare this registry (`get` returns the implementation).
const registry = proj4.Proj.projections as unknown as {
  get(name: string): ProjImpl;
  add(impl: ProjImpl): void;
};
const geos = registry.get("geos");

/** Ray direction (satellite frame, x toward Earth = -1) for scan angles in radians. */
function ray(g: Geos, sx: number, sy: number) {
  const vy = Math.tan(sx / g.radius_g_1);
  const vz = Math.tan(sy / g.radius_g_1) * Math.hypot(1, vy);
  const vzp = vz / g.radius_p;
  const a = vy * vy + vzp * vzp + 1;
  const b = -2 * g.radius_g;
  return { vy, vz, a, b, det: b * b - 4 * a * g.C };
}

/** Scale t such that (t*sx, t*sy) sits on the limb (det = 0). Needs one on- and one off-disk end. */
function limbScale(g: Geos, sx: number, sy: number, inside: number, outside: number) {
  for (let i = 0; i < 60; i++) {
    const mid = (inside + outside) / 2;
    if (ray(g, sx * mid, sy * mid).det >= 0) inside = mid;
    else outside = mid;
  }
  return inside;
}

/** Scan angles (radians) -> lon/lat (radians); `far` picks the hidden intersection. */
function surface(g: Geos, sx: number, sy: number, far: boolean): Point {
  const { vy, vz, a, b, det } = ray(g, sx, sy);
  const k = (-b + (far ? 1 : -1) * Math.sqrt(Math.max(det, 0))) / (2 * a);
  const vx = g.radius_g - k;
  const lon = Math.atan2(vy * k, vx);
  const lat = Math.atan(g.radius_p_inv2 * Math.tan(Math.atan((vz * k * Math.cos(lon)) / vx)));
  return { x: lon + g.long0, y: lat };
}

registry.add({
  ...geos,
  names: ["Geostationary Satellite (Sweep Y)"],

  init() {
    this.h = this.satellite_height!;
    this.sweep = "y";
    geos.init.call(this);
  },

  inverse(p) {
    const sx = p.x / this.a, sy = p.y / this.a;
    if (ray(this, sx, sy).det >= 0) return surface(this, sx, sy, false);
    // Off the disk at scale 1, limb at scale t < 1: reflect to scale 2t - 1, far side.
    const t = limbScale(this, sx, sy, 0, 1);
    return surface(this, sx * (2 * t - 1), sy * (2 * t - 1), true);
  },

  forward(p) {
    const lon = p.x - this.long0;
    const lat = Math.atan(this.radius_p2 * Math.tan(p.y));
    const r = this.radius_p / Math.hypot(this.radius_p * Math.cos(lat), Math.sin(lat));
    const vx = r * Math.cos(lon) * Math.cos(lat);
    const vy = r * Math.sin(lon) * Math.cos(lat);
    const vz = r * Math.sin(lat);
    const tmp = this.radius_g - vx;
    // Scan angles of the ray through the point; for hidden points that ray enters
    // the near side first, so these are on-disk and get folded back out below.
    let sx = this.radius_g_1 * Math.atan(vy / tmp);
    let sy = this.radius_g_1 * Math.atan(vz / Math.hypot(vy, tmp));
    const hidden = (this.radius_g - vx) * vx - vy * vy - vz * vz * this.radius_p_inv2 < 0;
    if (hidden) {
      let outside = 2;
      while (ray(this, sx * outside, sy * outside).det >= 0) outside *= 2;
      const t = limbScale(this, sx, sy, 1, outside); // limb at scale t > 1
      sx *= 2 * t - 1;
      sy *= 2 * t - 1;
    }
    return { x: sx * this.a, y: sy * this.a };
  },
});
