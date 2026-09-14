"""Run restart-safe ticks until SIGINT/SIGTERM; no seed is loaded implicitly."""

import logging
import signal
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
    with build_runtime() as runtime:
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
            stopping.wait(10)
    return 0
