# Host the booth presentation on Vercel for internal sharing

## TL;DR

The booth presentation (the TV slides with the animated forecast tiles) only ran locally on
the booth machine, and Negin wanted to share it internally. This adds a second Vercel
deployment of `presentation/`, next to the existing mobile `site/` and the local booth,
with the live SQL panel locked to the prebuilt city queries so a public link can't run
arbitrary SQL.

## Changes

- **Shared logic** moves from `server.py` into `presentation/booth.py`. The booth server keeps its
  pollers and behaves as before: demo and live `/api/state` and `/api/config` match a pre-change
  baseline, apart from the new `sql_editable: true`.
- **Vercel functions** `presentation/api/{config,state,sql}.py` answer the endpoints the page
  already calls:
  - `state` reads Arraylake per request (about 1.5 s warm, 8 s cold) and is cached by the CDN
    for 5 s with stale-while-revalidate.
  - `sql` takes only `GET ?place=<Berlin|Paris|London>&init=<recent run on the 10-min grid>`,
    and rejects query text, POSTs and unknown or old inits. Each URL is cached for a day, so
    each city costs at most one SQL run per forecast run.
- **Locked SQL on the hosted page**: when `/api/config` says `sql_editable: false`, the editor is
  read-only, Reset is hidden, and Run sends the GET above. On the booth nothing changes.
- **Windows smaller than 1920 × 1080** (laptops opening the link) get the booth layout zoomed to
  fit and centred. MapLibre drags were checked at 1440 × 900 and 1280 × 720.
- **Build**: `vercel.json` (install, `npm run vendor` copies MapLibre and qrcode into `web/vendor/`,
  output `web`, `maxDuration` 60 s, ignored build step for commits outside `presentation/`) and
  `requirements.txt` for the functions (about 160 MB installed, under the 250 MB limit).
- **Docs**: the top-level README describes the three deployments, `presentation/README.md` has
  the Vercel project settings, env vars and an offline `vercel build` check, and `AGENTS.md`
  is updated.

## For Earthmover (Vercel project setup)

- New Vercel project, root directory `presentation`, framework preset "Other"; the rest
  comes from `vercel.json`.
- Env vars: `ARRAYLAKE_API_TOKEN` (read-only, access to
  `gtc-berlin/stormscope-eu_gtc_berlin_2026`; also used as the SQL password), and optionally
  `ARRAYLAKE_SQL_HOST`.
- The tiles and SQL services need to stay up while the link is shared.

## Testing

Nothing was deployed to Vercel. Tested locally:

- **Booth**: `uv run presentation/server.py --demo` and live, diffed against a baseline taken
  before the refactor. Checked in headless Chrome: editor editable, Reset shown, typing
  accepted, Run and the cached rerun work.
- **Hosted**: `vercel build` run offline in a throwaway copy, then each function served only from
  the files its bundle lists, with a read-only `HOME`, only the token in the env, and a cache
  that honours `s-maxage` and sets `x-vercel-cache`.
  - Validation: rejects a bad place, an init off the grid, too old or in the future, an injection
    attempt, `?query=`, and POST.
  - A bad token returns 503 with `no-store`.
  - In headless Chrome: editor read-only, Reset hidden, a Paris run returns 12 rows and the
    rerun shows "cached".

Open points:

- SQL took 8.4–15.8 s warm, more than the < 5 s hoped for, and a cold SQL service wasn't
  measured. The 60 s function limit covers both.
- The local build used Python 3.14. Vercel picks its own version (the generated
  `requires-python >=3.12`); the code needs ≥ 3.11. Only a real deploy will confirm it.

This file is only there to hand over the PR description; delete it before merging.

#### intent revealed by prompt history

<!-- open-pr:prompts:start -->
It started as "can the TV presentation get its own Vercel page, with live SQL locked to one
query?", grew into keeping all three deployments side by side (local booth, hosted
presentation, mobile site), settled access, SQL scope, backend and service uptime through four
decisions, and ended with local-only testing of both versions and a push without deploying.

<details><summary>8 prompts, 4 decisions</summary>

1. "lets work on: would it be possible to get a second vercel page hosting the presentation https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/tree/main/presentation folder? (currently hosting site for mobile just animated tiles) this is the demo booths presentation slides incl animated tiles targeted for a TV. Negin would like to have it hosted for internal sharing. For the hosted version, I'd disable …"
2. ↳ **Access**: Claude asked "Who should be able to open the hosted presentation?"
   → **Unlisted public URL**
3. ↳ **SQL**: Claude asked "What should the SQL panel allow on the hosted version?"
   → **Run fixed query (Recommended)**
4. ↳ **Backend**: Claude asked "How should the hosted page get its live data (runs + ingest commits)?"
   → **Python functions (Recommended)**
5. ↳ **Offline**: Claude asked "Tiles and SQL services are disabled between events. What should the hosted page do then?"
   → **Keep services on**
6. "I want to have both: a locally running presentation for the gtc berlin and a hosted vercel presentation, while keeping the site vercel deployed"
7. "add this overview also to README in the new branch. earthmover sets up the second vercel project, also arraylake token. dont know whether pro or hobby. query should take <5s."
8. "yes. dont try to push anything on vercel, test locally as if on vercel and as if running locally in the booth"
9. "tell me how to run both booth versions locally in the terminal"
10. "push to branch"
11. "add potential PR body as a file to branch"

</details>
<!-- open-pr:prompts:end -->

🤖 Generated with [Claude Code](https://claude.com/claude-code)
