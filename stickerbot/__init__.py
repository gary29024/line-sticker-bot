"""LINE -> Telegram sticker bot."""

import os

# Ask the Telegram library for its newer time format (avoids a deprecation warning).
os.environ.setdefault("PTB_TIMEDELTA", "1")
