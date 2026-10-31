#!/usr/bin/env python3
"""Import ATOM SGLang-Mesh bench artifacts into LLM-Booster as MoRI SGLang series.

Reads per-case bench_serving JSON (from GitHub Actions artifacts or a local dir),
groups by (mesh config, ISL, OSL), and writes dashboard run files under
data/sglang-atom/ with:

  source: SGLang-ATOM Mesh
  backend: mori-sglang
  chart_label: MI355X (MoRI SGLang)

Usage:
  ./import_sglang_mesh_mori.py --run-id 26525397753
  ./import_sglang_mesh_mori.py --bench-dir /tmp/sglang-artifacts
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from urllib.request import urlopen

from update_data import index_root_for, rebuild_index

MORI_CHART_LABEL = "MI355X (MoRI SGLang)"
MORI_CHART_STYLE = {
    "color": "#ff4f73",
    "borderColor": "#ff4f73",
    "backgroundColor": "#ff4f73",
    "borderDash": [],
    "pointStyle": "circle",
}
SOURCE = "SGLang-ATOM Mesh"
BACKEND = "mori-sglang"
RUN_URL_TEMPLATE = "https://github.com/ROCm/ATOM/actions/runs/{run_id}"

ARTIFACT_NAME_RE = re.compile(
    r"^sglang-benchmark-(deepseek-r1-fp4-mesh-(?:tp4|tp8)-\d+-\d+-\d+-\d+)$"
)
BENCH_FILE_RE = re.compile(
    r"^deepseek-r1-fp4-mesh-(tp4|tp8)-(\d+)-(\d+)-(\d+)-\d+\.json$"
)
CSV_RUN_ID_SUFFIX = "manual_csv"
XLSX_RUN_ID_SUFFIX = "manual_xlsx"
CSV_MODEL = "DeepSeek-V2"
XLSX_MODEL = "DeepSeek-R1-0528-MXFP4"
CSV_DATE = "2026-05-28"


def round4(v):
    if v is None:
        return None
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def config_gpu_count(config: str) -> int | None:
    """Infer total GPUs from compact config labels like tp8 or dpa4_ep4."""
    m = re.search(r"tp(\d+)", config or "", re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"dpa(\d+)", config or "", re.I)
    if m:
        return int(m.group(1))
    return None


def mori_chart_label_for_config(config: str) -> str:
    config = config or ""
    m = re.search(r"dpa(\d+)[-_]?ep(\d+)", config, re.I)
    if m:
        return f"MI355X (MoRI SGLang DPA{m.group(1)} EP{m.group(2)})"
    m = re.search(r"tp(\d+)", config, re.I)
    if m:
        return f"MI355X (MoRI SGLang TP{m.group(1)})"
    return MORI_CHART_LABEL


def csv_to_point(row: dict, *, point_date: str, model_name: str = CSV_MODEL) -> dict:
    config = row["config"]
    chart_label = mori_chart_label_for_config(config)
    total_gpu = config_gpu_count(config)
    total_tput = round4(row.get("total_token_throughput"))
    output_tput = round4(row.get("output_throughput"))
    req_tput = round4(row.get("request_throughput"))
    mean_tpot = round4(row.get("mean_tpot_ms"))
    median_tpot = round4(row.get("median_tpot_ms"))
    interactivity = None
    if median_tpot and median_tpot > 0:
        interactivity = round4(1000.0 / median_tpot)
    elif mean_tpot and mean_tpot > 0:
        interactivity = round4(1000.0 / mean_tpot)
    else:
        ttft = round4(row.get("median_ttft_ms")) or round4(row.get("mean_ttft_ms"))
        if ttft and ttft > 0:
            interactivity = round4(1000.0 / ttft)

    return {
        "isl": int(row["isl"]),
        "osl": int(row["osl"]),
        "concurrency": int(row["concurrency"]),
        "ratio": 1.0,
        "client_bench": "sglang bench_serving",
        "ttft_ms": round4(row.get("mean_ttft_ms")),
        "ttft_p99": round4(row.get("p99_ttft_ms")),
        "median_ttft_ms": round4(row.get("median_ttft_ms")),
        "tpot_ms": mean_tpot,
        "median_tpot_ms": median_tpot,
        "tpot_p99": round4(row.get("p99_tpot_ms")),
        "itl_ms": None,
        "e2el_ms": round4(row.get("mean_e2el_ms")),
        "median_e2el_ms": round4(row.get("median_e2el_ms")),
        "e2el_p99": round4(row.get("p99_e2el_ms")),
        "interactivity": interactivity,
        "tput_per_gpu": round4(total_tput / total_gpu) if total_tput and total_gpu else None,
        "output_tput_per_gpu": round4(output_tput / total_gpu) if output_tput and total_gpu else None,
        "input_tput_per_gpu": None,
        "output_tput": output_tput,
        "total_tput": total_tput,
        "req_tput": req_tput,
        "completed": int(float(row["completed"])) if row.get("completed") else None,
        "duration": round4(row.get("benchmark_duration_s")),
        "num_prompts": int(float(row["num_prompts"])) if row.get("num_prompts") else None,
        "prefill_tp": None,
        "decode_tp": None,
        "tensor_parallel_size": total_gpu if config.lower().startswith("tp") else None,
        "data_parallel_size": total_gpu if config.lower().startswith("dpa") else None,
        "expert_parallel_size": int(re.search(r"ep(\d+)", config, re.I).group(1))
        if re.search(r"ep(\d+)", config, re.I)
        else None,
        "enable_dp_attention": config.lower().startswith("dpa"),
        "num_prefill_gpu": None,
        "num_decode_gpu": None,
        "total_gpu": total_gpu,
        "gpu_name": "AMD Instinct MI355X",
        "image": None,
        "rocm": None,
        "run_url": "",
        "model_path": model_name,
        "point_date": point_date,
        "precision": "fp4",
        "hardware": "mi355x",
        "chart_group": "inferencex-four-curve",
        "chart_label": chart_label,
        "chart_style": dict(MORI_CHART_STYLE),
        "source_file": row.get("file"),
    }


def bench_to_point(data: dict, *, run_url: str, point_date: str) -> dict:
    isl = int(data.get("random_input_len") or 0)
    osl = int(data.get("random_output_len") or 0)
    conc = int(data.get("max_concurrency") or 0)
    total_tput = round4(data.get("total_token_throughput"))
    output_tput = round4(data.get("output_throughput"))
    req_tput = round4(data.get("request_throughput"))
    tp = data.get("tensor_parallel_size")
    if tp is not None:
        tp = int(tp)
    total_gpu = tp
    chart_label = mori_chart_label_for_config(data.get("benchmark_model_name") or "")
    tpot = round4(data.get("mean_tpot_ms"))
    interactivity = None
    if tpot and tpot > 0:
        interactivity = round4(1000.0 / tpot)
    elif data.get("mean_ttft_ms") and osl <= 1:
        interactivity = round4(1.0 / (float(data["mean_ttft_ms"]) / 1000.0))

    return {
        "isl": isl,
        "osl": osl,
        "concurrency": conc,
        "ratio": 1.0,
        "client_bench": "sglang bench_serving",
        "ttft_ms": round4(data.get("mean_ttft_ms")),
        "ttft_p99": round4(data.get("p99_ttft_ms")),
        "tpot_ms": tpot,
        "tpot_p99": round4(data.get("p99_tpot_ms")),
        "itl_ms": round4(data.get("mean_itl_ms")),
        "e2el_ms": round4(data.get("mean_e2el_ms")),
        "interactivity": interactivity,
        "tput_per_gpu": round4(total_tput / total_gpu) if total_tput and total_gpu else None,
        "output_tput_per_gpu": round4(output_tput / total_gpu) if output_tput and total_gpu else None,
        "input_tput_per_gpu": None,
        "output_tput": output_tput,
        "total_tput": total_tput,
        "req_tput": req_tput,
        "completed": data.get("completed"),
        "duration": round4(data.get("duration")),
        "num_prompts": data.get("num_prompts"),
        "prefill_tp": None,
        "decode_tp": None,
        "tensor_parallel_size": tp,
        "data_parallel_size": data.get("data_parallel_size"),
        "expert_parallel_size": data.get("expert_parallel_size"),
        "enable_dp_attention": data.get("enable_dp_attention"),
        "num_prefill_gpu": None,
        "num_decode_gpu": None,
        "total_gpu": total_gpu,
        "gpu_name": data.get("gpu_name"),
        "image": data.get("sglang_image_tag"),
        "rocm": data.get("rocm_version"),
        "run_url": run_url,
        "model_path": data.get("model_id"),
        "point_date": point_date,
        "precision": "fp4",
        "hardware": "mi355x",
        "chart_group": "inferencex-four-curve",
        "chart_label": chart_label,
        "chart_style": dict(MORI_CHART_STYLE),
    }


def parse_bench_path(path: Path) -> tuple[str, int, int] | None:
    m = BENCH_FILE_RE.match(path.name)
    if not m:
        return None
    tp_label, isl, osl, _conc = m.groups()
    return tp_label, int(isl), int(osl)


def group_key_from_data(data: dict) -> tuple[str, int, int]:
    name = data.get("benchmark_model_name") or ""
    tp = "tp8" if "tp8" in name else "tp4"
    isl = int(data.get("random_input_len") or 0)
    osl = int(data.get("random_output_len") or 0)
    return tp, isl, osl


def build_run(
    *,
    tp_label: str,
    isl: int,
    osl: int,
    points: list[dict],
    sample: dict,
    github_run_id: str,
    commit_sha: str,
) -> dict:
    points = sorted(points, key=lambda p: p["concurrency"])
    date_raw = str(sample.get("date") or "")
    if len(date_raw) >= 8 and date_raw[:8].isdigit():
        iso_date = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
        try:
            timestamp = int(
                time.mktime(time.strptime(date_raw[:15], "%Y%m%d-%H%M%S")) * 1000
            )
        except ValueError:
            timestamp = int(time.time() * 1000)
    else:
        iso_date = "2026-05-27"
        timestamp = int(time.time() * 1000)

    model = sample.get("benchmark_model_name") or f"DeepSeek-R1-0528-MXFP4-mesh-{tp_label}"
    sha_short = (commit_sha or "unknown")[:7]
    run_id = (
        f"atom_sglang_mori-sglang_{iso_date.replace('-', '')}_"
        f"deepseek-r1-0528-mxfp4-mesh-{tp_label}_isl{isl}_osl{osl}_{sha_short}_{github_run_id}"
    )
    ep = int(sample.get("expert_parallel_size") or 1)
    config_label = (
        f"atom-sglang_mori-sglang_deepseek-r1-0528-mxfp4-mesh-{tp_label}_fp4_{tp_label}_ep{ep}"
    )
    chart_label = mori_chart_label_for_config(tp_label)
    run_url = RUN_URL_TEMPLATE.format(run_id=github_run_id)
    commit = {
        "id": commit_sha or "",
        "message": f"ATOM SGLang benchmark run {github_run_id} (MoRI SGLang import)",
        "timestamp": sample.get("atom_source_sha") and None,
        "url": f"https://github.com/ROCm/ATOM/commit/{commit_sha}" if commit_sha else "",
        "author": {},
        "committer": {},
    }
    if commit_sha:
        commit["timestamp"] = (
            f"{iso_date}T00:00:00+00:00"  # placeholder; dashboard uses run timestamp
        )

    return {
        "run_id": run_id,
        "date": iso_date,
        "timestamp": timestamp,
        "model": model,
        "backend": BACKEND,
        "config_label": config_label,
        "precision": "fp4",
        "source": SOURCE,
        "client_bench": "sglang bench_serving",
        "hardware": "mi355x",
        "chart_group": "inferencex-four-curve",
        "chart_label": chart_label,
        "chart_style": dict(MORI_CHART_STYLE),
        "commit": commit,
        "points": points,
        "gsm8k": None,
    }


def build_csv_run(
    *,
    config: str,
    isl: int,
    osl: int,
    points: list[dict],
    suffix: str,
    model_name: str = CSV_MODEL,
) -> dict:
    points = sorted(points, key=lambda p: p["concurrency"])
    compact_config = re.sub(r"[^a-zA-Z0-9]+", "-", config).strip("-").lower()
    compact_model = re.sub(r"[^a-zA-Z0-9]+", "-", model_name).strip("-").lower()
    run_id = (
        f"atom_sglang_mori-sglang_{CSV_DATE.replace('-', '')}_"
        f"{compact_model}-{compact_config}_isl{isl}_osl{osl}_{suffix}"
    )
    total_gpu = config_gpu_count(config)
    chart_label = mori_chart_label_for_config(config)
    config_label = f"atom-sglang_mori-sglang_{compact_model}_fp4_{compact_config}"
    if total_gpu:
        config_label += f"_g{total_gpu}"
    timestamp = int(time.mktime(time.strptime(CSV_DATE, "%Y-%m-%d")) * 1000)
    return {
        "run_id": run_id,
        "date": CSV_DATE,
        "timestamp": timestamp,
        "model": model_name,
        "backend": BACKEND,
        "config_label": config_label,
        "precision": "fp4",
        "source": SOURCE,
        "client_bench": "sglang bench_serving",
        "hardware": "mi355x",
        "chart_group": "inferencex-four-curve",
        "chart_label": chart_label,
        "chart_style": dict(MORI_CHART_STYLE),
        "commit": {
            "id": "",
            "message": "MoRI SGLang workbook import",
            "timestamp": f"{CSV_DATE}T00:00:00+00:00",
            "url": "",
            "author": {},
            "committer": {},
        },
        "points": points,
        "gsm8k": None,
    }


def load_bench_jsons(bench_dir: Path) -> list[dict]:
    out = []
    for path in sorted(bench_dir.rglob("deepseek-r1-fp4-mesh-*.json")):
        try:
            out.append(json.loads(path.read_text()))
        except Exception as exc:
            print(f"WARN skip {path}: {exc}", file=sys.stderr)
    return out


def download_artifacts(github_run_id: str, dest: Path) -> int:
    """Download mesh benchmark artifact zips via gh CLI."""
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "gh",
            "api",
            f"repos/ROCm/ATOM/actions/runs/{github_run_id}/artifacts?per_page=100",
            "--jq",
            ".artifacts[] | select(.name | test(\"mesh-tp[48]-\")) | [.id, .name] | @tsv",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return 0
    n = 0
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        aid, name = line.split("\t", 1)
        if not ARTIFACT_NAME_RE.match(name):
            continue
        zip_path = dest / f"{name}.zip"
        subprocess.run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"repos/ROCm/ATOM/actions/artifacts/{aid}/zip",
            ],
            stdout=open(zip_path, "wb"),
            check=True,
        )
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest)
        zip_path.unlink(missing_ok=True)
        n += 1
    return n


def remove_legacy_mesh_runs(data_dir: Path, github_run_id: str):
    """Drop old SGLang-Mesh entries for the same CI run to avoid duplicate curves."""
    for path in list(data_dir.glob(f"*sglang-mesh*_{github_run_id}.json")):
        print(f"remove legacy {path.name}", file=sys.stderr)
        path.unlink()


def remove_existing_manual_runs(data_dir: Path):
    for suffix in (CSV_RUN_ID_SUFFIX, XLSX_RUN_ID_SUFFIX):
        patterns = [
            f"atom_sglang_mori-sglang_*deepseek-v2*_{suffix}.json",
            f"atom_sglang_mori-sglang_*deepseek-r1-0528*_{suffix}.json",
        ]
        for pattern in patterns:
            for path in list(data_dir.glob(pattern)):
                print(f"remove existing manual import {path.name}", file=sys.stderr)
                path.unlink()


def import_rows(rows: list[dict], out_dir: Path, *, suffix: str, model_name: str) -> list[str]:
    grouped: dict[tuple[str, int, int], list[dict]] = defaultdict(list)
    for row in rows:
        config = row["config"]
        isl = int(row["isl"])
        osl = int(row["osl"])
        grouped[(config, isl, osl)].append(csv_to_point(row, point_date=CSV_DATE, model_name=model_name))

    remove_existing_manual_runs(out_dir)
    written = []
    for (config, isl, osl), points in sorted(grouped.items()):
        run = build_csv_run(config=config, isl=isl, osl=osl, points=points, suffix=suffix, model_name=model_name)
        out_path = out_dir / f"{run['run_id']}.json"
        out_path.write_text(json.dumps(run, indent=2) + "\n")
        written.append(out_path.name)
        print(f"wrote {out_path.name} ({len(points)} points)", file=sys.stderr)
    return written


def import_csv(csv_path: Path, out_dir: Path) -> list[str]:
    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    return import_rows(rows, out_dir, suffix=CSV_RUN_ID_SUFFIX, model_name=CSV_MODEL)


def import_xlsx(xlsx_path: Path, out_dir: Path) -> list[str]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise SystemExit("openpyxl is required for --xlsx imports") from exc

    wb = load_workbook(xlsx_path, read_only=True, data_only=True)
    try:
        ws = wb["mori-mesh VS slang-atom"] if "mori-mesh VS slang-atom" in wb.sheetnames else wb.active
        raw_header = list(next(ws.iter_rows(min_row=1, max_row=1, values_only=True)))
        header = ["engine" if h is None else str(h).strip() for h in raw_header]
        rows = []
        for values in ws.iter_rows(min_row=2, values_only=True):
            if not any(v is not None for v in values):
                continue
            row = {header[i]: values[i] for i in range(min(len(header), len(values)))}
            if str(row.get("engine") or "").strip().lower() != BACKEND:
                continue
            rows.append(row)
    finally:
        wb.close()

    if not rows:
        raise SystemExit(f"No {BACKEND} rows found in {xlsx_path}")
    return import_rows(rows, out_dir, suffix=XLSX_RUN_ID_SUFFIX, model_name=XLSX_MODEL)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", default="26525397753", help="GitHub Actions run id")
    ap.add_argument("--bench-dir", type=Path, help="Directory with extracted bench JSON files")
    ap.add_argument("--csv", type=Path, help="CSV summary to import as MoRI SGLang data")
    ap.add_argument("--xlsx", type=Path, help="Workbook with mori-sglang rows to import")
    ap.add_argument("-d", "--data-dir", type=Path, help="dashboard data root (default: ../data)")
    ap.add_argument("--no-download", action="store_true")
    args = ap.parse_args()

    data_root = args.data_dir or index_root_for(Path(__file__).resolve().parent / "data")
    out_dir = data_root / "sglang-atom"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.csv:
        written = import_csv(args.csv, out_dir)
        rebuild_index(data_root)
        print(f"Rebuilt index ({len(written)} CSV runs).", file=sys.stderr)
        return
    if args.xlsx:
        written = import_xlsx(args.xlsx, out_dir)
        rebuild_index(data_root)
        print(f"Rebuilt index ({len(written)} XLSX runs).", file=sys.stderr)
        return

    bench_records: list[dict]
    if args.bench_dir:
        bench_records = load_bench_jsons(args.bench_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="sglang-mesh-import-") as tmp:
            tmp_path = Path(tmp)
            if not args.no_download:
                n = download_artifacts(args.run_id, tmp_path)
                print(f"Downloaded {n} artifacts", file=sys.stderr)
            bench_records = load_bench_jsons(tmp_path)

    if not bench_records:
        print("No bench JSON files found.", file=sys.stderr)
        sys.exit(1)

    commit_sha = ""
    run_url = RUN_URL_TEMPLATE.format(run_id=args.run_id)
    grouped: dict[tuple[str, int, int], list[dict]] = defaultdict(list)
    samples: dict[tuple[str, int, int], dict] = {}

    for data in bench_records:
        if data.get("workload_label") != "SGLang-Mesh":
            continue
        key = group_key_from_data(data)
        commit_sha = commit_sha or str(data.get("atom_source_sha") or "")
        point_date = "2026-05-27"
        if data.get("date"):
            d = str(data["date"])
            if len(d) >= 8:
                point_date = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        pt = bench_to_point(data, run_url=run_url, point_date=point_date)
        grouped[key].append(pt)
        samples[key] = data

    if not grouped:
        print("No SGLang-Mesh bench records.", file=sys.stderr)
        sys.exit(1)

    remove_legacy_mesh_runs(out_dir, args.run_id)

    written = []
    for (tp_label, isl, osl), points in sorted(grouped.items()):
        run = build_run(
            tp_label=tp_label,
            isl=isl,
            osl=osl,
            points=points,
            sample=samples[(tp_label, isl, osl)],
            github_run_id=args.run_id,
            commit_sha=commit_sha,
        )
        out_path = out_dir / f"{run['run_id']}.json"
        out_path.write_text(json.dumps(run, indent=2) + "\n")
        written.append(out_path.name)
        print(f"wrote {out_path.name} ({len(points)} points)", file=sys.stderr)

    rebuild_index(data_root)
    print(f"Rebuilt index ({len(written)} runs).", file=sys.stderr)


if __name__ == "__main__":
    main()
