import unittest

from coach.analysis import Thresholds, analyze, render

FTP = 250


def details(power, hr=None, speed=8.0, gaps=()):
    """Garmin get_activity_details() payload at 1 Hz; `gaps` = (start_s, length_s) pauses with no samples."""
    keys = ["directTimestamp", "directPower", "directHeartRate", "directSpeed", "directBikeCadence"]
    skip = {t for s, n in gaps for t in range(s, s + n)}
    rows = []
    for t, p in enumerate(power):
        if t in skip:
            continue
        rows.append({"metrics": [1_700_000_000_000 + t * 1000, p, (hr or [140] * len(power))[t], speed, 90]})
    return {"metricDescriptors": [{"key": k, "metricsIndex": i} for i, k in enumerate(keys)],
            "activityDetailMetrics": rows}


def summary(seconds, **extra):
    return {"activityId": 1, "activityType": {"typeKey": "road_biking"}, "activityName": "test",
            "movingDuration": seconds, "elapsedDuration": seconds, "duration": seconds, **extra}


def intervals(reps, on_w, on_s=300, off_s=180, warm=600):
    p = [150] * warm
    for w in (on_w if isinstance(on_w, list) else [on_w] * reps):
        p += [w] * on_s + [140] * off_s
    return p + [140] * 600


class AnalysisTests(unittest.TestCase):
    th = Thresholds(ftp_watts=FTP, lthr_bike=170, lthr_run=172, easy_hr_cap=145)

    def run_analysis(self, power, **kw):
        return analyze(summary(len(power)), [], details(power, **kw), self.th)

    def test_detects_even_intervals(self):
        a = self.run_analysis(intervals(4, 240))
        self.assertEqual(len(a["reps"]), 4)
        self.assertIn("threshold intervals 4×", a["session_type"])
        self.assertTrue(any(f["level"] == "good" and "Even reps" in f["text"] for f in a["flags"]))

    def test_flags_first_rep_too_hard_and_fade(self):
        a = self.run_analysis(intervals(4, [285, 240, 236, 225]))
        texts = " ".join(f["text"] for f in a["flags"])
        self.assertIn("Rep 1 went out too hard", texts)
        self.assertIn("Faded", texts)

    def test_lone_surge_is_not_an_interval_set(self):
        p = [170] * 3600
        p[1800:1920] = [300] * 120
        a = self.run_analysis(p)
        self.assertEqual(a["reps"], [])

    def test_stop_inside_rep_is_flagged(self):
        p = [150] * 900 + [260] * 1200 + [140] * 600
        a = self.run_analysis(p, gaps=[(1300, 60)])
        self.assertTrue(any("not continuous" in f["text"] for f in a["flags"]))

    def test_render_is_markdown(self):
        out = render(self.run_analysis(intervals(3, 240)))
        self.assertTrue(out.startswith("### "))
        self.assertIn("| # |", out)


if __name__ == "__main__":
    unittest.main()
