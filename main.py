import asyncio
import logging
import sys
import time
from typing import Any, Dict, List, Optional

from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes

from handlers import setup_handlers
from params import Params
from rss import check_channels_and_notify, dispatch_pending_notifications
from state import StateManager
from youtube import sync_all_subscriptions

class TelegramGetUpdatesFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "getUpdates" not in record.getMessage()


def format_duration(seconds: float) -> str:
    secs = int(round(seconds))
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    rem_secs = secs % 60
    if mins < 60:
        return f"{mins}m{rem_secs}s"
    hours = mins // 60
    rem_mins = mins % 60
    return f"{hours}h{rem_mins}m{rem_secs}s"


class APSchedulerJobDurationFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.job_start_times = {}

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and len(record.args) >= 1:
            if "Running job" in record.msg:
                self.job_start_times[str(record.args[0])] = time.monotonic()
            elif "executed successfully" in record.msg and ", took " not in record.msg:
                job_key = str(record.args[0])
                start = self.job_start_times.pop(job_key, None)
                if start is not None:
                    duration = format_duration(time.monotonic() - start)
                    record.msg = f"{record.msg}, took {duration}"
        return True


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

# Append execution duration to apscheduler job execution logs
job_duration_filter = APSchedulerJobDurationFilter()
logging.getLogger("apscheduler.executors.default").addFilter(job_duration_filter)
logging.getLogger("apscheduler").addFilter(job_duration_filter)

logger = logging.getLogger("ytsub")


async def scheduled_rss_check(context: ContextTypes.DEFAULT_TYPE):
    state: StateManager = context.job.data["state"]

    async def send_fn(chat_id: int, text: str, reply_markup: Optional[Any] = None, parse_mode: Optional[Any] = None):
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            disable_web_page_preview=False,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )

    try:
        await check_channels_and_notify(state=state, send_message_fn=send_fn)
    except Exception as e:
        logger.error("Error in scheduled RSS check: %s", e)


async def scheduled_subscription_sync(context: ContextTypes.DEFAULT_TYPE):
    state: StateManager = context.job.data["state"]
    params: Params = context.job.data["params"]

    async def send_fn(chat_id: int, text: str):
        await context.bot.send_message(chat_id=chat_id, text=text)

    try:
        await sync_all_subscriptions(state=state, params=params, send_message_fn=send_fn)
    except Exception as e:
        logger.error("Error in scheduled subscription sync: %s", e)


async def notify_admins(app, params: Params):
    for admin_id in params.admin_user_ids:
        try:
            await app.bot.send_message(chat_id=admin_id, text="YTSub bot started.")
        except Exception as e:
            logger.warning("Failed to send startup message to admin %s: %s", admin_id, e)


async def notification_dispatcher_loop(app, state: StateManager, params: Params):
    logger.info("Notification dispatcher started (max %d posts/min per user).", params.max_posts_per_min)
    user_send_times: Dict[int, List[float]] = {}

    async def send_fn(chat_id: int, text: str, reply_markup=None, parse_mode=None):
        await app.bot.send_message(
            chat_id=chat_id,
            text=text,
            disable_web_page_preview=False,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )

    while True:
        try:
            users_with_pending = state.get_users_with_pending_notifications()
            if users_with_pending:
                await dispatch_pending_notifications(
                    state=state,
                    send_message_fn=send_fn,
                    max_posts_per_min=params.max_posts_per_min,
                    user_send_times=user_send_times
                )
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            logger.info("Notification dispatcher stopped.")
            break
        except Exception as e:
            logger.error("Error in notification dispatcher loop: %s", e)
            await asyncio.sleep(2.0)


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

    # Schedule periodic subscription sync (every 12 hours and at startup)
    sub_sync_interval = params.subscription_sync_interval_sec if params.subscription_sync_interval_sec > 0 else 43200
    app.job_queue.run_repeating(
        scheduled_subscription_sync,
        interval=sub_sync_interval,
        first=1,
        data={"state": state, "params": params},
        name="subscription_sync"
    )
    logger.info("Scheduled subscription sync job every %d seconds.", sub_sync_interval)

    # Send startup message to admins and start background notification dispatcher
    async def post_init(application):
        await notify_admins(application, params)
        application.create_task(notification_dispatcher_loop(application, state, params))

    app.post_init = post_init

    logger.info("Starting YTSub bot...")
    app.run_polling()


if __name__ == "__main__":
    main()
