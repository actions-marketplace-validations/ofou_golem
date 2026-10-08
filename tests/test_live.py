"""Live checks against OpenRouter. Skipped unless OPENROUTER_API_KEY is set. Each costs well under a cent."""

import os
import unittest

from golem import jev

KEY = os.environ.get("OPENROUTER_API_KEY")
TASK = "CI run 37533646169 failed. From the attached job logs, list every failing test and the job it failed in."


@unittest.skipUnless(KEY, "OPENROUTER_API_KEY is not set")
class JevLiveTest(unittest.TestCase):
    def test_decisions_endpoint_returns_calibrated_answers(self):
        proposal = {"name": "parse_ci_log", "description": "Extract failing pytest test ids from a raw GitHub Actions job log.", "access": "pure", "gap": {}}
        installed = [{"name": "count_lines", "description": "Count lines in a text file."}]
        advice = jev.advise_gap(KEY, "typesafe/jev-1.13", TASK, proposal, installed)
        self.assertEqual(advice.error, "", advice.line())
        self.assertTrue(advice.model.startswith("typesafe/jev-1.13"), advice.model)
        self.assertEqual(set(advice.probabilities), {"needed", "covered"})
        for value in advice.probabilities.values():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)
        self.assertLess(advice.probabilities["covered"], jev.COVERED_ABOVE, advice.line())
        self.assertTrue(advice.request_id.startswith("gen-dec-"), advice.request_id)
        print("\n" + advice.line())

    def test_an_installed_tool_that_does_the_job_reads_as_covered(self):
        proposal = {"name": "parse_ci_log_again", "description": "Extract failing pytest test ids from a raw GitHub Actions job log.", "access": "pure", "gap": {}}
        installed = [{"name": "parse_ci_log", "description": "Extract failing pytest test ids, with file and job, from a raw GitHub Actions job log (handles ANSI colour codes and timestamps)."}]
        advice = jev.advise_gap(KEY, "typesafe/jev-1.13", TASK, proposal, installed)
        self.assertEqual(advice.error, "", advice.line())
        print("\n" + advice.line())
        self.assertGreater(advice.probabilities["covered"], 0.5, advice.line())

    def test_a_bad_key_means_no_advice_not_a_crash(self):
        advice = jev.advise_gap("sk-or-v1-invalid", "typesafe/jev-1.13", TASK, {"name": "x", "description": "y", "access": "pure"}, [])
        self.assertFalse(advice.defer)
        self.assertIn("HTTP 401", advice.error)


if __name__ == "__main__":
    unittest.main()
