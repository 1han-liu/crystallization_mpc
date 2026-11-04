"""Structural validation for an archived MATLAB-GUI numerical workspace.

Source arrays can have different lengths after a caught loop exception. None
of this validation creates missing state or changes calculation semantics.
"""
from __future__ import annotations

from typing import Any, Mapping
import numpy as np


def validate_checkpoint(state: Mapping[str, Any], configured: Mapping[str, Any], history_keys: tuple[str, ...]) -> None:
    if state.get("schema") != "matlab_gui_workspace_v1":
        raise ValueError("Checkpoint does not contain the complete GUI workspace.")
    if type(state["frame_index"]) is not int or state["frame_index"] < 0:
        raise ValueError("Invalid source loop index.")
    if type(state["running"]) is not bool:
        raise ValueError("Invalid activation state.")
    for name in ("workspace", "params", "configured_params", "integrals", "filters", "simulation", "refresh", "adaptation"):
        if not isinstance(state[name], Mapping):
            raise ValueError(f"Checkpoint {name} must be an object.")
    simulation = state["simulation"]
    if type(simulation["seed_generation_completed"]) is not bool:
        raise ValueError("Checkpoint initialization completion is missing.")
    if (not simulation["seed_generation_completed"]
            and simulation["seed_population"]["initial_count"] == 0
            and (simulation["size_list_seed"] or simulation["size_list"])):
        raise ValueError("Checkpoint contains an unassigned initial seed population.")
    if set(state["history"]) != set(history_keys) or set(state["history_assigned"]) != set(history_keys):
        raise ValueError("Checkpoint source array names are incomplete.")
    for key, values in state["history"].items():
        assigned = state["history_assigned"][key]
        if not isinstance(values, list) or not isinstance(assigned, list) or len(values) != len(assigned):
            raise ValueError("Checkpoint indexed assignment metadata is incomplete.")
        if len(values) > state["frame_index"] or any(type(item) is not bool for item in assigned):
            raise ValueError("Checkpoint source array index is invalid.")
    for key in ("int_e_target_dt", "int_e_dT_dt_dt", "int_e_T_dt"):
        if key not in state["integrals"]:
            raise ValueError("Checkpoint control integral is missing.")
    for name, dim in (("main", 4), ("G", 3), ("E_A", 3), ("k_0", 3), ("n", 3)):
        saved = state["filters"][name]
        if saved is None:
            continue
        if np.asarray(saved["state"]).shape != (dim,):
            raise ValueError("Checkpoint filter state has incompatible dimensions.")
        for key in ("covariance", "sqrt_covariance", "process_noise", "initial_covariance"):
            if np.asarray(saved[key]).shape != (dim, dim):
                raise ValueError("Checkpoint filter matrix has incompatible dimensions.")
        measurement_dim = 2 if name == "main" else 3
        if np.asarray(saved["measurement_noise"]).shape != (measurement_dim, measurement_dim):
            raise ValueError("Checkpoint observation covariance has incompatible dimensions.")
    if state["filters"]["main"] is not None and not {"params", "dt"} <= set(state["filter_bindings"]):
        raise ValueError("Checkpoint filter closure snapshot is missing.")
    from .parameters import parameter_digest
    if state["params_digest"] != parameter_digest(state["configured_params"]):
        raise ValueError("Checkpoint parameter identity does not match its startup snapshot.")
    if parameter_digest(configured) != state["params_digest"]:
        raise ValueError("Checkpoint does not match the supplied startup parameter snapshot.")
