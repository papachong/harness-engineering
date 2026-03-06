from __future__ import annotations

import json
import logging
import time
import urllib.request
import unittest

from symphony.http_server import StatusServer


class _StubOrchestrator:
    def __init__(self) -> None:
        self._snapshot = {
            "generated_at": "2026-03-06T00:00:00Z",
            "counts": {"running": 1, "retrying": 1},
            "provider": {
                "selected": "copilot",
                "kind": "copilot",
                "available": ["copilot", "glm"],
            },
            "running": [
                {
                    "issue_identifier": "RUH-5",
                    "state": "Todo",
                    "session_id": "session-1",
                    "turn_count": 3,
                    "last_event": "session_started",
                    "last_message": "raw output",
                    "provider_summary": "finished lint and tests",
                    "provider": {"name": "copilot", "kind": "copilot"},
                },
                {
                    "issue_identifier": "RUH-7",
                    "state": "In Progress",
                    "session_id": "session-2",
                    "turn_count": 5,
                    "last_event": "message",
                    "last_message": "running checks",
                    "provider_summary": "running checks",
                    "provider": {"name": "glm", "kind": "openai-compatible"},
                },
            ],
            "retrying": [
                {
                    "issue_identifier": "RUH-6",
                    "attempt": 2,
                    "due_at": "2026-03-06T00:01:00Z",
                    "error": "rate limited",
                    "provider_summary": "will retry after backoff",
                }
            ],
            "codex_totals": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "seconds_running": 1.0},
        }

    def snapshot(self) -> dict[str, object]:
        return self._snapshot

    def issue_snapshot(self, issue_identifier: str) -> dict[str, object] | None:
        if issue_identifier == "RUH-5":
            return self._snapshot["running"][0]
        return None


class StatusServerTests(unittest.TestCase):
    def test_dashboard_includes_provider_panel(self) -> None:
        server = StatusServer(_StubOrchestrator(), 0, logging.getLogger("test-http-server"))
        server.start()
        try:
            time.sleep(0.05)
            url = f"http://127.0.0.1:{server._server.server_port}/"
            with urllib.request.urlopen(url, timeout=5) as response:
                html = response.read().decode("utf-8")
            self.assertIn("Latest Provider Output / Errors", html)
            self.assertIn("finished lint and tests", html)
            self.assertIn("will retry after backoff", html)
            self.assertIn("Selected provider", html)
        finally:
            server.stop()

    def test_state_endpoint_exposes_provider_data(self) -> None:
        server = StatusServer(_StubOrchestrator(), 0, logging.getLogger("test-http-server"))
        server.start()
        try:
            time.sleep(0.05)
            url = f"http://127.0.0.1:{server._server.server_port}/api/v1/state"
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["provider"]["selected"], "copilot")
            self.assertEqual(payload["running"][0]["provider_summary"], "finished lint and tests")
        finally:
            server.stop()

    def test_state_endpoint_filters_by_state_and_provider(self) -> None:
        server = StatusServer(_StubOrchestrator(), 0, logging.getLogger("test-http-server"))
        server.start()
        try:
            time.sleep(0.05)
            url = f"http://127.0.0.1:{server._server.server_port}/api/v1/state?state=todo&provider=copilot"
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["counts"]["running"], 1)
            self.assertEqual(payload["counts"]["retrying"], 0)
            self.assertEqual([row["issue_identifier"] for row in payload["running"]], ["RUH-5"])
        finally:
            server.stop()

    def test_state_endpoint_filters_by_status(self) -> None:
        server = StatusServer(_StubOrchestrator(), 0, logging.getLogger("test-http-server"))
        server.start()
        try:
            time.sleep(0.05)
            url = f"http://127.0.0.1:{server._server.server_port}/api/v1/state?status=retrying"
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["counts"]["running"], 0)
            self.assertEqual(payload["counts"]["retrying"], 1)
            self.assertEqual([row["issue_identifier"] for row in payload["retrying"]], ["RUH-6"])
        finally:
            server.stop()

    def test_state_endpoint_q_matches_multiple_terms(self) -> None:
        server = StatusServer(_StubOrchestrator(), 0, logging.getLogger("test-http-server"))
        server.start()
        try:
            time.sleep(0.05)
            url = f"http://127.0.0.1:{server._server.server_port}/api/v1/state?q=ruh-5+lint"
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["counts"]["running"], 1)
            self.assertEqual([row["issue_identifier"] for row in payload["running"]], ["RUH-5"])
        finally:
            server.stop()

    def test_state_endpoint_supports_csv_filters(self) -> None:
        server = StatusServer(_StubOrchestrator(), 0, logging.getLogger("test-http-server"))
        server.start()
        try:
            time.sleep(0.05)
            url = f"http://127.0.0.1:{server._server.server_port}/api/v1/state?state=todo,in%20progress&provider=glm,copilot"
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["counts"]["running"], 2)
            self.assertEqual([row["issue_identifier"] for row in payload["running"]], ["RUH-5", "RUH-7"])
        finally:
            server.stop()

    def test_issue_endpoint_ignores_query_string(self) -> None:
        server = StatusServer(_StubOrchestrator(), 0, logging.getLogger("test-http-server"))
        server.start()
        try:
            time.sleep(0.05)
            url = f"http://127.0.0.1:{server._server.server_port}/api/v1/RUH-5?status=running"
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["issue_identifier"], "RUH-5")
            self.assertEqual(payload["provider_summary"], "finished lint and tests")
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
