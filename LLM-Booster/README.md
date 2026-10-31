# ATOM Mesh PD Benchmark Dashboard

Static dashboard for SLURM nightly DeepSeek-R1 1P+1D benchmark runs.
Inspired by the ATOM benchmark dashboard, focused on InferenceX-style
latency-throughput Pareto curves.

Live: https://wuhuikx.github.io/LLM-Booster/

## Layout

```
index.html             # single-page dashboard (Chart.js, no build step)
data/
├── index.json         # manifest of runs; file paths are relative to data/
├── mesh/
│   └── <run_id>.json  # SLURM / mesh runs  (written by update_data.py)
├── mesh-workbook/
│   └── infx_*.json    # normalized ATOMesh workbook runs (written by fetch_external.py)
└── vllm-atom/
    └── atom_vllm_*.json # ATOM-vLLM imports (written by fetch_atom_dashboard.py)
update_data.py         # SLURM scan: /it-share/yajizhan/slurm_logs/ → data/<run>.json
fetch_external.py      # ONE-SHOT: normalize mesh workbook runs into data/mesh-workbook/
fetch_atom_dashboard.py # DAILY: pull ATOM dashboard ATOM-vLLM backend data
.nojekyll              # tell GitHub Pages to serve files as-is
```

The data scripts are independent. They each write only their own files; all
rebuild `data/index.json` at the end by scanning the whole `data/` folder, so
no script clobbers another source's results. Re-run `fetch_external.py` only
when you want to refresh the normalized mesh workbook snapshot — it is not a
daily job.
`fetch_atom_dashboard.py` is safe to run daily; it rewrites only
`data/vllm-atom/atom_vllm_*.json`, removes stale ATOM-vLLM imports, and
rebuilds the root `data/index.json`.
Mesh workbook runs live under `data/mesh-workbook/` with normalized
`infx_<hardware>_<backend>_<precision>_isl<ISL>_osl<OSL>_<date>.json` names,
so the dashboard can compare ATOMesh workbook baselines alongside SLURM and
ATOM-vLLM data.

External (InferenceX) lines are drawn **dashed** in the charts to set them
apart from your SLURM runs.

The frontend `fetch()`es `data/index.json` then each per-run JSON in parallel.
This means **the page must be served over http** (`python -m http.server` or
GitHub Pages); opening `index.html` via `file://` will not work because the
browser blocks `fetch` on local files.

## Views

- **Pareto** — TPOT (or TTFT, e2e) vs per-user/total throughput.
  Each line = one (date, ISL, OSL, ratio); each point = one concurrency level.
- **TTFT vs Concurrency** — diagnostic curve, lines per (date, ISL, OSL, ratio).
- **Time Series** — pick a fixed sweep point, plot any metric across dates.
- **Table** — all sweep points, sortable.

Filter bar narrows model / backend / ISL / OSL / ratio / date globally.

## Updating data

```bash
cd /it-share/yajizhan/code/atom-mesh-dashboard
./update_data.py                                # default /it-share/yajizhan/slurm_logs
./update_data.py /other/logs -d data/           # custom path
./fetch_external.py                             # refresh normalized mesh workbook imports
./fetch_atom_dashboard.py                       # refresh ATOM-vLLM imports
./fetch_b200_meta_baseline.py                   # import Meta Workbook B200 vLLM 0.19.0 baseline
git add data/ && git commit -m "data: $(date +%F)" && git push
```

`data/` is **append-only** by default — a SLURM run, once captured, stays in
the dashboard history forever, even if the source directory is later purged
from `slurm_logs/` (SLURM logs rotate; the dashboard archive must not).
Both scripts log "kept (use --prune to delete)" for orphaned files; run with
`--prune` only when you actually want to drop them.

`update_data.py` expects directory layout:

```
<MMDD>_<config>_<jobid>/
  bench/pd-mesh-<ISL>-<OSL>-<CONC>-<ratio>.json    # sglang benchmark_serving output
  gsm8k/<ts>_gsm8k/.../results_*.json              # lm_eval output (optional)
```

Year is read from the JSON `date` field (`YYYYMMDD-HHMMSS`); falls back to current year.

## Daily cron

```

GitHub Pages also runs `dashboard/fetch_atom_dashboard.py` on a daily
scheduled workflow before uploading the static dashboard artifact, so the
published dashboard includes the latest `ATOM-vLLM` data from
`https://rocm.github.io/ATOM/benchmark-dashboard/`.
# crontab -e
30 6 * * *  cd /it-share/yajizhan/code/atom-mesh-dashboard && \
            ./update_data.py && \
            git add data/ && \
            git -c user.name=slurm-bot -c user.email=slurm-bot@local \
                commit -m "data: $(date +%F)" --allow-empty && \
            git push origin main
```

## First-time GitHub Pages setup

1. Repo Settings → Pages → Source: **Deploy from a branch** → Branch: `main` → `/ (root)`.
2. Wait ~1 min, then open the URL.

The page is fully client-side, so any static host works (`python -m http.server` for local preview).

## Local preview

```bash
cd /it-share/yajizhan/code/atom-mesh-dashboard
python3 -m http.server 8765
# open http://<host>:8765/
```

## Local FastAPI validation

Validate dashboard changes with `local_main.py` before submitting. It serves
`index.html`, static assets, and `data/*.json` through the same paths the page
uses, which catches missing or malformed dashboard data before it reaches GitHub
Pages.

```bash
cd /home/xxx/workspace/mesh-all/LLM-Booster/dashboard
python3 local_main.py
# open http://127.0.0.1:8000/
```

`local_main.py` starts a FastAPI app on `127.0.0.1:8000` and provides:

- `/` and `/index.html` for the dashboard page.
- `/data/index.json` for the run manifest.
- `/data/aggregated/index.json` and `/data/aggregated/runs-*.json` for chunked
  dashboard data.
- `/healthz` for a quick server health check.

If dependencies are missing, install them in your Python environment:

```bash
python3 -m pip install fastapi uvicorn
```

If the dashboard runs on a remote development machine, keep the service bound to
`127.0.0.1` and forward it from your personal machine:

```bash
ssh -L 18000:127.0.0.1:8000 xxx@hjbog-srdc-18
# then open http://127.0.0.1:18000/ locally
```

Before committing dashboard data changes, confirm the page loads, filters work,
and the browser console has no JSON loading errors.
