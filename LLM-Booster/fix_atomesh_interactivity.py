#!/usr/bin/env python3
"""Fix ATOMesh interactivity to use 1000/median_tpot_ms (matching MoRI/InferenceX).

Reads median_tpot_ms from the original SLURM benchmark outputs and recalculates
interactivity for all dashboard/data/mesh-workbook/infx_mi355x_atomesh_*.json files.
"""

import json
import glob
import sys
from pathlib import Path

SLURM_ROOT = Path("/it-share/yajizhan/slurm_logs")
MESH_WB_DIR = Path(__file__).parent / "data" / "mesh-workbook"


def bench_file_path(source_run_id: str, isl: int, osl: int, conc: int, ratio: float, is_vllm: bool) -> Path:
    prefix = "pd-vllm-mesh" if is_vllm else "pd-mesh"
    return SLURM_ROOT / source_run_id / "bench" / f"{prefix}-{isl}-{osl}-{conc}-{ratio}.json"


def main():
    files = sorted(glob.glob(str(MESH_WB_DIR / "infx_mi355x_atomesh_*.json")))
    if not files:
        print("No ATOMesh files found.", file=sys.stderr)
        return

    total_points = 0
    fixed_points = 0
    missing = 0

    for fpath in files:
        data = json.loads(Path(fpath).read_text())
        is_vllm = "_vllm_" in Path(fpath).name
        changed = False

        for p in data.get("points", []):
            total_points += 1
            rid = p.get("source_run_id")
            if not rid:
                print(f"  WARN: no source_run_id in {Path(fpath).name}, conc={p.get('concurrency')}", file=sys.stderr)
                missing += 1
                continue

            bf = bench_file_path(rid, p["isl"], p["osl"], p["concurrency"], p.get("ratio", 0.8), is_vllm)
            if not bf.exists():
                print(f"  WARN: {bf} not found", file=sys.stderr)
                missing += 1
                continue

            raw = json.loads(bf.read_text())
            median_tpot = raw.get("median_tpot_ms")
            if median_tpot is None or median_tpot == 0:
                print(f"  WARN: no median_tpot_ms in {bf}", file=sys.stderr)
                missing += 1
                continue

            new_interactivity = round(1000.0 / median_tpot, 4)
            old_interactivity = p.get("interactivity")

            if old_interactivity != new_interactivity:
                p["interactivity"] = new_interactivity
                changed = True
                fixed_points += 1

        if changed:
            Path(fpath).write_text(json.dumps(data, indent=2) + "\n")
            print(f"  FIXED: {Path(fpath).name}")

    print(f"\nDone. {fixed_points}/{total_points} points updated, {missing} skipped.")


if __name__ == "__main__":
    main()
