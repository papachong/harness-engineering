from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .http_server import StatusServer
from .logging_utils import configure_logging, log_kv
from .orchestrator import Orchestrator
from .workflow import WorkflowLoader


async def _run(args: argparse.Namespace) -> int:
    logger = configure_logging(logging.INFO)
    orchestrator = Orchestrator(WorkflowLoader(args.workflow_path), logger, port_override=args.port)
    try:
        await orchestrator.start(status_server_factory=StatusServer)
    except KeyboardInterrupt:
        await orchestrator.stop()
        return 0
    except Exception as exc:  # noqa: BLE001
        log_kv(logger, logging.ERROR, "startup_failed", error=str(exc))
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Symphony service")
    parser.add_argument("workflow_path", nargs="?", default=None, help="Path to WORKFLOW.md")
    parser.add_argument("--port", type=int, default=None, help="Optional status server port")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    exit_code = asyncio.run(_run(args))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
