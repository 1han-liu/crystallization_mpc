"""Stable integration boundary for the translated Controller algorithm."""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from typing import Any, Mapping

from crystallization_mpc.apps.controller.result import ControllerStepResult
from crystallization_mpc.apps.controller.process import ProcessState
from crystallization_mpc.messaging.contracts import GrowthRateSamplePayload


class ControllerAdapter(ABC):
    """Small facade around any multi-module Controller implementation."""

    @abstractmethod
    def configure(self, params: Mapping[str, Any], run_id: str) -> None:
        """Apply one experiment's parameter snapshot."""

    @abstractmethod
    def start(self) -> None:
        """Start one configured experiment."""

    @abstractmethod
    def step(
        self,
        sample: GrowthRateSamplePayload,
        process_state: ProcessState | None = None,
    ) -> ControllerStepResult | None:
        """Consume one valid sample and optionally return a real calculation.

        In live-equipment mode, ``process_state`` contains the validated OPC UA
        values ``T``, ``T_j``, ``c``, ``count_middle`` and the current
        ``T_j_set``, using kelvin for temperatures. It is ``None`` when real
        equipment I/O is disabled.

        Returning ``None`` means that the translated algorithm has not
        produced an output for this frame. The service will not persist a
        placeholder record in that case.
        """

    @abstractmethod
    def stop(self) -> None:
        """Stop the current experiment."""

    def add_seed(self, event: Mapping[str, Any]) -> None:
        """Apply or record one operator-confirmed seed-addition event.

        The default is intentionally a no-op. A translated Controller can
        override this hook without having to change the RabbitMQ service.
        """

        return None

    def set_adaptation(
        self,
        enabled: bool,
        mode: str,
        event: Mapping[str, Any] | None = None,
    ) -> None:
        """Enable or disable runtime growth-parameter adaptation.

        ``mode`` identifies the MATLAB-compatible parameter combination. The
        optional event is present for an operator-requested runtime change and
        is ``None`` when applying the experiment's initial configuration.
        """

        return None

    def export_state(self) -> Mapping[str, Any] | None:
        """Return restart-safe algorithm state, or ``None`` if unsupported."""

        return None

    def restore_state(
        self,
        params: Mapping[str, Any],
        run_id: str,
        state: Mapping[str, Any],
    ) -> bool:
        """Restore a running algorithm and return whether recovery succeeded."""

        return False


class NoOpControllerAdapter(ControllerAdapter):
    """Safe default used until the translated Controller is connected."""

    def configure(self, params: Mapping[str, Any], run_id: str) -> None:
        return None

    def start(self) -> None:
        return None

    def step(
        self,
        sample: GrowthRateSamplePayload,
        process_state: ProcessState | None = None,
    ) -> ControllerStepResult | None:
        return None

    def stop(self) -> None:
        return None

    def export_state(self) -> Mapping[str, Any]:
        return {}

    def restore_state(
        self,
        params: Mapping[str, Any],
        run_id: str,
        state: Mapping[str, Any],
    ) -> bool:
        self.configure(params, run_id)
        self.start()
        return True


def load_controller_adapter(spec: str | None) -> ControllerAdapter:
    """Load ``module:Class`` or return the safe no-op adapter."""

    if not spec:
        return NoOpControllerAdapter()

    module_name, separator, attribute_name = spec.partition(":")
    if not separator or not module_name.strip() or not attribute_name.strip():
        raise ValueError("CONTROLLER_ADAPTER must use the format 'module:Class'.")

    module = importlib.import_module(module_name.strip())
    factory = getattr(module, attribute_name.strip())
    adapter = factory()
    if not isinstance(adapter, ControllerAdapter):
        raise TypeError(
            f"Configured Controller adapter {spec!r} must inherit ControllerAdapter."
        )
    return adapter


__all__ = [
    "ControllerAdapter",
    "NoOpControllerAdapter",
    "load_controller_adapter",
]
