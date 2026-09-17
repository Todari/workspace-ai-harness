import unittest
import codex_worker as worker
from execution_policy import select_profile


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.config = worker.load_config()

    def task(self, kind="medium", **kw):
        return dict(task_class=kind, objective="컴포넌트 수정", worker_effort="auto", **kw)

    def test_workload_and_risk(self):
        self.assertEqual(select_profile(self.task("simple"), self.config), ("gpt-5.6-luna", "low"))
        self.assertEqual(select_profile(self.task(), self.config), ("gpt-5.6-sol", "medium"))
        task = self.task("simple")
        task["objective"] = "인증 코드 한 줄 수정"
        self.assertEqual(select_profile(task, self.config), ("gpt-5.6-sol", "high"))

    def test_resume_keeps_selection_and_explicit_escalation_is_bounded(self):
        old = {"model": "gpt-5.6-sol", "effort": "high"}
        self.assertEqual(select_profile(self.task("simple"), self.config, old), ("gpt-5.6-sol", "high"))
        self.assertEqual(select_profile(self.task(), self.config, old, True), ("gpt-5.6-sol", "xhigh"))
        old["effort"] = "xhigh"
        self.assertEqual(select_profile(self.task(), self.config, old, True)[1], "xhigh")

    def test_explicit_values_win(self):
        task = self.task(worker_model="custom-model")
        task["worker_effort"] = "low"
        self.assertEqual(select_profile(task, self.config, {"effort": "high"}, True), ("custom-model", "low"))

    def test_bad_profiles_rejected(self):
        self.config["execution_profiles"] = [{"effort": "max"}]
        self.assertTrue(worker.model_config_errors(self.config))
