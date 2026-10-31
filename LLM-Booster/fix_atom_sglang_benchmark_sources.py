#!/usr/bin/env python3
"""Reclassify ATOM SGLang CI runs from legacy source to SGLang-ATOM Mesh/OOB."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from atom_sglang_source import LEGACY_SOURCE, normalize_atom_sglang_source
from update_data import index_root_for, rebuild_index


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "-d",
        "--data-dir",
        default=str(Path(__file__).parent / "data" / "sglang-atom"),
    )
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    updated = 0
    scanned = 0

    for path in sorted(data_dir.glob("*.json")):
        if path.name == "index.json":
            continue
        scanned += 1
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(run, dict) or run.get("source") != LEGACY_SOURCE:
            continue

        new_source = normalize_atom_sglang_source(run, path.name)
        if run.get("source") == new_source:
            continue

        run["source"] = new_source
        path.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
        updated += 1
        print(f"  {path.name}: {LEGACY_SOURCE} -> {new_source}")

    rebuild_index(index_root_for(data_dir))
    print(f"Reclassified {updated} / {scanned} run files under {data_dir}")
