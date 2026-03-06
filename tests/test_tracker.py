import unittest

from symphony.errors import TrackerError
from symphony.tracker import LinearTrackerClient


class _FakeLinearTrackerClient(LinearTrackerClient):
    def __init__(self):
        self._responses = []

    def push_response(self, response):
        self._responses.append(response)

    def _post(self, query, variables):
        if not self._responses:
            raise AssertionError("missing fake response")
        return self._responses.pop(0)


class TrackerWriteTests(unittest.TestCase):
    def test_update_issue_state_resolves_state_id_then_updates(self):
        client = _FakeLinearTrackerClient()
        client.push_response({
            "data": {
                "issue": {
                    "team": {
                        "states": {
                            "nodes": [{"id": "state-123"}]
                        }
                    }
                }
            }
        })
        client.push_response({
            "data": {
                "issueUpdate": {
                    "success": True
                }
            }
        })

        client.update_issue_state("issue-1", "Done")

    def test_update_issue_state_raises_when_state_not_found(self):
        client = _FakeLinearTrackerClient()
        client.push_response({"data": {"issue": {"team": {"states": {"nodes": []}}}}})

        with self.assertRaises(TrackerError):
            client.update_issue_state("issue-1", "Done")

    def test_update_issue_state_raises_when_update_fails(self):
        client = _FakeLinearTrackerClient()
        client.push_response({
            "data": {
                "issue": {
                    "team": {
                        "states": {
                            "nodes": [{"id": "state-123"}]
                        }
                    }
                }
            }
        })
        client.push_response({"data": {"issueUpdate": {"success": False}}})

        with self.assertRaises(TrackerError):
            client.update_issue_state("issue-1", "Done")


if __name__ == "__main__":
    unittest.main()
