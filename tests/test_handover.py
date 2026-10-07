import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from advisor import Advisor
from agent import ObserverAgent, RulesOnly
from handover import Handover, utc
from skymath import format_utc


START = utc("2026-10-02T00:00:00Z")
END = utc("2026-10-02T09:00:00Z")
TEXT = "Engineering correction: guider work at 2026-10-02 01:30 UTC, report then. Flat test 02:00-02:15 UTC, do not report."
REQUEST = {"request_id": "R1", "issued_at_utc": format_utc(START),
           "deadline_utc": format_utc(END), "reason": TEXT}


def operation(kind="report_fault", start="2026-10-02T01:30:00Z", end=None):
    result = {"kind": kind, "start_utc": start, "source_request_id": "R1", "evidence": TEXT}
    if end is not None:
        result["end_utc"] = end
    return result


class DoneCall:
    def __init__(self, answer):
        self.answer = answer

    def done(self):
        return True

    def wait(self, seconds):
        return True


class FakeClient:
    def __init__(self, plan=None):
        self.inputs = {}
        self.plan = plan

    def submit(self, tag, system, user, left):
        self.inputs[tag] = user
        answer = {"night_plan": self.plan, "fault_review": {"fault_likely": 0.8},
                  "confirm_report": {"report": True}}[tag]
        return DoneCall(answer)

    def collect(self, call):
        return call.answer


def agent_at(now=START, advisor=None):
    # Exercise the real agent decision loop with a small deterministic planner double.
    agent = ObserverAgent.__new__(ObserverAgent)
    planner = Mock()
    planner.current_night.return_value = (0, START, END)
    planner.notices = []
    planner.scale = 1.0
    planner.e_hours = []
    planner.min_exposure = 300
    planner.site_closed.return_value = False
    planner.plan.side_effect = lambda n, end, *_: {"action": "observe", "duration_seconds": int((end-n).total_seconds()),
                                                 "assignments": {"0": "T1"}, "program": "P1"}
    planner.forget_quality_history = Mock()
    agent.planner = planner
    agent.start = START
    agent.handover = Handover({"utc_offset_hours": -4})
    agent.advisor = advisor or RulesOnly()
    agent.night_seen = 0
    agent.last_now = None
    agent.sim_step_ema = None
    agent.forecast_notices = []
    agent.quake_on = False
    agent.quake_onset_hours = agent.quake_last_hours = -1e9
    agent.scale_hours = {}
    agent.reports = agent.correct_reports = agent.false_reports = agent.false_since_correct = agent.paid_false = 0
    agent.last_report_hours = agent.ref_from_hours = -1e9
    agent.free_allowance = 2
    agent.observes = 0
    agent.model_wait = 0
    agent.fault_likely = None
    agent.episode_blocked = False
    agent._pace = Mock()
    agent._fault_verdict = Mock(return_value=False)
    agent._to_next_slot = Mock(return_value=900)
    return agent


class HandoverTests(unittest.TestCase):
    def setUp(self):
        self.handover = Handover({"utc_offset_hours": -4})
        self.handover.ingest([REQUEST])
        self.context = self.handover.context(START, END)

    def test_original_text_reaches_both_night_calls_and_confirmation(self):
        client = FakeClient({"bad_night": False, "avoid_directions": [], "operations": [operation()]})
        advisor = Advisor(client)
        plan, fault = advisor.start_night("2026-10-01", [], [], {}, 100, 0, self.context)
        for tag in ("night_plan", "fault_review"):
            self.assertEqual(client.inputs[tag]["handover"]["request_texts"][0]["reason_lines"][0]["text"], TEXT)
            self.assertEqual(client.inputs[tag]["handover"]["site"]["utc_offset_hours"], -4)
        self.assertEqual(plan["operations"][0]["start"], START + timedelta(hours=1.5))
        agent = agent_at(advisor=advisor)
        agent.handover = self.handover
        agent.free_allowance = 0
        agent._fault_table = Mock(return_value={})
        agent._model_wait_budget = Mock(return_value=0)
        self.assertTrue(agent._model_agrees(1.5, {"now_utc": "2026-10-02T01:30:00Z"}))
        self.assertEqual(client.inputs["confirm_report"]["handover"]["request_texts"][0]["reason_lines"][0]["text"], TEXT)

    def test_numbered_source_lines_preserve_text_and_reject_unknown_references(self):
        valid = {**operation(), "source_line": 1}
        valid.pop("evidence")
        self.assertEqual(len(Handover.validate([valid], self.context)), 1)
        for line in (0, 2, True, "1"):
            self.assertEqual(Handover.validate([{**valid, "source_line": line}], self.context), [])

    def test_timezone_conversion_is_explicit_and_deduplicated(self):
        a = operation(start="2026-10-01T21:30:00-04:00")
        b = operation(start="2026-10-02T10:30:00+09:00")
        ops = Handover.validate([a, b], self.context)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["start"], START + timedelta(hours=1.5))

    def test_source_clock_conversion_is_done_by_code(self):
        raw = {"kind": "report_fault", "source_request_id": "R1", "source_line": 1,
               "start_time": "2026-10-02T10:30:00", "utc_offset_hours": 9}
        self.assertEqual(Handover.validate([raw], self.context)[0]["start"], START + timedelta(hours=1.5))
        for offset in (True, "9", float("nan"), 25):
            self.assertEqual(Handover.validate([{**raw, "utc_offset_hours": offset}], self.context), [])

    def test_old_explicit_event_cannot_be_rescheduled_to_current_date(self):
        self.handover.ingest([{**REQUEST, "reason": "Confirmed guider-camera work: 10/1 21:30 local."}])
        context = self.handover.context(START, END)
        raw = {"kind": "report_fault", "source_request_id": "R1", "source_line": 1,
               "start_time": "2026-10-03T21:30:00", "utc_offset_hours": -4}
        self.assertEqual(Handover.validate([raw], context), [])
        raw["start_time"] = "2026-10-01T21:30:00"
        self.assertEqual(Handover.validate([raw], context)[0]["start"], START + timedelta(hours=1.5))

    def test_focused_plan_preserves_conventions_and_original_line_numbers(self):
        self.handover.ingest([{**REQUEST, "reason": "Decode with the written key.\n10/1 instrument work.\n10/8 instrument work."}])
        context = self.handover.context(START, END)
        plan_context = Handover.night_context(context)
        self.assertEqual([r["line"] for r in plan_context["request_texts"][0]["reason_lines"]], [1, 2])
        self.assertEqual(len(context["request_texts"][0]["reason_lines"]), 3)

    def test_malformed_unsourced_and_outside_night_operations_rejected(self):
        invalid = [operation(start="2026-10-02T01:30:00"), operation(start="bad"),
                   operation(start=format_utc(END)), operation(start="2026-10-01T01:30:00Z"),
                   {**operation(), "evidence": "made up"}, {**operation(), "source_request_id": "unknown"},
                   operation("test_window", "2026-10-02T02:15:00Z", "2026-10-02T02:00:00Z"), None]
        self.assertEqual(Handover.validate(invalid, self.context), [])

    def test_test_window_crossing_night_is_clipped(self):
        ops = Handover.validate([operation("test_window", "2026-10-01T23:45:00Z", "2026-10-02T00:15:00Z")], self.context)
        self.assertEqual(ops[0]["start"], START)
        self.handover.apply(ops)
        self.assertEqual(self.handover.test_end(START), START + timedelta(minutes=15))
        self.assertIsNone(self.handover.test_end(START + timedelta(minutes=15)))

    def test_decoding_conventions_survive_request_expiry(self):
        self.handover.ingest([{**REQUEST, "reason": "decoding conventions " * 30}])
        self.handover.ingest([{"request_id": "R2", "reason": "new handover " * 30,
                              "issued_at_utc": format_utc(END), "deadline_utc": "2026-10-09T09:00:00Z"}])
        context = self.handover.context(START + timedelta(days=6), END + timedelta(days=6))
        self.assertEqual({r["request_id"] for r in context["request_texts"]}, {"R1", "R2"})

    def test_fault_onset_reports_without_waiting_for_low_E_and_only_once(self):
        agent = agent_at()
        agent.handover.apply(Handover.validate([operation()], self.context))
        payload = {"now_utc": "2026-10-02T01:30:00Z", "active_requests": [REQUEST]}
        self.assertEqual(agent._respond(payload)["action"], "report")
        self.assertEqual(agent.reports, 1)
        # A false report must not cause another attempt at the same scheduled event.
        agent._on_report_result({"correct": False}, 1.5)
        self.assertEqual(agent._respond({"now_utc": "2026-10-02T04:00:00Z"})["action"], "observe")
        self.assertEqual(agent.reports, 1)

    def test_correct_repair_covers_old_events_but_not_future_faults(self):
        agent = agent_at()
        agent._on_report_result({"correct": True}, 2)
        ops = Handover.validate([operation(), operation(start="2026-10-02T04:00:00Z")], self.context)
        agent.handover.apply(ops)
        self.assertIsNone(agent.handover.due_fault(START + timedelta(hours=3)))
        self.assertEqual(agent.handover.due_fault(START + timedelta(hours=4))["start"], START + timedelta(hours=4))

    def test_tests_wait_without_heuristic_report_then_resume_observing(self):
        agent = agent_at()
        agent._fault_verdict.return_value = True
        agent.handover.apply(Handover.validate([operation("test_window", "2026-10-02T02:00:00Z", "2026-10-02T02:15:00Z")], self.context))
        result = agent._respond({"now_utc": "2026-10-02T02:00:00Z"})
        self.assertEqual(result["until_utc"], "2026-10-02T02:15:00Z")
        agent._fault_verdict.assert_not_called()
        agent._fault_verdict.return_value = False
        self.assertEqual(agent._respond({"now_utc": result["until_utc"]})["action"], "observe")

    def test_test_wait_stops_for_a_confirmed_fault_inside_the_test_window(self):
        agent = agent_at()
        agent.handover.apply(Handover.validate([operation("test_window", "2026-10-02T01:00:00Z", "2026-10-02T02:00:00Z"),
                                              operation()], self.context))
        action = agent._respond({"now_utc": "2026-10-02T01:00:00Z"})
        self.assertEqual(action["until_utc"], "2026-10-02T01:30:00Z")
        self.assertEqual(agent._respond({"now_utc": action["until_utc"]})["action"], "report")

    def test_observe_and_weather_wait_cannot_cross_operating_boundary(self):
        agent = agent_at()
        agent.handover.apply(Handover.validate([operation()], self.context))
        action = agent._respond({"now_utc": "2026-10-02T01:00:00Z"})
        self.assertEqual(action["duration_seconds"], 1800)
        self.assertEqual(agent.planner.plan.call_args.args[1], START + timedelta(hours=1.5))
        agent.planner.site_closed.return_value = True
        action = agent._respond({"now_utc": "2026-10-02T01:28:00Z"})
        self.assertEqual(action["until_utc"], "2026-10-02T01:30:00Z")
        self.assertEqual(agent._respond({"now_utc": action["until_utc"]})["action"], "report")

    def test_model_failure_and_rules_only_keep_original_fallback(self):
        client = FakeClient(None)
        advisor = Advisor(client)
        self.assertIsNone(advisor.start_night("2026-10-01", [], [], {}, 100, 0, self.context)[0])
        agent = agent_at(advisor=advisor)
        self.assertEqual(agent._respond({"now_utc": format_utc(START), "active_requests": [REQUEST]})["action"], "observe")
        self.assertEqual(agent.handover.records["R1"]["reason"], TEXT)
        self.assertEqual(RulesOnly().start_night("2026-10-01", [], [], {}, 100, 0, self.context), (None, None))

    def test_paid_scheduled_report_can_be_vetoed_but_lifetime_errors_do_not_disable_new_faults(self):
        agent = agent_at()
        agent.handover.apply(Handover.validate([operation()], self.context))
        agent.free_allowance = 0
        agent._model_agrees = Mock(return_value=False)
        self.assertIsNone(agent._handover_report(1.5, {"now_utc": "2026-10-02T01:30:00Z"}))
        self.assertEqual(agent.reports, 0)
        self.assertIsNone(agent.handover.due_fault(START + timedelta(hours=3)))
        agent.handover.attempted_faults.clear()
        agent.false_reports = 8
        agent._model_agrees.return_value = True
        self.assertEqual(agent._handover_report(1.5, {"now_utc": "2026-10-02T01:30:00Z"})["action"], "report")

    def test_correct_repair_rearms_consecutive_protection(self):
        agent = agent_at()
        agent.false_reports = 20
        agent.false_since_correct = 8
        agent.sourced_false = 3
        agent.episode_blocked = True
        agent._on_report_result({"correct": True}, 1)
        self.assertEqual(agent.false_reports, 20)
        self.assertEqual(agent.false_since_correct, 0)
        self.assertEqual(agent.sourced_false, 0)
        self.assertFalse(agent.episode_blocked)
        agent._fault_verdict.return_value = True
        self.assertEqual(agent._maybe_report(4, {"now_utc": "2026-10-02T04:00:00Z"})["action"], "report")

    def test_repeated_bad_sources_cool_down_without_permanent_disable(self):
        agent = agent_at()
        agent.sourced_false = 3
        agent.last_report_hours = 0
        op = {"kind": "report_fault", "start": START + timedelta(hours=49), "end": START + timedelta(hours=49),
              "source_request_id": "new"}
        agent.handover.apply([op])
        self.assertIsNone(agent._handover_report(24, {"now_utc": format_utc(START + timedelta(hours=24))}))
        self.assertEqual(agent._handover_report(49, {"now_utc": format_utc(op["start"])})["action"], "report")

    def test_late_background_reply_applies_schedule(self):
        agent = agent_at()
        raw = {"bad_night": False, "avoid_directions": [], "reason": "", "operations": Handover.validate([operation()], self.context)}
        agent.advisor = SimpleNamespace(night_date="2026-10-01", poll=lambda: (raw, None))
        self.assertEqual(agent._respond({"now_utc": "2026-10-02T01:30:00Z"})["action"], "report")


if __name__ == "__main__":
    unittest.main()
