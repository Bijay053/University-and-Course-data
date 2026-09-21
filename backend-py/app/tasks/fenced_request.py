"""Parent-process death evidence; task IDs alone cannot identify a delivery.

An ordinary FAILURE, REVOKED backend result, revoke request, worker_ready signal,
missing inspect response or old heartbeat is NOT proof. Pool loss/termination
and hard-timeout callbacks only request observation. A pidfd bound to the
accepted OS process must confirm its actual exit before proof is persisted.
"""
import asyncio
import logging
import os
import select
import threading
import time
import weakref

from billiard.exceptions import Terminated, WorkerLostError
from celery import Task
from celery.worker.request import Request

from app.services.worker_fencing import process_identity, record_process_death

log = logging.getLogger(__name__)


def _wait_for_exit(task_id, identity, pidfd, proof):
    """A timeout callback is only a request to observe, never death evidence.

    The kernel pidfd belongs to the accepted process, not whichever process
    might later reuse its numeric PID. Readability confirms actual exit (also
    before the parent reaps a zombie). Keep the pool/result thread nonblocking:
    billiard may invoke on_timeout BEFORE it sends the terminating signal.
    """
    try:
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        events = poller.poll()  # even a delayed/uninterruptible kill must actually exit
        if not any(flags & select.POLLIN for _, flags in events):
            log.error("No authoritative exit event; keeping task %s fenced", task_id)
            return
        for attempt in range(3):
            try:
                asyncio.run(_persist_death(task_id, identity, proof))
                return
            except Exception:
                log.exception("Could not persist confirmed exit for task %s", task_id)
                if attempt < 2:
                    time.sleep(1)
    finally:
        os.close(pidfd)


async def _persist_death(task_id, identity, proof):
    # Parent callbacks must not reuse a child process's event-loop pool.
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.config import settings
    from app.database import postgres_tls_connect_args
    engine = create_async_engine(
        settings.database_url, connect_args=postgres_tls_connect_args(),
        pool_size=1, max_overflow=0,
    )
    try:
        async with async_sessionmaker(engine)() as db:
            await record_process_death(db, task_id, identity, proof)
    finally:
        await engine.dispose()


class FencedRequest(Request):
    _accepted_process_identity = None
    _accepted_pidfd = None
    _exit_observer_started = False

    def on_accepted(self, pid, time_accepted):
        try:
            identity = process_identity(pid)
            pidfd = os.pidfd_open(pid)
            try:
                if process_identity(pid) != identity:
                    raise ValueError("Accepted process identity changed")
            except BaseException:
                os.close(pidfd)
                raise
            self._accepted_process_identity = identity
            self._accepted_pidfd = pidfd
            # Celery's optimized Request subclass overrides on_success, so
            # cleanup cannot depend on our on_success being called.
            self._pidfd_finalizer = weakref.finalize(self, os.close, pidfd)
        except (OSError, ValueError, IndexError, AttributeError):
            # A process that died before /proc could be sampled cannot be
            # positively identified. Leave its claim fenced for investigation.
            log.warning("Cannot identify accepted process for task %s", self.id)
        return super().on_accepted(pid, time_accepted)

    def _observe_exit(self, proof):
        if (
            self._exit_observer_started
            or self._accepted_pidfd is None
            or self._accepted_process_identity is None
        ):
            return
        self._exit_observer_started = True
        # Observer owns a duplicate independently of Request garbage collection.
        try:
            fd = os.dup(self._accepted_pidfd)
        except OSError:
            self._exit_observer_started = False
            log.exception("Cannot observe exit for task %s; keeping it fenced", self.id)
            return
        observer = threading.Thread(
            target=_wait_for_exit,
            args=(self.id, self._accepted_process_identity, fd, proof),
            daemon=True, name="autonomous-worker-exit",
        )
        try:
            observer.start()
        except Exception:
            os.close(fd)
            self._exit_observer_started = False
            # Never prevent billiard from performing the actual kill because
            # our proof observer could not start.
            log.exception("Cannot start exit observer for task %s; keeping it fenced", self.id)

    def on_timeout(self, soft, timeout):
        if not soft:
            # Hard timeouts remove the billiard job before killing its process;
            # there need not be any subsequent WorkerLostError callback.
            self._observe_exit("worker_lost")
        return super().on_timeout(soft, timeout)

    def on_failure(self, exc_info, send_failed_event=True, return_ok=False):
        exc = exc_info.exception
        # Celery may wrap exceptions in ExceptionWithTraceback.
        exc = getattr(exc, "exc", exc)
        if not return_ok and isinstance(exc, (WorkerLostError, Terminated)):
            proof = "worker_lost" if isinstance(exc, WorkerLostError) else "terminated_acknowledgement"
            # Task-returned exceptions arrive here with return_ok=True and are
            # not pool death evidence. Even pool errors only schedule observation:
            # persist nothing until the accepted kernel process actually exits.
            self._observe_exit(proof)
        return super().on_failure(exc_info, send_failed_event, return_ok)


class FencedTask(Task):
    Request = "app.tasks.fenced_request:FencedRequest"