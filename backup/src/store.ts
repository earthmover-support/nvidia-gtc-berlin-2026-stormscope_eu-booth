import { IcechunkStore } from "icechunk-js";
import type { AbsolutePath, AsyncReadable, RangeQuery } from "@zarrita/storage";
import * as zarr from "zarrita";

/** Where the browser finds the raw Icechunk objects (see server.py). */
export const REPO_URL = new URL("/icechunk", location.href).href;

export const VARIABLES = ["true_color", "airmass"] as const;
export type Variable = (typeof VARIABLES)[number];

/** One opened snapshot of the repo: arrays, coordinates and GeoZarr attrs. */
export type Snapshot = {
  id: string;
  /** Root group attrs; carry `proj:projjson` + `spatial:*` for ZarrLayer. */
  attrs: Record<string, unknown>;
  arrays: Record<Variable, zarr.Array<"float32", zarr.Readable>>;
  /** Init times (ms since epoch), in `time` index order. */
  inits: number[];
  /** Lead times in minutes, in `lead_time` index order. */
  leads: number[];
};

const TIME_UNIT_MS: Record<string, number> = { us: 1e-3, ms: 1, s: 1e3, m: 60e3, h: 3600e3 };

/**
 * zarrita has no `numpy.datetime64` / `numpy.timedelta64` data types (xarray writes
 * them for `time` and `lead_time`). On disk they are plain little-endian int64, so
 * this wrapper presents them as `int64` and remembers the unit for decoding.
 */
class DatetimeAsInt64 implements AsyncReadable {
  units = new Map<string, number>();
  constructor(private inner: IcechunkStore) {}

  async get(key: AbsolutePath, opts?: { signal?: AbortSignal }) {
    const bytes = await this.inner.get(key, opts);
    if (!bytes || !key.endsWith("/zarr.json")) return bytes;
    const meta = JSON.parse(new TextDecoder().decode(bytes));
    const name: string | undefined = meta.data_type?.name;
    if (name !== "numpy.datetime64" && name !== "numpy.timedelta64") return bytes;
    this.units.set(key.slice(0, -"/zarr.json".length), TIME_UNIT_MS[meta.data_type.configuration.unit]!);
    meta.data_type = "int64";
    meta.fill_value = 0;
    return new TextEncoder().encode(JSON.stringify(meta));
  }

  getRange(key: AbsolutePath, range: RangeQuery, opts?: { signal?: AbortSignal }) {
    return this.inner.getRange(key, range, opts);
  }
}

async function readTimes(store: DatetimeAsInt64, root: zarr.Group<zarr.Readable>, name: string) {
  const arr = await zarr.open(root.resolve(name), { kind: "array" });
  const { data } = await zarr.get(arr);
  const ms = store.units.get(`/${name}`)!;
  return Array.from(data as BigInt64Array, (v) => Number(v) * ms);
}

/** Open the current `main` snapshot. Cheap when nothing changed: one small `repo` GET. */
export async function openSnapshot(): Promise<Snapshot> {
  const ice = await IcechunkStore.open(REPO_URL, { branch: "main", withRangeCoalescing: zarr.withRangeCoalescing });
  const store = new DatetimeAsInt64(ice);
  const root = await zarr.open(store, { kind: "group" });
  const arrays = {} as Snapshot["arrays"];
  for (const v of VARIABLES) {
    const arr = await zarr.open(root.resolve(v), { kind: "array" });
    if (!arr.is("float32")) throw new Error(`${v}: expected float32, got ${arr.dtype}`);
    arrays[v] = arr;
  }
  return {
    id: snapshotId(ice),
    attrs: root.attrs,
    arrays,
    inits: await readTimes(store, root, "time"),
    leads: (await readTimes(store, root, "lead_time")).map((ms) => ms / 60e3),
  };
}

function snapshotId(ice: IcechunkStore) {
  return Array.from(ice.session.getSnapshotId(), (b) => b.toString(16).padStart(2, "0")).join("");
}
