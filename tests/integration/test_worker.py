import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from paper_review.api.app import create_app
from paper_review.infrastructure.database.models import AnalysisJob, StageExecution
from paper_review.infrastructure.database.session import make_engine
from paper_review.worker import LeaseLost, Worker


def submit(engine, settings):
    with TestClient(create_app(settings, engine)) as client:
        return client.post(
            "/analyses", json={"arxiv_url": "https://arxiv.org/abs/1706.03762"}
        ).json()["job_id"]


def test_round_trip(engine, settings):
    job_id = submit(engine, settings)
    worker = Worker(engine, settings)
    assert worker.run_once() == job_id
    assert worker.run_once() is None
    with TestClient(create_app(settings, engine)) as client:
        data = client.get(f"/analyses/{job_id}").json()
        assert data["status"] == "completed"
        assert data["result"]["fake"] is True
        assert data["timing"]["worker_seconds"] >= 0


def test_restart_and_fencing(engine, settings):
    job_id = submit(engine, settings)
    first = Worker(engine, settings)
    assert first.claim() == job_id
    restarted_engine = make_engine(settings)
    second = Worker(restarted_engine, settings)
    assert second.claim() is None
    with Session(engine) as session:
        job = session.get(AnalysisJob, job_id)
        assert job is not None
        job.lease_until = time.time() - 1
        stage = session.scalar(select(StageExecution).where(StageExecution.job_id == job_id))
        assert stage is not None
        stage.attempt = 2
        stage.checkpoint = {"fake": True, "message": "保存済み"}
        stage.status = "completed"
        session.commit()
    assert second.claim() == job_id
    with pytest.raises(LeaseLost):
        first.process(job_id)
    second.process(job_id)
    with Session(engine) as session:
        stage = session.scalar(select(StageExecution).where(StageExecution.job_id == job_id))
        assert stage is not None
        assert stage.attempt == 2
        assert stage.checkpoint == {"fake": True, "message": "保存済み"}
    restarted_engine.dispose()


def test_atomic_claim(engine, settings):
    job_id = submit(engine, settings)
    workers = [Worker(engine, settings) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = list(pool.map(lambda worker: worker.claim(), workers))
    assert claimed.count(job_id) == 1
    assert claimed.count(None) == 3


def test_failure_preserves_partial_results(engine, settings):
    from paper_review.llm.gateway import LLMGateway
    from paper_review.llm.provider import FakeLLMProvider, ProviderFailure

    job_id = submit(engine, settings)
    with Session(engine) as session:
        job = session.get(AnalysisJob, job_id)
        assert job is not None
        job.result = {"fake": True, "message": "既存の成功結果"}
        job.feature_status = {"lineage": "completed", "reasoning": "failed"}
        session.commit()
    gateway = LLMGateway(engine, settings, FakeLLMProvider([ProviderFailure("refusal")]))
    assert Worker(engine, settings, gateway).run_once() == job_id
    with TestClient(create_app(settings, engine)) as client:
        result = client.get(f"/analyses/{job_id}").json()
        assert result["status"] == "partial_completed"
        assert result["result"]["message"] == "既存の成功結果"
        assert result["feature_status"]["lineage"] == "completed"
        assert result["error_code"] == "refusal"


def test_worker_heartbeat_during_slow_provider(engine, settings, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from paper_review.llm.gateway import LLMGateway
    from paper_review.llm.provider import FakeLLMProvider, LLMRequest, ProviderResponse

    # Advance lease time only at known renewal points. A busy CI runner must not
    # turn a heartbeat test into an unintended scheduler-latency assertion.
    clock = [time.time()]
    started = clock[0]
    controlled_time = SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic)
    monkeypatch.setattr("paper_review.worker.time", controlled_time)
    monkeypatch.setattr("paper_review.llm.gateway.time", controlled_time)
    waiting = asyncio.Event()
    renewed = asyncio.Event()
    renewals: list[float] = []

    class WaitingProvider(FakeLLMProvider):
        async def generate(self, request: LLMRequest) -> ProviderResponse:
            waiting.set()
            await asyncio.wait_for(renewed.wait(), timeout=5)
            return await super().generate(request)

    short = settings.model_copy(update={"lease_seconds": 0.06})
    job_id = submit(engine, short)
    gateway = LLMGateway(engine, short, WaitingProvider())
    worker = Worker(engine, short, gateway)
    contender = Worker(engine, short)
    original_heartbeat = worker.heartbeat

    def heartbeat(current_job_id: str) -> None:
        if waiting.is_set():
            clock[0] += short.lease_seconds / 2
        original_heartbeat(current_job_id)
        if waiting.is_set():
            renewals.append(clock[0])
            assert contender.claim() is None
            if len(renewals) >= 5:
                renewed.set()

    monkeypatch.setattr(worker, "heartbeat", heartbeat)
    assert worker.run_once() == job_id
    assert len(renewals) >= 5
    assert clock[0] - started > 2 * short.lease_seconds
    with Session(engine) as session:
        job = session.get(AnalysisJob, job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.heartbeat_at is not None and job.started_at is not None
        assert job.heartbeat_at > job.started_at + short.lease_seconds


def test_separate_worker_process(engine, settings):
    import subprocess
    import sys

    from paper_review.config import ROOT

    job_id = submit(engine, settings)
    completed = subprocess.run(
        [sys.executable, "-m", "paper_review.worker", "--once"],
        cwd=ROOT,
        env={
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PAPER_REVIEW_DATABASE_PATH": str(settings.database_path),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    with TestClient(create_app(settings, engine)) as client:
        result = client.get(f"/analyses/{job_id}").json()
        assert result["status"] == "completed"
        assert result["cost"]["estimated_usd"] > 0
        assert result["cost"]["simulated"] is True
