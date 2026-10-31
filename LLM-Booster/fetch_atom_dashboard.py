#!/usr/bin/env python3
"""Import ATOM dashboard vLLM backend data into this dashboard's data/ format.

The ATOM dashboard publishes github-action-benchmark data as a JavaScript
assignment (`window.BENCHMARK_DATA = {...}`) rather than plain JSON.  This
script fetches that file, extracts ATOM-vLLM benchmark entries, converts them
to the per-run schema consumed by index.html, writes data/vllm-atom/, and
refreshes data/index.json.

Usage:
    ./fetch_atom_dashboard.py
    ./fetch_atom_dashboard.py -d data/vllm-atom/
    ./fetch_atom_dashboard.py --source-file /tmp/atom-data.js
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from update_data import index_root_for, rebuild_index

DATA_JS_URL = "https://rocm.github.io/ATOM/benchmark-dashboard/data.js"
RUN_PREFIX = "atom_vllm_"
SOURCE_META_WORKBOOK = "Meta Workbook"
SOURCE_OOB = "OOB"
BACKEND_NAME = "vLLM-ATOM"
DEFAULT_BACKENDS = ("ATOM-vLLM", "vLLM-ATOM", "OOT")

BENCH_RE = re.compile(r"^([^:]+)::(.+)$")
METRIC_RE = re.compile(r"^(.+?)\s+(\d+)\/(\d+)\s+c=(\d+)\s+(.+?)\s*\((.+)\)$")
META_RE = re.compile(r"^(.+?)\s+(\d+)\/(\d+)\s+c=(\d+)\s+(_gpu_count|_tp)$")
SLUG_RE = re.compile(r"[^a-z0-9]+")
SOURCE_MARKER_RE = re.compile(r"\s*\((?P<source>OOB|AWS|AW|META|MET)\)\s*", re.IGNORECASE)
OWNED_RUN_FILE_RE = re.compile(r"^atom_vllm_\d{8}_.+_isl\d+_osl\d+_[0-9a-f]{7}\.json$", re.IGNORECASE)

METRIC_MAP = {
    "throughput": "output_tput",
    "Total Tput": "total_tput",
    "TTFT": "ttft_ms",
    "TPOT": "tpot_ms",
}


def fetch_text(url: str) -> str:
    print(f"GET {url}", file=sys.stderr)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "llm-booster-dashboard-importer/1.0",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            import gzip

            raw = gzip.decompress(raw)
        return raw.decode("utf-8")


def load_benchmark_data(text: str) -> dict:
    marker = "window.BENCHMARK_DATA"
    pos = text.find(marker)
    if pos < 0:
        raise ValueError("missing window.BENCHMARK_DATA assignment")
    start = text.find("{", pos)
    if start < 0:
        raise ValueError("missing JSON object after window.BENCHMARK_DATA")
    data, _ = json.JSONDecoder().raw_decode(text[start:])
    return data


def local_day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")


def slugify(s: str) -> str:
    return SLUG_RE.sub("-", s.lower()).strip("-") or "unknown"


def precision_from_model(model: str):
    upper = model.upper()
    if "MXFP4" in upper or "FP4" in upper:
        return "fp4"
    if "FP8" in upper:
        return "fp8"
    return None


def source_marker_from_model(model: str) -> str | None:
    for match in SOURCE_MARKER_RE.finditer(str(model or "")):
        marker = match.group("source").lower()
        if marker == "oob":
            return SOURCE_OOB
        if marker in {"aws", "aw"}:
            return "AWS Workbook"
        if marker in {"meta", "met"}:
            return SOURCE_META_WORKBOOK
    return None


def strip_source_markers(model: str) -> str:
    return SOURCE_MARKER_RE.sub(" ", str(model or "")).strip()


def display_model_name(model: str):
    # Keep ATOM imports aligned with existing dashboard model filters.
    model = strip_source_markers(model)
    if model == "DeepSeek-R1-0528":
        return "DeepSeek-R1"
    cleaned = re.sub(r"[-\s]TP\d+(?:\+EP\d+)?$", "", str(model or ""), flags=re.IGNORECASE).strip(" -_")
    cleaned = re.sub(r"(^|[-_\s./])aw(?=$|[-_\s./])", r"\1", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(^|[-\s])(mxfp\d+|fp\d+)(?=$|[-\s])", r"\1", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*-\s*", "-", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned).strip(" -_")
    canonical = slugify(cleaned)
    has_mtp = re.search(r"(^|-)mtp($|-)", canonical) is not None

    if canonical.startswith("deepseek-v3-2"):
        cleaned = "DeepSeek V3.2"
    elif canonical.startswith("minimax-m2-5"):
        cleaned = "MiniMax M2.5"
    elif canonical.startswith("qwen3-next") or canonical == "qwen3-n8a" or canonical.startswith("qwen3-n8a-"):
        cleaned = "Qwen3-Next"
    elif canonical.startswith("glm-4-7"):
        cleaned = "GLM 4.7"
    elif canonical.startswith("gpt-oss-120b"):
        cleaned = "gpt-oss-120B"

    if has_mtp and not re.search(r"\bmtp\b", cleaned, flags=re.IGNORECASE):
        cleaned = f"{cleaned} MTP"
    return cleaned or "unknown"


def source_for_model(model: str, config_label: str = "") -> str:
    marker_source = source_marker_from_model(model) or source_marker_from_model(config_label)
    if marker_source:
        return marker_source
    # Source is finalized by explicit archive-rule matching (AWS/Meta/OOB).
    # This importer lacks ratio/client-bench context required by those rules,
    # so imported runs default to OOB and can be reclassified later.
    return SOURCE_OOB


def parse_extra(extra: str) -> dict:
    out = {}
    if not extra:
        return out
    patterns = {
        "run_url": r"Run:\s*(https:\/\/\S+)",
        "image": r"Docker:\s*([^|]+)",
        "gpu_name": r"GPU:\s*([^|]+)",
        "rocm": r"ROCm:\s*([\d.]+)",
        "model_path": r"Model:\s*([^|]+)",
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, extra)
        if m:
            out[key] = m.group(1).strip()
    return out


def parse_bench_name(name: str):
    m = BENCH_RE.match(name)
    if not m:
        return None
    backend, raw = m.group(1), m.group(2)
    if backend == "OOT":
        backend = "ATOM-vLLM"
    output_backend = BACKEND_NAME if backend == "ATOM-vLLM" else backend

    m = METRIC_RE.match(raw)
    if m:
        raw_model = m.group(1)
        metric = m.group(5).strip()
        if metric not in METRIC_MAP:
            return None
        return {
            "backend": output_backend,
            "model": display_model_name(raw_model),
            "source": source_marker_from_model(raw_model),
            "isl": int(m.group(2)),
            "osl": int(m.group(3)),
            "concurrency": int(m.group(4)),
            "metric": METRIC_MAP[metric],
        }

    m = META_RE.match(raw)
    if m:
        raw_model = m.group(1)
        return {
            "backend": output_backend,
            "model": display_model_name(raw_model),
            "source": source_marker_from_model(raw_model),
            "isl": int(m.group(2)),
            "osl": int(m.group(3)),
            "concurrency": int(m.group(4)),
            "metric": "total_gpu" if m.group(5) == "_gpu_count" else "tp",
        }

    return None


def parse_run_configs(run, allowed_backends):
    configs = {}
    for bench in run.get("benches", []):
        parsed = parse_bench_name(bench.get("name", ""))
        if not parsed or parsed["backend"] not in allowed_backends:
            continue

        key = (
            parsed["backend"],
            parsed["model"],
            parsed["isl"],
            parsed["osl"],
            parsed["concurrency"],
        )
        cfg = configs.setdefault(
            key,
            {
                "backend": parsed["backend"],
                "model": parsed["model"],
                "source": parsed.get("source"),
                "isl": parsed["isl"],
                "osl": parsed["osl"],
                "concurrency": parsed["concurrency"],
                "commit": run.get("commit", {}),
                "timestamp": run.get("date"),
                "bench_count": len(run.get("benches", [])),
            },
        )
        cfg[parsed["metric"]] = bench.get("value")
        cfg.update(parse_extra(bench.get("extra", "")))
    return configs


def to_number(v):
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def round_or_none(v, digits=4):
    v = to_number(v)
    return round(v, digits) if v is not None else None


def build_point(cfg):
    output_tput = round_or_none(cfg.get("output_tput"))
    total_tput = round_or_none(cfg.get("total_tput"))
    total_gpu = to_number(cfg.get("total_gpu"))
    if total_gpu is not None and float(total_gpu).is_integer():
        total_gpu = int(total_gpu)
    tp = to_number(cfg.get("tp"))
    if tp is not None and float(tp).is_integer():
        tp = int(tp)

    concurrency = cfg["concurrency"]
    precision = precision_from_model(cfg["model"])
    tpot_ms = round_or_none(cfg.get("tpot_ms"))
    return {
        "isl": cfg["isl"],
        "osl": cfg["osl"],
        "concurrency": concurrency,
        "ratio": None,
        "client_bench": "vllm bench",
        "ttft_ms": round_or_none(cfg.get("ttft_ms")),
        "ttft_p99": None,
        "tpot_ms": tpot_ms,
        "median_tpot_ms": tpot_ms,
        "tpot_p99": None,
        "itl_ms": None,
        "e2el_ms": None,
        "interactivity": round(1000.0 / tpot_ms, 4) if tpot_ms else None,
        "tput_per_gpu": round(total_tput / total_gpu, 4) if total_tput and total_gpu else None,
        "output_tput_per_gpu": round(output_tput / total_gpu, 4) if output_tput and total_gpu else None,
        "input_tput_per_gpu": None,
        "output_tput": output_tput,
        "total_tput": total_tput,
        "req_tput": None,
        "completed": None,
        "duration": None,
        "num_prompts": None,
        "prefill_tp": None,
        "decode_tp": None,
        "tensor_parallel_size": tp,
        "num_prefill_gpu": None,
        "num_decode_gpu": None,
        "total_gpu": total_gpu,
        "gpu_name": cfg.get("gpu_name"),
        "image": cfg.get("image"),
        "rocm": cfg.get("rocm"),
        "run_url": cfg.get("run_url"),
        "model_path": cfg.get("model_path"),
        "point_date": local_day(cfg["timestamp"]) if cfg.get("timestamp") else None,
        "precision": precision,
        "hardware": "mi355x",
    }


def to_runs(raw_data, allowed_backends):
    dedup = {}
    for run in sorted(
        (r for runs in raw_data.get("entries", {}).values() for r in runs),
        key=lambda r: r.get("date") or 0,
        reverse=True,
    ):
        ts = run.get("date")
        if not ts:
            continue
        day = local_day(ts)
        commit = run.get("commit", {})
        cid = (commit.get("id") or "unknown")[:7]
        for cfg in parse_run_configs(run, allowed_backends).values():
            key = (
                cfg["backend"],
                cfg["model"],
                cfg["isl"],
                cfg["osl"],
                cfg["concurrency"],
                day,
                cid,
            )
            prev = dedup.get(key)
            if not prev or cfg["bench_count"] > prev["bench_count"]:
                dedup[key] = cfg

    grouped = defaultdict(list)
    for cfg in dedup.values():
        day = local_day(cfg["timestamp"])
        cid = (cfg.get("commit", {}).get("id") or "unknown")[:7]
        grouped[(day, cid, cfg["backend"], cfg["model"], cfg["isl"], cfg["osl"], cfg.get("source"))].append(cfg)

    runs = []
    for (day, cid, backend, model, isl, osl, source), cfgs in sorted(grouped.items()):
        cfgs.sort(key=lambda c: c["concurrency"])
        points = [build_point(c) for c in cfgs]
        ts = max(c["timestamp"] for c in cfgs if c.get("timestamp"))
        precision = precision_from_model(model)
        tp_values = sorted({p.get("tensor_parallel_size") for p in points if p.get("tensor_parallel_size")})
        tp_label = f"_tp{tp_values[0]}" if len(tp_values) == 1 else ""
        model_slug = slugify(model)
        run_id = f"{RUN_PREFIX}{day.replace('-', '')}_{model_slug}_isl{isl}_osl{osl}_{cid}"
        config_label = f"atom-vllm_{model_slug}"
        if precision:
            config_label += f"_{precision}"
        config_label += tp_label

        runs.append(
            {
                "run_id": run_id,
                "date": day,
                "timestamp": ts,
                "model": model,
                "backend": backend,
                "config_label": config_label,
                "precision": precision,
                "source": source or source_for_model(model, config_label),
                "client_bench": "vllm bench",
                "hardware": "mi355x",
                "commit": cfgs[0].get("commit", {}),
                "points": points,
                "gsm8k": None,
            }
        )
    return runs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-d", "--data-dir", default=str(Path(__file__).parent / "data" / "vllm-atom"))
    ap.add_argument("--source-url", default=DATA_JS_URL)
    ap.add_argument("--source-file", help="Read an already-downloaded data.js file instead of fetching")
    ap.add_argument(
        "--backend",
        action="append",
        dest="backends",
        default=[],
        help="Backend name to import; may be repeated. Defaults to ATOM-vLLM and legacy OOT.",
    )
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    allowed_backends = set(args.backends or DEFAULT_BACKENDS)
    if "OOT" in allowed_backends:
        allowed_backends.add("ATOM-vLLM")
        allowed_backends.add(BACKEND_NAME)

    try:
        text = Path(args.source_file).read_text() if args.source_file else fetch_text(args.source_url)
        raw_data = load_benchmark_data(text)
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as e:
        print(f"ERROR: ATOM dashboard fetch/parse failed: {e}", file=sys.stderr)
        sys.exit(1)

    runs = to_runs(raw_data, allowed_backends)
    written = set()
    for run in runs:
        run_file = f"{run['run_id']}.json"
        (data_dir / run_file).write_text(json.dumps(run, indent=2) + "\n")
        written.add(run_file)

    for f in data_dir.glob(f"{RUN_PREFIX}*.json"):
        if not OWNED_RUN_FILE_RE.match(f.name):
            continue
        if f.name not in written:
            print(f"  removing stale {f.name}", file=sys.stderr)
            f.unlink()

    rebuild_index(index_root_for(data_dir))
    n_points = sum(len(r["points"]) for r in runs)
    print(f"Done. {len(runs)} ATOM-vLLM runs ({n_points} points) written to {data_dir}/", file=sys.stderr)


if __name__ == "__main__":
    main()
