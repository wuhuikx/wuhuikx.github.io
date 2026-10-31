#!/usr/bin/env python3
"""One-shot: pull DeepSeek-R1 reference data from the public InferenceX API
and write data/mesh-workbook/infx_*.json + refresh data/index.json.

All configs for a given (hardware, framework, precision, spec, ISL, OSL) are merged
into ONE run file so the dashboard draws a single combined Pareto curve per
combo — matching InferenceX's own presentation.

This is a snapshot, not a recurring job. Re-run only when you want to refresh
the InferenceX baseline (e.g. they publish new numbers).

Usage:
    ./fetch_external.py
    ./fetch_external.py -d data/mesh/
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from pathlib import Path

from update_data import index_root_for, rebuild_index

API = "https://inferencex.semianalysis.com/api/v1/benchmarks?model=DeepSeek-R1-0528"
RUN_PREFIX = "infx_"
ISL_OSL_FILTER = [(8192, 1024)]

CURVES = [
    {
        "hardware": "b200",
        "framework": "dynamo-trt",
        "precision": "fp4",
        "spec_method": "none",
        "color": "#8bc34a",
        "label": "B200 (Dynamo TRT)",
    },
    {
        "hardware": "b200",
        "framework": "dynamo-trt",
        "precision": "fp4",
        "spec_method": "mtp",
        "color": "#0b8f4a",
        "label": "B200 (Dynamo TRT, MTP)",
    },
    {
        "hardware": "mi355x",
        "framework": "mori-sglang",
        "precision": "fp4",
        "spec_method": "none",
        "color": "#ff4f73",
        "label": "MI355X (MoRI SGLang)",
    },
    {
        "hardware": "mi355x",
        "framework": "mori-sglang",
        "precision": "fp4",
        "spec_method": "mtp",
        "color": "#e40012",
        "label": "MI355X (MoRI SGLang, MTP)",
    },
]


def fetch():
    print(f"GET {API}", file=sys.stderr)
    req = urllib.request.Request(API, headers={
        "User-Agent": "atom-mesh-dashboard/1.0",
        "Accept-Encoding": "gzip, deflate",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return json.loads(raw)


def ms(x):
    return round(x * 1000, 4) if x is not None else None


def rnd(v):
    return round(v, 4) if v is not None else None


def spec_suffix(spec_method):
    return "" if spec_method == "none" else f"_{spec_method}"


def chart_style(color):
    return {
        "color": color,
        "borderColor": color,
        "backgroundColor": color,
        "borderDash": [],
        "pointStyle": "circle",
    }


def to_runs(rows):
    all_runs = []
    for curve in CURVES:
        hardware = curve["hardware"]
        framework = curve["framework"]
        precision = curve["precision"]
        spec_method = curve["spec_method"]
        matches = [
            r for r in rows
            if r.get("hardware") == hardware
            and r.get("framework") == framework
            and r.get("precision") == precision
            and r.get("spec_method") == spec_method
        ]
        print(f"{len(matches)} rows for {hardware}/{framework}/{precision} "
              f"(spec_method={spec_method})",
              file=sys.stderr)

        groups = defaultdict(list)
        for r in matches:
            key = (r["isl"], r["osl"])
            if ISL_OSL_FILTER and key not in ISL_OSL_FILTER:
                continue
            groups[key].append(r)

        for (isl, osl), group_rows in sorted(groups.items()):
            latest_date = max(r["date"] for r in group_rows)
            latest_rows = [r for r in group_rows if r["date"] == latest_date]
            style = chart_style(curve["color"])

            points = []
            for r in latest_rows:
                m = r.get("metrics", {})
                ptp, dtp = r["prefill_tp"], r["decode_tp"]
                pgpu, dgpu = r["num_prefill_gpu"], r["num_decode_gpu"]
                total_gpu = pgpu + dgpu
                conc = r["conc"]
                mean_intvty = m.get("mean_intvty")
                median_intvty = m.get("median_intvty")
                tput_per_gpu = m.get("tput_per_gpu")
                output_tput_per_gpu = m.get("output_tput_per_gpu")
                input_tput_per_gpu = m.get("input_tput_per_gpu")
                output_tput = mean_intvty * conc if (mean_intvty is not None and conc) else None
                total_tput = tput_per_gpu * total_gpu if (
                    tput_per_gpu is not None and total_gpu is not None
                ) else None

                median_tpot_ms_val = ms(m.get("median_tpot"))
                points.append({
                    "isl": isl, "osl": osl, "concurrency": conc, "ratio": None,
                    "client_bench": "inferencemax",
                    "spec_method": spec_method,
                    "disagg": r.get("disagg"),
                    "is_multinode": r.get("is_multinode"),
                    "ttft_ms": ms(m.get("mean_ttft")),
                    "ttft_p99": ms(m.get("p99_ttft")),
                    "tpot_ms": ms(m.get("mean_tpot")),
                    "tpot_p99": ms(m.get("p99_tpot")),
                    "itl_ms": ms(m.get("mean_itl")),
                    "e2el_ms": ms(m.get("mean_e2el")),
                    "median_ttft_ms": ms(m.get("median_ttft")),
                    "median_tpot_ms": median_tpot_ms_val,
                    "median_itl_ms": ms(m.get("median_itl")),
                    "median_e2el_ms": ms(m.get("median_e2el")),
                    "p90_ttft_ms": ms(m.get("p90_ttft")),
                    "p90_tpot_ms": ms(m.get("p90_tpot")),
                    "p90_itl_ms": ms(m.get("p90_itl")),
                    "p90_e2el_ms": ms(m.get("p90_e2el")),
                    "interactivity": rnd(1000.0 / median_tpot_ms_val) if median_tpot_ms_val else rnd(median_intvty),
                    "mean_interactivity": rnd(mean_intvty),
                    "p90_interactivity": rnd(m.get("p90_intvty")),
                    "p99_interactivity": rnd(m.get("p99_intvty")),
                    "tput_per_gpu": rnd(tput_per_gpu),
                    "output_tput_per_gpu": rnd(output_tput_per_gpu),
                    "input_tput_per_gpu": rnd(input_tput_per_gpu),
                    "output_tput": rnd(output_tput),
                    "total_tput": rnd(total_tput),
                    "req_tput": None, "completed": None, "duration": None, "num_prompts": None,
                    "prefill_tp": ptp, "decode_tp": dtp,
                    "prefill_ep": r.get("prefill_ep"),
                    "prefill_dpa": r.get("prefill_dp_attention"),
                    "prefill_workers": r.get("prefill_num_workers"),
                    "decode_ep": r.get("decode_ep"),
                    "decode_dpa": r.get("decode_dp_attention"),
                    "decode_workers": r.get("decode_num_workers"),
                    "num_prefill_gpu": pgpu, "num_decode_gpu": dgpu,
                    "total_gpu": total_gpu,
                    "image": r.get("image"),
                    "run_url": r.get("run_url"),
                    "point_date": r["date"],
                    "precision": precision,
                    "hardware": hardware,
                    "chart_group": "inferencex-four-curve",
                    "chart_label": curve["label"],
                    "raw_metrics": m,
                    "chart_style": style,
                })

            dated_suffix = latest_date.replace("-", "")
            spec = spec_suffix(spec_method)
            run_id = (
                f"{RUN_PREFIX}{hardware}_{framework}_{precision}{spec}_"
                f"isl{isl}_osl{osl}_{dated_suffix}"
            )
            try:
                ts = int(time.mktime(time.strptime(latest_date, "%Y-%m-%d")) * 1000)
            except ValueError:
                ts = None
            all_runs.append({
                "run_id": run_id,
                "date": latest_date,
                "timestamp": ts,
                "model": "DeepSeek-R1-0528",
                "backend": framework,
                "config_label": f"{hardware}_{framework}_{precision}{spec}",
                "source": "Mesh Workbook",
                "client_bench": "inferencemax",
                "hardware": hardware,
                "precision": precision,
                "spec_method": spec_method,
                "chart_group": "inferencex-four-curve",
                "chart_label": curve["label"],
                "chart_style": style,
                "points": points,
                "gsm8k": None,
            })
    return all_runs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-d", "--data-dir", default=str(Path(__file__).parent / "data" / "mesh-workbook"))
    args = ap.parse_args()
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    try:
        rows = fetch()
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"ERROR: external fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    runs = to_runs(rows)
    written = set()
    for run in runs:
        f = data_dir / f"{run['run_id']}.json"
        f.write_text(json.dumps(run, indent=2) + "\n")
        written.add(f.name)
        print(f"  wrote {f.name}: {len(run['points'])} points (latest {run['date']})",
              file=sys.stderr)

    rebuild_index(index_root_for(data_dir))
    print(f"Done. {len(runs)} InferenceX runs written to {data_dir}/", file=sys.stderr)


if __name__ == "__main__":
    main()
