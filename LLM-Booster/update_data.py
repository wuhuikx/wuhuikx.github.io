#!/usr/bin/env python3
"""Scan SLURM benchmark log directories and refresh data/ for the dashboard.

Expected layout under <logs_dir>:
    <MMDD>_<config>_<jobid>/
        bench/pd-mesh-<ISL>-<OSL>-<CONC>-<ratio>.json    (sglang benchmark_serving output)
        gsm8k/<ts>_gsm8k/.../results_*.json              (lm_eval output, optional)

Output (under -d / --data-dir, default data/mesh):
    <run_id>.json           one file per run (points + gsm8k + metadata)
    ../index.json           manifest listing every run under data/

Stale per-run files (runs no longer present in <logs_dir>) are deleted.

Usage:
    ./update_data.py                                     # default /it-share/yajizhan/slurm_logs
    ./update_data.py /path/to/slurm_logs -d data/
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

from atom_sglang_source import normalize_atom_sglang_source

DIR_RE = re.compile(r"^(\d{4})_(.+)$")
FILE_RE = re.compile(r"^pd-mesh-(\d+)-(\d+)-(\d+)-([\d.]+)\.json$")
VLLM_FILE_RE = re.compile(r"^pd-vllm-mesh-(\d+)-(\d+)-(\d+)-([\d.]+)\.json$")
GPU_RE = re.compile(r"(\d+)p_tp(\d+)_(\d+)d_tp(\d+)")
PRECISION_RE = re.compile(r"_(fp\d+)_")

# Prefix used for SLURM run files so this script knows which entries are "ours".
# Keep in sync with parse_run_dir(): the run_id is the directory name itself.
SLURM_PREFIX = ""  # SLURM run_ids look like "0421_..." (digits)
EXTERNAL_PREFIXES = ("infx_", "atom_vllm_", "atom_sglang_")  # per-source prefixes update_data.py must NOT touch
DATA_SUBDIRS = {"mesh", "mesh-workbook", "vllm-atom", "sglang-atom"}
EXCLUDED_ROOT_SUBDIRS = {"kernel-breakdown"}
AGGREGATED_DIR = "aggregated"
AGGREGATED_CHUNK_SIZE = 80

METRIC_KEYS = [
    ("ttft_ms", "mean_ttft_ms"),
    ("ttft_p99", "p99_ttft_ms"),
    ("tpot_ms", "mean_tpot_ms"),
    ("tpot_p99", "p99_tpot_ms"),
    ("median_tpot_ms", "median_tpot_ms"),
    ("itl_ms", "mean_itl_ms"),
    ("e2el_ms", "mean_e2el_ms"),
    ("output_tput", "output_throughput"),
    ("total_tput", "total_token_throughput"),
    ("req_tput", "request_throughput"),
    ("completed", "completed"),
    ("duration", "duration"),
    ("num_prompts", "num_prompts"),
]


def short_model(model_id: str) -> str:
    return model_id.rstrip("/").split("/")[-1] if model_id else "unknown"


def parse_gpu_count(config_label: str):
    """Extract total GPU count from config like '1p_tp4_1d_tp8' → (4, 8, 12)."""
    m = GPU_RE.search(config_label)
    if not m:
        return None, None, None, None, None
    n_prefill = int(m.group(1))
    tp_prefill = int(m.group(2))
    n_decode = int(m.group(3))
    tp_decode = int(m.group(4))
    pgpu = n_prefill * tp_prefill
    dgpu = n_decode * tp_decode
    return pgpu, dgpu, pgpu + dgpu, tp_prefill, tp_decode


def read_gsm8k(gsm8k_dir: Path):
    if not gsm8k_dir.is_dir():
        return None
    for results in gsm8k_dir.rglob("results_*.json"):
        try:
            d = json.loads(results.read_text())
        except Exception:
            continue
        gsm = d.get("results", {}).get("gsm8k", {})
        for key in ("exact_match,strict-match", "exact_match,flexible-extract"):
            if key in gsm:
                try:
                    return float(gsm[key])
                except (TypeError, ValueError):
                    pass
    return None


def parse_run_dir(run_dir: Path):
    m = DIR_RE.match(run_dir.name)
    if not m:
        return None
    mmdd, rest = m.group(1), m.group(2)
    bench_dir = run_dir / "bench"
    if not bench_dir.is_dir():
        return None

    is_vllm = "_vllm_" in rest
    points = []
    year = None
    timestamp = None
    backend = None
    model = None

    glob_pattern = "pd-vllm-mesh-*.json" if is_vllm else "pd-mesh-*.json"
    file_re = VLLM_FILE_RE if is_vllm else FILE_RE
    for f in sorted(bench_dir.glob(glob_pattern)):
        fm = file_re.match(f.name)
        if not fm:
            continue
        isl, osl, conc = int(fm.group(1)), int(fm.group(2)), int(fm.group(3))
        ratio = float(fm.group(4))
        try:
            data = json.loads(f.read_text())
        except Exception as e:
            print(f"  skip {f.name}: {e}", file=sys.stderr)
            continue

        date_str = data.get("date", "")
        if year is None and len(date_str) >= 8 and date_str[:8].isdigit():
            year = date_str[:4]
            try:
                timestamp = int(time.mktime(time.strptime(date_str, "%Y%m%d-%H%M%S")) * 1000)
            except ValueError:
                pass

        backend = backend or data.get("backend")
        model = model or short_model(data.get("model_id", ""))

        point = {"isl": isl, "osl": osl, "concurrency": conc, "ratio": ratio}
        for out_key, in_key in METRIC_KEYS:
            v = data.get(in_key)
            if isinstance(v, float):
                v = round(v, 4)
            point[out_key] = v
        points.append(point)

    if not points:
        return None
    if year is None:
        year = str(time.gmtime().tm_year)

    pgpu, dgpu, total_gpu, tp_prefill, tp_decode = parse_gpu_count(rest)
    pm = PRECISION_RE.search(rest)
    precision = pm.group(1) if pm else "fp8"

    for p in points:
        p["num_prefill_gpu"] = pgpu
        p["num_decode_gpu"] = dgpu
        p["total_gpu"] = total_gpu
        p["prefill_tp"] = tp_prefill
        p["decode_tp"] = tp_decode
        p["precision"] = precision
        tpot = p.get("median_tpot_ms") or p.get("tpot_ms")
        p["interactivity"] = round(1000.0 / tpot, 4) if tpot else None
        total_t = p.get("total_tput")
        p["tput_per_gpu"] = round(total_t / total_gpu, 4) if (total_t and total_gpu) else None
        out_t = p.get("output_tput")
        p["output_tput_per_gpu"] = round(out_t / total_gpu, 4) if (out_t and total_gpu) else None

    if is_vllm:
        backend = "vllm"

    iso_date = f"{year}-{mmdd[:2]}-{mmdd[2:]}"
    return {
        "run_id": run_dir.name,
        "date": iso_date,
        "timestamp": timestamp,
        "model": model or "unknown",
        "backend": backend or "unknown",
        "config_label": rest,
        "precision": precision,
        "source": "SLURM",
        "client_bench": "inferencemax",
        "points": points,
        "gsm8k": read_gsm8k(run_dir / "gsm8k"),
    }


def rebuild_index(data_dir: Path, source_filter: str = ""):
    """Read every per-run *.json under data_dir and rewrite data_dir/index.json."""
    runs = []
    run_payloads = []
    for f in sorted(data_dir.rglob("*.json")):
        if f.name == "index.json":
            continue
        rel = f.relative_to(data_dir)
        if rel.parts and rel.parts[0] in EXCLUDED_ROOT_SUBDIRS:
            continue
        if rel.parts and rel.parts[0] == AGGREGATED_DIR:
            continue
        try:
            r = json.loads(f.read_text())
        except Exception as e:
            print(f"  WARN: skip malformed {f.name}: {e}", file=sys.stderr)
            continue
        if f.parent.name == "sglang-atom" or "sglang-atom" in f.as_posix():
            r = dict(r)
            r["source"] = normalize_atom_sglang_source(r, f.name)
        runs.append({
            "run_id": r.get("run_id", f.stem),
            "date": r.get("date"),
            "timestamp": r.get("timestamp"),
            "model": r.get("model"),
            "backend": r.get("backend"),
            "config_label": r.get("config_label"),
            "source": r.get("source", "SLURM"),
            "client_bench": client_bench_for(r),
            "precision": r.get("precision"),
            "hardware": r.get("hardware"),
            "n_points": len(r.get("points", [])),
            "gsm8k": r.get("gsm8k"),
            "file": f.relative_to(data_dir).as_posix(),
        })
        run_payloads.append(r)
    runs.sort(key=lambda x: (x.get("date") or "", x.get("run_id") or ""))
    run_payloads.sort(key=lambda x: (x.get("date") or "", x.get("run_id") or ""))
    last_update = int(time.time() * 1000)
    index = {
        "lastUpdate": last_update,
        "source": source_filter,
        "runs": runs,
    }
    (data_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    write_aggregated_runs(data_dir, run_payloads, source_filter, last_update)
    return runs


def write_aggregated_runs(data_dir: Path, runs: list, source_filter: str, last_update: int):
    """Write chunked aggregated run files to reduce browser request fan-out."""
    agg_dir = data_dir / AGGREGATED_DIR
    agg_dir.mkdir(parents=True, exist_ok=True)
    chunks = []
    expected_files = {"index.json"}
    total_points = 0
    for i in range(0, len(runs), AGGREGATED_CHUNK_SIZE):
        chunk_runs = runs[i:i + AGGREGATED_CHUNK_SIZE]
        chunk_n = i // AGGREGATED_CHUNK_SIZE + 1
        chunk_file = f"runs-{chunk_n:04d}.json"
        expected_files.add(chunk_file)
        n_points = sum(len(r.get("points", [])) for r in chunk_runs)
        total_points += n_points
        payload = {
            "lastUpdate": last_update,
            "n_runs": len(chunk_runs),
            "n_points": n_points,
            "runs": chunk_runs,
        }
        (agg_dir / chunk_file).write_text(json.dumps(payload, indent=2) + "\n")
        chunks.append({
            "file": f"{AGGREGATED_DIR}/{chunk_file}",
            "n_runs": len(chunk_runs),
            "n_points": n_points,
        })

    agg_index = {
        "lastUpdate": last_update,
        "source": source_filter,
        "format": "runs-v1-chunked",
        "chunkSize": AGGREGATED_CHUNK_SIZE,
        "totalRuns": len(runs),
        "totalPoints": total_points,
        "chunks": chunks,
    }
    (agg_dir / "index.json").write_text(json.dumps(agg_index, indent=2) + "\n")

    for f in agg_dir.glob("*.json"):
        if f.name not in expected_files:
            f.unlink()


def index_root_for(data_dir: Path):
    """Return the dashboard data root when writing into a known data subdir."""
    return data_dir.parent if data_dir.name in DATA_SUBDIRS else data_dir


def client_bench_for(run: dict):
    if run.get("client_bench"):
        return run.get("client_bench")
    return "vllm bench" if run.get("source") in ("vLLM-ATOM", "ATOM Dashboard", "AWS Workbook", "Meta Workbook", "OOB") else "inferencemax"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs_dir", nargs="?", default="/it-share/yajizhan/slurm_logs")
    ap.add_argument("-d", "--data-dir", default=str(Path(__file__).parent / "data" / "mesh"))
    ap.add_argument("--prune", action="store_true",
                    help="Also delete data/<run>.json files whose source SLURM directory "
                         "no longer exists. Default: keep them as a permanent archive.")
    args = ap.parse_args()

    logs_dir = Path(args.logs_dir)
    if not logs_dir.is_dir():
        print(f"error: {logs_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    n_pts = 0
    written_files = set()
    for run_dir in sorted(logs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        run = parse_run_dir(run_dir)
        if not run:
            continue
        run_file = f"{run['run_id']}.json"
        (data_dir / run_file).write_text(json.dumps(run, indent=2) + "\n")
        written_files.add(run_file)
        n_pts += len(run["points"])
        print(f"  {run['run_id']}: {len(run['points'])} points, gsm8k={run['gsm8k']}",
              file=sys.stderr)

    # Default behaviour is APPEND-ONLY: data/ is a permanent archive — once a
    # SLURM run is committed it stays even if the source dir is purged from
    # logs_dir (SLURM logs rotate; the dashboard history must not).
    # Use --prune to delete data files whose source dir is gone.
    candidates = []
    for f in data_dir.glob("*.json"):
        if f.name == "index.json" or f.name in written_files:
            continue
        if any(f.name.startswith(p) for p in EXTERNAL_PREFIXES):
            continue
        if not DIR_RE.match(f.stem):
            continue
        candidates.append(f)
    if candidates:
        action = "removing" if args.prune else "kept (use --prune to delete)"
        for f in candidates:
            print(f"  {action}: {f.name}", file=sys.stderr)
            if args.prune:
                f.unlink()

    runs = rebuild_index(index_root_for(data_dir), source_filter=str(logs_dir))
    print(f"Wrote {len(written_files)} SLURM runs ({n_pts} points). "
          f"index.json now lists {len(runs)} runs in total.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
