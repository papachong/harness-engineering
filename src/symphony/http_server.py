from __future__ import annotations

import asyncio
import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit


class StatusServer:
    def __init__(self, orchestrator: Any, port: int, logger: logging.Logger):
        self.orchestrator = orchestrator
        self.port = port
        self.logger = logger
        self._server = ThreadingHTTPServer(("127.0.0.1", port), self._handler_factory())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _handler_factory(self):
        orchestrator = self.orchestrator

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                split = urlsplit(self.path)
                path = split.path
                query_params = parse_qs(split.query)
                if path == "/":
                    return self._write_html(orchestrator.snapshot())
                if path == "/api/v1/state":
                    return self._write_json(_filter_state_snapshot(orchestrator.snapshot(), query_params))
                if path.startswith("/api/v1/"):
                    issue_identifier = path.split("/api/v1/", 1)[1]
                    payload = orchestrator.issue_snapshot(issue_identifier)
                    if payload is None:
                        return self._write_json({"error": {"code": "issue_not_found", "message": issue_identifier}}, status=HTTPStatus.NOT_FOUND)
                    return self._write_json(payload)
                return self._write_json({"error": {"code": "not_found", "message": path}}, status=HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/api/v1/refresh":
                    return self._write_json({"error": {"code": "not_found", "message": self.path}}, status=HTTPStatus.NOT_FOUND)
                asyncio.run(orchestrator.request_refresh())
                return self._write_json({"queued": True, "coalesced": False, "requested_at": orchestrator.snapshot()["generated_at"], "operations": ["poll", "reconcile"]}, status=HTTPStatus.ACCEPTED)

            def do_PUT(self) -> None:  # noqa: N802
                self._write_json({"error": {"code": "method_not_allowed", "message": "Use GET or POST"}}, status=HTTPStatus.METHOD_NOT_ALLOWED)

            def do_DELETE(self) -> None:  # noqa: N802
                self._write_json({"error": {"code": "method_not_allowed", "message": "Use GET or POST"}}, status=HTTPStatus.METHOD_NOT_ALLOWED)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                return

            def _write_html(self, snapshot: dict[str, Any]) -> None:
                provider_cards = []
                for row in snapshot.get("running", []):
                    summary = (row.get("provider_summary") or row.get("last_message") or "").strip()
                    provider_cards.append(
                        f"<tr><td>{row['issue_identifier']}</td><td>{row.get('provider', {}).get('name', '')}</td><td>running</td><td>{summary}</td></tr>"
                    )
                for row in snapshot.get("retrying", []):
                    summary = (row.get("provider_summary") or row.get("error") or "").strip()
                    provider_cards.append(
                        f"<tr><td>{row['issue_identifier']}</td><td>{snapshot.get('provider', {}).get('selected', '')}</td><td>retrying</td><td>{summary}</td></tr>"
                    )
                if not provider_cards:
                    provider_cards.append("<tr><td colspan='4'>No provider output available.</td></tr>")

                body = f"""
                <html><head><title>Symphony</title><style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 2rem; }}
                table {{ border-collapse: collapse; width: 100%; }}
                td, th {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
                code {{ background: #f5f5f5; padding: 0.15rem 0.3rem; border-radius: 4px; }}
                .panel {{ margin-top: 1.5rem; padding: 1rem; border: 1px solid #ddd; border-radius: 8px; background: #fafafa; }}
                .muted {{ color: #666; }}
                </style></head><body>
                <h1>Symphony</h1>
                <p>Running: {snapshot['counts']['running']} | Retrying: {snapshot['counts']['retrying']}</p>
                <p>Selected provider: <strong>{snapshot.get('provider', {}).get('selected', '')}</strong> ({snapshot.get('provider', {}).get('kind', '')})</p>
                <p>Available providers: {', '.join(snapshot.get('provider', {}).get('available', []))}</p>
                <h2>Running Sessions</h2>
                <table><tr><th>Issue</th><th>Provider</th><th>State</th><th>Session</th><th>Turn Count</th><th>Last Event</th><th>Summary</th></tr>
                {''.join(f"<tr><td>{row['issue_identifier']}</td><td>{row.get('provider', {}).get('name', '')}</td><td>{row['state']}</td><td>{row['session_id'] or ''}</td><td>{row['turn_count']}</td><td>{row['last_event'] or ''}</td><td>{row.get('provider_summary', '') or ''}</td></tr>" for row in snapshot['running'])}
                </table>
                <h2>Retry Queue</h2>
                <table><tr><th>Issue</th><th>Attempt</th><th>Due At</th><th>Error</th><th>Summary</th></tr>
                {''.join(f"<tr><td>{row['issue_identifier']}</td><td>{row['attempt']}</td><td>{row['due_at']}</td><td>{row['error'] or ''}</td><td>{row.get('provider_summary', '') or ''}</td></tr>" for row in snapshot['retrying'])}
                </table>
                                <div class="panel">
                                    <h2>Latest Provider Output / Errors</h2>
                                    <p class="muted">Shows the most recent provider summary currently tracked for running or retrying issues.</p>
                                    <table><tr><th>Issue</th><th>Provider</th><th>Status</th><th>Summary</th></tr>
                                    {''.join(provider_cards)}
                                    </table>
                                </div>
                <pre>{json.dumps(snapshot['codex_totals'], ensure_ascii=False, indent=2)}</pre>
                </body></html>
                """
                encoded = body.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _write_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
                encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        return Handler


def _split_csv_query_values(values: list[str] | None) -> set[str]:
    if not values:
        return set()
    parsed: set[str] = set()
    for value in values:
        for piece in value.split(","):
            normalized = piece.strip().lower()
            if normalized:
                parsed.add(normalized)
    return parsed


def _row_text_haystack(row: dict[str, Any], status: str) -> str:
    provider_name = (row.get("provider", {}) or {}).get("name", "")
    parts = [
        str(row.get("issue_identifier", "")),
        str(row.get("issue_id", "")),
        str(row.get("state", "")),
        str(row.get("provider_summary", "")),
        str(row.get("error", "")),
        str(provider_name),
        status,
    ]
    return " ".join(parts).lower()


def _query_matches(haystack: str, query_text: str) -> bool:
    terms = [term for term in query_text.split() if term]
    if not terms:
        return True
    return all(term in haystack for term in terms)


def _filter_state_snapshot(snapshot: dict[str, Any], query_params: dict[str, list[str]]) -> dict[str, Any]:
    statuses = _split_csv_query_values(query_params.get("status"))
    states = _split_csv_query_values(query_params.get("state"))
    providers = _split_csv_query_values(query_params.get("provider"))
    issue_identifiers = _split_csv_query_values(query_params.get("issue_identifier") or query_params.get("issue"))
    query = " ".join(query_params.get("q", [])).strip().lower()
    include_running = not statuses or "running" in statuses
    include_retrying = not statuses or "retrying" in statuses
    running_rows = snapshot.get("running", [])
    retry_rows = snapshot.get("retrying", [])
    filtered_running: list[dict[str, Any]] = []
    filtered_retrying: list[dict[str, Any]] = []

    for row in running_rows:
        if not isinstance(row, dict):
            continue
        if not include_running:
            continue
        if states and str(row.get("state", "")).strip().lower() not in states:
            continue
        if providers:
            provider_name = str((row.get("provider", {}) or {}).get("name", "")).strip().lower()
            if provider_name not in providers:
                continue
        if issue_identifiers and str(row.get("issue_identifier", "")).strip().lower() not in issue_identifiers:
            continue
        if query and not _query_matches(_row_text_haystack(row, "running"), query):
            continue
        filtered_running.append(row)

    for row in retry_rows:
        if not isinstance(row, dict):
            continue
        if not include_retrying:
            continue
        if states:
            continue
        if providers:
            provider_name = str(snapshot.get("provider", {}).get("selected", "")).strip().lower()
            if provider_name not in providers:
                continue
        if issue_identifiers and str(row.get("issue_identifier", "")).strip().lower() not in issue_identifiers:
            continue
        if query and not _query_matches(_row_text_haystack(row, "retrying"), query):
            continue
        filtered_retrying.append(row)

    filtered = dict(snapshot)
    filtered["running"] = filtered_running
    filtered["retrying"] = filtered_retrying
    filtered["counts"] = {"running": len(filtered_running), "retrying": len(filtered_retrying)}
    return filtered
