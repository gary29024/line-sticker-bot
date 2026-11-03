"""Start the bot:  python -m stickerbot"""

import logging

from . import config
from .bot import build_app


def main() -> None:
    settings = config.load()
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=getattr(logging, settings.log_level, logging.INFO),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every web request
    app = build_app(settings)
    # "Polling" = the bot keeps asking Telegram "any new messages?". No public web address needed.
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
