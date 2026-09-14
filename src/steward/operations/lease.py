"""Renew long-running job ownership using wall time, never simulated time sleeps."""

from threading import Event, Thread


class JobHeartbeat:
    def __init__(self, store, clock, job_id, token, lease_seconds):
        self.store, self.clock, self.job_id = store, clock, job_id
        self.token, self.seconds = token, lease_seconds
        self.stopped = Event()
        self.failure = None

    def __enter__(self):
        def renew():
            while not self.stopped.wait(max(0.1, self.seconds / 3)):
                try:
                    self.store.renew_job(
                        self.job_id,
                        token=self.token,
                        now=self.clock.now(),
                        lease_seconds=self.seconds,
                    )
                except Exception as exc:
                    self.failure = exc
                    return

        self.thread = Thread(target=renew, daemon=True, name="steward-job-lease")
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        self.thread.join(timeout=5)
