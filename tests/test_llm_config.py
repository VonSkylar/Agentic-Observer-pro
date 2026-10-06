import json
import unittest
from unittest.mock import patch

from llm_client import CompletionTruncated, LLMClient


class Response:
    def __init__(self, finish_reason="stop"):
        self.finish_reason = finish_reason

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"choices": [{"finish_reason": self.finish_reason,
                                       "message": {"content": '{"ok": true}'}}]}).encode()


class LLMConfigTests(unittest.TestCase):
    def request_payload(self, env):
        with patch.dict("os.environ", env, clear=True), patch("urllib.request.urlopen", return_value=Response()) as call:
            LLMClient()._request("system", {}, 10)
            return json.loads(call.call_args.args[0].data)

    def test_kimi_request_preserves_existing_compatible_shape(self):
        body = self.request_payload({})
        self.assertEqual(body["max_tokens"], 2000)
        self.assertNotIn("thinking", body)
        self.assertNotIn("reasoning_effort", body)

    def test_official_deepseek_flash_delivers_bounded_json_without_reasoning(self):
        body = self.request_payload({"OPENAI_BASE_URL": "https://api.deepseek.com/v1", "OPENAI_MODEL": "deepseek-flash"})
        self.assertEqual(body["max_tokens"], 2000)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", body)

    def test_explicit_thinking_override_budgets_reasoning_and_json(self):
        body = self.request_payload({"OPENAI_BASE_URL": "https://api.deepseek.com/v1", "OPENAI_MODEL": "deepseek-flash",
                                     "PRO_MODEL_THINKING": "enabled"})
        self.assertEqual(body["max_tokens"], 8192)
        self.assertEqual(body["reasoning_effort"], "low")

    def test_provider_specific_options_do_not_leak_to_other_endpoints(self):
        body = self.request_payload({"OPENAI_BASE_URL": "https://api.deepseek.com.example/v1", "OPENAI_MODEL": "deepseek-flash"})
        self.assertNotIn("thinking", body)
        self.assertEqual(body["max_tokens"], 2000)

    def test_overrides_and_disabled_thinking_omit_effort(self):
        body = self.request_payload({"OPENAI_BASE_URL": "https://api.deepseek.com/v1", "OPENAI_MODEL": "deepseek-flash",
                                     "PRO_MODEL_MAX_TOKENS": "4000", "PRO_MODEL_THINKING": "disabled"})
        self.assertEqual(body["max_tokens"], 4000)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", body)

    def test_truncation_is_distinguishable_from_network_failure(self):
        with patch.dict("os.environ", {}, clear=True), patch("urllib.request.urlopen", return_value=Response("length")):
            with self.assertRaises(CompletionTruncated):
                LLMClient()._request("system", {}, 10)


if __name__ == "__main__":
    unittest.main()
