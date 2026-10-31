#!/usr/bin/env python3
"""Reclassify dashboard run sources using the latest AWS/Meta/OOB rules.

Rules:
  - Meta Workbook:
      explicit filename/run metadata token: "met" or "meta"
      OR:
      model+tp in Meta allowlist
      ISL/OSL in {1024/1024, 1024/8192, 8192/1024}
      client bench == inferencemax
      ratio == 0.8
  - AWS Workbook:
      model+tp in AWS allowlist
      ISL/OSL in {1000/100, 5000/500, 10000/1000, 100000/1000}
      client bench == vllm bench
      ratio in {1, 0}
  - Otherwise (ATOM data): OOB
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path

from update_data import rebuild_index

ATOM_SOURCES = {"ATOM Dashboard", "vLLM-ATOM", "AWS Workbook", "Meta Workbook", "Meta workbook", "OOB"}
ATOM_BACKENDS = {"vLLM-ATOM", "ATOM-vLLM"}
CUSTOMER_TOKEN_RE = re.compile(r"(?:^|[-_.\s/])(?P<token>inferencemax|aws|aw|meta|met)(?=$|[-_.\s/])", re.IGNORECASE)
OOB_TOKEN_RE = re.compile(r"(?:^|[-_.\s/()])oob(?=$|[-_.\s/()])", re.IGNORECASE)


def normalize_marker(model: str = "", config_label: str = "") -> str:
    return f"{model or ''} {config_label or ''}".lower()


def customer_tokens(*values: object) -> set[str]:
    text = " ".join(str(value) for value in values if value not in (None, ""))
    return {match.group("token").lower() for match in CUSTOMER_TOKEN_RE.finditer(text)}


def has_explicit_meta_token(run: dict, file_path: Path) -> bool:
    tokens = customer_tokens(
        file_path.name,
        run.get("run_id"),
        run.get("model"),
        run.get("config_label"),
    )
    return "meta" in tokens or "met" in tokens


def has_explicit_oob_token(run: dict, point: dict, file_path: Path | None) -> bool:
    # Do not use run["source"] here. Existing source may be stale; this is only
    # for explicit benchmark labels like "DeepSeek-V4-Pro TP8 (OOB)".
    values = [
        file_path.name if file_path is not None else "",
        run.get("run_id"),
        run.get("model"),
        run.get("config_label"),
        point.get("model_path"),
        point.get("chart_label"),
        point.get("image"),
    ]
    text = " ".join(str(value) for value in values if value not in (None, ""))
    return OOB_TOKEN_RE.search(text) is not None


def canonical_marker(model: str = "", config_label: str = "") -> str:
    marker = normalize_marker(model, config_label)
    marker = re.sub(r"(^|[-_\s./])aw(?=$|[-_\s./])", r"\1", marker)
    marker = re.sub(r"(^|[-\s])(mxfp\d+|fp\d+)(?=$|[-\s])", r"\1", marker)
    return re.sub(r"[^a-z0-9]+", "-", marker).strip("-")


def normalize_precision(raw_precision: str | None, model_name: str = "") -> str:
    merged = f"{raw_precision or ''} {model_name or ''}".lower()
    if "bf16" in merged:
        return "bf16"
    if "fp4" in merged:
        return "fp4"
    if "fp8" in merged:
        return "fp8"
    return "fp8"


def client_bench_for(run: dict, point: dict) -> str:
    raw = point.get("client_bench") or run.get("client_bench") or (
        "vllm bench" if run.get("source") in ATOM_SOURCES else "inferencemax bench"
    )
    val = str(raw or "").strip().lower()
    if val in {"inferencemax", "inferencemax bench"}:
        return "inferencemax bench"
    if val == "vllm bench":
        return "vllm bench"
    return str(raw or "")


def tp_for(run: dict, point: dict) -> str | None:
    if point.get("tensor_parallel_size") is not None:
        return str(point["tensor_parallel_size"])
    model_tp = re.search(r"[-\s]TP(\d+)(?:\+EP\d+)?$", str(run.get("model", "")), flags=re.IGNORECASE)
    if model_tp:
        return model_tp.group(1)
    if point.get("prefill_tp") is not None and point.get("decode_tp") is not None:
        return None
    if point.get("prefill_tp") is not None:
        return str(point["prefill_tp"])
    if point.get("decode_tp") is not None:
        return str(point["decode_tp"])
    return None


def is_meta_model_tp(model: str = "", config_label: str = "", tp: str | None = None, precision: str = "") -> bool:
    marker = normalize_marker(model, config_label)
    canonical = canonical_marker(model, config_label)
    tp_num = int(tp) if str(tp or "").isdigit() else None
    p = str(precision or "").lower()

    if "kimi-k2-5" in marker or "kimi-k2-5" in canonical:
        return tp_num == 4
    if "kimi-k2" in marker:
        return tp_num in {4, 8}
    if "gpt-oss-120b" in marker:
        return tp_num == 1
    if "qwen3-next" in marker or "qwen3-n8a" in marker or "qwen3-next" in canonical:
        return tp_num in {1, 4}
    if any(x in marker for x in ("qwen3.5-397b", "qwen3-5-397b", "qwen35-397b")) or "qwen3-5-397b" in canonical:
        return tp_num == 4 and p == "fp8"
    if "deepseek-r1" in marker:
        return tp_num == 8 and p in {"fp8", "fp4"}
    return False


def is_aws_model_tp(model: str = "", config_label: str = "", tp: str | None = None) -> bool:
    marker = normalize_marker(model, config_label)
    canonical = canonical_marker(model, config_label)
    tp_num = int(tp) if str(tp or "").isdigit() else None

    if ("minimax" in marker and "m2.5" in marker) or "minimax-m2-5" in canonical:
        return tp_num in {2, 4}
    if ("deepseek" in marker and "3.2" in marker) or "deepseek-v3.2" in marker or "deepseek-v3-2" in canonical:
        return tp_num in {4, 8}
    if ("glm" in marker and "4.7" in marker) or "glm-4-7" in canonical:
        return tp_num in {4, 8}
    if "gpt-oss-120b" in marker:
        return tp_num in {1, 2, 8}
    if "qwen3-next" in marker or "qwen3-n8a" in marker or "qwen3-next" in canonical:
        return tp_num in {1, 2, 4}
    if "kimi-k2.5" in marker or "kimi-k2-5" in marker or "kimi-k2-5" in canonical:
        return tp_num in {4, 8}
    return False


def is_meta_point(run: dict, point: dict) -> bool:
    isl = int(point.get("isl")) if point.get("isl") is not None else None
    osl = int(point.get("osl")) if point.get("osl") is not None else None
    if (isl, osl) not in {(1024, 1024), (1024, 8192), (8192, 1024)}:
        return False
    if client_bench_for(run, point) != "inferencemax bench":
        return False
    ratio = point.get("ratio")
    try:
        ratio = float(ratio)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(ratio) or abs(ratio - 0.8) > 1e-6:
        return False
    tp = tp_for(run, point)
    precision = normalize_precision(point.get("precision", run.get("precision")), run.get("model", ""))
    return is_meta_model_tp(run.get("model", ""), run.get("config_label", ""), tp, precision)


def is_aws_point(run: dict, point: dict) -> bool:
    isl = int(point.get("isl")) if point.get("isl") is not None else None
    osl = int(point.get("osl")) if point.get("osl") is not None else None
    if (isl, osl) not in {(1000, 100), (5000, 500), (10000, 1000), (100000, 1000)}:
        return False
    if client_bench_for(run, point) != "vllm bench":
        return False
    ratio = point.get("ratio")
    try:
        ratio = float(ratio)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(ratio) or not (abs(ratio - 1.0) <= 1e-6 or abs(ratio - 0.0) <= 1e-6):
        return False
    tp = tp_for(run, point)
    return is_aws_model_tp(run.get("model", ""), run.get("config_label", ""), tp)


def classify_point(run: dict, point: dict, file_path: Path | None = None) -> str:
    if has_explicit_oob_token(run, point, file_path):
        return "OOB"
    if file_path is not None and has_explicit_meta_token(run, file_path):
        return "Meta Workbook"
    if is_meta_point(run, point):
        return "Meta Workbook"
    if is_aws_point(run, point):
        return "AWS Workbook"
    return "OOB"


def is_atom_run(run: dict, file_path: Path) -> bool:
    source = str(run.get("source", ""))
    backend = str(run.get("backend", ""))
    if source in ATOM_SOURCES:
        return True
    if backend in ATOM_BACKENDS:
        return True
    return "vllm-atom" in file_path.as_posix().lower()

def is_b200_run(run: dict, file_path: Path) -> bool:
    if "/vllm-atom/B200/".lower() in file_path.as_posix().lower():
        return True
    hardware = str(run.get("hardware", "")).lower()
    if "b200" in hardware:
        return True
    for point in run.get("points", []) or []:
        if "b200" in str(point.get("hardware", "")).lower():
            return True
    return False


def classify_run(run: dict, file_path: Path | None = None) -> str:
    points = run.get("points", []) or []
    if not points:
        return "OOB"
    counts = Counter(classify_point(run, p, file_path) for p in points)
    # Deterministic tie-break: Meta > AWS > OOB
    best = sorted(counts.items(), key=lambda kv: (-kv[1], {"Meta Workbook": 0, "AWS Workbook": 1, "OOB": 2}[kv[0]]))[0][0]
    return best


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-d", "--data-root", default=str(Path(__file__).parent / "data"))
    args = ap.parse_args()

    data_root = Path(args.data_root)
    updated = 0
    scanned = 0
    for file in sorted(data_root.rglob("*.json")):
        if file.name == "index.json":
            continue
        scanned += 1
        try:
            run = json.loads(file.read_text())
        except json.JSONDecodeError:
            continue
        if not isinstance(run, dict) or not is_atom_run(run, file):
            continue
        if is_b200_run(run, file):
            continue
        new_source = classify_run(run, file)
        if run.get("source") == new_source:
            continue
        run["source"] = new_source
        file.write_text(json.dumps(run, indent=2) + "\n")
        updated += 1

    rebuild_index(data_root)
    print(f"Reclassified {updated} / {scanned} json files under {data_root}")


if __name__ == "__main__":
    main()

