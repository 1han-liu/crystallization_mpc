"""Bounded, single-owner AMQP delivery lanes for Central commands."""

from __future__ import annotations

import copy
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable


class DeliveryCancelled(RuntimeError):
    pass


class DeliveryBusy(RuntimeError):
    pass


@dataclass
class Delivery:
    key: str
    run_id: str
    generation: int
    action: Callable
    completed: Callable | None = None
    status: str = "queued"
    error: str | None = None
    result: Any = None
    done: threading.Event = field(default_factory=threading.Event)

    def public(self):
        return {"key": self.key, "run_id": self.run_id, "status": self.status,
                "error": self.error}


class DeliveryLane:
    """Own the publisher's connection on one thread, including reconnect/close.

    Ordinary and stop lanes must be constructed with distinct publishers.
    Cancellation prevents queued work and further envelopes/retries of a job;
    an envelope already handed to the socket cannot be recalled.
    """

    def __init__(self, publisher, name: str, capacity: int = 32):
        self.publisher, self.name, self.capacity = publisher, name, capacity
        self._condition = threading.Condition()
        self._queue = deque()
        self._jobs: OrderedDict[str, Delivery] = OrderedDict()
        self._generations: dict[str, int] = {}
        self._thread = None
        self._closed = False

    def submit(self, key, run_id, action, completed=None):
        with self._condition:
            previous = self._jobs.get(key)
            if previous and not previous.done.is_set():
                return previous
            if self._closed or len(self._queue) >= self.capacity:
                raise DeliveryBusy(f"{self.name} delivery is busy; retry the same request.")
            job = Delivery(key, run_id, self._generations.get(run_id, 0), action, completed)
            self._jobs[key] = job
            self._queue.append(job)
            # Keep pending jobs plus a bounded recent delivery summary.
            for old_key, old in list(self._jobs.items()):
                if len(self._jobs) <= self.capacity * 2:
                    break
                if old.done.is_set():
                    del self._jobs[old_key]
            if not self._thread or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
                self._thread.start()
            self._condition.notify()
            return job

    def cancel_run(self, run_id):
        with self._condition:
            self._generations[run_id] = self._generations.get(run_id, 0) + 1
            self._condition.notify()

    def _check(self, job):
        with self._condition:
            if self._closed or job.generation != self._generations.get(job.run_id, 0):
                raise DeliveryCancelled("Experiment ended before delivery completed.")

    def _run(self):
        try:
            while True:
                with self._condition:
                    if not self._queue and not self._closed:
                        self._condition.wait(0.25)
                    if self._closed and not self._queue:
                        return
                    job = self._queue.popleft() if self._queue else None
                if job is None:
                    # Service heartbeats only on this connection's owner.
                    connection = getattr(self.publisher, "_conn", None)
                    if connection is not None and getattr(connection, "is_open", False):
                        try:
                            connection.process_data_events(time_limit=0)
                        except Exception:
                            self.publisher.close()
                    continue
                try:
                    self._check(job)
                    job.status = "sending"
                    self.publisher._delivery_guard = lambda: self._check(job)
                    job.result = job.action(self.publisher)
                    job.status = "sent"
                except DeliveryCancelled as exc:
                    job.status, job.error = "cancelled", str(exc)
                except Exception as exc:
                    job.status, job.error = "unconfirmed", f"{type(exc).__name__}: {exc}"
                finally:
                    self.publisher._delivery_guard = None
                    if job.completed:
                        try:
                            job.completed(job)
                        except Exception as exc:
                            job.error = f"{job.error or ''} Delivery record failed: {type(exc).__name__}".strip()
                    job.done.set()
        finally:
            close = getattr(self.publisher, "close", None)
            if close:
                close()

    def wait(self, job, timeout=0.1):
        job.done.wait(timeout)
        return job.public(), copy.deepcopy(job.result)

    def status(self):
        with self._condition:
            return [job.public() for job in self._jobs.values()]

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout=1)
