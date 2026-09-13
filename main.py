import logging
import sys

from telegram.ext import ApplicationBuilder, ContextTypes

from handlers import setup_handlers
from params import Params
from rss import check_channels_and_notify
from state import StateManager

class TelegramGetUpdatesFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "getUpdates" not in record.getMessage()


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# Suppress api.telegram.org getUpdates polling logs
get_updates_filter = TelegramGetUpdatesFilter()
logging.getLogger("httpx").addFilter(get_updates_filter)
logging.getLogger("httpcore").addFilter(get_updates_filter)
logging.getLogger("telegram").addFilter(get_updates_filter)
for handler in logging.root.handlers:
    handler.addFilter(get_updates_filter)

logger = logging.getLogger("ytsub")


async def scheduled_rss_check(context: ContextTypes.DEFAULT_TYPE):
    state: StateManager = context.job.data["state"]

    async def send_fn(chat_id: int, text: str):
        await context.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=False)

    try:
        await check_channels_and_notify(state=state, send_message_fn=send_fn)
    except Exception as e:
        logger.error("Error in scheduled RSS check: %s", e)


async def notify_admins(app, params: Params):
    for admin_id in params.admin_user_ids:
        try:
            await app.bot.send_message(chat_id=admin_id, text="YTSub bot started.")
        except Exception as e:
            logger.warning("Failed to send startup message to admin %s: %s", admin_id, e)


def main():
    params = Params()
    try:
        params.parse()
    except Exception as e:
        logger.error("Configuration error: %s", e)
        sys.exit(1)

    state = StateManager(params.state_file)
    state.load()

    app = ApplicationBuilder().token(params.bot_token).build()

    setup_handlers(app, params, state)

    # Schedule periodic RSS feed check
    interval = params.check_interval_sec if params.check_interval_sec > 0 else 300
    app.job_queue.run_repeating(
        scheduled_rss_check,
        interval=interval,
        first=15,
        data={"state": state},
        name="rss_check"
    )
    logger.info("Scheduled RSS polling job every %d seconds.", interval)

    # Send startup message to admins
    async def post_init(application):
        await notify_admins(application, params)

    app.post_init = post_init

    logger.info("Starting YTSub bot...")
    app.run_polling()


if __name__ == "__main__":
    main()
