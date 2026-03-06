from __future__ import annotations

import json
import socket
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import (
    MissingTrackerApiKeyError,
    MissingTrackerProjectSlugError,
    TrackerError,
    UnsupportedTrackerKindError,
)
from .models import BlockerRef, Issue, ServiceConfig


LINEAR_NETWORK_TIMEOUT_SECONDS = 30
LINEAR_PAGE_SIZE = 50


class BaseTrackerClient(ABC):
    @abstractmethod
    def fetch_candidate_issues(self) -> List[Issue]:
        raise NotImplementedError

    @abstractmethod
    def fetch_issues_by_states(self, state_names: List[str]) -> List[Issue]:
        raise NotImplementedError

    @abstractmethod
    def fetch_issue_states_by_ids(self, issue_ids: List[str]) -> List[Issue]:
        raise NotImplementedError

    @abstractmethod
    def update_issue_state(self, issue_id: str, state_name: str) -> None:
        raise NotImplementedError


class LinearTrackerClient(BaseTrackerClient):
    def __init__(self, config: ServiceConfig):
        self.config = config
        if not config.tracker.api_key:
            raise MissingTrackerApiKeyError("tracker.api_key is required")
        if not config.tracker.project_slug:
            raise MissingTrackerProjectSlugError("tracker.project_slug is required")

    def fetch_candidate_issues(self) -> List[Issue]:
        query = """
        query SymphonyCandidateIssues($projectSlug: String!, $states: [String!], $first: Int!, $after: String) {
          issues(
            filter: {
              project: { slugId: { eq: $projectSlug } }
              state: { name: { in: $states } }
            }
            first: $first
            after: $after
          ) {
            nodes {
              id
              identifier
              title
              description
              priority
              branchName
              url
              createdAt
              updatedAt
              state { name }
              labels(first: 50) { nodes { name } }
              inverseRelations(first: 50) {
                nodes {
                  type
                  issue { id identifier }
                  relatedIssue { id identifier state { name } }
                }
              }
            }
            pageInfo {
              hasNextPage
              endCursor
            }
          }
        }
        """
        after: Optional[str] = None
        issues: List[Issue] = []
        while True:
            payload = self._post(
                query,
                {
                    "projectSlug": self.config.tracker.project_slug,
                    "states": self.config.tracker.active_states,
                    "first": LINEAR_PAGE_SIZE,
                    "after": after,
                },
            )
            connection = (((payload.get("data") or {}).get("issues")) or {})
            nodes = connection.get("nodes") or []
            issues.extend(self._normalize_issue(node) for node in nodes)
            page_info = connection.get("pageInfo") or {}
            has_next = bool(page_info.get("hasNextPage"))
            after = page_info.get("endCursor")
            if not has_next:
                break
            if after in (None, ""):
                raise TrackerError("linear_missing_end_cursor")
        return issues

    def fetch_issues_by_states(self, state_names: List[str]) -> List[Issue]:
        if not state_names:
            return []
        query = """
        query SymphonyIssuesByStates($projectSlug: String!, $states: [String!], $first: Int!, $after: String) {
          issues(
            filter: {
              project: { slugId: { eq: $projectSlug } }
              state: { name: { in: $states } }
            }
            first: $first
            after: $after
          ) {
            nodes {
              id
              identifier
              title
              description
              priority
              branchName
              url
              createdAt
              updatedAt
              state { name }
              labels(first: 50) { nodes { name } }
              inverseRelations(first: 50) {
                nodes {
                  type
                  issue { id identifier }
                  relatedIssue { id identifier state { name } }
                }
              }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        """
        after: Optional[str] = None
        issues: List[Issue] = []
        while True:
            payload = self._post(
                query,
                {
                    "projectSlug": self.config.tracker.project_slug,
                    "states": state_names,
                    "first": LINEAR_PAGE_SIZE,
                    "after": after,
                },
            )
            connection = (((payload.get("data") or {}).get("issues")) or {})
            nodes = connection.get("nodes") or []
            issues.extend(self._normalize_issue(node) for node in nodes)
            page_info = connection.get("pageInfo") or {}
            has_next = bool(page_info.get("hasNextPage"))
            after = page_info.get("endCursor")
            if not has_next:
                break
            if after in (None, ""):
                raise TrackerError("linear_missing_end_cursor")
        return issues

    def fetch_issue_states_by_ids(self, issue_ids: List[str]) -> List[Issue]:
        if not issue_ids:
            return []
        query = """
        query SymphonyIssueStates($ids: [ID!]!) {
          issues(filter: { id: { in: $ids } }) {
            nodes {
              id
              identifier
              title
              description
              priority
              branchName
              url
              createdAt
              updatedAt
              state { name }
              labels(first: 50) { nodes { name } }
              inverseRelations(first: 50) {
                nodes {
                  type
                  issue { id identifier }
                  relatedIssue { id identifier state { name } }
                }
              }
            }
          }
        }
        """
        payload = self._post(query, {"ids": issue_ids})
        nodes = ((((payload.get("data") or {}).get("issues")) or {}).get("nodes")) or []
        return [self._normalize_issue(node) for node in nodes]

    def update_issue_state(self, issue_id: str, state_name: str) -> None:
        issue_id = str(issue_id or "").strip()
        state_name = str(state_name or "").strip()
        if not issue_id or not state_name:
            raise TrackerError("linear_issue_update_invalid_input")

        state_id = self._resolve_state_id(issue_id, state_name)
        mutation = """
        mutation SymphonyUpdateIssueState($issueId: String!, $stateId: String!) {
          issueUpdate(id: $issueId, input: { stateId: $stateId }) {
            success
          }
        }
        """
        payload = self._post(mutation, {"issueId": issue_id, "stateId": state_id})
        success = (((payload.get("data") or {}).get("issueUpdate") or {}).get("success")) is True
        if not success:
            raise TrackerError("linear_issue_update_failed")

    def _resolve_state_id(self, issue_id: str, state_name: str) -> str:
        query = """
        query SymphonyResolveStateId($issueId: String!, $stateName: String!) {
          issue(id: $issueId) {
            team {
              states(filter: { name: { eq: $stateName } }, first: 1) {
                nodes {
                  id
                }
              }
            }
          }
        }
        """
        payload = self._post(query, {"issueId": issue_id, "stateName": state_name})
        nodes = ((((payload.get("data") or {}).get("issue") or {}).get("team") or {}).get("states") or {}).get("nodes") or []
        state_id = None
        if nodes and isinstance(nodes[0], dict):
            state_id = nodes[0].get("id")
        if not state_id:
            raise TrackerError("linear_state_not_found")
        return str(state_id)

    def _post(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = Request(
            self.config.tracker.endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": self.config.tracker.api_key or "",
            },
        )
        try:
            with urlopen(request, timeout=LINEAR_NETWORK_TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise TrackerError(f"linear_api_status:{status}")
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise TrackerError(f"linear_api_status:{exc.code}") from exc
        except (URLError, socket.timeout) as exc:
            raise TrackerError("linear_api_request") from exc
        except json.JSONDecodeError as exc:
            raise TrackerError("linear_unknown_payload") from exc
        if payload.get("errors"):
            raise TrackerError("linear_graphql_errors")
        if "data" not in payload:
            raise TrackerError("linear_unknown_payload")
        return payload

    def _normalize_issue(self, node: Dict[str, Any]) -> Issue:
        state_name = (((node.get("state") or {}).get("name")) or "").strip()
        labels = [str(label.get("name", "")).strip().lower() for label in ((node.get("labels") or {}).get("nodes") or [])]
        blockers: List[BlockerRef] = []
        for relation in ((node.get("inverseRelations") or {}).get("nodes") or []):
            if str(relation.get("type") or "").lower() != "blocks":
                continue
            related = relation.get("relatedIssue") or relation.get("issue") or {}
            blockers.append(
                BlockerRef(
                    id=related.get("id"),
                    identifier=related.get("identifier"),
                    state=((related.get("state") or {}).get("name")) if isinstance(related.get("state"), dict) else related.get("state"),
                )
            )
        priority = node.get("priority")
        try:
            priority_value = int(priority) if priority is not None else None
        except (TypeError, ValueError):
            priority_value = None
        return Issue(
            id=str(node.get("id") or "").strip(),
            identifier=str(node.get("identifier") or "").strip(),
            title=str(node.get("title") or "").strip(),
            description=node.get("description"),
            priority=priority_value,
            state=state_name,
            branch_name=node.get("branchName"),
            url=node.get("url"),
            labels=[label for label in labels if label],
            blocked_by=blockers,
            created_at=_parse_datetime(node.get("createdAt")),
            updated_at=_parse_datetime(node.get("updatedAt")),
        )


def build_tracker_client(config: ServiceConfig) -> BaseTrackerClient:
    if config.tracker.kind != "linear":
        raise UnsupportedTrackerKindError(config.tracker.kind)
    return LinearTrackerClient(config)


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
