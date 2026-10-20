"""Test-only GSensor protocol producer for an independent Prosys process model.

This service never loads images or runs the real GSensor filters. The four edge
fields carry the same model-generated observation for wire compatibility, and
every persisted record explicitly identifies this fact. Do not deploy alongside
the real GSensor on the same RabbitMQ queue or experiment root.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any, Callable, Mapping
from urllib.request import urlopen

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from crystallization_mpc.apps.gsensor.experiments import GsensorExperimentManager
from crystallization_mpc.apps.gsensor.status_publisher import RabbitStatusPublisher
from crystallization_mpc.apps.gsensor.telemetry import GsensorMeasurementRecord
from crystallization_mpc.experiments.models import TERMINAL_EXPERIMENT_STATUSES
from crystallization_mpc.infra.influxdb.durable import (
    DurableInfluxWriter, mark_finalized, telemetry_path,
)
from crystallization_mpc.infra.rabbitmq.connection import connect
from crystallization_mpc.infra.rabbitmq.topology import declare_exchange, declare_queue
from crystallization_mpc.messaging.codecs import decode_json
from crystallization_mpc.messaging.commands import (
    EXPERIMENT_SELECT_COMMAND, EXPERIMENT_START_COMMAND, EXPERIMENT_STOP_COMMAND,
    GSENSOR_DISABLE_COMMAND, GSENSOR_ENABLE_COMMAND, GROWTH_RATE_COMPLETED_MESSAGE,
    GROWTH_RATE_SAMPLE_MESSAGE, GROWTH_RATE_STATUS_MESSAGE,
)
from crystallization_mpc.messaging.contracts import (
    ExperimentStartPayload, ExperimentStopPayload, GsensorActivationPayload,
    GrowthRateSamplePayload, GrowthRateStatus, GrowthRateStatusPayload,
)
from crystallization_mpc.messaging.idgen import next_seq
from crystallization_mpc.messaging.routing import EXCHANGE, QUEUES, bindings_for, route
from crystallization_mpc.messaging.schema import build_envelope

LOG = logging.getLogger(__name__)
SOURCE = "Synthetic G / Model-generated input"
STATE_FILE = "synthetic_gsensor_state.json"


def _timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include its timezone.")
    return parsed.timestamp()


class SyntheticGSensor:
    """One serialized state machine; injected I/O makes transport tests local."""

    def __init__(self, *, root: str | Path, plant_status: Callable[[], Mapping[str, Any]],
                 publish: Callable[[str, Mapping[str, Any]], None], writer: Any,
                 clock: Callable[[], float] = time.time, sample_interval_s: float = 15,
                 warmup_s: float = 120, plant_max_age_s: float = 5) -> None:
        if (not all(math.isfinite(v) for v in (sample_interval_s, warmup_s, plant_max_age_s))
                or sample_interval_s <= 0 or warmup_s < 0 or plant_max_age_s <= 0):
            raise ValueError("Invalid synthetic G timing configuration.")
        self.root = Path(root)
        self.experiments = GsensorExperimentManager(root)
        self.plant_status = plant_status
        self.publish = publish
        self.writer = writer
        self.clock = clock
        self.interval = sample_interval_s
        self.warmup_s = warmup_s
        self.max_age = plant_max_age_s
        self.lock = threading.RLock()
        self.selection = self.experiments.current()
        self._reset()
        if self.selection:
            path = self._state_path()
            if path.is_file():
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("run_id") != self.selection["run_id"] or saved.get("source") != SOURCE:
                    raise ValueError("Synthetic G recovery identity mismatch.")
                for key in ("started_at", "parameter_version", "revision", "event_id",
                            "last_command_enabled", "frame_seq", "plant_seq", "generation",
                            "write_failures", "publish_failures", "valid_count"):
                    setattr(self, key, saved[key])
                self.lifecycle = saved["lifecycle"]
                if self.started_at and self.lifecycle not in {"completed", "stopped"}:
                    self.lifecycle = "error"
                    self.last_error = "Synthetic G service restarted; end this run before starting a new run."
                    self.recovery_required = True
                    self._restart_notice_pending = {"controller", "central"}
                    self._persist()
            else:
                self.lifecycle = "selected"

    def _reset(self) -> None:
        self.started_at = None
        self.parameter_version = None
        self.lifecycle = "not_started"
        self.enabled = False
        self.ready = False
        self.revision = 0
        self.event_id = None
        self.last_command_enabled = None
        self.frame_seq = 0
        self.plant_seq = 0
        self.generation = 0
        self.warmup_until = None
        self.enabled_at = None
        self.last_emit_at = None
        self.last_captured_at = None
        self.last_error = None
        self.command_error = None
        self.last_sample = None
        self.write_failures = 0
        self.publish_failures = 0
        self.valid_count = 0
        self.recovery_required = False
        self._restart_notice_pending: set[str] = set()

    def _stamp(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="microseconds")

    def _state_path(self) -> Path:
        return self.experiments.registry.run_dir(self.selection["run_id"]) / STATE_FILE

    def _persist(self) -> None:
        if not self.selection:
            return
        document = {key: getattr(self, key) for key in (
            "started_at", "parameter_version", "revision", "event_id", "last_command_enabled",
            "frame_seq", "plant_seq", "generation", "lifecycle", "write_failures",
            "publish_failures", "valid_count",
        )}
        document.update(source=SOURCE, run_id=self.selection["run_id"], schema_version=1,
                        enabled=self.enabled, measurement_ready=self.ready, updated_at=self._stamp())
        path = self._state_path()
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(document, stream, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _send(self, dst: str, name: str, payload: dict[str, Any], *, kind: str = "status") -> None:
        message = build_envelope(src="gsensor", dst=dst, msg_type=kind, name=name,
                                 seq=next_seq(), payload={**payload, "source": SOURCE, "synthetic": True})
        self.publish(route("gsensor", dst), message)

    def _status_payload(self) -> dict[str, Any]:
        return GrowthRateStatusPayload(
            run_id=self.selection["run_id"], status=GrowthRateStatus(self.lifecycle),
            occurred_at=self._stamp(), frame_seq=self.frame_seq, image_name=None,
            error=self.last_error if self.lifecycle == "error" else None,
            enabled=self.enabled, control_revision=self.revision, control_event_id=self.event_id,
            experiment_started_at=self.started_at, initialization_generation=self.generation,
            alignment_revision=0, measurement_ready=self.ready,
        ).to_dict()

    def _status(self, *, completed: bool = False) -> None:
        name = GROWTH_RATE_COMPLETED_MESSAGE if completed else GROWTH_RATE_STATUS_MESSAGE
        payload = self._status_payload()
        # Controller availability precedes every measurement on this same channel.
        self._send("controller", name, payload)
        self._send("central", name, payload)

    def _publish_restart_notice(self) -> None:
        """Advertise a restored terminal state, never resume the old stream.

        A successful publish call is not a consumer acknowledgement. Ambiguous
        transport failures can redeliver the same terminal state; Controller's
        existing identity/order checks remain authoritative. Destinations whose
        calls returned successfully are not repeatedly notified by this process.
        """
        for destination in ("controller", "central"):
            if destination not in self._restart_notice_pending:
                continue
            payload = {**self._status_payload(), "requires_new_run": True,
                       "restart_notification": True}
            try:
                self._send(destination, GROWTH_RATE_STATUS_MESSAGE, payload)
            except Exception:
                self.publish_failures += 1
                self._persist()
                raise
            self._restart_notice_pending.remove(destination)

    def handle(self, message: Mapping[str, Any]) -> None:
        """Handle a Central command, rejecting replay/conflict without reinitializing."""
        try:
            self._handle(message)
        except ValueError:
            raise  # A rejected request must not disrupt the accepted configuration.
        except Exception as exc:
            # Persistence/transport failures cannot leave a partially accepted
            # activation producing observations in the background.
            with self.lock:
                self.enabled = self.ready = False
                self.lifecycle = "error"
                self.last_error = f"Synthetic G command failed closed: {type(exc).__name__}"
                self.recovery_required = True
            raise

    def _handle(self, message: Mapping[str, Any]) -> None:
        with self.lock:
            if message.get("msg_type") != "command":
                return  # Parameter broadcasts belong to the production services.
            name = message.get("name")
            if name not in {EXPERIMENT_SELECT_COMMAND, EXPERIMENT_START_COMMAND,
                            EXPERIMENT_STOP_COMMAND, GSENSOR_ENABLE_COMMAND, GSENSOR_DISABLE_COMMAND}:
                return
            if message.get("src") != "central" or message.get("dst") != "gsensor":
                raise ValueError("Lifecycle commands must be central -> gsensor.")
            payload = message.get("payload", {})
            if name == EXPERIMENT_SELECT_COMMAND:
                selection, changed = self.experiments.select(
                    payload, running=bool(self.started_at and self.lifecycle not in {"completed", "error"}))
                self.selection = selection
                if changed:
                    self._reset()
                    self.lifecycle = "selected"
                    self._persist()
                return
            if not self.selection or payload.get("run_id") != self.selection["run_id"]:
                raise ValueError("Command run_id is not the selected experiment.")
            if name == EXPERIMENT_START_COMMAND:
                command = ExperimentStartPayload.from_mapping(payload)
                manifest = self.experiments.registry.get(command.run_id)
                if manifest.status in TERMINAL_EXPERIMENT_STATUSES:
                    raise ValueError("A finished registry experiment cannot be restarted.")
                if manifest.started_at is not None and manifest.started_at != command.started_at:
                    raise ValueError("Start timestamp does not match the experiment registry.")
                if self.started_at:
                    if (command.started_at != self.started_at
                            or command.parameter_version != self.parameter_version
                            or self.lifecycle in {"completed", "stopped", "error"}):
                        raise ValueError("A started or finished run cannot be replaced/restarted.")
                    self._status()
                    return
                self.started_at = command.started_at
                self.parameter_version = command.parameter_version
                self.lifecycle = "disabled"
                self._persist()
                self._status()
            elif name == EXPERIMENT_STOP_COMMAND:
                ExperimentStopPayload.from_mapping(payload)
                if not self.started_at:
                    raise ValueError("Cannot stop a run that has not started.")
                if "experiment_started_at" in payload:
                    try:
                        matching_start = _timestamp(payload["experiment_started_at"]) == _timestamp(self.started_at)
                    except (AttributeError, TypeError, ValueError):
                        matching_start = False
                    if not matching_start:
                        raise ValueError("Stop timestamp does not match the current experiment.")
                self.enabled = self.ready = False
                self._restart_notice_pending.clear()
                self.lifecycle = "stopped"
                self._persist()
                self._status()
                mark_finalized(self.root, self.selection["run_id"], "gsensor", self.frame_seq,
                               enqueue_failures=self.write_failures, persistence_enabled=self.writer is not None)
                self.lifecycle = "completed"
                self._persist()
                self._status(completed=True)
            else:
                command = GsensorActivationPayload.from_mapping(payload)
                if command.experiment_started_at != self.started_at or self.lifecycle in {
                    "not_started", "selected", "stopped", "completed", "error",
                }:
                    raise ValueError("Activation does not match an active experiment.")
                enabled = name == GSENSOR_ENABLE_COMMAND
                if command.revision < self.revision:
                    raise ValueError("Stale activation revision.")
                if command.revision == self.revision:
                    if command.event_id != self.event_id or enabled != self.last_command_enabled:
                        raise ValueError("Conflicting activation replay.")
                    self._status()
                    return
                if command.event_id == self.event_id:
                    raise ValueError("A new revision requires a new event_id.")
                self.revision, self.event_id = command.revision, command.event_id
                self.last_command_enabled = enabled
                self.enabled, self.ready = enabled, False
                if enabled:
                    self.enabled_at = self.clock()
                    if self.warmup_until is None:
                        self.generation = 1
                        self.warmup_until = self.clock() + self.warmup_s
                        self.lifecycle = "waiting_for_initial_image"
                        self._persist()
                        self._status()
                    self.lifecycle = "initializing" if self.clock() < self.warmup_until else "measuring"
                else:
                    self.lifecycle = "disabled"
                self._persist()
                self._status()
            self.command_error = None

    def _validate_plant(self, plant: Mapping[str, Any]) -> tuple[int, str, float, float]:
        if plant.get("source") != SOURCE or plant.get("running") is not True:
            raise ValueError("Independent synthetic plant is not running.")
        if (plant.get("run_id") != self.selection["run_id"]
                or _timestamp(str(plant.get("experiment_started_at"))) != _timestamp(str(self.started_at))):
            raise ValueError("Independent plant experiment identity mismatch.")
        heartbeat_age = self.clock() - _timestamp(str(plant["heartbeat_at"]))
        sampled_at = str(plant["observed_at"])
        sample_age = self.clock() - _timestamp(sampled_at)
        if not (-1 <= heartbeat_age <= self.max_age and -1 <= sample_age <= 2 * self.interval):
            raise ValueError("Independent plant heartbeat or G observation is stale/future-dated.")
        sequence = plant["sample_seq"]
        if type(sequence) is not int or sequence < 1:
            raise ValueError("Plant sample_seq must be a positive integer.")
        measured, truth = float(plant["G_measured_m_s"]), float(plant["G_true_m_s"])
        if not math.isfinite(measured) or not math.isfinite(truth):
            raise ValueError("Independent plant G is not finite.")
        return sequence, sampled_at, measured, truth

    def poll(self) -> dict[str, Any] | None:
        """Poll the model without publishing reused, pre-enable or stale observations."""
        with self.lock:
            self._publish_restart_notice()
            if not self.enabled or self.lifecycle in {"error", "completed", "stopped"}:
                return None
            try:
                sequence, captured, measured, truth = self._validate_plant(self.plant_status())
            except Exception as exc:
                self.ready = False
                self.last_error = f"Independent plant unavailable: {type(exc).__name__}: {exc}"
                self._status()  # Invalidate Controller's previously cached G.
                return None
            if self.clock() < self.warmup_until:
                return None
            if sequence < self.plant_seq:
                self.ready = False
                self.last_error = "Independent plant sequence regressed; cached input is unavailable."
                self._status()
                return None
            if (sequence <= self.plant_seq or _timestamp(captured) <= self.enabled_at
                    or (self.last_emit_at is not None and self.clock() - self.last_emit_at < self.interval)):
                return None
            # Publish a ready transition at the previous frame; the next sample
            # must lie strictly beyond Controller's transition floor.
            self.lifecycle = "measuring"
            self.ready = True
            self.last_error = None
            self._status()
            processed = self._stamp()
            interval = (_timestamp(captured) - _timestamp(self.last_captured_at)
                        if self.last_captured_at else self.interval)
            if interval <= 0:
                self.ready = False
                self.last_error = "Plant capture time did not advance."
                self._status()
                return None
            sample = GrowthRateSamplePayload(
                run_id=self.selection["run_id"], frame_seq=self.frame_seq + 1,
                image_name=f"synthetic-model-sample-{sequence}", captured_at=captured,
                processed_at=processed, dt_s=interval, valid=True, status="measured",
                G_u=measured, G_u_KF=measured, G_v=measured, G_v_KF=measured,
                experiment_started_at=self.started_at, control_revision=self.revision,
                initialization_generation=self.generation, alignment_revision=0,
            )
            record = GsensorMeasurementRecord(
                run_id=sample.run_id, frame_seq=sample.frame_seq, image_name=sample.image_name,
                captured_at=captured, processed_at=processed, dt_s=interval, valid=True,
                G_u=measured, G_u_KF=measured, G_v=measured, G_v_KF=measured,
            )
            try:
                if self.writer is None:
                    raise RuntimeError("Synthetic G requires a durable telemetry writer.")
                self.writer.write_tagged_fields(
                    {**record.fields(), "synthetic": True, "source": SOURCE,
                     "edge_filtering": "not_applied_synthetic", "plant_sample_seq": sequence,
                     "G_true": truth}, tags=record.tags(), measurement="gsensor_measurement",
                    timestamp=record.timestamp(),
                )
            except Exception:
                self.write_failures += 1
                self.ready = False
                self._persist()
                self._status()
                raise
            self.frame_seq, self.plant_seq = sample.frame_seq, sequence
            self.last_emit_at, self.last_captured_at = self.clock(), captured
            self.last_sample = {**sample.to_dict(), "source": SOURCE, "synthetic": True}
            self.valid_count += 1
            self._persist()
            try:
                self._send("controller", GROWTH_RATE_SAMPLE_MESSAGE, self.last_sample, kind="measurement")
            except Exception:
                self.publish_failures += 1
                self._persist()
                raise
            return self.last_sample

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "role": "gsensor", "source": SOURCE, "synthetic": True,
                "camera_processing": "NOT RUN", "edge_filtering": "not_applied_synthetic",
                "timing": {"sample_interval_s": self.interval, "warmup_s": self.warmup_s},
                "active": self.enabled, "initialized": self.generation > 0,
                "initialization_status": "synthetic_ready" if self.ready else "synthetic_waiting",
                "measurement_running": self.enabled, "measurement_step_count": self.frame_seq,
                "measurement_ready": self.ready, "initialization_generation": self.generation,
                "alignment_revision": 0, "current_run_id": (self.selection or {}).get("run_id"),
                "experiment": self.selection, "experiment_started_at": self.started_at,
                "experiment_parameter_version": self.parameter_version,
                "experiment_lifecycle_status": self.lifecycle,
                "experiment_lifecycle_error": self.last_error,
                "experiment_selection_status": "selected" if self.selection else "not_selected",
                "last_lifecycle_command_error": self.command_error,
                "gsensor_activation": {"enabled": self.enabled, "control_revision": self.revision,
                    "control_event_id": self.event_id, "last_command_enabled": self.last_command_enabled,
                    "last_command_error": self.command_error},
                "growth_rate_processing": {"latest_result": self.last_sample, "last_error": self.last_error,
                    "valid_frame_count": self.valid_count, "invalid_frame_count": 0,
                    "latest_overlay_path": None, "final_overlay_path": None},
                "sample_publishing": {"enabled": True, "last_message": self.last_sample,
                    "failure_count": self.publish_failures},
                "influx_persistence": {"enabled": self.writer is not None,
                    "failure_count": self.write_failures,
                    "archive": self.writer.status((self.selection or {}).get("run_id"))
                        if self.writer is not None and hasattr(self.writer, "status") else None},
                "recovery": {"requires_new_run": self.recovery_required,
                    "restart_notification_pending": sorted(self._restart_notice_pending)},
            }


class SyntheticRuntime:
    """Rabbit consumer and serialized model/publisher worker with clean shutdown."""

    def __init__(self) -> None:
        if os.getenv("PROSYS_TEST_MODE") != "1":
            raise RuntimeError("Synthetic GSensor requires explicit PROSYS_TEST_MODE=1 isolation.")
        self.url = os.environ["RABBIT_URL"]
        root = os.environ["EXPERIMENT_ROOT"]
        plant_url = os.environ.get("PROSYS_PLANT_URL", "http://plant:8010").rstrip("/")
        def fetch() -> Mapping[str, Any]:
            with urlopen(plant_url + "/status", timeout=2) as response:
                return json.load(response)
        self.publishers = {dst: RabbitStatusPublisher(url=self.url, exchange=EXCHANGE,
            destination_queue=QUEUES[dst], destination_role=dst) for dst in ("central", "controller")}
        self.writer = DurableInfluxWriter(path=telemetry_path("gsensor", root))
        self.service = SyntheticGSensor(root=root, plant_status=fetch,
            publish=lambda key, msg: self.publishers[msg["dst"]].publish(key, msg), writer=self.writer,
            sample_interval_s=float(os.getenv("PROSYS_G_SAMPLE_INTERVAL_S", "15")),
            warmup_s=float(os.getenv("PROSYS_G_WARMUP_S", "120")),
            plant_max_age_s=float(os.getenv("PROSYS_PLANT_MAX_AGE_S", "5")))
        self.stop = threading.Event()
        self.pending: queue.Queue[tuple[dict[str, Any], threading.Event]] = queue.Queue()
        self.connected = False
        self.threads = [threading.Thread(target=self._consume, daemon=True, name="synthetic-g-consumer"),
                        threading.Thread(target=self._work, daemon=True, name="synthetic-g-worker")]

    def _consume(self) -> None:
        while not self.stop.is_set():
            connection = None
            try:
                connection, channel = connect(self.url)
                declare_exchange(channel, EXCHANGE)
                declare_queue(channel, QUEUES["gsensor"], bindings_for("gsensor"), EXCHANGE)
                channel.basic_qos(prefetch_count=1)
                def receive(ch, method, properties, body):
                    done = threading.Event()
                    try:
                        message = decode_json(body)
                    except Exception:
                        LOG.warning("Rejected invalid synthetic G command encoding.")
                        ch.basic_ack(method.delivery_tag)
                        return
                    self.pending.put((message, done))
                    while not done.wait(0.1):
                        if self.stop.is_set():
                            return  # Unacked delivery is requeued by closing the channel.
                    ch.basic_ack(method.delivery_tag)
                channel.basic_consume(QUEUES["gsensor"], receive, auto_ack=False)
                self.connected = True
                while not self.stop.is_set():
                    connection.process_data_events(time_limit=1)
            except Exception:
                LOG.exception("Synthetic G command transport disconnected.")
            finally:
                self.connected = False
                if connection and connection.is_open:
                    connection.close()
            self.stop.wait(2)

    def _work(self) -> None:
        try:
            next_poll = 0.0
            while not self.stop.is_set():
                try:
                    message, done = self.pending.get(timeout=0.2)
                except queue.Empty:
                    pass
                else:
                    try:
                        self.service.handle(message)
                    except Exception as exc:
                        self.service.command_error = f"{type(exc).__name__}: {exc}"
                        LOG.warning("Synthetic G command rejected: %s", self.service.command_error)
                    finally:
                        done.set()
                if time.monotonic() >= next_poll:
                    try:
                        self.service.poll()
                    except Exception:
                        LOG.exception("Synthetic G observation publication failed.")
                    next_poll = time.monotonic() + 1
        finally:
            for publisher in self.publishers.values():
                publisher.close()

    def start(self) -> None:
        for thread in self.threads:
            thread.start()

    def close(self) -> None:
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=10)
        self.writer.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = SyntheticRuntime()
    app.state.runtime = runtime
    runtime.start()
    try:
        yield
    finally:
        runtime.close()


app = FastAPI(title=SOURCE, lifespan=lifespan)


@app.get("/api/status")
@app.get("/status")
def status() -> dict[str, Any]:
    value = app.state.runtime.service.status()
    value["transport_connected"] = app.state.runtime.connected
    return value


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "source": SOURCE, "synthetic": True,
            "transport_connected": app.state.runtime.connected}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return ("<h1>Synthetic G / Model-generated input</h1><p>Prosys integration test service. "
            "No camera processing or real GSensor edge filtering is performed.</p>"
            '<p><a href="/status">Live protocol and archive status</a></p>')
