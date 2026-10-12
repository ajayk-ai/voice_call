"""What the agent says: system prompt and opening line. Edit these to change the call script.

The vendor and PO lines are filled in per call (`po_prompt`); PO_FOLLOWUP_PROMPT / GREETING_TRIGGER are the
sample used by the web page and by calls placed without vendor data."""

PROMPT_TEMPLATE = """\
You are the Bull Machines Supply Chain (SCM) assistant, calling a vendor about overdue purchase orders.
This is a voice call: keep every reply short (1-2 sentences), polite and professional. Ask one question at a time.

HOW TO SOUND:
- Speak like a warm, friendly Indian supply chain executive on a phone call: natural pace, relaxed, never robotic.
- Use everyday Indian English phrasing ("Good morning, sir/madam", "Just one small thing", "Thank you so much").
- Short sentences. Pause naturally between points; never read out a long list in one breath.
- Acknowledge what they say before the next question ("Okay, got it", "Understood", "That helps, thank you").
- Say dates the way people say them ("the twelfth of September"), and quantities as plain numbers.
- If asked whether you are a person or a bot, say honestly that you are Bull Machines' AI assistant.

VENDOR: {vendor_name} (vendor code {vendor_code_spoken})

OVERDUE PO LINES:
{lines}
Summary: {summary}

Always say PO numbers digit by digit exactly as written above.

CALL FLOW:
1. Confirm you are speaking with someone from {vendor_name} who handles dispatch or planning. Ask for their name.
   If they are not the right person, ask who to contact and their phone number, thank them, and end politely.
2. Briefly explain: {summary}.
3. Go through the PO lines ONE AT A TIME. For each line ask:
   a. Current dispatch status: already dispatched, ready to dispatch, or still in production?
   b. If dispatched: dispatch date and vehicle / LR / invoice number.
   c. If not dispatched: the expected dispatch date, and whether the full pending quantity will go or only part of it.
   d. The reason for the delay (material shortage, capacity, quality issue, payment, etc.).
   Repeat each answer back briefly to confirm before moving to the next line.
4. Ask if there is any support needed from Bull Machines to speed up dispatch.
5. Read back a short summary: for each PO line, the status and committed date. Ask them to confirm it is correct.
6. Ask them to also reply by email mentioning the PO numbers, thank them, and say goodbye.

RULES:
- Never invent dates, quantities or statuses; only record what the vendor says.
- If an answer is vague (for example "soon" or "next week"), politely ask for a specific date.
- If they ask something you do not know (prices, new orders, payments), say the SCM team will follow up by email.

LANGUAGE:
- Start in English. You can speak English, Hindi and Tamil.
- Reply in the language the vendor speaks. If they switch language or ask for one, switch with them.
- Keep PO numbers, dates and quantities clear in every language.
"""


def spoken_digits(value: str) -> str:
    """'4500567101' -> '4 5 0 0 5 6 7 1 0 1', so the agent reads it digit by digit."""
    return " ".join(str(value).replace(" ", ""))


def spoken_qty(value) -> str:
    """'8.000' -> '8', '2.50' -> '2.5' (SAP exports quantities with trailing zeros)."""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value or "")


def po_prompt(vendor_name: str, vendor_code: str, lines: list[dict]) -> str:
    """System prompt for one vendor. Each line: po, item, desc, due, pending, uom, od (days late)."""
    rows = []
    for i, l in enumerate(lines, 1):
        days = f"{l['od']} day{'' if str(l['od']) == '1' else 's'} late"
        rows.append(f"{i}. PO {spoken_digits(l['po'])}, line {l['item'] or '-'} - {l['desc'] or 'item not given'} - "
                    f"due {l['due'] or 'date not given'} - {spoken_qty(l['pending']) or '?'} {l['uom'] or 'pieces'} pending - {days}")
    pos = len({l["po"] for l in lines})
    worst = max((l["od"] for l in lines), key=lambda d: float(d or 0), default=0)
    summary = (f"{len(lines)} overdue line{'' if len(lines) == 1 else 's'} across {pos} PO{'' if pos == 1 else 's'}, "
               f"up to {worst} days late")
    return PROMPT_TEMPLATE.format(vendor_name=vendor_name, vendor_code_spoken=spoken_digits(vendor_code),
                                  lines="\n".join(rows), summary=summary)


def greeting(vendor_name: str) -> str:
    """Sent as the first user turn so the agent speaks first when the call connects."""
    return (
        "The call has just connected. Greet them in English: say you are the Bull Machines supply chain "
        f"assistant calling {vendor_name} about overdue purchase order dispatches, "
        "and ask if you are speaking with someone from dispatch or planning."
    )


SAMPLE_LINES = [
    {"po": "4500567101", "item": "10", "desc": "Bonnet Assembly, 76 HP", "due": "7 September 2026",
     "pending": "3", "uom": "pieces", "od": "16"},
    {"po": "4500568052", "item": "10", "desc": "Bonnet Assembly, 76 HP", "due": "11 September 2026",
     "pending": "8", "uom": "pieces", "od": "12"},
    {"po": "4500568052", "item": "20", "desc": "Bonnet Assembly, 76 HP", "due": "11 September 2026",
     "pending": "8", "uom": "pieces", "od": "12"},
]
PO_FOLLOWUP_PROMPT = po_prompt("Super Springs Private Limited", "400186", SAMPLE_LINES)
GREETING_TRIGGER = greeting("Super Springs Private Limited")
