import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict


@dataclass(frozen=True)
class TaskPolicy:
    model: str
    max_input_tokens: int
    max_output_tokens: int
    timeout: float
    deadline: float


EXTRACTION = TaskPolicy("gpt-5-nano", 8000, 2000, 30, 90)
COMPARISON = TaskPolicy("gpt-5.6-luna", 16000, 4000, 60, 150)
KNOWLEDGE = TaskPolicy("gpt-6.1-sol", 8000, 3000, 45, 120)
ROUTING = {
    "contribution_extraction": TaskPolicy("gpt-6.1-sol", 8000, 2000, 30, 90),
    "problem_extraction": EXTRACTION,
    "citation_classification": TaskPolicy("gpt-6.1-sol", 8000, 2000, 30, 90),
    "paper_classification": EXTRACTION,
    "evidence_extraction": EXTRACTION,
    "contribution_relevance": COMPARISON,
    "candidate_comparison": COMPARISON,
    "edge_evaluation": COMPARISON,
    "lineage": COMPARISON,
    "lineage_edge_review": TaskPolicy("gpt-6.1-sol", 8000, 2400, 45, 120),
    "lineage_merge_review": TaskPolicy("gpt-6.1-sol", 8000, 2000, 45, 120),
    "reasoning": TaskPolicy("gpt-6.1-sol", 24000, 8000, 90, 180),
    "reasoning_review": TaskPolicy("gpt-6.1-sol", 8000, 2400, 60, 150),
    "contribution_review": TaskPolicy("gpt-6.1-sol", 8000, 4000, 60, 150),
    "concept_extraction": KNOWLEDGE,
    "concept_necessity": KNOWLEDGE,
    "short_knowledge": KNOWLEDGE,
    "knowledge_detail": TaskPolicy("gpt-6.1-terra", 12000, 4000, 60, 150),
    "knowledge_detail_review": TaskPolicy("gpt-6.1-sol", 8000, 1000, 30, 90),
}


class FakeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    fake: Literal[True]
    message: str


@dataclass(frozen=True)
class LLMRequest:
    job_id: str
    paper_id: str
    stage_id: str
    logical_call_id: str
    task_type: str
    model: str
    prompt_hash: str
    input_hash: str
    output_schema: dict[str, Any]
    max_input_tokens: int
    max_output_tokens: int
    attempt_timeout: float
    task_deadline: float
    cost_category: str
    remaining_budget: float
    context_spans: tuple[dict[str, str], ...] = ()
    generation_settings: dict[str, Any] = field(
        default_factory=lambda: {
            "fake_deterministic": True,
        }
    )
    repair_error: str | None = None
    client_request_id: str | None = None


@dataclass(frozen=True)
class ProviderResponse:
    output: Any
    request_id: str
    returned_model: str
    input_tokens: int = 100
    cached_input_tokens: int | None = 0
    output_tokens: int = 20
    reasoning_tokens: int | None = 0


class ProviderFailure(Exception):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
        http_status: int | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.http_status = (
            http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        )


class ProviderTimeout(TimeoutError):
    """Safe transport phase only; never retain HTTP exception messages or requests."""

    def __init__(self, phase: str = "unknown") -> None:
        super().__init__("provider_timeout")
        self.phase = phase if phase in {"connect", "read", "write", "pool"} else "unknown"


class LLMProvider(Protocol):
    async def generate(self, request: LLMRequest) -> ProviderResponse: ...


class FakeLLMProvider:
    """Deterministic scripted responses. No SDK, network or credential access."""

    def __init__(self, outcomes: list[Any] | None = None, delay: float = 0):
        self.outcomes = list(outcomes or [])
        self.delay = delay
        self.calls: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> ProviderResponse:
        self.calls.append(request)
        await asyncio.sleep(self.delay)
        outcome = (
            self.outcomes.pop(0)
            if self.outcomes
            else {
                "fake": True,
                "message": "基盤の動作確認が完了しました。論文解析は未実行です。",
            }
        )
        if isinstance(outcome, Exception):
            raise outcome
        return ProviderResponse(
            output=outcome, request_id=f"fake-{len(self.calls)}", returned_model=request.model
        )
