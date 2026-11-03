"""Run a bounded diagnostic in the operator's existing API-key environment."""

import json

from paper_review.application.reasoning_review_evaluation import evaluate

if __name__ == "__main__":
    result = evaluate()
    print(
        json.dumps(
            {
                "status": result["status"],
                "reason": result.get("reason"),
                "cases_finished": len(result.get("cases", [])),
                "summary": result.get("summary"),
                "cost": result.get("cost"),
            }
        )
    )
