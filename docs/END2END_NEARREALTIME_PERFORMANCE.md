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
| 3 | Download hold-off | Without the NRT licence, a download of a product less than 1 h past its slot time fails with 403 (measured unlock at `T`+60.1–60.4; hourly :00 slots are free at once, but the 6-frame input window always needs five sub-hourly ones). The pipeline only considers slots older than `MIN_LAG` = 62 min, so it waits from `T`+14 to `T`+62. | 48 min | - `MIN_LAG` 80 → 62 min, boundary measured against slot time instead of sensing end ([#30] 🟣)<br>- EUMETSAT NRT licence: `MIN_LAG` → 0, run ~15 min behind real time (not obtained) |
| 4 | Pickup + download + preprocess | From `T`+62 until inference starts: notice the new slot, fetch the six input frames (the newest ~0.9 GB zip is the only new one per cycle). A loop polls every 60 s. Measured with the per-step pipeline, whose `extend schema` commit marked the end of this stage (`T`+81.5–82.1 with the earlier `MIN_LAG` = 80). | 2.1 min | - persistent pipeline that keeps polling [#19] 🟣<br>- model and data source initialised once [#19] 🟣<br>- staying in `us-east-1` next to the bucket (Frankfurt saves only ~5 s of download, [#1]); the bucket has since moved to eu-west-1 ([#62] 🟢) |
| 5 | Inference + ingest | StormScope-EU produces the analysis frame (lead 0) and rolls forward 12 × 10 min. Each step's output is turned into the `true_color` / `airmass` composites and uploaded to Icechunk on Arraylake in the background while the next step runs; one commit after the last step publishes the whole init ([#32] 🟣). Eight steady-state runs on 2026-10-07 committed a median `T`+67.7 min, so stages 4 + 5 take ~5.7 min from `T`+62; minus stage 4 that leaves ~3.6 min (was 4.6 min, ~23 s/step, with a commit per step). Measured writing to the us-east-1 repo; not yet re-measured since the pipeline writes to the eu-west-1 bucket ([#62] 🟢). | 3.6 min (~17 s/step) | - persistent GPU container [#19] 🟣, [#26] 🟣<br>- spatial chunks + zstd so each write is smaller [#7] 🟣<br>- uint8 composites, 4× smaller before compression ([#27] 🟣)<br>- one commit per init, so uploads overlap inference: with a commit per step, each commit waited for its upload and a run took ~1.5 min longer with `--publish-target arraylake` than with `local` ([#31], [#32] 🟣) |
| 6 | Visible to tiles service | Flux reads Icechunk directly, so a commit is renderable as soon as it lands. With one commit per init, all 13 leads land at once. | 0 s | - none needed |
| 7 | Discovered by client | Booth page: `server.py` polls Arraylake every 5 s, the browser polls `/api/state` every 4 s (worst case shown). Phone viewer (`site/`): re-reads the init list from `/tree` every 60 s. | 9 s (phone: 60 s) | - booth page with live polling [#2] 🟣<br>- phone viewer [#18] 🟣<br>- `extend schema` shown as a complete run until lead 0 lands (issue #13): gone with one commit per init, an init only appears once complete ([#32] 🟣)<br>- phone viewer: check for new inits every 60 s instead of only on page load ([#33] 🟣; [#28] asked for 10 s) |
| 8 | First frame rendered | The tiles service renders the ~20 z4 tiles of one lead in one browser. Waking a sleeping service (0 replicas) adds ~23 s: 29 s for the first frame (us-east-1). With 8 pinned, warm replicas the booth page has lead 0 after ~1.5 s. | 1.5 s (us-east-1: 6 s, was 7–9 s) | - spatial chunks + zstd [#7] 🟣<br>- `polygons/rgb` style, 2–3× faster per tile [#24] 🟣, [#25] 🟣; back to `raster/rgb` once the service rendered it as fast [#33] 🟣<br>- 2× overviews: rejected, visibly coarser image ([#23] 🔴)<br>- tiles service and data in eu-west-1, the service next to its S3 bucket and ~70 ms closer to Berlin: ~2× faster per tile ([#61] 🟢, [#62] 🟢) |
| 9 | Full run rendered | All 13 leads loaded in one browser, in playback order with 3 leads in flight, on the tiles service pinned to 8 warm replicas (SKU S). Booth page in Chrome, mean 9.7 s (7.5–11.3 s) on inits no pod had served; 13.8 s for a run a minute after its commit. One lead at a time took 14.8 s on the same setup; on autoscaling 0–8 single tiles stalled up to 17 s. | 10 s (us-east-1: 49 s, was 92 s) | - load leads in playback order [#2] 🟣, [#18] 🟣<br>- snapshot-pinned tile URLs, so reloads come from the browser cache [#2] 🟣<br>- 2× overviews: 39 s, but rejected, visibly coarser image ([#23] 🔴)<br>- uint8 RGB (trial, 74 s, [#22] 🔴; in the pipeline [#27] 🟣: 92 → 49 s)<br>- HTTP/2 on the service, live by 2026-10-15 [arraylake#7938](https://github.com/earth-mover/arraylake/pull/7938)<br>- eu-west-1 tiles service and data ([#61] 🟢, [#62] 🟢): 49 → 24 s<br>- `min = max = 8` replicas during booth hours, so no pod starts cold mid-load: spread of 10–38 s → ~10 s<br>- 3 leads in flight and MapLibre's parallel image requests 16 → 64: 14.8 → 9.7 s |

PRs: 🟣 merged, 🟢 open, 🔴 closed without merging.

## What's left, by expected impact

1. **NRT licence:** ~48 min. Account question with EUMETSAT, not engineering.
2. **Shorter poll (stage 4):** up to 60 s, at the cost of more Data Store searches.
3. **504s right after a commit:** a booth page that loaded a run within seconds of its
   commit got 34 `504`s on its first 3 leads and needed 25.9 s. To raise with Earthmover.
4. **Several viewers:** rendered tiles aren't shared, so each phone re-renders the run.
   8 replicas render ~26 tiles/s, about one full run per 10 s. Untested with more viewers.

## Sources and reproduce

- Stages 1–2: `uv run python scripts/measure_fci_latency.py history --hours 24`, run
  2026-10-06 (143 slots; listed after slot min / median / p95 = 13.4 / 14.1 / 14.6 min).
- Stage 3: `uv run python scripts/measure_fci_latency.py poll`, a 70-min run on 2026-10-06
  (sub-hourly slots first downloadable 60.1–60.4 min after slot time).
- Stages 4–5: Arraylake commit timestamps on `main` of
  `gtc-berlin/stormscope-eu_gtc_berlin_2026` (`repo.ancestry(branch="main")`): init
  2026-09-30T09:00 (initial), inits 2026-10-05/06 (per-step commits). With one commit per
  init ([#32] 🟣) the commits no longer split stage 4 from stage 5: eight steady-state Brev
  runs on 2026-10-07 committed a median 67.7 min after init (e96cac0, also the source of
  `run_phases_s` in `presentation/config.json`); stage 5 is that total minus stage 4.
- Stage 4 download: issue #1.
- Stages 8–9: issue #11 and [TILE_SERVER_PERFORMANCE.md](TILE_SERVER_PERFORMANCE.md).
  Current numbers: 2026-10-07, headless Chrome (1512×725) on `site/` with `cache: "no-store"`
  on tile requests, SKU S with 1 replica, init 2026-10-07T13:10 (uint8), 221 tiles of 512 px.
  First run woke the sleeping service (frame 0 at 29.0 s, all 13 at 74.2 s); second run on
  the awake pod (frame 0 at 6.2 s, all 13 at 48.9 s). The earlier 92 s / 7–9 s were float RGB
  on a freshly restarted pod (2026-10-05).
  eu-west-1 numbers: 2026-10-09, [#61] 🟢, Chrome (1512×783) on `site/` from Berlin, SKU S,
  six runs over inits 08:30/08:40 with `no-store` and a warm-up; medians of first frame
  (2.0 s, range 1.3–6.4 s) and ready (24 s, range 19–41 s). Details in
  [TILE_SERVER_PERFORMANCE.md](TILE_SERVER_PERFORMANCE.md#region-us-east-1-vs-eu-west-1-2026-10-09).
  Current numbers: 2026-10-15, Chrome (1512×749) on `presentation/` served by local
  `server.py`, `tiles-eu` SKU S pinned to 8 warm replicas, four inits no pod had served per
  strategy, times from page load until the loop starts. Details in
  [TILE_SERVER_PERFORMANCE.md](TILE_SERVER_PERFORMANCE.md#booth-page-loading-strategy) and
  [Server-side caches and HTTP/2](TILE_SERVER_PERFORMANCE.md#server-side-caches-and-http2-2026-10-15).

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
[#33]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/33
[#61]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/61
[#62]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/62
[#30]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/30
[#31]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/issues/31
[#32]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/32
