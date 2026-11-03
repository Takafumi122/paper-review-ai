"""Run migrations, API and the existing DB-backed Worker on one host."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

import uvicorn
from alembic import command
from alembic.config import Config

from paper_review.config import ROOT


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--operator-checks", action="store_true")
    parser.add_argument("--reading-only", action="store_true",
                        help="Generate Reasoning and Knowledge without S2 or Lineage")
    args = parser.parse_args()
    reading_only = args.reading_only or os.environ.get("PAPER_REVIEW_READING_ONLY", "").lower() in {
        "true", "1", "yes", "on"
    }
    os.environ["PAPER_REVIEW_READING_ONLY"] = str(reading_only).lower()
    if not os.environ.get("OPENAI_API_KEY") or (not reading_only and not (
        os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or os.environ.get("S2_API_KEY")
    )):
        parser.error("API environment variables are missing; never paste keys in command arguments")
    if args.operator_checks and args.host not in {"127.0.0.1", "localhost"}:
        parser.error("operator checks require loopback binding")
    if args.host not in {"127.0.0.1", "localhost"} and not os.environ.get(
        "PAPER_REVIEW_ACCESS_TOKEN"
    ):
        parser.error("public binding requires PAPER_REVIEW_ACCESS_TOKEN")
    os.environ["PAPER_REVIEW_PROVIDER"] = "openai"
    os.environ["PAPER_REVIEW_PIPELINE_MODE"] = "full"
    os.environ.setdefault("PAPER_REVIEW_DATABASE_PATH", str(ROOT / ".work/app.db"))
    if args.operator_checks:
        os.environ["PAPER_REVIEW_ENABLE_PROVIDER_CHECKS"] = "1"
    else:
        os.environ.pop("PAPER_REVIEW_ENABLE_PROVIDER_CHECKS", None)
    (ROOT / ".work").mkdir(exist_ok=True)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")
    worker = subprocess.Popen(
        [sys.executable, "-m", "paper_review.worker", "--mode", "full"], cwd=ROOT
    )
    try:
        uvicorn.run("main:app", host=args.host, port=args.port, log_level="warning")
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()


if __name__ == "__main__":
    main()
