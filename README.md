# nvidia-gtc-berlin-2026-stormscope_eu-booth

StormScope-EU nowcasting on MTG-I1 FCI, streamed into Arraylake — the booth demo for
NVIDIA GTC Berlin 2026.

[`demo.ipynb`](demo.ipynb) runs
[`StormScopeMeteosatEU`](https://nvidia.github.io/earth2studio/main/modules/generated/models/px/StormScopeMeteosatEU/)
end to end: fetch six consecutive FCI frames from the EUMETSAT Data Store, roll the model
forward autoregressively in 10-minute steps, and write each step to Icechunk on Arraylake as
it is produced. By default it initialises from the freshest slot the Data Store can serve
(~15–25 min behind real time), so the notebook can be run on a schedule as is.

**Ingest target:**
[gtc-berlin/stormscope_eu_gtc_berlin_2026](https://app.earthmover.io/gtc-berlin/stormscope_eu_gtc_berlin_2026)

## Running it

The model needs a CUDA GPU; the notebook is run on a Coiled GPU VM in the `earthmover-devs`
workspace. The environment definition lives in [`coiled/`](coiled/) and the notebook's *Run
this on Coiled* section has the commands to build it and launch the notebook on a VM.

Credentials come from a local `.env` (see [`.env.example`](.env.example)):

- `EUMETSAT_CONSUMER_KEY` / `EUMETSAT_CONSUMER_SECRET` — model input, from
  <https://api.eumetsat.int/api-key/>
- `ARRAYLAKE_TOKEN` — ingest target, or `al auth login`
