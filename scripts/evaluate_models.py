"""Run only in the operator terminal with externally supplied OPENAI_API_KEY.

Prints counts/status only. Validated outputs/usage remain in workspace artifacts.
"""

import argparse
import json

from paper_review.application.model_evaluation import evaluate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["sol", "luna", "terra"])
    args = parser.parse_args()
    result = evaluate(args.phase)
    print(
        json.dumps(
            {
                "status": result["status"],
                "reason": result.get("reason"),
                "cases_finished": len(result.get("cases", [])),
            }
        )
    )


if __name__ == "__main__":
    main()
