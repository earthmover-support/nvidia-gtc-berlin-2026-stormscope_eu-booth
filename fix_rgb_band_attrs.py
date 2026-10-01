"""One-off: add the RGB band metadata to the existing Arraylake repo (#10).

Writes what `06_stormscope_eu_pipeline.py` now writes at schema creation, onto the
cube already in `gtc-berlin/stormscope-eu_gtc_berlin_2026`:

- `valid_min`/`valid_max` = 0/1 on `true_color` and `airmass`. These are the only
  attributes the tiles service reads on the RGB path; they set the byte stretch,
  which before this fell back to the float-dtype default of 0..1.
- `long_name` and `color_interpretation = "red green blue"` on the `rgb` coordinate,
  recording the band order next to the values.

Metadata only: no chunk is read or rewritten. Idempotent: if the attributes are
already there, nothing is committed.

Usage:
    uv run python fix_rgb_band_attrs.py BRANCH            # dry run: print the changes
    uv run python fix_rgb_band_attrs.py BRANCH --commit   # commit them to BRANCH
"""

import argparse
import os

import arraylake
import zarr
from dotenv import load_dotenv

REPO = "gtc-berlin/stormscope-eu_gtc_berlin_2026"
# Same values as RGB_VALID_RANGE / RGB_BAND_ATTRS in 06_stormscope_eu_pipeline.py.
UPDATES = {
    "true_color": {"valid_min": 0.0, "valid_max": 1.0},
    "airmass": {"valid_min": 0.0, "valid_max": 1.0},
    "rgb": {"long_name": "colour band", "color_interpretation": "red green blue"},
}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("branch")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--commit", action="store_true", help="commit (default: dry run)")
    args = parser.parse_args()

    load_dotenv()
    client = arraylake.Client(token=os.environ.get("ARRAYLAKE_API_TOKEN"))
    repo = client.get_repo(args.repo)
    session = repo.writable_session(args.branch)
    root = zarr.open_group(session.store, mode="r+")

    band_order = root["rgb"][:].tolist()
    if band_order != ["red", "green", "blue"]:
        raise SystemExit(f"rgb coordinate is {band_order}, not red/green/blue; not touching it")

    changed = False
    for name, attrs in UPDATES.items():
        current = dict(root[name].attrs)
        todo = {k: v for k, v in attrs.items() if current.get(k) != v}
        print(f"{name}: {todo or 'up to date'}")
        if todo:
            root[name].attrs.update(todo)
            changed = True

    if not changed:
        print("nothing to commit")
    elif args.commit:
        print("->", session.commit("RGB band metadata: valid_min/max, rgb color_interpretation (#10)"))
    else:
        print("dry run; pass --commit to write")


if __name__ == "__main__":
    main()
