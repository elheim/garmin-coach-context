import json
import sqlite3
import unittest
from datetime import date, timedelta

from coach import cycle
from coach.context import build_readiness
from coach.models import HealthMetrics
from coach.utils import classify_sport

TODAY = date.today()


def health(today_rhr=44, today_hrv=67):
    days = [HealthMetrics(metric_date=TODAY - timedelta(days=i), resting_hr=44, hrv_last_night=67,
                          sleep_score=80, sleep_duration_seconds=27000) for i in range(40)]
    days[0] = HealthMetrics(metric_date=TODAY, resting_hr=today_rhr, hrv_last_night=today_hrv,
                            sleep_score=80, sleep_duration_seconds=27000)
    return days


def luteal_day():
    payload = {"daySummary": {"dayInCycle": 22, "predictedCycleLength": 28, "lutealPhaseStart": 15,
                              "periodLength": 5}, "dailyLog": {"symptoms": ["BLOATING"]}}
    return {TODAY: {**cycle.parse_day(TODAY.isoformat(), payload), "symptoms": ["BLOATING"], "flags": []}}


class ReadinessTests(unittest.TestCase):
    def test_green_at_baseline(self):
        self.assertIn(": GREEN", build_readiness(health()).splitlines()[0])

    def test_red_when_resting_hr_jumps(self):
        self.assertIn(": RED", build_readiness(health(today_rhr=50)).splitlines()[0])

    def test_amber_on_moderate_shift(self):
        self.assertIn(": AMBER", build_readiness(health(today_rhr=47)).splitlines()[0])


class CycleTests(unittest.TestCase):
    def test_phases(self):
        self.assertEqual(cycle.phase_for(2, 28, 5, 15), "menstrual")
        self.assertEqual(cycle.phase_for(9, 28, 5, 15), "follicular")
        self.assertEqual(cycle.phase_for(14, 28, 5, 15), "ovulatory")
        self.assertEqual(cycle.phase_for(20, 28, 5, 15), "luteal")
        self.assertEqual(cycle.phase_for(26, 28, 5, 15), "late_luteal")

    def test_luteal_shift_is_not_read_as_fatigue(self):
        self.assertIn(": GREEN", build_readiness(health(today_rhr=47), luteal_day()).splitlines()[0])

    def test_context_file_reveals_nothing(self):
        out = build_readiness(health(today_rhr=47), luteal_day(), detailed=False).lower()
        for word in ("cycle", "luteal", "period", "menstr", "bloating"):
            self.assertNotIn(word, out)

    def test_amenorrhea_flag(self):
        row = cycle.parse_day(TODAY.isoformat(), {"daySummary": {"dayInCycle": 60}, "analysis": {"hasAmenorrhea": True}})
        flags = json.loads(row["flags"])
        self.assertEqual(flags, ["amenorrhea"])
        self.assertIn("RED-S", cycle.coaching_note({**row, "symptoms": [], "flags": flags}, True))

    def test_purge(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cycle.save_days(conn, [cycle.parse_day(TODAY.isoformat(), {"daySummary": {"dayInCycle": 3}})])
        self.assertEqual(len(cycle.load_days(conn)), 1)
        cycle.purge(conn)
        self.assertEqual(cycle.load_days(conn), {})


class ClassifyTests(unittest.TestCase):
    def test_categories(self):
        cases = {"road_biking": "bike", "virtual_ride": "bike", "indoor_cycling": "bike", "running": "run",
                 "treadmill_running": "run", "lap_swimming": "swim", "open_water_swimming": "swim",
                 "strength_training": "strength", "walking": "walk", "multi_sport": "multisport"}
        for raw, expected in cases.items():
            self.assertEqual(classify_sport(raw), expected, raw)


if __name__ == "__main__":
    unittest.main()
