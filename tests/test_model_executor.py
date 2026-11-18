import json
import os
import unittest
from unittest.mock import Mock, patch

from model_executor import DeepSeek
from replay import paired_outcomes, replace_once, summarize


FUNCTION = "def example():\n    return 0\n"


def response(action, tokens):
    return {"choices": [{"message": {"content": json.dumps(action)}, "finish_reason": "stop"}],
            "usage": {"prompt_cache_hit_tokens": 2, "prompt_cache_miss_tokens": 8,
                      "completion_tokens": tokens}}


class ExecutorTests(unittest.TestCase):
    def execute(self, request):
        workspace = Mock()
        workspace.read.return_value = FUNCTION
        workspace.smoke.return_value = {"exit_code": 1, "timed_out": False,
                                        "stdout": "", "stderr": "visible failure"}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}):
            result = DeepSeek("test-model", (1, 2, 3), request).run(
                workspace, {"task": "Example"}, 2, 100, workspace.read, replace_once)
        return workspace, result

    def test_budget_and_usage_across_calls(self):
        fixed = FUNCTION.replace("return 0", "return 1")
        request = Mock(side_effect=[response({"action": "smoke"}, 25),
                                    response({"action": "replace", "function": fixed}, 75)])
        workspace, result = self.execute(request)
        workspace.edit.assert_called_once_with(fixed)
        workspace.smoke.assert_called_once()
        self.assertEqual([call.args[3] for call in request.call_args_list], [100, 75])
        self.assertIn("visible failure", request.call_args.args[2][-1]["content"])
        self.assertEqual(result["actions"], 2)
        self.assertEqual(result["stop"], "budget_exhausted")
        self.assertAlmostEqual(result["api_cost_usd"], 336 / 1_000_000)

    def test_rejects_edits_outside_function(self):
        request = Mock(return_value=response({"action": "replace", "function": FUNCTION + "print(1)"}, 20))
        workspace, result = self.execute(request)
        workspace.edit.assert_not_called()
        self.assertEqual(result["stop"], "error")

    def test_timeout_preserves_unknown_cost(self):
        workspace, result = self.execute(Mock(side_effect=TimeoutError))
        workspace.smoke.assert_not_called()
        self.assertEqual(result["stop"], "timeout")
        self.assertFalse(result["usage_complete"])
        self.assertIsNone(result["api_cost_usd"])


class StatisticsTests(unittest.TestCase):
    def test_pairing_does_not_hide_offsetting_wins(self):
        reports = [
            {"routes": {"restart": {"status": first, "stop": "submitted"},
                        "rollback": {"status": second, "stop": "submitted"}}}
            for first, second in [("pass", "fail"), ("fail", "pass")]
        ]
        self.assertEqual(paired_outcomes(reports, ["restart", "rollback"]),
                         {"restart vs rollback": {"first_only": 1, "second_only": 1, "both": 0, "neither": 0}})

    def test_retains_missing_verdicts_in_trial_denominator(self):
        reports = []
        for status, stop, code, cost in [
            ("pass", "submitted", 0, 1), ("fail", "submitted", 0, 2), ("timeout", "timeout", None, None),
        ]:
            reports.append({"routes": {"restart": {"status": status, "stop": stop, "visible": {"exit_code": code},
                            "api_cost_usd": cost, "actions": 2, "recovery_seconds": 1}}})
        stats = summarize(reports, ["restart"])["restart"]
        self.assertEqual(stats["success_rate"], 1 / 3)
        self.assertEqual(stats["false_fix_rate"], 1 / 2)
        self.assertEqual(stats["timeouts"], 1)
        self.assertIsNone(stats["total_cost_usd"])
        self.assertIsNone(stats["cost_per_success_usd"])
