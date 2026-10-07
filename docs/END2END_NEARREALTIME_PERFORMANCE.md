# End-to-end near-real-time performance

How long it takes from the satellite scanning the disk to the forecast playing on the
booth screen, stage by stage. `T` is the nominal FCI slot time, which the pipeline uses as
the forecast init time.
Related: [TILE_SERVER_PERFORMANCE.md](TILE_SERVER_PERFORMANCE.md) (tiles in detail).


## Stages

| # | stage | description | latency | improvement measures |
|---|---|---|---|---|
| 1 | Scan | MTG-I1 FCI scans the full disk once per 10-min repeat cycle. Sensing ends at `T`+9:27. | 9.5 min | - none (instrument) |
| 2 | Listed on Data Store | EUMETSAT processes the FDHSI product (459–1051 MB zip) and lists it in Data Store search. Median after sensing end, so listed at `T`+14.1 min. | 4.7 min | - none (EUMETSAT side) |
| 3 | Download hold-off | Without the NRT licence, a download of a product less than 1 h past its slot time fails with 403 (measured unlock at `T`+60.1–60.4; hourly :00 slots are free at once, but the 6-frame input window always needs five sub-hourly ones). The pipeline only considers slots older than `MIN_LAG` = 62 min, so it waits from `T`+14 to `T`+62. | 48 min | - `MIN_LAG` 80 → 62 min, boundary measured against slot time instead of sensing end ([#30] 🟢)<br>- EUMETSAT NRT licence: `MIN_LAG` → 0, run ~15 min behind real time (not obtained) |
| 4 | Pickup + download + preprocess | From `T`+62 until inference starts: notice the new slot, fetch the six input frames (the newest ~0.9 GB zip is the only new one per cycle). A loop polls every 60 s. Measured with the per-step pipeline, whose `extend schema` commit marked the end of this stage (`T`+81.5–82.1 with the earlier `MIN_LAG` = 80). | 2.1 min | - persistent pipeline that keeps polling [#19] 🟣<br>- model and data source initialised once [#19] 🟣<br>- staying in `us-east-1` next to the bucket (Frankfurt saves only ~5 s of download, [#1]) |
| 5 | Inference + ingest | StormScope-EU produces the analysis frame (lead 0) and rolls forward 12 × 10 min. Each step's output is turned into the `true_color` / `airmass` composites and uploaded to Icechunk on Arraylake in the background while the next step runs; one commit after the last step publishes the whole init ([#32] 🟣). The 4.6 min was measured with a commit per step. | 4.6 min (~23s/step), one commit per init not yet measured | - persistent GPU container [#19] 🟣, [#26] 🟣<br>- spatial chunks + zstd so each write is smaller [#7] 🟣<br>- uint8 composites, 4× smaller before compression ([#27] 🟣)<br>- one commit per init, so uploads overlap inference: with a commit per step, each commit waited for its upload and a run took ~1.5 min longer with `--publish-target arraylake` than with `local` ([#31], [#32] 🟣) |
| 6 | Visible to tiles service | Flux reads Icechunk directly, so a commit is renderable as soon as it lands. With one commit per init, all 13 leads land at once. | 0 s | - none needed |
| 7 | Discovered by client | Booth page: `server.py` polls Arraylake every 5 s, the browser polls `/api/state` every 4 s (worst case shown). Phone viewer (`site/`): reads the init list from `/tree` on page load only. | 9 s | - booth page with live polling [#2] 🟣<br>- phone viewer [#18] 🟣<br>- `extend schema` shown as a complete run until lead 0 lands (issue #13): gone with one commit per init, an init only appears once complete ([#32] 🟣)<br>- phone viewer: fetch new inits every 10 s instead of only on page load ([#28] 🟢) |
| 8 | First frame rendered | The tiles service renders the ~20 z4 tiles of one lead in one browser | 7–9 s | - spatial chunks + zstd [#7] 🟣<br>- `polygons/rgb` style, 2–3× faster per tile [#24] 🟣, [#25] 🟣<br>- 2× overviews: rejected, visibly coarser image ([#23] 🔴) |
| 9 | Full run rendered | All 13 leads loaded in one browser, one lead at a time in playback order. | 92 s | - load leads in playback order [#2] 🟣, [#18] 🟣<br>- snapshot-pinned tile URLs, so reloads come from the browser cache [#2] 🟣<br>- 2× overviews: 39 s, but rejected, visibly coarser image ([#23] 🔴)<br>- uint8 RGB (trial, 74 s, [#22] 🔴; in the pipeline [#27] 🟣)<br>- HTTP/2 on the service (asked, ~2× per frame expected) [arraylake#7938](https://github.com/earth-mover/arraylake/pull/7938) |

PRs: 🟣 merged, 🟢 open, 🔴 closed without merging.

## What's left, by expected impact

1. **NRT licence:** ~48 min. Account question with EUMETSAT, not engineering.
2. **HTTP/2 on the tiles service:** about 2× per frame for one browser (issue #11).
3. **One commit per init ([#32] 🟣):** up to ~1.5 min in stage 5, the gap between
   `--publish-target arraylake` and `local`. To be measured on Brev.
4. **Shorter poll (stage 4):** up to 60 s, at the cost of more Data Store searches.
5. **uint8 RGB in the pipeline ([#27] 🟣):** full run in the browser from ~92 s to ~74 s,
   measured on the trial branch ([#22] 🔴, superseded by #27). Needs Arraylake `main` reset
   before the first uint8 run.

## Sources and reproduce

- Stages 1–2: `uv run python scripts/measure_fci_latency.py history --hours 24`, run
  2026-10-06 (143 slots; listed after slot min / median / p95 = 13.4 / 14.1 / 14.6 min).
- Stage 3: `uv run python scripts/measure_fci_latency.py poll`, a 70-min run on 2026-10-06
  (sub-hourly slots first downloadable 60.1–60.4 min after slot time).
- Stages 4–5: Arraylake commit timestamps on `main` of
  `gtc-berlin/stormscope-eu_gtc_berlin_2026` (`repo.ancestry(branch="main")`): init
  2026-09-30T09:00 (initial), inits 2026-10-05/06 (current). These runs committed per step.
  With one commit per init ([#32] 🟣) the commits no longer split stage 4 from stage 5;
  use the pipeline log timestamps instead.
- Stage 4 download: issue #1.
- Stages 8–9: issue #11 and [TILE_SERVER_PERFORMANCE.md](TILE_SERVER_PERFORMANCE.md).
  Current numbers are Chrome on `site/`, cold service, `cache: "no-store"`.

[#1]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/issues/1
[#2]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/2
[#7]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/7
[#18]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/18
[#19]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/19
[#22]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/22
[#23]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/23
[#24]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/24
[#25]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/25
[#26]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/26
[#27]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/27
[#28]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/issues/28
[#30]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/30
[#31]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/issues/31
[#32]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/32
