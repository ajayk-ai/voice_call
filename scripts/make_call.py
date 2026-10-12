"""Ring MY_PHONE_NUMBER with the voice agent (sample script), using the server at PUBLIC_URL.

    python scripts/make_call.py
"""

import os
import sys
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from anywhere

from teler import Client  # noqa: E402

from common import env  # noqa: E402,F401 - loads .env
from voice_agent import call_context  # noqa: E402


def main() -> None:
    keys = ["TELER_API_KEY", "FREJUN_PHONE_NUMBER", "MY_PHONE_NUMBER", "PUBLIC_URL"]
    missing = [k for k in keys if not os.getenv(k)]
    if missing:
        sys.exit(f"Fill these in .env first: {', '.join(missing)}")

    public_url = os.environ["PUBLIC_URL"].rstrip("/")
    with Client(api_key=os.environ["TELER_API_KEY"]) as client:
        call = client.voice.calls.create(
            from_number=os.environ["FREJUN_PHONE_NUMBER"],
            to_number=os.environ["MY_PHONE_NUMBER"],
            # the server only streams audio for calls carrying a ticket signed with TELER_API_KEY
            flow_url=f"{public_url}/flow?{urlencode({'ticket': call_context.ticket()})}",
            status_callback_url=f"{public_url}/call-status",
            record=True,
        )
    print("Calling you... call:", call)


if __name__ == "__main__":
    main()
