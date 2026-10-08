"""Live checks against OpenRouter. Skipped unless OPENROUTER_API_KEY is set. Each costs well under a cent.
The full threshold evals are scripts/jev_evals.py."""

import os
import unittest

from golem import jev

KEY = os.environ.get("OPENROUTER_API_KEY") or ""
MODEL = "typesafe/jev-1.13"
TASK = "CI run 37533646169 failed. From the attached job logs, list every failing test and the job it failed in."
QUOTE = "list every failing test and the job it failed in"


def proposal(name, description, outputs):
    return {
        "name": name,
        "access": "pure",
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "log file"}},
        },
        "output_schema": {"type": "object", "properties": outputs},
        "gap": {"task_quote": QUOTE},
    }


PARSER = proposal(
    "parse_ci_log",
    "Extract failing pytest test ids, with the job name, from a raw GitHub Actions job log.",
    {
        "failures": {
            "type": "array",
            "items": {"type": "string"},
            "description": "pytest node ids of failing tests, deduplicated",
        }
    },
)


@unittest.skipUnless(KEY, "OPENROUTER_API_KEY is not set")
class JevLiveTest(unittest.TestCase):
    def test_gate_answers_every_question_and_lets_a_real_parser_build(self):
        state, questions = jev.gate_request(
            TASK,
            PARSER,
            [
                {
                    "name": "count_lines",
                    "access": "pure",
                    "description": "Count lines in a text file.",
                }
            ],
            new_interface=True,
        )
        reply = jev.ask(KEY, MODEL, state, questions, session_id="golem-live-test")
        self.assertTrue(reply.ok, reply.error)
        self.assertEqual(
            set(reply.probs), {"exact_op", "same_job::count_lines", "clear::failures"}
        )
        self.assertTrue(reply.request_id.startswith("gen-dec-"), reply.request_id)
        verdict = jev.judge_gate(reply)
        self.assertEqual(verdict.acting, [], reply.record())
        print("\n", reply.record())

    def test_a_vague_output_field_is_sent_back(self):
        vague = proposal(
            "parse_ci_log",
            PARSER["description"],
            {"info": {"type": "string", "description": "information from the log"}},
        )
        state, questions = jev.gate_request(TASK, vague, [], new_interface=True)
        reply = jev.ask(KEY, MODEL, state, questions)
        self.assertTrue(reply.ok, reply.error)
        self.assertTrue(
            any(
                check.startswith("clear::info")
                for check in jev.judge_gate(reply).acting
            ),
            reply.record(),
        )

    def test_a_bad_key_means_no_answer_not_a_crash(self):
        state, questions = jev.gate_request(TASK, PARSER, [], new_interface=False)
        reply = jev.ask("sk-or-v1-invalid", MODEL, state, questions)
        self.assertFalse(reply.ok)
        self.assertIn("HTTP 401", reply.error)
        self.assertEqual(jev.judge_gate(reply).acting, [])


if __name__ == "__main__":
    unittest.main()
