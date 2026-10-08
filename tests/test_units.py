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
