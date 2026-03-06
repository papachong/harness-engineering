import tempfile
import unittest
from pathlib import Path

from symphony.logging_utils import configure_logging
from symphony.models import HooksConfig
from symphony.workspace import WorkspaceManager, sanitize_workspace_key


class WorkspaceTests(unittest.TestCase):
    def test_sanitizes_workspace_key(self):
        self.assertEqual(sanitize_workspace_key("ABC/123:*"), "ABC_123__")

    def test_creates_and_reuses_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = WorkspaceManager(Path(tmp), HooksConfig(), configure_logging())
            first = manager.create_for_issue("ABC-1")
            second = manager.create_for_issue("ABC-1")
            self.assertTrue(first.created_now)
            self.assertFalse(second.created_now)
            self.assertEqual(first.path, second.path)


if __name__ == "__main__":
    unittest.main()
