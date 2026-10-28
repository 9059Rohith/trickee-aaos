"""Independent durable in-app reminder generation, with a stoppable server worker."""

from __future__ import annotations
import logging
import threading

log = logging.getLogger(__name__)


class ReminderWorker:
    """Use start()/stop() inside application lifespan; stop before closing SQLite.

    Each pass is one short SQLite transaction. UNIQUE(owner,event_id,revision)
    makes concurrent processes and restarts safe. No external transport is used.
    """

    def __init__(self, store, interval_seconds=30):
        if not 1 <= interval_seconds <= 3600:
            raise ValueError("Reminder interval must be between 1 and 3600 seconds")
        self.store = store
        self.interval_seconds = interval_seconds
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name="workspace-reminders", daemon=True
        )
        self.last_error = None

    def _run(self):
        self.store.worker_running = True
        try:
            while not self.stop_event.is_set():
                try:
                    self.store.generate_reminders()
                    self.last_error = None
                except Exception as exc:
                    self.last_error = str(exc)
                    log.exception(
                        "Workspace reminder generation failed; retrying next interval"
                    )
                if self.stop_event.wait(self.interval_seconds):
                    break
        finally:
            self.store.worker_running = False

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("Reminder worker did not stop before database shutdown")
