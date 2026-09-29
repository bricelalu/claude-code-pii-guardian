"""Offline tests for the LeakGuard LiteLLM adapter (no proxy, no GPU).

The adapter is the piece that makes LeakGuard load at all: as a bare `LeakGuard` the
config entry `guardrail: leak_guard.LeakGuard` was silently dropped by LiteLLM's
loader, because it is not a CustomGuardrail. These tests pin the interface LiteLLM
needs and the exemptions it must share with code_guard.

    python3 guardrail/test_leak_guard_hook.py
"""
import asyncio
import importlib.util
import json
import unittest

from leak_guard_hook import DATA_EXTENSIONS, LeakGuardHook

HAS_LITELLM = importlib.util.find_spec("litellm") is not None

# Half-masked, as FailingMasker leaves it: tokens in some cells, real PII in others.
PARTIAL_JSON = json.dumps([
    {"id": "1", "firstname": "<PERSON>", "lastname": "martin", "city": "<LOCATION>",
     "email": "[EMAIL_REDACTED]"},
    {"id": "2", "firstname": "bruno", "lastname": "<PERSON>", "city": "Donges",
     "email": "[EMAIL_REDACTED]"},
], indent=2)

PARTIAL_CSV = "id,firstname,lastname\n1,<PERSON>,martin\n2,bruno,<PERSON>\n"

PARTIAL_MD = (
    "| id | firstname | lastname |\n"
    "|---|---|---|\n"
    "| 1 | <PERSON> | martin |\n"
    "| 2 | bruno | <PERSON> |\n"
)

FULLY_MASKED = json.dumps([{"id": "1", "firstname": "<PERSON>", "lastname": "<PERSON>"}])


def request(blocks, extra_messages=()):
    """An Anthropic /v1/messages body whose last user turn carries `blocks`."""
    messages = list(extra_messages) + [
        {"role": "user", "content": [{"type": "text", "text": "show me the customers"}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "sqlite3 db .dump"}},
        ]},
        {"role": "user", "content": blocks},
    ]
    return {"system": [{"type": "text", "text": "You are Claude."}], "messages": messages}


def tool_result(text):
    return [{"type": "tool_result", "tool_use_id": "t1", "content": text}]


def run(data):
    hook = LeakGuardHook()
    asyncio.run(hook.mask_request(data))
    return data


def last_result(data):
    return data["messages"][-1]["content"][0]["content"]


class CompletenessTest(unittest.TestCase):
    """The actual job: finish a half-masked document."""

    def test_completes_partial_json(self):
        out = json.loads(last_result(run(request(tool_result(PARTIAL_JSON)))))
        self.assertEqual([r["firstname"] for r in out], ["<PERSON>", "<PERSON>"])
        self.assertEqual([r["lastname"] for r in out], ["<PERSON>", "<PERSON>"])
        self.assertEqual([r["city"] for r in out], ["<LOCATION>", "<LOCATION>"])

    def test_completes_partial_csv(self):
        out = last_result(run(request(tool_result(PARTIAL_CSV))))
        self.assertNotIn("martin", out)
        self.assertNotIn("bruno", out)
        self.assertIn("<PERSON>", out)

    def test_completes_partial_markdown(self):
        out = last_result(run(request(tool_result(PARTIAL_MD))))
        self.assertNotIn("martin", out)
        self.assertNotIn("bruno", out)

    def test_completes_a_user_text_block(self):
        data = run(request([{"type": "text", "text": PARTIAL_CSV}]))
        self.assertNotIn("bruno", data["messages"][0]["content"][0]["text"])

    def test_structure_survives(self):
        self.assertEqual(len(json.loads(last_result(run(request(tool_result(PARTIAL_JSON)))))), 2)

    def test_unmasked_columns_are_left_alone(self):
        # `id` holds no token, so LeakGuard must not touch it — it is not PII.
        out = json.loads(last_result(run(request(tool_result(PARTIAL_JSON)))))
        self.assertEqual([r["id"] for r in out], ["1", "2"])


class NoOpTest(unittest.TestCase):
    def test_fully_masked_is_unchanged(self):
        self.assertEqual(last_result(run(request(tool_result(FULLY_MASKED)))), FULLY_MASKED)

    def test_text_without_tokens_is_unchanged(self):
        plain = "SELECT * FROM customers_leaked LIMIT 2;"
        self.assertEqual(last_result(run(request(tool_result(plain)))), plain)

    def test_is_idempotent(self):
        once = last_result(run(request(tool_result(PARTIAL_JSON))))
        twice = last_result(run(request(tool_result(once))))
        self.assertEqual(json.loads(once), json.loads(twice))


class ExemptionTest(unittest.TestCase):
    """LeakGuard must agree with code_guard about what is not data.

    A second, divergent definition of "this is a data file" is how a guardrail ends up
    rewriting a file Claude is in the middle of editing.
    """

    def _result(self, tool_name, tool_input, text):
        data = {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "go"}]},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t1", "name": tool_name, "input": tool_input},
                ]},
                {"role": "user", "content": tool_result(text)},
            ],
        }
        return last_result(run(data))

    def test_write_result_is_untouched(self):
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            with self.subTest(tool=tool):
                self.assertEqual(self._result(tool, {"file_path": "/e.csv"}, PARTIAL_CSV),
                                 PARTIAL_CSV)

    def test_read_of_a_code_file_is_untouched(self):
        self.assertEqual(self._result("Read", {"file_path": "/app/report.py"},
                                      "rows = ['<PERSON>', 'martin']\n"),
                         "rows = ['<PERSON>', 'martin']\n")

    def test_read_of_a_data_file_is_completed(self):
        out = self._result("Read", {"file_path": "/exports/customers.csv"}, PARTIAL_CSV)
        self.assertNotIn("bruno", out)

    def test_system_prompt_is_untouched(self):
        data = run(request(tool_result(PARTIAL_CSV)))
        self.assertEqual(data["system"][0]["text"], "You are Claude.")

    def test_assistant_text_is_untouched(self):
        # messages[0] is a *user* turn; the assistant turn is the one carrying tool_use.
        data = request(tool_result(PARTIAL_CSV))
        data["messages"][1]["content"].insert(0, {"type": "text", "text": PARTIAL_CSV})
        self.assertEqual(run(data)["messages"][1]["content"][0]["text"], PARTIAL_CSV)

    def test_tool_use_input_is_untouched(self):
        data = request(tool_result(PARTIAL_CSV))
        data["messages"][1]["content"][0]["input"] = {"command": PARTIAL_CSV}
        self.assertEqual(run(data)["messages"][1]["content"][0]["input"],
                         {"command": PARTIAL_CSV})


class LiteLLMInterfaceTest(unittest.TestCase):
    """What LiteLLM's loader requires before it will keep the guardrail at all."""

    def test_use_native_lifecycle_hooks(self):
        self.assertTrue(LeakGuardHook.use_native_lifecycle_hooks)

    def test_hook_methods_exist(self):
        for name in ("async_pre_call_hook", "apply_guardrail", "mask_request"):
            self.assertTrue(callable(getattr(LeakGuardHook, name, None)), name)

    def test_skip_tools_default_matches_code_guard(self):
        self.assertEqual(set(LeakGuardHook().skip_tools),
                         {"Write", "Edit", "MultiEdit", "NotebookEdit"})

    def test_data_extensions_default_matches_code_guard(self):
        self.assertEqual(LeakGuardHook().data_extensions, DATA_EXTENSIONS)

    @unittest.skipUnless(HAS_LITELLM, "litellm is in the proxy image, not here")
    def test_is_a_real_custom_guardrail(self):
        from litellm.integrations.custom_guardrail import CustomGuardrail
        self.assertTrue(issubclass(LeakGuardHook, CustomGuardrail))

    @unittest.skipUnless(HAS_LITELLM, "litellm is in the proxy image, not here")
    def test_pre_call_hook_passes_data_through(self):
        data = request(tool_result(PARTIAL_CSV))
        out = asyncio.run(LeakGuardHook().async_pre_call_hook(None, None, data, "completion"))
        self.assertIs(out, data)
        self.assertNotIn("bruno", last_result(out))






if __name__ == "__main__":
    unittest.main(verbosity=2)
