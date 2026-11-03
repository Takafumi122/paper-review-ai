from fastapi.testclient import TestClient

from paper_review.api.app import create_app


def create_job(engine, settings):
    with TestClient(create_app(settings, engine)) as client:
        return client.post(
            "/analyses", json={"arxiv_url": "https://arxiv.org/abs/1706.03762"}
        ).json()["job_id"]
