"""Job queue with JSON persistence.

- one analysis job runs at a time (heavy CPU/GPU work)
- clip edits run on their own queue so they never wait behind a long job
- live "watch mode" runs on its own thread so it never blocks the queue
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from .config import JOBS_DIR
from .logs import JobLog, friendly, strip_ansi


class Job:
    def __init__(self, source: dict, options: dict, job_id: str | None = None, kind: str = "clips"):
        self.id = job_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.kind = kind                # clips | transcribe | enhance
        self.result: dict = {}          # tool outputs (transcribe / enhance)
        self.source = source            # {"type": "youtube", "url": ...} | {"type": "file", "upload_id": ...}
        self.options = options
        self.status = "queued"          # queued | running | done | error | cancelled
        self.stage = "Waiting in queue"
        self.progress = 0.0
        self.error: str | None = None
        self.meta: dict = {}
        self.clips: list[dict] = []
        self.timeline: dict = {}
        self.transcript_file: str | None = None
        self.watch = {"active": False, "rounds": 0, "next_check": None, "note": ""}
        self.warnings: list[str] = []   # non-fatal problems shown in the UI
        self.downloads: dict[str, dict] = {}   # live download tracker shown on the job page
        self.stages: list[dict] = []    # [{"stage", "at", "progress"}] for the step tracker
        self.started: float | None = None
        self.finished: float | None = None
        self.created = time.time()
        self.log: list[str] = []
        self.checkpoint: dict = {}      # finished steps a resumed job can skip (see pipeline.run)
        self.lock = threading.RLock()
        self._cancel = threading.Event()
        self.stop_watch = threading.Event()
        self._last_save = 0.0
        self._joblog: JobLog | None = None

    @property
    def dir(self) -> Path:
        path = JOBS_DIR / self.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ---- logging
    @property
    def joblog(self) -> JobLog:
        if self._joblog is None:
            self._joblog = JobLog(self.id, self.dir)
        return self._joblog

    @property
    def logger(self):
        return self.joblog.log

    def warn(self, message: str) -> None:
        """Non-fatal problem: logged and shown to the user."""
        self.warnings.append(message)
        self.log.append(f"{time.strftime('%H:%M:%S')}  WARNING: {message}")
        self.logger.warning(message)
        self.save()

    # ---- progress reporting
    def update(self, stage: str | None = None, progress: float | None = None) -> None:
        if stage and stage != self.stage:
            self.stage = stage
            self.log.append(f"{time.strftime('%H:%M:%S')}  {stage}")
            self.stages.append({"stage": stage, "at": round(time.time(), 2),
                                "progress": round(progress if progress is not None else self.progress, 3)})
            self.logger.info("STAGE %s (%.0f%%)", stage, 100 * (progress or self.progress))
        if progress is not None:
            self.progress = round(max(self.progress, min(progress, 1.0)), 4)
        if stage or time.time() - self._last_save > 3:
            self.save()

    def _eta(self) -> float | None:
        """Seconds left, estimated from progress so far (rough, but useful)."""
        if self.status != "running" or not self.started or self.progress < 0.08:
            return None
        elapsed = time.time() - self.started
        return round(elapsed * (1 - self.progress) / self.progress)

    def cancel(self) -> None:
        self._cancel.set()
        self.stop_watch.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def resumable(self) -> bool:
        """Failed, cancelled or interrupted jobs can be picked up again. Live streams that
        were being watched can't (the stream has moved on); start a new job for those."""
        return (self.status in ("error", "cancelled")
                and self.meta.get("live_status") != "is_live")

    def clip(self, name: str) -> dict | None:
        return next((c for c in self.clips if c["name"] == name), None)

    # ---- serialization
    def track_download(self, key: str, **fields) -> None:
        """Update one download's live stats (bytes, speed, status, attempt…)."""
        with self.lock:
            item = self.downloads.setdefault(key, {"key": key, "bytes": 0, "speed": 0,
                                                   "status": "queued", "since": time.time()})
            if fields.get("status") == "downloading" and item["status"] != "downloading":
                item["since"] = time.time()
            item.update(fields)
            if item["status"] in ("done", "failed", "unused"):
                item["speed"] = 0 if item["status"] != "done" else item.get("speed", 0)
                item.setdefault("ended", time.time())

    def download_summary(self) -> dict:
        with self.lock:
            items = [dict(i) for i in self.downloads.values()]
        shown = [i for i in items if i["status"] != "unused"]
        active = [i for i in shown if i["status"] in ("downloading", "retrying")]
        return {
            "items": shown,
            "bytes": sum(i.get("bytes") or 0 for i in items),
            "speed": sum(i.get("speed") or 0 for i in active),
            "active": len(active),
            "done": sum(i["status"] == "done" for i in shown),
            "failed": sum(i["status"] == "failed" for i in shown),
            "retries": sum(max((i.get("attempt") or 1) - 1, 0) for i in shown),
        }

    def to_dict(self, full: bool = True) -> dict:
        data = {
            "id": self.id, "kind": self.kind, "source": self.source, "status": self.status, "stage": self.stage,
            "progress": self.progress, "error": self.error, "meta": self.meta,
            "created": self.created, "clip_count": len(self.clips), "watch": self.watch,
            "warnings": self.warnings, "started": self.started, "finished": self.finished,
            "eta": self._eta(), "options": self.options, "resumable": self.resumable(),
        }
        if full:
            data.update(stages=self.stages[-80:], result=self.result,
                        downloads=self.download_summary())
            data.update(clips=self.clips, timeline=self.timeline, checkpoint=self.checkpoint,
                        transcript_file=self.transcript_file, log=self.log[-60:])
        return data

    def save(self) -> None:
        with self.lock:
            self._last_save = time.time()
            tmp = self.dir / "job.json.tmp"
            tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False), "utf-8")
            tmp.replace(self.dir / "job.json")

    @classmethod
    def load(cls, path: Path) -> "Job":
        d = json.loads(path.read_text("utf-8"))
        job = cls(d["source"], d.get("options", {}), d["id"], d.get("kind", "clips"))
        for key in ("status", "stage", "progress", "error", "meta", "clips", "timeline",
                    "transcript_file", "created", "log", "watch", "warnings", "stages", "result",
                    "started", "finished", "checkpoint"):
            setattr(job, key, d.get(key, getattr(job, key)))
        if job.status in ("queued", "running"):   # app was closed mid-job
            job.status, job.error = "error", "Interrupted (the app was closed). Click Resume to continue where it stopped."
        if job.watch.get("active"):
            job.watch.update(active=False, note="Watching stopped because the app was closed.")
        for c in job.clips:
            if c.get("status") == "rendering":
                c["status"] = "ready"
        return job


class JobManager:
    def __init__(self, runners: dict[str, Callable[[Job], None]], watcher: Callable[[Job], None]):
        self._runners, self._watcher = runners, watcher
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="job")
        self._edits = ThreadPoolExecutor(max_workers=1, thread_name_prefix="edit")
        self.jobs: dict[str, Job] = {}
        for f in sorted(JOBS_DIR.glob("*/job.json")):
            try:
                job = Job.load(f)
                self.jobs[job.id] = job
            except (OSError, ValueError, KeyError):
                continue

    def submit(self, source: dict, options: dict, kind: str = "clips") -> Job:
        job = Job(source, options, kind=kind)
        self.jobs[job.id] = job
        job.save()
        self._pool.submit(self._run, job)
        return job

    def _run(self, job: Job) -> None:
        with job.joblog.attach() as log:
            self._run_logged(job, log)

    def _run_logged(self, job: Job, log) -> None:
        if job.cancelled():
            job.status = "cancelled"
            job.save()
            return
        job.status = "running"
        job.started = t0 = time.time()
        log.info("JOB START %s kind=%s source=%s", job.id, job.kind, job.source)
        log.info("options: %s", job.options)
        try:
            self._runners[job.kind](job)
            job.status = "cancelled" if job.cancelled() else "done"
            if job.status == "done":
                job.update("Finished", 1.0)
        except Exception as exc:  # surfaced to the UI
            job.status = "cancelled" if job.cancelled() else "error"
            job.error = None if job.cancelled() else friendly(exc)
            if job.status == "error":
                log.exception("JOB FAILED at stage '%s': %s", job.stage, job.error)
                job.log.append(f"ERROR during '{job.stage}': {job.error}")
        job.finished = time.time()
        log.info("JOB END status=%s clips=%d in %.1fs", job.status, len(job.clips), time.time() - t0)
        if job.status == "done" and job.watch["active"]:
            job.save()
            threading.Thread(target=self._watch, args=(job,), daemon=True,
                             name=f"watch-{job.id}").start()
            return
        recorder = getattr(job, "_recorder", None)
        if recorder:
            recorder.stop()
        job.watch["active"] = False
        job.save()

    def _watch(self, job: Job) -> None:
        with job.joblog.attach() as log:
            log.info("WATCH START")
            try:
                self._watcher(job)
            except Exception as exc:
                msg = friendly(exc)
                log.exception("WATCH FAILED: %s", msg)
                job.watch["note"] = f"Watching stopped: {msg}"
                job.log.append(f"ERROR (watch): {msg}")
            finally:
                job.watch.update(active=False, next_check=None)
                log.info("WATCH END after %d checks", job.watch["rounds"])
                job.save()

    def resume(self, job: Job) -> Job:
        """Run a failed / cancelled / interrupted job again in place. Its checkpoints (audio,
        transcripts, chosen clips) and already-rendered clips are reused, so only the
        missing work is done."""
        if not job.resumable():
            raise ValueError("This job can't be resumed.")
        job._cancel.clear()
        job.stop_watch.clear()
        job.status, job.error, job.finished = "queued", None, None
        job.stage = "Waiting in queue (resuming)"
        job.log.append("Resumed: skipping the steps that already finished.")
        job.save()
        self._pool.submit(self._run, job)
        return job

    def submit_edit(self, job: Job, fn: Callable[[], None], label: str) -> None:
        def run():
            with job.joblog.attach() as log:
                log.info("EDIT START %s", label)
                try:
                    fn()
                    log.info("EDIT DONE %s", label)
                except Exception as exc:
                    msg = friendly(exc)
                    log.exception("EDIT FAILED %s: %s", label, msg)
                    job.warn(f"Editing {label} failed: {msg}")
        self._edits.submit(run)

    def delete(self, job_id: str) -> None:
        job = self.jobs.pop(job_id, None)
        if job:
            job.cancel()
            shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
