"""Fast unit tests (no network, no models, no video files).

Run from the project folder:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import captions, config, feedback, reframe, scoring, songid
from app.copyright import _music_ranges
from app.transcribe import Segment, Word
from app.visual_check import classify_text


def seg(start, end, text):
    words, t = [], start
    parts = text.split()
    step = (end - start) / max(len(parts), 1)
    for p in parts:
        words.append(Word(t, t + step * 0.9, p))
        t += step
    return Segment(start, end, text, words)


class ConfigKeys(unittest.TestCase):
    """API keys live in .env, never in settings.json; old settings files migrate."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(config, "ENV_FILE", self.tmp / ".env"),
                        mock.patch.object(config, "SETTINGS_FILE", self.tmp / "settings.json"),
                        mock.patch.dict("os.environ", {}, clear=False)]
        for p in self.patches:
            p.start()
        for var in ("GEMINI_API_KEYS", "GEMINI_API_KEY", "GROQ_API_KEYS", "GROQ_API_KEY",
                    "ANTHROPIC_API_KEY", "GOOGLE_VISION_API_KEY"):
            import os
            os.environ.pop(var, None)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_migrates_keys_out_of_settings(self):
        (self.tmp / "settings.json").write_text(json.dumps(
            {"gemini_api_keys": ["g1", "g2"], "groq_api_key": "q0", "groq_api_keys": ["q1"],
             "whisper_model": "medium"}))
        s = config.load_settings()
        self.assertEqual(config.api_keys(s, "gemini"), ["g1", "g2"])
        self.assertEqual(config.api_keys(s, "groq"), ["q0", "q1"])
        self.assertEqual(s["whisper_model"], "medium")
        stored = json.loads((self.tmp / "settings.json").read_text())
        self.assertFalse([k for k in stored if "api_key" in k], "keys left in settings.json")
        self.assertIn("GEMINI_API_KEYS=g1,g2", (self.tmp / ".env").read_text())

    def test_save_writes_keys_to_env_only(self):
        config.save_settings({"groq_api_keys": ["a", "b"], "video_quality": 720})
        self.assertIn("GROQ_API_KEYS=a,b", (self.tmp / ".env").read_text())
        stored = (self.tmp / "settings.json").read_text()
        self.assertNotIn('"a"', stored)
        self.assertEqual(json.loads(stored)["video_quality"], 720)

    def test_env_file_parsing(self):
        (self.tmp / ".env").write_text('# comment\nGEMINI_API_KEYS="k1, k2"\nANTHROPIC_API_KEY=sk\n')
        s = config.load_settings()
        self.assertEqual(config.api_keys(s, "gemini"), ["k1", "k2"])
        self.assertEqual(s["anthropic_api_key"], "sk")


class Scoring(unittest.TestCase):
    def test_hook_scores(self):
        self.assertGreater(scoring._hook_score("Nobody talks about the biggest mistake you make?"), 0.6)
        self.assertLess(scoring._hook_score("and so yeah"), 0.2)

    def test_clean_ending_runs_to_end_of_sentence(self):
        segs = [seg(0, 4, "Here's why most people fail."), seg(4, 9, "They start without a plan."),
                seg(9, 14, "And the worst part is that"), seg(14, 18, "nobody ever tells them."),
                seg(18, 22, "Next topic now.")]
        c = scoring.Candidate(0, 12, 6, 0.5)
        scoring.refine_with_transcript(c, segs, min_len=10, max_len=30, duration=22, has_audience=False)
        self.assertGreaterEqual(c.end, 18, "clip stopped mid-sentence")
        self.assertLessEqual(c.end, 19)

    def test_unpunctuated_transcript_unchanged(self):
        segs = [seg(i * 4, i * 4 + 4, "some words here without stops") for i in range(6)]
        self.assertFalse(scoring._punctuated(segs))


class Captions(unittest.TestCase):
    def _events(self, style, vertical=True):
        segs = [seg(0, 3.2, "He made $5,000 in one insane week."), seg(3.3, 6, "Then he lost it all.")]
        with tempfile.TemporaryDirectory() as d:
            p = captions.write_ass(Path(d) / "c.ass", segs, 0, 7, 1080 if vertical else 1920,
                                   1920 if vertical else 1080, style)
            return [ln for ln in p.read_text().splitlines() if ln.startswith("Dialogue:")]

    @staticmethod
    def _t(ts):
        h, m, s = ts.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    def test_no_overlapping_phrases(self):
        for style in ("bold", "pop", "clean"):
            ev = [(self._t(e.split(",")[1]), self._t(e.split(",")[2])) for e in self._events(style)]
            for (a0, a1), (b0, b1) in zip(ev, ev[1:]):
                self.assertLessEqual(a1, b0 + 1e-6, f"{style}: captions overlap")

    def test_pop_colours_keywords(self):
        text = "\n".join(self._events("pop"))
        self.assertIn(captions.NUMBER_C, text)   # $5,000
        self.assertIn(captions.STRONG_C, text)   # INSANE
        self.assertIn(r"\t(", text)              # bounce animation

    def test_join_words(self):
        self.assertEqual(captions.join_words(["16", "-year", "-old", "Jason", "'s"]), "16-year-old Jason's")


class Feedback(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.p = mock.patch.object(feedback, "FILE", self.tmp / "feedback.json")
        self.p.start()
        feedback._cache = None

    def tearDown(self):
        self.p.stop()
        feedback._cache = None

    def _clip(self, name, **b):
        return {"name": name, "breakdown": {k: b.get(k, 0.5) for k in feedback.DIMS}}

    def test_no_change_below_min_votes(self):
        feedback.record("j", self._clip("a", hook=1.0), 1)
        self.assertEqual(feedback.weights(scoring.BASE_CONTENT_WEIGHTS), scoring.BASE_CONTENT_WEIGHTS)

    def test_likes_shift_weight_and_keep_total(self):
        for i in range(4):
            feedback.record("j", self._clip(f"g{i}", hook=0.9, emotion=0.2), 1)
            feedback.record("j", self._clip(f"b{i}", hook=0.2, emotion=0.9), -1)
        base = scoring.BASE_CONTENT_WEIGHTS
        w = feedback.weights(base)
        self.assertGreater(w["hook"] / base["hook"], 1.3)
        self.assertLess(w["emotion"] / base["emotion"], 0.8)
        self.assertAlmostEqual(sum(w.values()), sum(base.values()), places=6)
        for k in base:
            self.assertLessEqual(w[k] / base[k], feedback.CLAMP[1] * 1.5)

    def test_clearing_a_vote(self):
        feedback.record("j", self._clip("a"), 1)
        s = feedback.record("j", self._clip("a"), 0)
        self.assertEqual(s["likes"] + s["dislikes"], 0)


class Reframe(unittest.TestCase):
    def test_holds_still_inside_dead_zone(self):
        cam = reframe.camera_path([0.5 + 0.01 * (i % 2) for i in range(20)], crop_w=0.3164)
        self.assertLess(cam.max() - cam.min(), 1e-9)

    def test_follows_and_cuts(self):
        xs = [0.3] * 10 + [0.8] * 10            # shot change: face jumps across the frame
        cam = reframe.camera_path(xs, crop_w=0.3164)
        self.assertAlmostEqual(cam[5], 0.3, places=2)
        self.assertGreater(cam[11], 0.75)        # cut, not a slow pan

    def test_stays_inside_frame(self):
        cam = reframe.camera_path([0.01] * 10 + [0.99] * 10, crop_w=0.3164)
        self.assertGreaterEqual(cam.min(), 0.3164 / 2 - 1e-9)
        self.assertLessEqual(cam.max(), 1 - 0.3164 / 2 + 1e-9)

    def test_too_few_faces(self):
        self.assertIsNone(reframe.camera_path([None] * 18 + [0.5, 0.5], crop_w=0.3))

    def test_gaps_hold_nearest_face(self):
        cam = reframe.camera_path([0.4, None, None, 0.4, 0.4], crop_w=0.3)
        self.assertTrue(all(abs(c - 0.4) < 1e-9 for c in cam))


class Copyright(unittest.TestCase):
    def test_music_ranges(self):
        flags = [False] * 3 + [True] * 5 + [False] * 2 + [True] * 4 + [False] * 10 + [True]
        self.assertEqual(_music_ranges(flags), [(3, 14)])   # gap of 2 merged, 1-second blip dropped

    def test_shazam_parse(self):
        out = {"track": {"title": "Song", "subtitle": "Artist", "url": "u", "sections": [
            {"type": "SONG", "metadata": [{"title": "Album", "text": "A"}, {"title": "Label", "text": "L"}]}]}}
        r = songid._parse(out)
        self.assertEqual((r["song"], r["artist"], r["album"], r["label"]), ("Song", "Artist", "A", "L"))
        self.assertIsNone(songid._parse({"matches": []}))

    def _text(self, text, frames=6, share=0.6, where="top-right"):
        return {"text": text, "norm": re.sub(r"[^a-z@_ ]", " ", text.lower()).strip(), "frames": frames,
                "share": share, "times": [1.0, 30.0], "where": where, "raw": [text]}

    def test_classify_watermarks(self):
        kinds = {f["kind"] for f in classify_text([
            self._text("TikTok"), self._text("@someone_clips", frames=2, share=0.2),
            self._text("ESPN", where="bottom-right"), self._text("CapCut")])}
        self.assertEqual(kinds, {"platform", "handle", "brand"})   # CapCut = editor mark, ignored

    def test_stream_alert_seen_once_is_flagged(self):
        # real OCR output from a donation alert: glued + misspelled, visible in one frame
        found = classify_text([self._text("OTYFHayr donated", frames=1, share=0.06, where="bottom-left")])
        self.assertEqual([f["kind"] for f in found], ["stream"])

    def test_captions_with_apply_edits(self):
        segs = [seg(0, 3, "the quick brown fox."), seg(3, 6, "jumps over it.")]
        out = captions.apply_edits(segs, [{"start": 0, "end": 2.9, "text": "the quick brown cat."}])
        self.assertEqual(" ".join(w.text for s in out for w in s.words), "the quick brown cat. jumps over it.")
        self.assertAlmostEqual(out[0].words[3].start, segs[0].words[3].start)   # timing kept
        out = captions.apply_edits(segs, [{"start": 3, "end": 6, "text": ""}])  # delete a line
        self.assertEqual(" ".join(w.text for s in out for w in s.words), "the quick brown fox.")

    def test_captions_are_not_flagged(self):
        # changing subtitle lines appear in 1-2 frames and in the bottom center
        self.assertEqual(classify_text([self._text("and then he said", frames=2, share=0.15,
                                                   where="bottom center")]), [])


if __name__ == "__main__":
    unittest.main()
