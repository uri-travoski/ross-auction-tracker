"""In-process scheduler.

One container runs both the web UI and these jobs, so everything lands in
``docker logs`` and there is no cron/root complexity.

===============================  ==========================================
``discovery``                    daily at ``schedule.discovery_at``
``change``                       every ``schedule.change_every_hours`` hours
``heartbeat``                    every ``schedule.heartbeat_minutes`` minutes:
                                 final-stretch polling, reminder emails,
                                 finalisation, status transitions
``report``                       hourly snapshot HTML export
===============================  ==========================================

Jobs never overlap (``max_instances=1``) and a missed run still fires within
``misfire_grace_time``, so a container restart cannot silently skip a reminder.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import Config
from .logging_setup import get_logger
from .pipeline import Pipeline
from .report import purge_old_reports, write_snapshot_report
from .store import Store
from .util import now_utc

log = get_logger(__name__)


class TrackerScheduler:
    """Owns the APScheduler instance and the jobs' shared lock."""

    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store
        # Scrape jobs share one lock: two cycles hitting the site at once would
        # be rude and would race on the same rows.
        self._scrape_lock = threading.Lock()
        self.scheduler = BackgroundScheduler(
            timezone=str(config.get("site.timezone", "UTC")),
            executors={"default": ThreadPoolExecutor(4)},
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 3600,
            },
        )

    # ------------------------------------------------------------------
    def _run(self, name: str, work: Callable[[Pipeline], Any]) -> None:
        """Run one job with its own pipeline, serialised against other scrapes."""
        if not self._scrape_lock.acquire(timeout=1800):
            log.warning("job skipped: another scrape is still running", extra={"job": name})
            return
        started = now_utc()
        try:
            with Pipeline(self.config, self.store) as pipeline:
                work(pipeline)
        except Exception:
            log.exception("scheduled job failed", extra={"job": name})
        finally:
            self._scrape_lock.release()
            log.debug(
                "job complete",
                extra={
                    "job": name,
                    "seconds": round((now_utc() - started).total_seconds(), 1),
                },
            )

    # -- jobs -----------------------------------------------------------
    def job_discovery(self) -> None:
        self._run("discovery", lambda p: p.run_discovery())

    def job_change(self) -> None:
        self._run("change", lambda p: p.run_change_scan())

    def job_heartbeat(self) -> None:
        self._run("heartbeat", lambda p: p.heartbeat())

    def job_report(self) -> None:
        try:
            write_snapshot_report(self.config, self.store)
            purge_old_reports(self.config)
        except Exception:
            log.exception("snapshot report failed")

    # ------------------------------------------------------------------
    def start(self, *, run_discovery_now: bool = False) -> BackgroundScheduler:
        schedule = self.config.section("schedule")

        hour, _, minute = str(schedule.get("discovery_at", "06:00")).partition(":")
        try:
            discovery_trigger: Any = CronTrigger(
                hour=int(hour), minute=int(minute or 0)
            )
        except ValueError:
            log.warning("invalid schedule.discovery_at; falling back to 06:00")
            discovery_trigger = CronTrigger(hour=6, minute=0)
        every_hours = int(schedule.get("discovery_every_hours", 24))
        if every_hours and every_hours != 24:
            # Sub-daily or multi-day discovery: use an interval instead.
            discovery_trigger = IntervalTrigger(hours=every_hours)

        self.scheduler.add_job(
            self.job_discovery,
            discovery_trigger,
            id="discovery",
            name="Discover new IT auctions (24h)",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.job_change,
            IntervalTrigger(hours=int(schedule.get("change_every_hours", 6))),
            id="change",
            name="Re-scan tracked auctions for changes (6h)",
            replace_existing=True,
            next_run_time=now_utc(),
        )
        self.scheduler.add_job(
            self.job_heartbeat,
            IntervalTrigger(minutes=int(schedule.get("heartbeat_minutes", 1))),
            id="heartbeat",
            name="Final-stretch polling, reminders, finalisation",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.job_report,
            IntervalTrigger(hours=1),
            id="report",
            name="Export the snapshot HTML report",
            replace_existing=True,
        )
        if run_discovery_now:
            self.scheduler.add_job(
                self.job_discovery,
                "date",
                run_date=now_utc(),
                id="discovery_now",
                name="Initial discovery run",
                replace_existing=True,
            )

        self.scheduler.start()
        for job in self.scheduler.get_jobs():
            log.info(
                "job scheduled",
                extra={"job": job.id, "job_name": job.name, "next_run": str(job.next_run_time)},
            )
        return self.scheduler

    def shutdown(self, wait: bool = False) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=wait)
            log.info("scheduler stopped")

    def describe(self) -> list[dict[str, str]]:
        return [
            {
                "id": job.id,
                "name": job.name,
                "next_run": str(getattr(job, "next_run_time", "")),
            }
            for job in self.scheduler.get_jobs()
        ]
