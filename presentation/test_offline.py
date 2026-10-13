"""Checks for server.py --offline. Run: uv run --with pytest --with python-dotenv pytest presentation"""

import datetime as dt

import server


def test_tiles_in():
    assert list(server.tiles_in([-180, -85, 180, 85], [0])) == [(0, 0, 0)]
    assert len(list(server.tiles_in([-180, -85, 180, 85], [2]))) == 16
    berlin = server.tile_xy(13.4, 52.5, 5)
    assert berlin == (17, 10)  # config.json qr_tile, the tile Berlin sits in
    assert (5, *berlin) in server.tiles_in(server.CONFIG["data_bounds"], [5])


def test_offline_path_matches_fetch_layout():
    run = {"time": "2026-10-09T10:00:00", "ref": "ABC123"}
    # the page asks for the online URL shape with tile_base swapped (offline_config)
    url = server.tile_url(run, "airmass", 30, 5, 17, 10)
    got = server.offline_path(url.replace(server.CONFIG["tile_base"], "/offline/tiles"))
    assert got == (server.OFFLINE / "tiles/ABC123/airmass/30/5/10/17.png").resolve()
    assert server.offline_path("/offline/../config.json").name == "__forbidden__"


def test_offline_state_rotates_saved_runs():
    saved = [{"init": f"2026-10-08T1{i}:00", "ref": f"REF{i}"} for i in range(3)]
    now = dt.datetime(2026, 10, 9, 12, 7, tzinfo=dt.UTC)
    commits, runs = server.offline_state(now, saved)
    refs = [r["ref"] for r in runs]
    assert len(runs) == 3 and len(set(refs)) == 3  # every visible run is a different saved one
    assert [c["id"] for c in commits] == refs
    later = server.offline_state(now + dt.timedelta(minutes=10), saved)[1]
    assert later[0]["ref"] != refs[0] and later[1]["ref"] == refs[0]  # a new run landed on top
