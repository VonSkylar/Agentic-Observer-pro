import json
import threading
import unittest
import urllib.error
from unittest.mock import patch

from advisor import Advisor, WEATHER_KINDS
from handover import Handover, utc
from llm_client import LLMClient


class Response:
    def __init__(self, content='{"ok":true}', finish="stop"):
        self.content, self.finish = content, finish

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"choices": [{"finish_reason": self.finish, "message": {"content": self.content}}],
                           "usage": {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 800,
                                     "completion_tokens": 100, "completion_tokens_details": {"reasoning_tokens": 30}}}).encode()


class ModelCostTests(unittest.TestCase):
    def test_unicode_is_lossless_and_not_literal_escape_text(self):
        source = {"reason": "导星相机訂正 カメラ", "path": r"literal\n"}
        with patch.dict("os.environ", {}, clear=True), patch("urllib.request.urlopen", return_value=Response()) as request:
            LLMClient()._request("system", source, 10)
        content = json.loads(request.call_args.args[0].data)["messages"][1]["content"]
        self.assertEqual(json.loads(content), source)
        self.assertIn(source["reason"], content)
        self.assertNotIn(r"\u5bfc", content)

    def test_single_flight_and_exact_cache_do_not_mix_dates_or_mutable_answers(self):
        entered, release = threading.Event(), threading.Event()
        def respond(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return Response()
        with patch.dict("os.environ", {}, clear=True), patch("urllib.request.urlopen", side_effect=respond) as request:
            client = LLMClient()
            first = client.submit("night_plan", "s", {"night": 1}, 100)
            self.assertTrue(entered.wait(2))
            self.assertIs(client.submit("night_plan", "s", {"night": 1}, 100), first)
            release.set()
            self.assertTrue(first.wait(2))
            first.answer["ok"] = False
            cached = client.submit("night_plan", "s", {"night": 1}, 100)
            self.assertTrue(cached.answer["ok"])
            changed = client.submit("night_plan", "s", {"night": 2}, 100)
            self.assertTrue(changed.wait(2))
            self.assertEqual(request.call_count, 2)

    def test_account_errors_stop_retries_and_future_paid_requests(self):
        for status in (401, 402):
            with self.subTest(status=status), patch.dict("os.environ", {}, clear=True), patch(
                    "urllib.request.urlopen", side_effect=urllib.error.HTTPError("url", status, "secret error body", {}, None)) as request:
                logs = []
                client = LLMClient(log=logs.append, max_retries=3)
                call = client.submit("night_plan", "s", {}, 100)
                self.assertTrue(call.wait(2))
                self.assertEqual(call.error, f"HTTP_{status}")
                self.assertIsNone(client.submit("fault_review", "s", {"night": 2}, 100))
                self.assertEqual(request.call_count, 1)
                self.assertNotIn("secret error body", " ".join(logs))

    def test_delivered_invalid_and_truncated_replies_are_not_billed_three_times(self):
        for reply in (Response(content="invalid"), Response(finish="length")):
            with patch.dict("os.environ", {}, clear=True), patch("urllib.request.urlopen", return_value=reply) as request:
                client = LLMClient(max_retries=3)
                call = client.submit("night_plan", "s", {}, 100)
                self.assertTrue(call.wait(2))
                self.assertIsNone(call.answer)
                self.assertEqual(request.call_count, 1)
                self.assertEqual(client.stats["completion_tokens"], 100)

    def test_only_transient_errors_retry_and_socket_timeout_uses_remaining_budget(self):
        with patch.dict("os.environ", {}, clear=True), patch("time.sleep"), patch("urllib.request.urlopen", side_effect=[
                urllib.error.HTTPError("url", 503, "busy", {}, None), Response()]) as request:
            client = LLMClient()
            call = client.submit("fault_review", "s", {}, 100)
            self.assertTrue(call.wait(2))
            self.assertEqual(call.answer, {"ok": True})
            self.assertEqual(request.call_count, 2)
            self.assertLessEqual(request.call_args_list[1].kwargs["timeout"], request.call_args_list[0].kwargs["timeout"])

    def test_usage_counts_uncollected_completions_and_reasoning_once(self):
        with patch.dict("os.environ", {"OPENAI_BASE_URL": "https://api.deepseek.com/v1", "OPENAI_MODEL": "deepseek-flash"}, clear=True), patch(
                "urllib.request.urlopen", return_value=Response()):
            client = LLMClient()
            call = client.submit("fault_review", "s", {}, 100)
            self.assertTrue(call.wait(2))  # no collect(): late/abandoned handles also cost money
            self.assertEqual(client.stats["prompt_tokens"], 1000)
            self.assertEqual(client.stats["reasoning_tokens"], 30)
            self.assertAlmostEqual(client.stats["estimated_cny"], (200*2 + 800*.04 + 100*8)/1e6)

    def test_request_limit_counts_retries_not_only_night_handles(self):
        with patch.dict("os.environ", {}, clear=True), patch("time.sleep"), patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")) as request:
            client = LLMClient(max_calls=1, max_retries=3)
            call = client.submit("fault_review", "s", {}, 100)
            self.assertTrue(call.wait(2))
            self.assertEqual(request.call_count, 1)
            self.assertIsNone(client.submit("night_plan", "s", {}, 100))

    def test_cache_eviction_is_bounded(self):
        with patch.dict("os.environ", {"PRO_MODEL_CACHE_SIZE": "1"}, clear=True), patch("urllib.request.urlopen", return_value=Response()) as request:
            client = LLMClient()
            for night in (1, 2, 1):
                self.assertTrue(client.submit("night_plan", "s", {"night": night}, 100).wait(2))
            self.assertEqual(request.call_count, 3)
            self.assertEqual(len(client._cache), 1)

    def test_source_content_and_line_references_survive_compaction(self):
        h = Handover({"utc_offset_hours": -4})
        h.ingest([{"request_id": "r", "reason": "Conventions\n10/01 guider work at midnight"}])
        before = h.context(utc("2026-10-02T00:00:00Z"), utc("2026-10-02T09:00:00Z"))
        after = Handover.model_context(before)
        self.assertEqual(before["request_texts"], after["request_texts"])
        self.assertEqual(before, after)
        self.assertLess(list(after).index("request_texts"), list(after).index("night_start_utc"))

    def test_no_text_weather_matches_prompt_and_keeps_fault_review(self):
        class Client:
            def __init__(self): self.tags = []
            def submit(self, tag, *args): self.tags.append(tag); return None
        for kind in WEATHER_KINDS | {"earthquake", "rocket_launch"}:
            for direction in ("ALL", "SW"):
                with self.subTest(kind=kind, direction=direction):
                    c = Client()
                    a = Advisor(c)
                    plan, fault = a.start_night("date", [{"event_kind": kind, "direction": direction}], [], {}, 100, 0)
                    self.assertEqual(plan["bad_night"], kind in WEATHER_KINDS - {"cold_snap"} and direction == "ALL")
                    self.assertEqual(plan["avoid_directions"], ["SW"] if kind in WEATHER_KINDS and direction == "SW" else [])
                    self.assertEqual(c.tags, ["fault_review"])

    def test_unknown_short_text_still_calls_model(self):
        class Client:
            def __init__(self): self.tags = []
            def submit(self, tag, *args): self.tags.append(tag); return None
        h = Handover({})
        h.ingest([{"request_id": "r", "reason": "re mi do"}])
        c = Client()
        Advisor(c).start_night("date", [], [], {}, 100, 0, h.context(utc("2026-10-02T00:00:00Z"), utc("2026-10-02T09:00:00Z")))
        self.assertEqual(c.tags, ["night_plan", "fault_review"])


if __name__ == "__main__":
    unittest.main()
