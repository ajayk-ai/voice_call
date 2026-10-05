import os
import sys

from dotenv import load_dotenv
from teler import Client

load_dotenv()

keys = ["TELER_API_KEY", "FREJUN_PHONE_NUMBER", "MY_PHONE_NUMBER", "PUBLIC_URL"]
missing = [k for k in keys if not os.getenv(k)]
if missing:
    sys.exit(f"Fill these in .env first: {', '.join(missing)}")

public_url = os.environ["PUBLIC_URL"].rstrip("/")

with Client(api_key=os.environ["TELER_API_KEY"]) as client:
    call = client.voice.calls.create(
        from_number=os.environ["FREJUN_PHONE_NUMBER"],
        to_number=os.environ["MY_PHONE_NUMBER"],
        flow_url=f"{public_url}/flow",
        status_callback_url=f"{public_url}/call-status",
        record=True,
    )
print("Calling you... call:", call)
