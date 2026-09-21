"""Single-host persistent queue. All pipeline work in this phase is fake."""

import argparse
import asyncio
import logging
import time
from contextlib import suppress
from uuid import uuid4

from sqlalchemy import Engine, or_, select

from paper_review.config import Settings
from paper_review.domain.jobs import TERMINAL, JobStatus, transition
from paper_review.infrastructure.database.models import AnalysisJob, StageExecution
from paper_review.infrastructure.database.session import make_engine, transaction
from paper_review.infrastructure.observability import record
from paper_review.llm.gateway import ExecutionFailure, LLMGateway
from paper_review.llm.provider import FakeLLMProvider, FakeOutput


class LeaseLost(RuntimeError):
    pass


class Worker:
    def __init__(self, engine: Engine, settings: Settings, gateway: LLMGateway | None = None):
        self.engine = engine
        self.settings = settings
        self.owner = str(uuid4())
        self.gateway = gateway or LLMGateway(engine, settings, FakeLLMProvider())

    def claim(self) -> str | None:
        now = time.time()
        with transaction(self.engine) as session:
            # Single active job across worker processes on this host.
            active = session.scalar(
                select(AnalysisJob.id).where(
                    AnalysisJob.status.not_in(TERMINAL),
                    AnalysisJob.owner.is_not(None),
                    AnalysisJob.lease_until > now,
                )
            )
            if active is not None:
                return None
            job = session.scalar(
                select(AnalysisJob)
                .where(
                    AnalysisJob.status.not_in(TERMINAL),
                    or_(AnalysisJob.owner.is_(None), AnalysisJob.lease_until <= now),
                )
                .order_by(AnalysisJob.created_at, AnalysisJob.id)
                .limit(1)
            )
            if job is None:
                return None
            job.owner = self.owner
            job.lease_until = now + self.settings.lease_seconds
            job.heartbeat_at = now
            job.started_at = job.started_at or now
            if job.status == JobStatus.QUEUED:
                job.status = transition(JobStatus.QUEUED, JobStatus.PARSING)
            job.current_stage = "parsing"
            stage = session.scalar(
                select(StageExecution).where(
                    StageExecution.job_id == job.id, StageExecution.name == "parsing"
                )
            )
            if stage is None:
                session.add(
                    StageExecution(job_id=job.id, name="parsing", input_hash=job.fingerprint)
                )
            job_id = job.id
        record("job_claimed", job_id=job_id, status="parsing")
        return job_id

    def heartbeat(self, job_id: str) -> None:
        with transaction(self.engine) as session:
            job = session.get(AnalysisJob, job_id)
            self.assert_owned(job)
            assert job is not None
            now = time.time()
            job.heartbeat_at = now
            job.lease_until = now + self.settings.lease_seconds

    def assert_owned(self, job: AnalysisJob | None) -> None:
        if (
            job is None
            or job.owner != self.owner
            or job.status in TERMINAL
            or job.lease_until is None
            or job.lease_until <= time.time()
        ):
            raise LeaseLost("worker lease lost")

    async def keep_lease(self, job_id: str) -> None:
        while True:
            await asyncio.sleep(self.settings.lease_seconds / 3)
            self.heartbeat(job_id)

    async def generate(self, job_id: str, stage_id: str) -> FakeOutput:
        heartbeat = asyncio.create_task(self.keep_lease(job_id))
        try:
            result = await self.gateway.execute(
                job_id=job_id,
                stage_id=stage_id,
                logical_call_id="fake-readiness-v1",
                task_type="contribution_extraction",
                output_schema=FakeOutput,
                owner=self.owner,
            )
            if heartbeat.done():
                heartbeat.result()
            return result
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    def process(self, job_id: str) -> None:
        self.heartbeat(job_id)
        with transaction(self.engine) as session:
            job = session.get(AnalysisJob, job_id)
            self.assert_owned(job)
            stage = session.scalar(
                select(StageExecution).where(
                    StageExecution.job_id == job_id, StageExecution.name == "parsing"
                )
            )
            assert stage is not None
            stage_id = stage.id
            checkpoint = stage.checkpoint
        error = None
        if checkpoint is None:
            try:
                checkpoint = asyncio.run(self.generate(job_id, stage_id)).model_dump()
            except ExecutionFailure as exc:
                error = str(exc)
        with transaction(self.engine) as session:
            job = session.get(AnalysisJob, job_id)
            self.assert_owned(job)
            assert job is not None
            stage = session.get(StageExecution, stage_id)
            assert stage is not None
            if stage.checkpoint is None:
                stage.checkpoint = checkpoint
                stage.attempt += 1
                stage.status = "failed" if error else "completed"
                stage.error_code = error
                stage.finished_at = time.time()
            if stage.checkpoint is not None:
                job.result = stage.checkpoint
            target = (
                JobStatus.PARTIAL
                if error and job.result
                else JobStatus.FAILED
                if error
                else JobStatus.COMPLETED
            )
            job.status = transition(JobStatus(job.status), target, fake=True)
            job.error_code = error
            job.finished_at = time.time()
            job.owner = None
            job.lease_until = None
            status = job.status
        record("job_finished", job_id=job_id, status=status, error_code=error)

    def run_once(self) -> str | None:
        job_id = self.claim()
        if job_id is not None:
            self.process(job_id)
        return job_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Phase 0 fake worker")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_environment()
    engine = make_engine(settings)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    worker = Worker(engine, settings)
    try:
        if args.once:
            worker.run_once()
        else:
            while True:
                if worker.run_once() is None:
                    time.sleep(settings.poll_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
