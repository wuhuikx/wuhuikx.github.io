#!/usr/bin/env python3
"""Import B200 vLLM expected benchmark data from an Excel workbook.

The workbook has one sheet per model/config and side-by-side MI355X/B200
metric groups. This script extracts only the B200 VLLM columns and writes
dashboard run JSON files grouped by (model, TP, ISL, OSL).

Usage:
    ./fetch_b200_expected.py
    ./fetch_b200_expected.py --workbook data/vllm-atom/Copy\ of\ vLLM_0.21.0_expected.xlsx
"""

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

from update_data import index_root_for, rebuild_index

RUN_PREFIX = "b200_vllm_aws_"
SOURCE_NAME = "AWS Workbook"
BACKEND_NAME = "vLLM 0.21.0"
CLIENT_BENCH = "vllm bench"
HARDWARE = "b200"
RATIO_RANGE = 1.0
DEFAULT_WORKBOOK = Path(__file__).parent / "data" / "vllm-atom" / "Copy of vLLM_0.21.0_expected.xlsx"
DEFAULT_DATA_DIR = Path(__file__).parent / "data" / "vllm-atom"
METRIC_KEYS = {
    "median_itl_ms": "itl_ms",
    "median_ttft_ms": "ttft_ms",
    "median_tpot_ms": "tpot_ms",
    "median_e2el_ms": "e2el_ms",
    "output_throughput": "output_tput",
    "total_token_throughput": "total_tput",
}
SLUG_RE = re.compile(r"[^a-z0-9]+")
TP_RE = re.compile(r"\bTP(\d+)\b", re.IGNORECASE)


def slugify(s: str) -> str:
    return SLUG_RE.sub("-", s.lower()).strip("-") or "unknown"


def clean_number(v):
    if isinstance(v, (int, float)):
        return round(float(v), 4)
    return None


def precision_from_model(model: str):
    upper = model.upper()
    if "MXFP4" in upper or "FP4" in upper:
        return "fp4"
    if "FP8" in upper:
        return "fp8"
    return None


def parse_sheet_name(title: str):
    m = TP_RE.search(title)
    tp = int(m.group(1)) if m else None
    model = TP_RE.sub("", title).strip(" -")
    model = re.sub(r"\s+", " ", model)
    return model, tp


def b200_metric_columns(ws):
    row1 = [c for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
    row2 = [c for c in next(ws.iter_rows(min_row=2, max_row=2, values_only=True))]
    current_group = None
    cols = {}
    group_label = None
    for idx, header in enumerate(row1):
        if header:
            current_group = str(header)
        if current_group and "B200" in current_group:
            group_label = current_group
            metric = row2[idx]
            if metric in METRIC_KEYS:
                cols[METRIC_KEYS[metric]] = idx
    return group_label, cols


def build_point(row, cols, *, isl, osl, concurrency, tp, precision, image):
    output_tput = clean_number(row[cols["output_tput"]]) if "output_tput" in cols else None
    total_tput = clean_number(row[cols["total_tput"]]) if "total_tput" in cols else None
    if output_tput is None and total_tput is None:
        return None

    total_gpu = tp
    ttft_ms = clean_number(row[cols["ttft_ms"]]) if "ttft_ms" in cols else None
    tpot_ms = clean_number(row[cols["tpot_ms"]]) if "tpot_ms" in cols else None
    itl_ms = clean_number(row[cols["itl_ms"]]) if "itl_ms" in cols else None
    e2el_ms = clean_number(row[cols["e2el_ms"]]) if "e2el_ms" in cols else None

    point = {
        "isl": isl,
        "osl": osl,
        "concurrency": concurrency,
        "ratio": RATIO_RANGE,
        "client_bench": CLIENT_BENCH,
        "ttft_ms": ttft_ms,
        "median_ttft_ms": ttft_ms,
        "ttft_p99": None,
        "tpot_ms": tpot_ms,
        "median_tpot_ms": tpot_ms,
        "tpot_p99": None,
        "itl_ms": itl_ms,
        "median_itl_ms": itl_ms,
        "e2el_ms": e2el_ms,
        "median_e2el_ms": e2el_ms,
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
        "gpu_name": "B200",
        "image": image,
        "rocm": None,
        "run_url": None,
        "model_path": None,
        "point_date": None,
        "precision": precision,
        "hardware": HARDWARE,
    }
    return point


def to_runs(workbook: Path, date: str):
    try:
        import openpyxl
    except ImportError as e:
        raise SystemExit("openpyxl is required to import .xlsx files") from e

    wb = openpyxl.load_workbook(workbook, data_only=True, read_only=True)
    ts = int(time.mktime(time.strptime(date, "%Y-%m-%d")) * 1000)
    grouped = defaultdict(list)
    images = {}

    for ws in wb.worksheets:
        if ws.title.lower() == "summary":
            continue
        model, tp = parse_sheet_name(ws.title)
        if not tp:
            print(f"  WARN: skip {ws.title}: TP not found in sheet name", file=sys.stderr)
            continue
        group_label, cols = b200_metric_columns(ws)
        if not group_label or "output_tput" not in cols:
            print(f"  WARN: skip {ws.title}: B200 output_throughput column not found", file=sys.stderr)
            continue
        image_match = re.search(r"\((?:Other,\s*)?([^)]+)\)", group_label)
        image = image_match.group(1).strip() if image_match else None
        precision = precision_from_model(model)

        for row in ws.iter_rows(min_row=3, values_only=True):
            isl, osl, concurrency = row[0], row[1], row[2]
            if not isinstance(isl, (int, float)) or not isinstance(osl, (int, float)) or not isinstance(concurrency, (int, float)):
                continue
            point = build_point(
                row,
                cols,
                isl=int(isl),
                osl=int(osl),
                concurrency=int(concurrency),
                tp=tp,
                precision=precision,
                image=image,
            )
            if not point:
                continue
            key = (model, tp, int(isl), int(osl))
            grouped[key].append(point)
            images[key] = image

    runs = []
    for (model, tp, isl, osl), points in sorted(grouped.items()):
        points.sort(key=lambda p: p["concurrency"])
        precision = precision_from_model(model)
        model_slug = slugify(model)
        run_id = f"{RUN_PREFIX}{date.replace('-', '')}_{model_slug}_tp{tp}_isl{isl}_osl{osl}"
        config_label = f"b200-vllm-aws_{model_slug}"
        if precision:
            config_label += f"_{precision}"
        config_label += f"_tp{tp}"
        runs.append(
            {
                "run_id": run_id,
                "date": date,
                "timestamp": ts,
                "model": f"{model}-TP{tp}",
                "backend": BACKEND_NAME,
                "config_label": config_label,
                "precision": precision,
                "source": SOURCE_NAME,
                "client_bench": CLIENT_BENCH,
                "hardware": HARDWARE,
                "image": images.get((model, tp, isl, osl)),
                "points": points,
                "gsm8k": None,
            }
        )
    return runs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workbook", default=str(DEFAULT_WORKBOOK))
    ap.add_argument("-d", "--data-dir", default=str(DEFAULT_DATA_DIR))
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    args = ap.parse_args()

    workbook = Path(args.workbook)
    if not workbook.is_file():
        print(f"ERROR: workbook not found: {workbook}", file=sys.stderr)
        sys.exit(1)

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    runs = to_runs(workbook, args.date)
    written = set()
    for run in runs:
        run_file = f"{run['run_id']}.json"
        (data_dir / run_file).write_text(json.dumps(run, indent=2) + "\n")
        written.add(run_file)

    for f in data_dir.glob(f"{RUN_PREFIX}*.json"):
        if f.name not in written:
            print(f"  removing stale {f.name}", file=sys.stderr)
            f.unlink()

    rebuild_index(index_root_for(data_dir))
    n_points = sum(len(r["points"]) for r in runs)
    print(f"Done. {len(runs)} B200 vLLM runs ({n_points} points) written to {data_dir}/", file=sys.stderr)


if __name__ == "__main__":
    main()
