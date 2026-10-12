import numpy as np

from po_sync.sync import build_tab, unique_names
from voice_agent.audio import Downsampler
from voice_agent.security import RateLimiter, mask_number


def test_downsampler_reduces_rate_by_three():
    tone = (np.sin(np.arange(2400) * 2 * np.pi * 440 / 24000) * 10000).astype(np.int16)
    out = Downsampler().process(tone.tobytes())
    assert abs(len(out) // 2 - 800) <= 25  # 100 ms of 24 kHz -> ~100 ms of 8 kHz


def test_downsampler_is_continuous_across_chunks():
    tone = (np.sin(np.arange(4800) * 2 * np.pi * 440 / 24000) * 10000).astype(np.int16).tobytes()
    whole = Downsampler().process(tone)
    d = Downsampler()
    split = d.process(tone[:3001 * 2]) + d.process(tone[3001 * 2 :])
    assert whole == split


def test_rate_limiter():
    limiter = RateLimiter(limit=2, window_seconds=60)
    assert [limiter.allow("a") for _ in range(3)] == [True, True, False]
    assert limiter.allow("b")


def test_mask_number():
    assert mask_number("+917200081289") == "+917******289"


def test_unique_column_names():
    headers = ["Material", "Mat. Desc", "OD Days\r", "", "Material", "1st Reminder"]
    assert unique_names(headers, "col_", {"sheet_row"}) == [
        "material", "mat_desc", "od_days", "col_4", "material_2", "c_1st_reminder",
    ]


def test_build_tab_skips_blank_rows_and_pads():
    tab = build_tab(1, "Master", "master", [["A", "B"], ["1", "", "extra"], ["", ""], ["x"]])
    assert tab.columns == ["a", "b", "col_3"]
    assert tab.rows == [(2, ["1", None, "extra"]), (4, ["x", None, None])]


def test_mask_phones_keeps_po_numbers():
    from monitoring.tracing import mask_phones

    assert mask_phones("call +91 98765 43210 / 09876543210 re PO 4500567101") == (
        "call 98765***** / 98765***** re PO 4500567101")


def test_turn_recorder_measures_reply_time():
    from types import SimpleNamespace as NS

    from voice_agent.turns import TurnRecorder

    class Trace:
        def turn(self, *a, **k):
            self.meta = k["metadata"]

    def sc(**k):
        base = dict(input_transcription=None, output_transcription=None, interrupted=False, turn_complete=False)
        return NS(data=None, usage_metadata=None, server_content=NS(**{**base, **k}))

    import voice_agent.turns as turns
    now = [1.0]
    real = turns.time.monotonic
    turns.time.monotonic = lambda: now[0]
    try:
        trace = Trace()
        r = TurnRecorder(trace)
        r.observe(sc(input_transcription=NS(text="hello")))  # caller heard at 1.0 s
        now[0] = 1.4
        r.observe(NS(data=b"x", usage_metadata=None, server_content=None))  # agent audio at 1.4 s
        assert r.observe(sc(turn_complete=True)) == ("hello", "")
    finally:
        turns.time.monotonic = real
    assert r.last_reply_ms == 400 and trace.meta["reply_ms"] == 400
