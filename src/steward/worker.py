"""Run restart-safe ticks until SIGINT/SIGTERM; no seed is loaded implicitly."""

import logging
import os
import signal
from contextlib import ExitStack
from threading import Event

from steward.runtime import build_runtime


def main():
    stopping = Event()
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    logging.basicConfig(level=logging.INFO)
    # Telegram credentials are embedded in Bot API URLs; never log HTTP request URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    with ExitStack() as stack:
        runtime = stack.enter_context(build_runtime())
        review_runtime = None
        if os.getenv("STEWARD_REVIEW_ENABLED") == "true" and os.getenv(
            "STEWARD_REVIEW_ACCESS_CODE"
        ):
            from steward.review import build_review_runtime

            review_runtime = stack.enter_context(build_review_runtime(runtime._settings))
        while not stopping.is_set():
            try:
                if runtime._settings.ses_queue_url:
                    from steward.mail.receipt_queue import ReceiptQueue

                    ReceiptQueue(runtime, queue_url=runtime._settings.ses_queue_url).poll()
                if runtime._settings.telegram_test_polling:
                    from steward.channels.private_telegram import TestTelegramPoller

                    TestTelegramPoller(runtime).poll()
                report = runtime.tick()
                if runtime.telegram_enabled:
                    from steward.channels.notifications import TelegramNotices

                    TelegramNotices(runtime).dispatch()
                logging.info(
                    "tick inbound=%s outbound=%s failures=%s",
                    len(report.inbound.processed_external_ids),
                    len(report.outbound.deliveries),
                    len(report.workflow_failures) + len(report.outbound.failures),
                )
            except Exception as exc:
                logging.error("tick failed: %s", type(exc).__name__)
            if review_runtime is not None:
                try:
                    from steward.review import review_ready, tick_review

                    if not review_ready(review_runtime):
                        stopping.wait(10)
                        continue
                    report = tick_review(review_runtime)
                    logging.info(
                        "review tick inbound=%s outbound=%s failures=%s",
                        len(report.inbound.processed_external_ids),
                        len(report.outbound.deliveries),
                        len(report.workflow_failures) + len(report.outbound.failures),
                    )
                except Exception as exc:
                    logging.error("review tick failed: %s", type(exc).__name__)
            stopping.wait(10)
    return 0
