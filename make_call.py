import os
import sys

from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()

keys = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "MY_PHONE_NUMBER", "PUBLIC_URL"]
missing = [k for k in keys if not os.getenv(k)]
if missing:
    sys.exit(f"Fill these in .env first: {', '.join(missing)}")

client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

call = client.calls.create(
    to=os.environ["MY_PHONE_NUMBER"],
    from_=os.environ["TWILIO_PHONE_NUMBER"],
    url=f"{os.environ['PUBLIC_URL']}/incoming-call",
)
print("Calling you... SID:", call.sid)