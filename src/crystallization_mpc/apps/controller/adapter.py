"""Stable integration boundary for the translated Controller algorithm."""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from typing import Any, Mapping

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
    def step(self, sample: GrowthRateSamplePayload) -> None:
        """Consume one valid growth-rate sample."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the current experiment."""

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

    def step(self, sample: GrowthRateSamplePayload) -> None:
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
