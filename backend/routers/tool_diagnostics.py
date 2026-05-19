from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends

from backend.auth import AuthenticatedUser, require_roles
from scripts import run_lineage_dr_validation as lineage_dr


router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


def _probe_local_tool(names: list[str], version_args: list[str]) -> dict[str, Any]:
    executable = lineage_dr._which_many(names)
    if not executable:
        return {
            "status": "not_found",
            "executable": None,
            "version_probe": None,
            "message": f"None of these executables were found: {', '.join(names)}",
        }

    status, output = lineage_dr._probe_version(executable, version_args)
    return {
        "status": "ready" if status == "ok" else "found_but_unusable",
        "executable": executable,
        "version_probe": {"status": status, "output": output},
        "message": "tool version probe succeeded" if status == "ok" else "tool executable was found but version probe failed",
    }


def _wsl_tool_probe(wsl_env: str, tool_args: list[str]) -> dict[str, Any]:
    status, output = lineage_dr._wsl_probe_tool(
        lineage_dr._build_wsl_micromamba_command(wsl_env, tool_args)
    )
    return {
        "status": "ready" if status == "ok" else "not_ready",
        "probe": {"status": status, "output": output},
    }


def _recommend_runner(local: dict[str, Any], wsl: dict[str, Any], docker: dict[str, Any]) -> str:
    if local.get("status") == "ready":
        return "local"
    if wsl.get("tbprofiler", {}).get("status") == "ready":
        return "wsl"
    if docker.get("available") and docker.get("daemon_running"):
        return "docker"
    return "none"


@router.get("/tbprofiler")
def tbprofiler_diagnostics(
    _user: AuthenticatedUser = Depends(require_roles("analyst")),
):
    """Return actionable diagnostics for TBProfiler/Mykrobe execution paths.

    This endpoint is deliberately read-only. It checks the local Python/Windows
    runner, WSL micromamba environment, Docker daemon, selected FASTA inputs,
    and whether FASTA sample IDs can resolve to cases before lineage/DR jobs run.
    """
    wsl_env = os.getenv("TBPROFILER_WSL_ENV", "tbtools")
    wsl_fallback_enabled = os.getenv("TBPROFILER_WSL_FALLBACK", "1") == "1"
    docker_fallback_enabled = os.getenv("TBPROFILER_DOCKER_FALLBACK", "1") == "1"
    docker_image = os.getenv("TBPROFILER_DOCKER_IMAGE", "quay.io/jodyphelan/tbprofiler:latest")

    local_tbprofiler = _probe_local_tool(
        ["tb-profiler", "tb-profiler.exe", "tb_profiler", "tb-profiler-tools"],
        ["--version"],
    )
    local_mykrobe = _probe_local_tool(["mykrobe", "mykrobe.exe"], ["--version"])

    wsl_status = lineage_dr._probe_wsl()
    wsl_tools: dict[str, Any] = {
        "available": wsl_status.get("available"),
        "status": wsl_status.get("status"),
        "probe": wsl_status.get("output"),
        "details": wsl_status.get("details", []),
        "fallback_enabled": wsl_fallback_enabled,
        "env": wsl_env,
        "tbprofiler": {"status": "not_checked", "message": "WSL probe not run"},
        "mykrobe": {"status": "not_checked", "message": "WSL probe not run"},
    }
    if wsl_status.get("available") and wsl_fallback_enabled:
        wsl_tools["tbprofiler"] = _wsl_tool_probe(wsl_env, ["tb-profiler", "version"])
        wsl_tools["mykrobe"] = _wsl_tool_probe(wsl_env, ["mykrobe", "--help"])

    docker_status = lineage_dr._probe_docker()
    docker = {
        "available": docker_status.get("available"),
        "status": docker_status.get("status"),
        "probe": docker_status.get("output"),
        "daemon_running": docker_status.get("daemon_running", False),
        "daemon_probe": docker_status.get("daemon_probe", "unknown"),
        "fallback_enabled": docker_fallback_enabled,
        "tbprofiler_image": docker_image,
    }

    fasta_inputs = lineage_dr._discover_fasta_inputs()
    sample_inputs = lineage_dr._prepare_fasta_sample_inputs(fasta_inputs)
    sample_validation = lineage_dr._validate_fasta_sample_inputs(sample_inputs)
    input_metadata = lineage_dr._fasta_input_metadata(fasta_inputs, sample_inputs)
    discovered_inputs = lineage_dr._discover_inputs()
    discovered_inputs.update(input_metadata)
    discovered_inputs["sample_id_validation"] = sample_validation

    blockers: list[str] = []
    warnings: list[str] = []

    if not fasta_inputs:
        blockers.append("no_fasta_inputs_found")
    if sample_validation.get("status") not in {"matched", "no_samples"}:
        blockers.append("fasta_sample_ids_do_not_match_cases")
    if local_tbprofiler.get("status") == "found_but_unusable":
        warnings.append("Local TBProfiler is present but unusable; use WSL or Docker fallback.")
    if wsl_fallback_enabled and not wsl_status.get("available"):
        warnings.append("WSL fallback is enabled but WSL is not available.")
    if wsl_fallback_enabled and wsl_tools.get("tbprofiler", {}).get("status") == "not_ready":
        warnings.append(f"WSL fallback is enabled but tb-profiler is not ready in env '{wsl_env}'.")
    if docker_fallback_enabled and not docker.get("daemon_running"):
        warnings.append("Docker fallback is enabled but Docker daemon is not reachable.")

    recommended_runner = _recommend_runner(local_tbprofiler, wsl_tools, docker)
    ready_to_run = (
        bool(fasta_inputs)
        and sample_validation.get("status") == "matched"
        and recommended_runner != "none"
    )

    return {
        "status": "ready" if ready_to_run else "needs_attention",
        "recommended_runner": recommended_runner,
        "ready_to_run": ready_to_run,
        "blockers": blockers,
        "warnings": warnings,
        "environment": {
            "TBPROFILER_WSL_FALLBACK": os.getenv("TBPROFILER_WSL_FALLBACK", "1"),
            "TBPROFILER_DOCKER_FALLBACK": os.getenv("TBPROFILER_DOCKER_FALLBACK", "1"),
            "TBPROFILER_WSL_ENV": wsl_env,
            "TBPROFILER_DOCKER_IMAGE": docker_image,
        },
        "local": {
            "tbprofiler": local_tbprofiler,
            "mykrobe": local_mykrobe,
        },
        "wsl": wsl_tools,
        "docker": docker,
        "inputs": discovered_inputs,
        "next_steps": [
            "If local TBProfiler fails on Windows, prefer WSL micromamba or Docker rather than compiling pysam locally.",
            "Run: wsl -- bash -lc \"micromamba run -n tbtools tb-profiler version\" to verify the WSL env.",
            "If sample_id_validation is not matched, create exports/sample_id_map.csv or align FASTA headers with case IDs/local_lab_sample_id.",
            "If Docker fallback is preferred, start Docker Desktop and confirm docker info succeeds.",
        ],
    }
