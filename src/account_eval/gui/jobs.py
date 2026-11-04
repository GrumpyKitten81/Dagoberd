"""In-process jobs and browser TAN bridge."""

from __future__ import annotations

import base64
import threading
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from account_eval.providers.comdirect import ComdirectAuthenticationError

OPEN_JOB_STATES = frozenset({"running", "awaiting_tan"})


@dataclass(slots=True)
class Job:
    status: str = "running"
    result: dict[str, Any] | None = None
    error: str | None = None
    challenge: dict[str, str | None] | None = None
    event: threading.Event = field(default_factory=threading.Event)
    tan: str | None = None

    def finish(self, result: dict[str, Any]) -> None:
        self.result = result
        self.challenge = None
        self.status = "succeeded"

    def fail(self, message: str) -> None:
        self.error = message
        self.challenge = None
        self.status = "failed"

    def settle(self) -> None:
        """DESIGN: releases the registry slot even when a worker dies unexpectedly."""
        if self.status in OPEN_JOB_STATES:
            self.fail(self.error or "Sync ended unexpectedly")


class JobRegistry:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def create(self) -> tuple[str, Job]:
        with self.lock:
            if any(job.status in OPEN_JOB_STATES for job in self.jobs.values()):
                raise ValueError("A Comdirect sync job is already running")
            identifier = str(uuid4())
            job = Job()
            self.jobs[identifier] = job
            return identifier, job

    def get(self, identifier: str) -> Job:
        with self.lock:
            try:
                return self.jobs[identifier]
            except KeyError as error:
                raise KeyError(f"Unknown job {identifier}") from error

    def status(self, identifier: str) -> dict[str, Any]:
        job = self.get(identifier)
        return {
            "job_id": identifier,
            "status": job.status,
            "result": job.result,
            "error": job.error,
            "challenge": job.challenge,
        }


class WebChallengeHandler:
    def __init__(self, job: Job) -> None:
        self.job = job

    def push_tan(self) -> str:
        return self._wait("P_TAN_PUSH")

    def photo_tan(self, challenge: bytes) -> str:
        return self._wait("P_TAN", base64.b64encode(challenge).decode())

    def sms_tan(self) -> str:
        return self._wait("M_TAN")

    def _wait(self, kind: str, image_base64: str | None = None) -> str:
        self.job.challenge = {"kind": kind, "image_base64": image_base64}
        self.job.status = "awaiting_tan"
        if not self.job.event.wait(timeout=300):
            raise ComdirectAuthenticationError("TAN input expired")
        if not self.job.tan:
            raise ComdirectAuthenticationError("TAN input missing")
        self.job.status = "running"
        self.job.challenge = None
        return self.job.tan
