"""Frozen Phase 0 SQLite schema."""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE papers (
    id VARCHAR NOT NULL, 
    external_ids JSON NOT NULL, 
    PRIMARY KEY (id)
)
""")
    op.execute("""
CREATE TABLE analysis_jobs (
    id VARCHAR NOT NULL, 
    paper_id VARCHAR NOT NULL, 
    arxiv_revision VARCHAR NOT NULL, 
    fingerprint VARCHAR NOT NULL, 
    status VARCHAR NOT NULL, 
    current_stage VARCHAR, 
    feature_status JSON NOT NULL, 
    result JSON, 
    error_code VARCHAR, 
    owner VARCHAR, 
    lease_until FLOAT, 
    heartbeat_at FLOAT, 
    created_at FLOAT NOT NULL, 
    started_at FLOAT, 
    finished_at FLOAT, 
    PRIMARY KEY (id), 
    FOREIGN KEY(paper_id) REFERENCES papers (id)
)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_active_fingerprint ON analysis_jobs (fingerprint)
WHERE status NOT IN ('completed', 'failed', 'partial_completed')
""")
    op.execute("""
CREATE TABLE stage_executions (
    id VARCHAR NOT NULL, 
    job_id VARCHAR NOT NULL, 
    name VARCHAR NOT NULL, 
    status VARCHAR NOT NULL, 
    input_hash VARCHAR NOT NULL, 
    checkpoint JSON, 
    attempt INTEGER NOT NULL, 
    started_at FLOAT NOT NULL, 
    finished_at FLOAT, 
    error_code VARCHAR, 
    PRIMARY KEY (id), 
    UNIQUE (job_id, name), 
    FOREIGN KEY(job_id) REFERENCES analysis_jobs (id)
)
""")
    op.execute("""
CREATE TABLE llm_executions (
    id VARCHAR NOT NULL, 
    job_id VARCHAR NOT NULL, 
    paper_id VARCHAR NOT NULL, 
    stage_id VARCHAR NOT NULL, 
    logical_call_id VARCHAR NOT NULL, 
    attempt INTEGER NOT NULL, 
    status VARCHAR NOT NULL, 
    cost_status VARCHAR NOT NULL, 
    reserved_usd FLOAT NOT NULL, 
    estimated_cost_usd FLOAT, 
    cost_category VARCHAR NOT NULL, 
    metadata_json JSON NOT NULL, 
    output JSON, 
    error_code VARCHAR, 
    started_at FLOAT NOT NULL, 
    finished_at FLOAT, 
    PRIMARY KEY (id), 
    UNIQUE (job_id, logical_call_id, attempt), 
    FOREIGN KEY(job_id) REFERENCES analysis_jobs (id), 
    FOREIGN KEY(paper_id) REFERENCES papers (id), 
    FOREIGN KEY(stage_id) REFERENCES stage_executions (id)
)
""")


def downgrade() -> None:
    op.drop_table("llm_executions")
    op.drop_table("stage_executions")
    op.drop_table("analysis_jobs")
    op.drop_table("papers")
