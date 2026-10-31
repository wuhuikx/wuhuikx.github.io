#!/usr/bin/env python3
"""Backfill median latency metrics from ATOM GitHub Actions artifacts.

The ATOM benchmark dashboard summary can omit median fields that exist in the
raw benchmark JSON artifacts. This script scans dashboard/data/vllm-atom runs,
downloads the matching raw benchmark artifacts by GitHub Actions run id, and
copies median/std/p99 fields back into the dashboard JSON.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from update_data import index_root_for, rebuild_index

ATOM_REPO_API = "https://api.github.com/repos/ROCm/ATOM"
RUN_ID_RE = re.compile(r"github\.com/ROCm/ATOM/actions/runs/(\d+)")
DASHBOARD_RUN_RE = re.compile(
    r"atom_vllm_\d{8}_(.+)_isl\d+_osl\d+_[0-9a-f]+_(\d+)_vllmbench$"
)


def github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "llm-booster-median-backfill/1.0",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_json(url: str) -> dict:
    req = Request(url, headers=github_headers())
    with urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_bytes(url: str) -> bytes:
    req = Request(url, headers=github_headers())
    with urlopen(req, timeout=120) as resp:
        return resp.read()


def run_id_from_url(url: str | None) -> str | None:
    match = RUN_ID_RE.search(str(url or ""))
    return match.group(1) if match else None


def run_id_from_dashboard_run(run: dict, path: Path) -> str | None:
    for point in run.get("points", []):
        run_id = run_id_from_url(point.get("run_url") or run.get("run_url"))
        if run_id:
            return run_id
    match = DASHBOARD_RUN_RE.match(str(run.get("run_id") or path.stem))
    return match.group(2) if match else None


def prefix_from_dashboard_run(run: dict, path: Path) -> str | None:
    match = DASHBOARD_RUN_RE.match(str(run.get("run_id") or path.stem))
    return match.group(1) if match else None


def artifact_name_candidates(prefix: str, isl: int, osl: int, concurrency: int) -> list[str]:
    return [
        f"oot-benchmark-{prefix}-{isl}-{osl}-{concurrency}-",
        # Some MTP benchmark artifacts include the AW marker after TP.
        f"oot-benchmark-{prefix}-aw-{isl}-{osl}-{concurrency}-0",
    ]


def load_artifact_jsons(run_id: str, needed_names: set[str], cache_dir: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    cache_dir.mkdir(parents=True, exist_ok=True)
    artifact_index = fetch_json(f"{ATOM_REPO_API}/actions/runs/{run_id}/artifacts?per_page=100")
    for artifact in artifact_index.get("artifacts", []):
        name = artifact.get("name")
        if name not in needed_names:
            continue
        if artifact.get("expired"):
            print(f"WARN: artifact expired, skip {run_id}:{name}", file=sys.stderr)
            continue
        cache_path = cache_dir / f"{run_id}_{name}.json"
        if cache_path.exists():
            try:
                out[name] = json.loads(cache_path.read_text())
                continue
            except Exception:
                cache_path.unlink(missing_ok=True)
        try:
            payload = fetch_bytes(artifact["archive_download_url"])
        except (HTTPError, URLError, TimeoutError) as exc:
            print(f"WARN: failed to download {run_id}:{name}: {exc}", file=sys.stderr)
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                json_members = [
                    member for member in zf.namelist()
                    if member.endswith(".json") and not member.endswith(("summary.json", "regression_report.json"))
                ]
                if not json_members:
                    print(f"WARN: no raw json in {run_id}:{name}", file=sys.stderr)
                    continue
                raw = json.loads(zf.read(json_members[0]).decode("utf-8"))
        except Exception as exc:
            print(f"WARN: failed to parse {run_id}:{name}: {exc}", file=sys.stderr)
            continue
        cache_path.write_text(json.dumps(raw))
        out[name] = raw
    return out


def copy_metrics(point: dict, raw: dict) -> bool:
    changed = False
    for src, dst in [
        ("median_tpot_ms", "median_tpot_ms"),
        ("median_ttft_ms", "median_ttft_ms"),
        ("median_e2el_ms", "median_e2el_ms"),
        ("std_tpot_ms", "std_tpot_ms"),
        ("std_ttft_ms", "std_ttft_ms"),
        ("std_e2el_ms", "std_e2el_ms"),
        ("p99_e2el_ms", "e2el_p99"),
    ]:
        if raw.get(src) is not None and point.get(dst) != raw[src]:
            point[dst] = raw[src]
            changed = True
    median_tpot = point.get("median_tpot_ms")
    if isinstance(median_tpot, (int, float)) and median_tpot > 0:
        interactivity = 1000.0 / median_tpot
        if point.get("interactivity") != interactivity:
            point["interactivity"] = interactivity
            changed = True
    return changed


def backfill(data_root: Path, cache_dir: Path, rebuild: bool = True) -> tuple[int, int, int]:
    vllm_dir = data_root / "vllm-atom"
    missing_by_run: dict[str, list[tuple[Path, int, list[str]]]] = {}
    runs_by_file: dict[Path, dict] = {}
    for path in sorted(vllm_dir.glob("*.json")):
        try:
            run = json.loads(path.read_text())
        except Exception as exc:
            print(f"WARN: skip malformed {path}: {exc}", file=sys.stderr)
            continue
        prefix = prefix_from_dashboard_run(run, path)
        run_id = run_id_from_dashboard_run(run, path)
        if not prefix or not run_id:
            continue
        for idx, point in enumerate(run.get("points", [])):
            if point.get("median_tpot_ms") not in (None, ""):
                continue
            isl = point.get("isl")
            osl = point.get("osl")
            concurrency = point.get("concurrency")
            if isl is None or osl is None or concurrency is None:
                continue
            candidates = artifact_name_candidates(prefix, int(isl), int(osl), int(concurrency))
            missing_by_run.setdefault(run_id, []).append((path, idx, candidates))
            runs_by_file[path] = run

    changed_files: set[Path] = set()
    updated_points = 0
    unmatched = 0
    for run_id, items in sorted(missing_by_run.items()):
        needed = {name for _path, _idx, names in items for name in names}
        raw_by_name = load_artifact_jsons(run_id, needed, cache_dir)
        for path, idx, names in items:
            raw = next((raw_by_name[name] for name in names if name in raw_by_name), None)
            if not raw:
                unmatched += 1
                continue
            run = runs_by_file[path]
            if copy_metrics(run["points"][idx], raw):
                changed_files.add(path)
                updated_points += 1

    for path in sorted(changed_files):
        path.write_text(json.dumps(runs_by_file[path], indent=2) + "\n")

    if rebuild and changed_files:
        rebuild_index(data_root)
    return len(changed_files), updated_points, unmatched


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-d", "--data-dir", type=Path, default=Path(__file__).resolve().parent / "data")
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/atom-median-artifacts"))
    parser.add_argument("--no-rebuild", action="store_true")
    args = parser.parse_args()
    data_root = index_root_for(args.data_dir)
    changed_files, updated_points, unmatched = backfill(data_root, args.cache_dir, rebuild=not args.no_rebuild)
    print(
        f"median backfill: changed_files={changed_files} "
        f"updated_points={updated_points} unmatched_or_expired={unmatched}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
