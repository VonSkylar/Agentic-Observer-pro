import unittest
from datetime import timedelta
from unittest.mock import patch

from handover import Handover, utc, declared_clock_hint, explicit_weather_clocks
from planner import Planner, REQUEST_MULT
from skymath import format_utc, local_sidereal_deg, radec_to_altaz


START = utc("2026-11-01T00:00:00Z")
END = START + timedelta(hours=6)


def fixture():
    lst = local_sidereal_deg(START, 0)
    return {
        "site": {"latitude_deg": 0, "longitude_deg": 0, "minimum_altitude_deg": 30},
        "survey": {"start_utc": format_utc(START), "end_utc": format_utc(END), "slot_seconds": 900,
                   "nights": [{"observing_start_utc": format_utc(START), "observing_end_utc": format_utc(END)}]},
        "instrument": {"grid_side": 4, "n_fibers": 16, "glass_side_deg": .5, "pitch_deg": .55,
                       "fov_side_deg": 2.2, "exposure": {"min_duration_seconds": 60, "max_duration_seconds": 3600}},
        "scoring": {"flux_zero_point": .5, "exposure_zero_point_seconds": 900, "q0": .68,
                    "airmass_exponent": .6, "program": {"bands": {"DARK": .65, "BRIGHT": .4},
                    "multipliers": {"DARK": 1.2, "BRIGHT": 1.12, "BACKUP": 1.06}, "mismatch_multiplier": 1},
                    "lunar_model": {"maximum_penalty": .75, "altitude_exponent": 1, "angular_decay_scale_deg": 35}},
        "targets": {"columns": ["target_id", "ra_deg", "dec_deg", "feature_flux", "science_weight", "required"],
                    "rows": [["near", lst + 20, 0, 1, 1, True], ["bright", lst + 25, 0, 2, 1, False],
                             ["faint", lst + 30, 0, .00001, 1, False], ["never", lst + 180, 0, 1, 1, False]]}}


def request(targets, count=1, reward=100, deadline=END):
    return {"request_id": "R", "target_ids": targets, "minimum_completed": count, "remaining_count": count,
            "completion_reward": reward, "completion_factor_threshold": .5,
            "issued_at_utc": format_utc(START), "deadline_utc": format_utc(deadline)}


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.h = Handover({"utc_offset_hours": 0})
        self.h.ingest([{"request_id": "r", "reason": "Corrected SW ridge 37 degrees.\nE clear within zenith distance 56 degrees.\n11/1 01:00-02:00 UTC rain: close ALL."}])
        self.ctx = self.h.context(START, END)

    def test_only_sourced_finite_numeric_heights_are_used(self):
        item = {"source_request_id": "r", "source_line": 1, "directions": ["SW"], "altitude_deg": 37}
        for value in [True, float("nan"), -1, 90, 32]:
            self.assertEqual(Handover.validate_environment({"terrain": [{**item, "altitude_deg": value}]}, self.ctx)[0], [])
        terrain, _ = Handover.validate_environment({"terrain": [item, {**item, "directions": ["E"], "source_line": 2, "altitude_deg": 34}]}, self.ctx)
        self.assertEqual([t["altitude_deg"] for t in terrain], [37, 34])
        self.h.apply_environment(terrain, [])
        self.assertEqual(len(self.h.context(START, END)["known_terrain"]), 2)

    def test_original_sparse_line_numbers_are_validated(self):
        context = {**self.ctx, "request_texts": [{"request_id": "r", "reason_lines": [{"line": 3, "text": "11/1 01:00-02:00 UTC rain: close ALL."}]}]}
        item = {"source_request_id": "r", "source_line": 3, "effect": "closed", "directions": ["ALL"],
                "start_time": "2026-11-01T01:00:00", "end_time": "2026-11-01T02:00:00", "utc_offset_hours": 0}
        _, weather = Handover.validate_environment({"weather": [item]}, context)
        self.assertEqual(len(weather), 1)
        self.h.apply_environment([], weather)
        self.assertEqual(self.h.next_boundary(START, END), START + timedelta(hours=1))
        self.assertEqual(self.h.next_boundary(START + timedelta(hours=1), END), START + timedelta(hours=2))

    def test_corrected_clock_replaces_old_weather_and_uses_source_digits(self):
        self.h.ingest([{"request_id": "r", "reason": "11/1 01:00-03:00 UTC rain.\nCorrection: 11/1 02:00-04:00 UTC rain."}])
        item = {"source_request_id": "r", "effect": "closed", "directions": ["ALL"],
                "start_time": "2026-11-01T01:00:00", "end_time": "2026-11-01T03:00:00", "time_zone": "UTC"}
        _, weather = Handover.validate_environment({"weather": [{**item, "source_line": n} for n in (1, 2)]}, self.h.context(START, END))
        self.assertEqual([(w["start"], w["end"]) for w in weather], [(START + timedelta(hours=2), START + timedelta(hours=4))])

    def test_old_source_does_not_recur_next_year(self):
        self.h.ingest([{"request_id": "r", "issued_at_utc": "2025-11-01T00:00:00Z", "reason": "11/1 01:00-02:00 UTC rain."}])
        item = {"source_request_id": "r", "source_line": 1, "effect": "closed", "directions": ["ALL"],
                "start_time": "2026-11-01T01:00:00", "end_time": "2026-11-01T02:00:00", "time_zone": "UTC"}
        self.assertEqual(Handover.validate_environment({"weather": [item]}, self.h.context(START, END))[1], [])

    def test_clock_span_crosses_year_boundary(self):
        start, end = explicit_weather_clocks("12/31 23:30–1/1 01:15 UTC", 2026)
        self.assertEqual((end - start).total_seconds(), 6300)
        self.assertEqual(end.year, 2027)

    def test_conflicting_horizons_use_safe_limit_and_replaced_text_invalidates_it(self):
        terrain, _ = Handover.validate_environment({"terrain": [
            {"source_request_id": "r", "source_line": 1, "directions": ["SW"], "altitude_deg": 37},
            {"source_request_id": "r", "source_line": 2, "directions": ["SW"], "altitude_deg": 34}]}, self.ctx)
        self.h.apply_environment(terrain, [])
        self.assertEqual(self.h.terrain["SW"]["altitude_deg"], 37)
        self.h.ingest([{"request_id": "r", "reason": "Replaced source; previous ridge measurement withdrawn."}])
        self.assertEqual(self.h.terrain, {})

    def test_declared_digit_hint_requires_public_mapping(self):
        self.assertIsNone(declared_clock_hint("RE 休止:DO SOL", "no digit legend"))
        self.assertEqual(declared_clock_hint("RE 休止:DO SOL", "do=1 re=2 sol=5 休止=0"), ["20:15"])

    def test_late_environment_does_not_clear_fault_schedule(self):
        self.h.add_operations([{"kind": "report_fault", "start": START, "end": START}])
        self.h.add_operations([])
        self.h.apply_environment([], [])
        self.assertEqual(self.h.due_fault(START)["start"], START)

    def test_postponement_requires_separate_confirmed_source(self):
        self.h.ingest([{"request_id": "r", "reason": "10/31 guider work at 01:30 local; if rain, postpone one day.\n10/31 rained; confirmed postpone one day."}])
        ctx = self.h.context(START, END)
        op = {"kind": "report_fault", "source_request_id": "r", "source_line": 1,
              "start_time": "2026-11-01T01:30:00", "time_zone": "local"}
        self.assertEqual(Handover.validate([op], ctx), [])
        self.assertEqual(Handover.validate([{**op, "source_lines": [1, 2]}], ctx)[0]["start"], START + timedelta(minutes=90))

    def test_signed_site_offset_and_named_night_midnight_test(self):
        h = Handover({"utc_offset_hours": -4})
        h.ingest([{"request_id": "r", "reason": "10/31 night flat-lamp test 01:00-01:15 local."}])
        op = {"kind": "test_window", "source_request_id": "r", "source_line": 1,
              "start_time": "2026-11-01T01:00:00", "end_time": "2026-11-01T01:15:00", "time_zone": "local"}
        self.assertEqual(Handover.validate([op], h.context(START, END))[0]["start"], START + timedelta(hours=5))
        wrong = {**op, "start_time": "2026-10-31T01:00:00", "end_time": "2026-10-31T01:15:00"}
        self.assertEqual(Handover.validate([wrong], h.context(START - timedelta(days=1), END)), [])

    def test_precise_terrain_replaces_only_sourced_default_sectors(self):
        p = Planner(fixture())
        p.terrain = {"SW", "NE"}
        p.set_environment([{"direction": "SW", "altitude_deg": 37}], [], START)
        self.assertEqual(p._direction_factor(40, 225), 1)
        self.assertEqual(p._direction_factor(37, 225), 0)
        self.assertEqual(p._direction_factor(40, 45), 0)

    def test_rain_stops_and_resumes_but_thin_cloud_allows_observing(self):
        p = Planner(fixture())
        rain = {"start": START, "end": START + timedelta(hours=1), "effect": "closed", "directions": ["ALL"]}
        p.set_environment([], [rain], START)
        self.assertTrue(p.site_closed())
        p.set_environment([], [rain], rain["end"])
        self.assertFalse(p.site_closed())
        p.set_environment([], [{**rain, "effect": "thin_cloud"}], START)
        self.assertFalse(p.site_closed())
        self.assertGreater(p._direction_factor(60, 0), 0)

    def test_exposure_never_crosses_a_known_terrain_horizon(self):
        init = fixture()
        lst = local_sidereal_deg(START, 0)
        # Setting in the west: starts at 45 degrees; crosses ridge during a long exposure.
        init["targets"]["rows"] = [["setting", (lst - 45) % 360, 0, .1, 100, True]]
        p = Planner(init)
        p.fast_level = 1
        p.set_environment([{"direction": "W", "altitude_deg": 40}], [], START)
        action = p.plan(START, END, 0, 0)
        self.assertIsNotNone(action)
        finish = START + timedelta(seconds=action["duration_seconds"])
        alt, _ = radec_to_altaz(lst - 45, 0, local_sidereal_deg(finish, 0), 0)
        self.assertGreaterEqual(alt, 40.6)


class RequestTests(unittest.TestCase):
    def test_selects_a_completable_group_and_caps_total_reward(self):
        p = Planner(fixture())
        p.on_requests([request(["near", "bright", "faint", "never"], count=2)])
        p._prepare_requests(START, 0)
        self.assertEqual(set(p.request_bonus), {0, 1})
        self.assertLessEqual(sum(p.request_bonus.values()), REQUEST_MULT * 100)

    def test_no_completion_bonus_for_impossible_whole_group(self):
        p = Planner(fixture())
        p.on_requests([request(["near", "faint"], count=2)])
        p._prepare_requests(START, 0)
        self.assertEqual(p.request_bonus, {})

    def test_exposure_must_fit_deadline_and_overlapping_thresholds_are_separate(self):
        p = Planner(fixture())
        p.request_bonus = {0: 100}
        p.request_terms = {0: [(START, START + timedelta(seconds=600), .5, 50),
                              (START, END, .9, 50)]}
        self.assertEqual(p._request_gain(0, 900, .3, START), 0)
        self.assertGreater(p._request_gain(0, 600, 1, START), 0)
        self.assertEqual(p._request_gain(0, 600, 1, START - timedelta(seconds=1)), 0)

    def test_cached_selection_does_not_restore_unselected_targets(self):
        p = Planner(fixture())
        r = request(["near", "bright", "faint"], count=1)
        p.on_requests([r])
        p._prepare_requests(START, 0)
        selected = dict(p.request_bonus)
        p.on_requests([r])
        with patch.object(p, "_request_opportunity", side_effect=AssertionError("must reuse plan")):
            p._prepare_requests(START, 0)
        self.assertEqual(p.request_bonus, selected)
        r["remaining_count"] = 0
        p.on_requests([r])
        p._prepare_requests(START, 0)
        self.assertEqual(p.request_bonus, {})

    def test_required_urgency_counts_visible_nights_and_resync_reopens_targets(self):
        p = Planner(fixture())
        self.assertEqual(p.required_windows[0], [0])
        p.factor[0], p.cur[0] = 1, 1.2
        p.active.remove(0)
        p._resync({"best_scores": []})
        self.assertEqual(p.factor[0], 0)
        self.assertIn(0, p.active)


if __name__ == "__main__":
    unittest.main()
