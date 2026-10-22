"""Visual review skips heuristic scores, not completeness or the AI review queue."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from client.agent_core import AgentSession


class VisualSubmitTest(unittest.TestCase):
    def session(self, missing=None, visible=1):
        s = object.__new__(AgentSession)
        s.work_root = "/unused"
        s.worker = "test"
        s.method = {}
        s.load_session = Mock(return_value={"lease_id": "lease", "worker": "test"})
        s.prep = Mock(return_value=SimpleNamespace(meta={}))
        s._meta_dict = Mock(return_value={})
        s._progress = Mock(return_value={
            "labels": {"1": {"frame_idx": 1, "visible": bool(visible)}},
            "missing": missing or [], "n_visible": visible,
        })
        s._gate_labels = Mock(return_value={"n_fail": 1, "failed": [{"frame_idx": 1}]})
        s.save_session = Mock()
        s.api = SimpleNamespace(submit=Mock(return_value={"submission_id": 1}))
        return s

    @patch("client.agent_core.is_ready", return_value=True)
    @patch("client.agent_core.build_result", return_value={})
    def test_visual_review_skips_score_but_sends_to_ai_review(self, build, ready):
        s = self.session()
        result = s.submit("packet", visual_reviewed=True)
        self.assertTrue(result["ok"])
        s._gate_labels.assert_not_called()
        self.assertEqual(s.api.submit.call_args.kwargs["source"], "ai")
        self.assertEqual(s.api.submit.call_args.args[0]["review_mode"], "visual")

    @patch("client.agent_core.is_ready", return_value=True)
    def test_visual_review_does_not_allow_missing_or_all_absent(self, ready):
        for s in [self.session(missing=[2]), self.session(visible=0)]:
            self.assertIn("error", s.submit("packet", visual_reviewed=True))
            s.api.submit.assert_not_called()

    @patch("client.agent_core.is_ready", return_value=True)
    def test_legacy_score_gate_is_preserved(self, ready):
        s = self.session()
        self.assertIn("error", s.submit("packet"))
        s._gate_labels.assert_called_once()
        s.api.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
