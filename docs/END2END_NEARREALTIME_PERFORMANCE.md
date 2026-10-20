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
| 3 | Download hold-off | Without the NRT licence, a download of a product less than 1 h past its slot time fails with 403 (measured unlock at `T`+60.1–60.4; hourly :00 slots are free at once, but the 6-frame input window always needs five sub-hourly ones). The pipeline only considers slots older than `MIN_LAG` = 62 min, so it waits from `T`+14 to `T`+62. | 48 min | - `MIN_LAG` 80 → 62 min, boundary measured against slot time instead of sensing end ([#30] 🟣)<br>- EUMETSAT NRT licence: `--min-lag 0`, run ~15 min behind real time (not obtained; `STORMSCOPE_REALTIME=1` in `compose.yaml` runs it this way) |
| 4 | Pickup + download + preprocess | From `T`+62 until inference starts: notice the new slot, fetch the six input frames (the newest ~0.9 GB zip is the only new one per cycle). A loop polls every `--wait-seconds` (default 60 s; 15 s with `STORMSCOPE_REALTIME=1` in `compose.yaml`). Measured with the per-step pipeline, whose `extend schema` commit marked the end of this stage (`T`+81.5–82.1 with the earlier `MIN_LAG` = 80). | 2.1 min | - persistent pipeline that keeps polling [#19] 🟣<br>- model and data source initialised once [#19] 🟣<br>- poll interval adjustable with `--wait-seconds`; real-time mode polls every 15 s |
| 5 | Inference + ingest | StormScope-EU produces the analysis frame (lead 0) and rolls forward 12 × 10 min. Each step's output is turned into the `true_color` / `airmass` composites and uploaded to Icechunk on Arraylake in the background while the next step runs; one commit after the last step publishes the whole init ([#32] 🟣). | 3.6 min (~17 s/step) | - persistent GPU container [#19] 🟣, [#26] 🟣<br>- spatial chunks + zstd so each write is smaller [#7] 🟣<br>- uint8 composites, 4× smaller before compression ([#27] 🟣)<br>- one commit per init, so uploads overlap inference: with a commit per step, each commit waited for its upload and a run took ~1.5 min longer with `--publish-target arraylake` than with `local` ([#31] ✅ closed by [#32] 🟣: a run ~1 min shorter)<br>- co-locate storage with inference compute: the bucket moved to eu-west-1 ([#62] 🟣) |
| 6 | Visible to tiles service | Flux reads Icechunk directly, so a commit is renderable as soon as it lands. With one commit per init, all 13 leads land at once. | 0 s | - none needed |
| 7 | Discovered by client | Booth page: `server.py` polls Arraylake every 5 s, the browser polls `/api/state` every 4 s (worst case shown). Phone viewer (`site/`): re-reads the init list from `/tree` every 60 s. | 9 s (phone: 60 s) | - booth page with live polling [#2] 🟣<br>- phone viewer [#18] 🟣<br>- one commit per init, so an init only appears once complete ([#32] 🟣)<br>- phone viewer: check for new inits every 60 s instead of only on page load ([#33] 🟣; [#28] ✅ closed by [#33], which polls every 60 s instead of the 10 s asked for) |
| 8 | First frame rendered | The tiles service renders the ~20 z4 tiles of one lead in one browser. Waking a sleeping service (0 replicas) adds ~23 s: 29 s for the first frame (us-east-1). With 8 pinned, warm replicas the booth page has lead 0 after ~1.5 s. | 1.5 s (us-east-1: 6 s, was 7–9 s) | - spatial chunks + zstd [#7] 🟣<br>- `polygons/rgb` style, 2–3× faster per tile [#24] 🟣, [#25] 🟣; back to `raster/rgb` once the service rendered it as fast [#33] 🟣<br>- 2× overviews: rejected, visibly coarser image ([#23] 🔴)<br>- tiles service and data in eu-west-1, the service next to its S3 bucket and ~70 ms closer to Berlin: ~2× faster per tile ([#62] 🟣) |
| 9 | Full run rendered | All 13 leads loaded in one browser, in playback order with 3 leads in flight, on the tiles service pinned to 8 warm replicas (SKU S). Booth page in full-screen Chrome (1920×907), `airmass`, inits no pod had served: mean 12.3 s (11.0–13.3 s), one lead at a time 17.0 s. Phone viewer (`site/`), same window, switching to an unseen init: 12.0 s (was 16.9 s). A run loaded ~10 s after its commit took 13.8 s (smaller window, `true_color`); on autoscaling 0–8 single tiles stalled up to 17 s. | 12 s (us-east-1: 49 s, was 92 s) | - load leads in playback order [#2] 🟣, [#18] 🟣<br>- snapshot-pinned tile URLs, so reloads come from the browser cache [#2] 🟣<br>- 2× overviews: 39 s, but rejected, visibly coarser image ([#23] 🔴)<br>- uint8 RGB in the pipeline ([#27] 🟣): 92 → 49 s<br>- HTTP/2 on the service, live by 2026-10-15<br>- eu-west-1 tiles service and data ([#62] 🟣): 49 → 24 s<br>- `min = max = 8` replicas during booth hours, so no pod starts cold mid-load: spread of 10–38 s → ~10 s<br>- 3 leads in flight and MapLibre's parallel image requests 16 → 64: 17.0 → 12.3 s (booth), 16.9 → 12.0 s (phone viewer); closes [#11] ⏳ (target: full run in ~10 s) via [#81] 🟢 |

PRs: 🟣 merged, 🟢 open, 🔴 closed without merging. Issues: ✅ closed, ⏳ open.


[#2]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/2
[#7]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/7
[#18]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/18
[#19]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/19
[#23]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/23
[#24]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/24
[#25]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/25
[#26]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/26
[#27]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/27
[#28]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/issues/28
[#33]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/33
[#81]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/81
[#11]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/issues/11
[#62]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/62
[#30]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/30
[#31]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/issues/31
[#32]: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/32
