"""Check the optional Open WebUI filter for replayed Ollama tool results."""

from copy import deepcopy
from runpy import run_path
import unittest

from tests import ROOT


Filter = run_path(str(ROOT / "docs/examples/ollama_tool_history.py"))["Filter"]
OLLAMA = {"owned_by": "ollama"}
USER = {"role": "user", "content": "Check the status."}
CALL = {
    "role": "assistant",
    "thinking": "I can ask for the status.",
    "tool_calls": [
        {"id": "call_first", "function": {"name": "get_status", "arguments": "{}"}},
        {"id": "call_second", "function": {"name": "get_status", "arguments": "{}"}},
    ],
}
RESULTS = [
    {"role": "tool", "tool_call_id": "call_first", "content": "ready"},
    {"role": "tool", "tool_call_id": "call_second", "content": "idle"},
]
FOLLOW_UP = {"role": "user", "content": "What did you find?"}
EMPTY_REPLY = {"role": "assistant", "content": ""}


class OllamaToolHistoryTests(unittest.TestCase):
    def setUp(self):
        self.filter = Filter()

    def test_filter_is_toggleable_and_applies_only_to_native_ollama(self):
        body = {"messages": [USER, CALL, *RESULTS, FOLLOW_UP]}
        self.assertTrue(self.filter.toggle)
        self.assertIs(self.filter.request(body), body)
        self.assertIs(self.filter.request(body, {"owned_by": "openai"}), body)

    def test_new_and_completed_turns_are_unchanged(self):
        reply = {"role": "assistant", "content": "The status is ready."}
        for messages in ([USER], [USER, CALL, *RESULTS], [USER, CALL, *RESULTS, reply, FOLLOW_UP]):
            with self.subTest(roles=[message["role"] for message in messages]):
                body = {"messages": messages}
                self.assertIs(self.filter.request(body, OLLAMA), body)

    def test_prior_assistant_reply_does_not_need_an_extra_turn(self):
        reply = {"role": "assistant", "content": "I will check once more."}
        body = {"messages": [USER, reply, CALL, *RESULTS, FOLLOW_UP]}
        self.assertIs(self.filter.request(body, OLLAMA), body)

    def test_replays_tool_results_before_a_follow_up_without_changing_history(self):
        system = {"role": "system", "content": "Answer concisely."}
        follow_up = {"role": "user", "content": [{"type": "text", "text": FOLLOW_UP["content"]}]}
        body = {"messages": [USER, CALL, *RESULTS, system, follow_up], "stream": True}
        original = deepcopy(body)

        repaired = self.filter.request(body, OLLAMA)

        self.assertEqual(repaired, {**body, "messages": [USER, CALL, *RESULTS, system, EMPTY_REPLY, follow_up]})
        self.assertEqual(body, original)
        self.assertIs(self.filter.request(repaired, OLLAMA), repaired)

    def test_closes_each_interrupted_turn_in_saved_history(self):
        body = {"messages": [USER, CALL, *RESULTS, USER, CALL, *RESULTS, FOLLOW_UP]}
        repaired = self.filter.request(body, OLLAMA)
        self.assertEqual(repaired["messages"], [
            USER, CALL, *RESULTS, EMPTY_REPLY,
            USER, CALL, *RESULTS, EMPTY_REPLY,
            FOLLOW_UP,
        ])
