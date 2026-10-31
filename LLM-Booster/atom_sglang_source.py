"""Normalize legacy ATOM SGLang CI source labels for LLM-Booster."""

from __future__ import annotations

LEGACY_SOURCE = "ATOM SGLang Benchmark"
MESH_SOURCE = "SGLang-ATOM Mesh"
OOB_SOURCE = "SGLang-ATOM OOB"


def llm_booster_source_for(
    *,
    workload_label: str = "",
    backend: str = "",
    filename: str = "",
    run_id: str = "",
) -> str | None:
    """Return Mesh/OOB source when legacy CI label applies; None if unchanged."""
    label = (workload_label or "").strip()
    backend = (backend or "").strip()
    name = (filename or run_id or "").lower()

    if label == "SGLang-Mesh" or backend == "SGLang-Mesh" or "sglang-mesh" in name:
        return MESH_SOURCE
    if (
        label == "SGLang-OOB"
        or backend == "SGLang-OOB"
        or "sglang-oob" in name
        or "_oob_" in name
        or name.startswith("atom_sglang_oob")
    ):
        return OOB_SOURCE
    return None


def normalize_atom_sglang_source(run: dict, filename: str = "") -> str:
    """Return the dashboard source string for a run payload."""
    source = str(run.get("source") or "SLURM")
    if source != LEGACY_SOURCE:
        return source
    mapped = llm_booster_source_for(
        workload_label=str(run.get("workload_label", "")),
        backend=str(run.get("backend", "")),
        filename=filename,
        run_id=str(run.get("run_id", "")),
    )
    return mapped or source
