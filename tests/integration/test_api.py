from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from paper_review.api.app import create_app
from paper_review.infrastructure.database.models import AnalysisJob


def test_api(engine, settings):
    with TestClient(create_app(settings, engine)) as client:
        assert client.get("/health").json() == {"status": "ok", "pipeline": "fake"}
        response = client.post("/analyses", json={"arxiv_url": "https://arxiv.org/abs/1706.03762"})
        assert response.status_code == 202
        accepted = response.json()
        assert accepted["paper_id"] == "arxiv:1706.03762"
        status = client.get(f"/analyses/{accepted['job_id']}")
        assert status.status_code == 200
        assert status.json()["status"] == "queued"
        assert status.json()["feature_status"]["lineage"] == "not_implemented"
        assert client.get("/analyses/missing").status_code == 404
        for url in [
            "https://example.org/abs/1706.03762",
            "file:///tmp/paper.pdf",
            "https://arxiv.org.evil/abs/1706.03762",
            "https://arxiv.org/abs/nope",
        ]:
            assert client.post("/analyses", json={"arxiv_url": url}).status_code == 422


def test_concurrent_dedup_and_revisions(engine, settings):
    with TestClient(create_app(settings, engine)) as client:

        def submit(_):
            return client.post(
                "/analyses", json={"arxiv_url": "https://arxiv.org/pdf/1706.03762v1.pdf"}
            ).json()["job_id"]

        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(submit, range(8)))
        assert len(set(ids)) == 1
        other = client.post(
            "/analyses", json={"arxiv_url": "https://arxiv.org/abs/1706.03762v2"}
        ).json()["job_id"]
        assert other != ids[0]
        with Session(engine) as session:
            job = session.get(AnalysisJob, ids[0])
            assert job is not None
            job.status = "completed"
            session.commit()
        assert submit(0) != ids[0]
