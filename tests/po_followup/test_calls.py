"""Scenario 3 call results: checking what the model read from a transcript, FreJun callbacks, the call script token."""

from datetime import date

import pytest

from backend.po_followup.services import calls
from backend.voice_agent import call_context

TODAY = date(2026, 10, 31)
ASKED = [{"po": "4500567101", "item": "10", "desc": "Bonnet", "due": "7 Sep", "pending": "3", "uom": "EA", "od": "54"},
         {"po": "4500568052", "item": "10", "desc": "Gear housing", "due": "11 Sep", "pending": "8", "uom": "EA", "od": "50"},
         {"po": "4500568052", "item": "20", "desc": "Gear cover", "due": "11 Sep", "pending": "8", "uom": "EA", "od": "50"}]


def line(po="4500567101", item="10", status="in_production", committed="2026-11-12", reason="steel was late", cat="material_shortage"):
    return {"po": po, "item": item, "status": status, "committed_date": committed, "delay_reason": reason,
            "reason_category": cat, "evidence": "steel late"}


def read(lines, **extra):
    return calls.normalise({"outcome": "reached", "summary": "s", "confidence": "high", "lines": lines, **extra}, ASKED, TODAY)


def test_a_clean_answer_has_no_flags_and_finds_the_earliest_date():
    a = read([line(), line("4500568052", "10", committed="2026-11-05"), line("4500568052", "20", status="dispatched", committed="")])
    assert a["flags"] == [] and not a["needs_review"]
    assert a["earliest_commitment"] == "2026-11-05"


def test_model_mistakes_are_repaired_or_flagged_not_trusted():
    a = read([
        line("4 5 0 0 5 6 7 1 0 1", "Bonnet"),                           # digits read back with spaces, description as item
        line("4500568052", "10", committed="next week"),                 # not a date
        line("4500568052", "20", committed="2026-10-01"),                # in the past
        line("999", "1", status="weird", cat="invented"),                # not a line we asked about, invented values
    ])
    first, second, third, fourth = a["lines"]
    assert (first["po"], first["item"]) == ("4500567101", "10") and first["committed_date"] == "2026-11-12"
    assert second["committed_date"] == "" and third["committed_date"] == ""
    assert fourth["status"] == "unknown" and fourth["reason_category"] == "other"
    flags = " | ".join(a["flags"])
    assert "'next week' is not a valid date" in flags and "in the past" in flags and "not one of the lines" in flags
    assert a["needs_review"] and a["follow_up_needed"]


def test_a_line_that_needs_a_date_but_has_none_is_flagged():
    a = read([line(committed="")])
    assert any("no firm dispatch date" in f for f in a["flags"])
    assert any("not in the analysis" in f for f in a["flags"])  # the other two lines were never covered


def test_low_confidence_and_garbage_input_are_safe():
    a = calls.normalise({"outcome": "???", "confidence": "dunno", "lines": ["x", None, {"po": "4500567101"}]}, ASKED, TODAY)
    assert a["outcome"] == "incomplete" and a["confidence"] == "low" and a["needs_review"]
    assert a["lines"][0]["status"] == "unknown"
    assert calls.normalise({}, [], TODAY)["lines"] == []


def test_a_call_where_the_vendor_never_spoke_is_not_analysed():
    assert not calls.vendor_spoke([{"role": "agent", "text": "Hello?"}])
    assert calls.vendor_spoke([{"role": "agent", "text": "Hello?"}, {"role": "vendor", "text": "Haan"}])
    assert "VENDOR: Haan" in calls.transcript_text([{"role": "vendor", "text": "Haan"}, {"role": "agent", "text": "Hi"}])


@pytest.mark.parametrize("payload, call_id, recording", [
    ({"call_id": "abc", "status": "completed", "recording_url": "https://x/r.mp3"}, "abc", "https://x/r.mp3"),
    ({"id": "abc", "data": {"recordingUrl": "https://x/r.mp3"}}, "abc", "https://x/r.mp3"),
    ({"event": "ringing"}, None, None),
])
def test_frejun_callbacks_are_matched_by_call_id_and_may_carry_a_recording(payload, call_id, recording):
    assert calls._find(payload, calls.CALL_ID_KEYS) == call_id
    assert calls._find(payload, calls.RECORDING_KEYS) == recording


def test_the_call_script_token_names_the_calls_row():
    token = call_context.encode("ACME (400200)", "ACME", "400200", [], "k123")
    script = call_context.decode(token)
    assert (script["key"], script["code"]) == ("k123", "400200")
    old = call_context.decode(call_context.encode("ACME (400200)", "ACME", "400200", []))  # a call without a record
    assert old["key"] == ""


def test_masked_numbers_never_reach_the_page():
    assert calls.mask("919876543210") == "91******210" and calls.mask("123") == "123"
