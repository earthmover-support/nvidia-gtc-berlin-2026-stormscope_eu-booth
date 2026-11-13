# Run the pipeline on NVIDIA Brev

A [Brev launchable](https://docs.nvidia.com/brev/concepts/launchables) starts a GPU VM,
asks the deployer for the EUMETSAT and Arraylake credentials, and runs the pipeline
continuously with the [Docker setup](../README.md#docker). The launchable is created in the
Brev Console; the Brev CLI cannot create launchables.

## How it works

[`scripts/brev_setup.sh`](../scripts/brev_setup.sh) is the launchable's setup script (VM
Mode). Brev passes the setup values to it as environment variables and deletes them after
setup. The script:

- clones this repo to `/opt/stormscope-eu`;
- writes the three credentials to `/opt/stormscope-eu/secrets/` (root, mode 600) and the
  other values to `/opt/stormscope-eu/.env`, which [`compose.yaml`](../compose.yaml) reads;
- runs `docker compose up -d --build`. The first start builds the image from the NGC
  PhysicsNeMo 26.08 container. `restart: unless-stopped` restarts the pipeline after a
  crash, a reboot, or a Brev stop/start.

The model checkpoint and downloaded FCI products stay in the compose volume `cache`, so a
restart does not download them again.

## Create the launchable

In the [Brev Console](https://brev.nvidia.com), open **Launchables → Create Launchable**:

| Section | Setting |
|---|---|
| Software configuration | **VM Mode**, Jupyter off. Setup script: paste the full contents of `scripts/brev_setup.sh`. |
| Source | **I don't have any code files.** The setup script clones the repo itself, to a fixed path. |
| Default hardware configuration | 1x L40S on **Nebius** (`gpu-l40s-d.1gpu-16vcpu-96gb`, driver 580). Any GPU with BF16, 16 GB or more, and NVIDIA driver 580 or newer works; the NGC 26.08 container needs CUDA 13.2. If an L40S breaks (an old driver such as Crusoe's 565, or a missing CDI spec), deploy on an **H200 or B200** instead. Prefer a European location when offered: the EUMETSAT Data Store is in Europe. Disk: 100 GB or more (the image is about 25 GB). |
| Network | Nothing to expose. The pipeline only makes outgoing connections. |
| Launch parameters | The setup values below. |

Setup values:

| Name | Required | Type | Default | Description |
|---|---|---|---|---|
| `EUMETSAT_CONSUMER_KEY` | yes | Text | none | From <https://api.eumetsat.int/api-key/>. |
| `EUMETSAT_CONSUMER_SECRET` | yes | Text | none | From the same page. Pick **Use a secret** when deploying. |
| `ARRAYLAKE_API_TOKEN` | yes | Text | none | Arraylake token with write access to the repo. Pick **Use a secret** when deploying. |
| `ARRAYLAKE_REPO` | yes | Text | none | Arraylake `<org>/<repo>` to publish to. No default, so nobody writes to the booth repo by accident. |
| `STORMSCOPE_PUBLISH_TARGET` | no | Choice: `arraylake`, `local` | `arraylake` | `local` writes to the compose volume `outputs` on the VM instead, for a test. |
| `STORMSCOPE_REALTIME` | no | Choice: `0`, `1` | `0` | `1` if the EUMETSAT account holds the NRT licence: `--min-lag 0 --wait-seconds 15`. With `0`, forecasts start about 62 min behind real time. |
| `STORMSCOPE_GIT_REF` | no | Text | `main` | Branch, tag or commit of this repo to run. |

Never store a credential as a parameter default. Brev keeps launchable defaults in the
launchable itself.

## Deploy and watch

Deploy the launchable and fill in the setup values. If the Console says **"This instance
type is currently out of stock"**, open **Compute → Change compute** and pick another GPU,
e.g. an H200 on Nebius (driver 580, tested). The launchable's Nebius L40S exists only in
eu-north1 and often runs out. `brev launch <launchable-id>` from the CLI reports the same
condition only as `Internal Server Error`, so deploy from the Console when it fails.

The setup log ends with
`[brev-setup] ... started`. Then, from your machine with the
[Brev CLI](https://docs.nvidia.com/brev/latest/cli/getting-started) (`brev login`, then
`brev ls` for the instance name):

```bash
brev exec <instance> 'sudo docker compose --project-directory /opt/stormscope-eu logs --tail 50'
brev shell <instance>   # then: cd /opt/stormscope-eu && sudo docker compose logs -f
```

Measured on 2026-11-13 on a Nebius L40S (`gpu-l40s-d.1gpu-16vcpu-96gb`, driver 580):

| Stage | Time |
|---|---|
| Setup script, mostly the image build (25 GB base image pull) | 14 min |
| Download of the six FCI input frames, ~1 GB each from the EUMETSAT Data Store | 15 min (1.1–3.4 min each) |
| Rollout, 12 steps (first step includes torch.compile) | 12.5 min (~62 s per step) |
| Commit to Arraylake | seconds |
| **Setup start to first published init** | **43 min** |

At ~62 s per 10-minute step, a 12-step rollout takes longer than the 10-minute FCI
cadence, so an L40S skips slots and publishes about every 20 min, each time from the
freshest slot. Later inits download only the newest frame. An H200 or B200 is several times
faster per step and keeps up with every slot.

| Task | Command on the VM, in `/opt/stormscope-eu` |
|---|---|
| Status | `sudo docker compose ps` |
| Stop the pipeline | `sudo docker compose stop` (the VM keeps running and costing) |
| Restart | `sudo docker compose restart` |
| Change a setup value | edit `.env` or a file in `secrets/` with `sudo`, then `sudo docker compose up -d` |
| Run another commit | rerun the setup script with new values, e.g. `sudo EUMETSAT_CONSUMER_KEY=... EUMETSAT_CONSUMER_SECRET=... ARRAYLAKE_API_TOKEN=... ARRAYLAKE_REPO=<org>/<repo> STORMSCOPE_GIT_REF=<ref> bash scripts/brev_setup.sh` |

Stop or delete the instance in the Brev Console (or `brev stop` / `brev delete`) when you
are done; stopping the pipeline alone does not stop billing.

## Notes

- The credentials sit on the VM disk in `/opt/stormscope-eu/secrets/`, so Docker can
  restart the pipeline. Anyone with root on the VM can read them. Delete the instance when
  you are done and revoke the Arraylake token.
- All runs into the same Arraylake repo must use the same `--n-steps` (see
  [Pipeline script](../README.md#pipeline-script)). The container uses the default, 12.
