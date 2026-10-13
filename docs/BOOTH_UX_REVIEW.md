# Booth UX, readability, and narrative review

Reviewed the seven-panel PR #76 implementation against the shared design brief and
rendered booth layouts. The main issues were legibility, diagram semantics, time
references, and the visitor's control over the rotating story.

| Finding | Improvement |
| --- | --- |
| Dense introduction competes with its diagram during a 25 s rotation | Shorter scientific copy; 18 px body text and clear message-first headlines |
| AI appeared before its observation source | Order: definition → observations → generative prediction → orchestration → publication → delivery → exploration |
| Model feedback was a text annotation | Directed SVG shows six observations, next-frame inference, the updated context dropping −50 and adding +10, and a feedback edge |
| Cycle was a collection of cards without connecting edges | Clockwise, numbered ingest → initialize → infer → output cycle; newer observations trigger repetition |
| GPU/upload lanes did not establish causal ordering or publication barrier | Each upload starts after its prediction and overlaps the following GPU step; all writes finish before atomic publication |
| Delivery branches and architecture hid the scientific stages | Explicit one-to-many delivery graph; observe → predict → publish → serve → explore architecture |
| Disconnected polling still showed animated phase estimates | Suppress phase activity and countdowns while disconnected; retain the last publication |
| Forecast valid time was repeated in a pill that looked like a current clock | Header explicitly says “Now”; forecast valid time remains on the map |
| General “present to +6 h” and demo “init to +2 h” looked like the same time origin | Separate reference labels, plus observation age and horizon-from-init disclosure |
| Pausing depended on discovering Space in a small shortcut list | Visible Pause/Resume and directly selectable story chapters |
| Inactive panels remained keyboard focusable | Inactive articles are inert and marked aria-hidden |
| Manual navigation and SQL interaction could disagree with the rotation timer | One resettable timer; 60 s manual reading hold and 120 s SQL interaction hold, reflected by the progress marker |
| Raw commit messages hid the forecast history | Actual initialization UTC, snapshot ID, and published age shown in aligned columns; unmatched messages labeled Other commit |
| Two equal QR destinations lacked visitor priority | Larger viewer QR, direct viewer link, and smaller developer tile QR |

## Validation

Headless Chromium checks cover all seven panels at 1920×1080, 2560×1440, and the
scaled 1440×900 layout. Content is measured against the navigation footer, and
screenshots are visually inspected. Functional checks cover composite switching,
UTC labels, disconnected data, strip collapse, direct chapters, inactive-panel
focus exclusion, pause/resume, manual reading holds, and SQL interaction timing.

The rehearsal server supplies simulated commits and SQL. External basemap and
forecast services are replaced with deterministic browser fixtures, including a
valid synthetic PNG tile. This checks frontend behavior and layout; it does not
validate live service availability, forecast skill, pipeline runtime, or measured
upload/inference durations. The scientific diagrams remain explicitly schematic.
Physical exhibition-distance readability and QR scanning on the final booth screen
still require a venue/device check.


## Approved copy update

The user's edited seven-panel copy now supplies the headings, paragraphs, and captions.
Typos in the introduction were corrected and its unfinished initial-condition sentence
was completed as “A forecast starts from an initial condition derived from recent observations.”
The introductory horizon comparison was replaced by the requested observe → predict →
publish → explore workflow. The persistent UTC metadata retains the separate time references.

The panel column is widened to 680 px; body text stays at 18 px and numbered titles stay
at 24 px bold. Diagrams stay capped at 544 px. Repeated widget descriptions are removed,
and the publication/delivery diagrams are compacted without reducing their label font sizes.
All four publication-history rows remain visible. The initialization selector scrolls when
needed so the narrower globe's playback controls remain visible with a full run history.
All seven panels and the playback row fit the three previously tested viewport sizes.
