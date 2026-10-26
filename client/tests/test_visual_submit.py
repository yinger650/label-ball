"""Visual review skips heuristic scores, not completeness or the AI review queue."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from client.agent_core import AgentSession


class VisualSubmitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def session(self, missing=None, visible=1):
        s = object.__new__(AgentSession)
        s.work_root = Path(self.tmp.name)
        s.worker = "test"
        s.method = {}
        s.load_session = Mock(return_value={"lease_id": "lease", "worker": "test"})
        s.prep = Mock(return_value=SimpleNamespace(meta={}))
        s._meta_dict = Mock(return_value={})
        labels = {"1": {"frame_idx": 1, "visible": bool(visible), "pano_x": 10.0, "pano_y": 20.0}}
        s._progress = Mock(return_value={
            "labels": labels, "targets": [1] + list(missing or []),
            "missing": missing or [], "n_visible": visible, "n_labeled": 1,
        })
        s._gate_labels = Mock(return_value={"n_fail": 1, "failed": [{"frame_idx": 1}]})
        s.save_session = Mock()
        s.api = SimpleNamespace(submit=Mock(return_value={"submission_id": 1}))
        return s

    def write_review(self, s, review_id, labels, targets, full=True):
        p = s._reviews_path("packet")
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"labels_hash": s._labels_hash(labels, targets), "full_coverage": full}
        p.write_text(json.dumps({review_id: rec}))

    @patch("client.agent_core.is_ready", return_value=True)
    @patch("client.agent_core.build_result", return_value={})
    def test_visual_review_skips_score_but_sends_to_ai_review(self, build, ready):
        s = self.session()
        result = s.submit("packet", visual_reviewed=True)
        self.assertTrue(result["ok"])
        s._gate_labels.assert_not_called()
        self.assertEqual(s.api.submit.call_args.kwargs["source"], "ai")
        self.assertEqual(s.api.submit.call_args.args[0]["review_mode"], "visual")
        receipt = json.loads(Path(result["receipt"]).read_text())
        self.assertEqual(receipt["submission_id"], 1)
        self.assertEqual(receipt["review_state"], "pending")
        self.assertTrue(result["review_warnings"][0].startswith("no_review_id"))

    @patch("client.agent_core.is_ready", return_value=True)
    def test_visual_review_does_not_allow_missing_or_all_absent(self, ready):
        for s in [self.session(missing=[2]), self.session(visible=0)]:
            self.assertIn("error", s.submit("packet", visual_reviewed=True))
            s.api.submit.assert_not_called()

    @patch("client.agent_core.is_ready", return_value=True)
    def test_visual_review_rejects_force(self, ready):
        s = self.session(missing=[2])
        self.assertIn("error", s.submit("packet", force=True, visual_reviewed=True))
        s.api.submit.assert_not_called()

    @patch("client.agent_core.is_ready", return_value=True)
    @patch("client.agent_core.build_result", return_value={})
    def test_review_id_fresh_stale_unknown_partial(self, build, ready):
        s = self.session()
        prog = s._progress()
        self.write_review(s, "rfresh", prog["labels"], prog["targets"])
        self.assertEqual(s.submit("packet", visual_reviewed=True, review_id="rfresh")["review_warnings"], [])

        s = self.session()
        moved = {"1": {**prog["labels"]["1"], "pano_x": 99.0}}
        self.write_review(s, "rold", moved, prog["targets"], full=False)
        warns = s.submit("packet", visual_reviewed=True, review_id="rold")["review_warnings"]
        self.assertEqual([w.split(":")[0] for w in warns], ["review_stale", "partial_review"])

        s = self.session()
        warns = s.submit("packet", visual_reviewed=True, review_id="rnope")["review_warnings"]
        self.assertTrue(warns[0].startswith("unknown_review"))
        s.api.submit.assert_called_once()

    @patch("client.agent_core.is_ready", return_value=True)
    def test_legacy_score_gate_is_preserved(self, ready):
        s = self.session()
        self.assertIn("error", s.submit("packet"))
        s._gate_labels.assert_called_once()
        s.api.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
