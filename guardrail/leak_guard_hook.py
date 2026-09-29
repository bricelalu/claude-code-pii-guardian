"""LeakGuard's LiteLLM adapter: the only form of LeakGuard the proxy will actually load.

Why this file exists: with `guardrail: leak_guard.LeakGuard` in the config, LiteLLM
dropped the guardrail silently — no error, no log line, and should_run_guardrail never
fired for it — because `LeakGuard` is a plain class, not a CustomGuardrail subclass.
Nothing in the config can tell you the guardrail is not running. This class is the
adapter the loader accepts.

What it does: a second pre_call pass over the same blocks code_guard masks, running
LeakGuard.complete() instead of the regex/NER masker. LeakGuard only acts on a document
that already carries masking tokens, so it is a no-op on a request code_guard fully
handled, and a finisher on one it did not.

Order matters: leak-guard must be listed AFTER code-guard in the config. LeakGuard
completes columns from the tokens code_guard left; run first, it has nothing to key on.

Split of responsibilities, deliberately:
  leak_guard.py     the engine — pure stdlib, no LiteLLM, importable offline
  leak_guard_hook.py this file — the LiteLLM surface, the only part that knows about
                    CustomGuardrail, and the only part mounted next to config.yaml
  code_guard.py     owns "which blocks are data", shared via data_targets()

The target walk is imported rather than reimplemented on purpose. Two guardrails with
two definitions of "a data file" is how a second layer ends up rewriting a .py file
Claude is in the middle of editing.
"""
import asyncio

try:
    from litellm.integrations.custom_guardrail import CustomGuardrail
    from litellm.types.guardrails import GuardrailEventHooks
except ImportError:  # offline tests exercise the request walk, not the LiteLLM hooks
    CustomGuardrail = object

from code_guard import DATA_EXTENSIONS, data_targets, restore_code_lines
from leak_guard import LeakGuard

# Matches code_guard.Cache_max: Claude Code resends the whole conversation every turn, so
# without a cache the same multi-KB export is re-parsed on every single request.
CACHE_MAX = 10_000

# A tool result that echoes the file Claude is editing, or a code file it must quote
# verbatim, is not data. Same set code_guard uses.
DEFAULT_SKIP_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


class LeakGuardHook(CustomGuardrail):
    # Pre-call gets the raw request (tool names intact) rather than LiteLLM's flattened
    # text list, which is what makes the code_guard exemptions expressible at all.
    use_native_lifecycle_hooks = True

    def __init__(self, skip_tools=DEFAULT_SKIP_TOOLS, data_extensions=DATA_EXTENSIONS, **kwargs):
        super().__init__(**kwargs)
        self.leak = LeakGuard()
        self.skip_tools = set(skip_tools)
        self.data_extensions = tuple(e.lower() for e in data_extensions)
        self.cache = {}

    def _complete(self, text):
        if text in self.cache:
            return self.cache[text]
        out = self.leak.complete(text)
        if len(self.cache) > CACHE_MAX:  # ponytail, same as code_guard: wholesale reset
            self.cache.clear()
        self.cache[text] = out
        return out

    async def _complete_all(self, texts):
        # LeakGuard is pure CPU; run the blocks concurrently anyway so a large export
        # does not serialize behind a slow one. _complete is sync, so this is a formality
        # today — it keeps the seam open if complete() ever grows an analyzer call.
        return await asyncio.gather(*(asyncio.to_thread(self._complete, t) for t in texts))

    async def mask_request(self, data):
        """Complete partial masking on every data block. In place, like code_guard's."""
        messages = data.get("messages")
        if not isinstance(messages, list):
            return
        targets = data_targets(messages, self.skip_tools, self.data_extensions)
        originals = [c[k] for c, k, _ in targets]
        for (container, key, tool_use), original, completed in zip(
                targets, originals, await self._complete_all(originals)):
            container[key] = restore_code_lines(original, completed, tool_use, self.data_extensions)

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if self.should_run_guardrail(data=data, event_type=GuardrailEventHooks.pre_call):
            await self.mask_request(data)
        return data

    async def apply_guardrail(self, inputs, request_data, input_type, logging_obj=None):
        # Stays for POST /guardrails/apply_guardrail (the benchmark endpoint), which hands
        # over a flat list of texts rather than a request body.
        inputs["texts"] = list(await self._complete_all(
            [t for t in inputs.get("texts", []) if isinstance(t, str)]))
        return inputs
