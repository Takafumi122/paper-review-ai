import json
import logging
import time

logger = logging.getLogger("paper_review")


def record(
    event: str,
    *,
    job_id: str,
    stage_id: str | None = None,
    status: str,
    attempt_id: str | None = None,
    error_code: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    estimated_cost_usd: float | None = None,
    retry_count: int = 0,
) -> None:
    """Deliberately narrow fields: no arbitrary payloads or exception strings."""
    logger.info(
        json.dumps(
            {
                "event": event,
                "job_id": job_id,
                "stage_id": stage_id,
                "status": status,
                "attempt_id": attempt_id,
                "error_code": error_code,
                "timestamp": time.time(),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost_usd": estimated_cost_usd,
                "retry_count": retry_count,
            }
        )
    )
