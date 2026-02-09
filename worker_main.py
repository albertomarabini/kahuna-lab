# worker_main.py
"""
DB Queue Worker + Multi-App Router (STRICT)

Receiver logic (your question: "how do we know the receiver?")
--------------------------------------------------------------
Each QueueMessage row has a receiver_id column.

This worker process is identified by QUEUE_RECEIVER_ID (env var).
AsyncGuard polls ONLY messages where:
    QueueMessage.receiver_id == QUEUE_RECEIVER_ID

So: the *client* decides which worker should process a message by writing
receiver_id = <that worker's QUEUE_RECEIVER_ID>.

Routing logic (how we pick the right app/class)
-----------------------------------------------
Routing is NOT done by receiver_id. Routing is done by sender_id prefix.

We interpret QueueMessage.sender_id as:
    "<app_key><app_key_delim><project_id>"

Example sender_ids:
  - "bss_chat::proj_123" -> ChatApp (key="bss_chat", key_delim="::"), project_id="proj_123"
  - "jobs::proj_123"     -> JobsApp (key="jobs",     key_delim="::"), project_id="proj_123"

STRICT mode:
  - There is NO default/fallback app here.
  - If sender_id does not match any registered app prefix, the worker returns an error.

How many queues/workers are we administering?
---------------------------------------------
- One worker process administers exactly ONE logical queue:
    receiver_id == QUEUE_RECEIVER_ID
- All apps share that queue in the same process.
- max_concurrent is a GLOBAL concurrency cap across all apps combined.
- To create separate worker pools per app, you need different receiver_id values
  (run separate processes with different QUEUE_RECEIVER_ID).
"""

import os
import asyncio
import logging
import traceback
from typing import Any, Dict, List, Optional, Tuple, Callable
from sqlalchemy.orm import sessionmaker

from dotenv import load_dotenv

from classes.idempotency_cache import IDEMPOTENCY_CACHE
load_dotenv()


from classes.entities import Base, QueueMessage
from classes.backend import (
    Backend,
    _job_ctx_var,
    GLOBAL_BSS_HISTORY_CACHE,
)
from classes.GCConnection_hlpr import GCConnection


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(name)s\n%(message)s\n",
)
logger = logging.getLogger("kahuna_worker")

QUEUE_RECEIVER_ID = os.getenv("QUEUE_RECEIVER_ID")
CURRENCY = os.getenv("CURRENCY")
CONCURRENT_INSTANCES = int(os.getenv("CONCURRENT_INSTANCES"))


class JobContext:
    def __init__(
        self,
        host: "AppHost",
        job: Dict[str, Any],
        sender_full: str,
        project_id: str,
        app_prefix: str,
    ):
        self.host = host
        self.job = job
        self.sender_full = sender_full
        self.project_id = project_id
        self.app_prefix = app_prefix

    def emit(self, msg_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        payload = payload or {}
        payload.setdefault("correlation_id", self.job.get("id"))
        payload.setdefault("project_id", self.project_id)
        payload.setdefault("app_prefix", self.app_prefix)

        self.host._send_queue_message(
            to_receiver_id=self.sender_full,                 # send back to the original sender "address"
            msg_type=msg_type,
            payload=payload,
            from_sender_id=str(self.job.get("receiver_id")),  # worker id
        )


class AppHost:
    def __init__(self, Session, receiver_id: str, apps: List[Any]):
        self.SessionFactory = Session
        self.receiver_id = receiver_id
        self.apps = list(apps or [])

    def sweep(self) -> None:
        for app in self.apps:
            fn = getattr(app, "sweep", None)
            if callable(fn):
                fn()

    def _send_queue_message(
        self,
        to_receiver_id: str,
        msg_type: str,
        payload: Dict[str, Any],
        from_sender_id: str,
    ) -> None:
        session = self.SessionFactory()
        try:
            session.add(
                QueueMessage(
                    sender_id=str(from_sender_id),
                    receiver_id=str(to_receiver_id),
                    type=msg_type,
                    payload=payload,
                )
            )
            session.commit()
        finally:
            session.close()

    def _resolve_app(self, sender_full: str) -> Tuple[Any, str, str]:
        """
        STRICT: must match a registered app prefix.
        Returns: (app, matched_prefix, project_id)
        """
        candidates: List[Tuple[int, str, Any]] = []

        for app in self.apps:
            key = getattr(app, "key", "")
            delim = getattr(app, "key_delim", "")
            prefix = f"{key}{delim}"
            if not prefix:
                continue
            if sender_full.startswith(prefix):
                candidates.append((len(prefix), prefix, app))

        if not candidates:
            known = [f"{getattr(a,'key','')}{getattr(a,'key_delim','')}" for a in self.apps]
            raise RuntimeError(f"No app matched sender_id='{sender_full}'. Known prefixes: {known}")

        candidates.sort(key=lambda x: x[0], reverse=True)
        _, prefix, app = candidates[0]
        return app, prefix, sender_full[len(prefix):]

    def process_queue_job(self, job: Dict[str, Any]) -> None:
        sender_full = str(job.get("sender_id") or "")
        msg_type = job.get("type") or "unknown"

        app, prefix, project_id = self._resolve_app(sender_full)
        ctx = JobContext(self, job, sender_full, project_id, prefix)

        try:
            response_payload = app.handle(job, ctx)
            self._send_queue_message(
                to_receiver_id=sender_full,
                msg_type=f"{msg_type}_response",
                payload=response_payload,
                from_sender_id=str(job.get("receiver_id")),
            )
        except Exception as e:
            logger.info("Error processing job id=%s type=%s: %s", job.get("id"), msg_type, e)
            traceback.print_exc()
            self._send_queue_message(
                to_receiver_id=sender_full,
                msg_type=f"{msg_type}_response",
                payload={"status": "error", "message": str(e), "project_id": project_id},
                from_sender_id=str(job.get("receiver_id")),
            )


class ChatApp:
    """
    Chat backend app wrapper.
    Routes ANY request_type supported by Backend._process_request_data(...).

    KEY WIRING (what you asked for):
      sender_id must start with:  "bss_chat::"
    """
    key = "bss_chat"
    key_delim = "::"

    def __init__(self) -> None:
        # no per-instance Backend, we create one per job
        pass

    def sweep(self) -> None:
        removed2 = GLOBAL_BSS_HISTORY_CACHE.sweep_expired()
        if removed2:
            logger.debug("BSS HistoryCache sweep: removed %d expired BSS histories", removed2)
        removed3 = IDEMPOTENCY_CACHE.sweep_charged()
        if removed3:
            logger.debug("IdempotencyCache sweep: removed %d charged keys", removed3)

    def handle(self, job: Dict[str, Any], ctx: JobContext) -> Dict[str, Any]:
        backend = Backend()
        token = _job_ctx_var.set(ctx)
        try:
            # Backend stores/loads by project_id, not the prefixed sender_id
            job2 = dict(job)
            job2["sender_id"] = ctx.project_id
            return backend._process_request_data(job2)
        finally:
            _job_ctx_var.reset(token)


class Executor:
    def __init__(self, host: AppHost):
        self.host = host

    def execute(self, job: Dict[str, Any]) -> None:
        self.host.process_queue_job(job)


class AsyncGuard:
    def __init__(
        self,
        host: AppHost,
        receiver_id: str,
        poll_interval: float = 1.0,
        max_concurrent: int = 4,
    ):
        self.host = host
        self.receiver_id = receiver_id
        self.poll_interval = poll_interval
        self.max_concurrent = max_concurrent
        self._in_flight = set()
        self.SessionFactory = GCConnection().build_db_session_factory()

    async def _run_executor_for_message(self, job: Dict[str, Any]) -> None:
        executor = Executor(self.host)
        try:
            await asyncio.to_thread(executor.execute, job)
        finally:
            self._in_flight.discard(job["id"])

    async def run(self) -> None:
        logger.info("AsyncGuard running – receiver_id=%s (max_concurrent=%d)", self.receiver_id, self.max_concurrent)

        while True:
            self.host.sweep() # ! cleaning up the cache

            available_slots = self.max_concurrent - len(self._in_flight)
            if available_slots <= 0:
                await asyncio.sleep(self.poll_interval)
                continue

            session = self.SessionFactory()
            try:
                rows = (
                    session.query(QueueMessage)
                    .filter(QueueMessage.receiver_id == str(self.receiver_id))
                    .order_by(QueueMessage.created_at.asc())
                    .with_for_update(skip_locked=True)
                    .limit(available_slots)
                    .all()
                )

                jobs = [
                    {
                        "id": r.id,
                        "sender_id": r.sender_id,
                        "receiver_id": r.receiver_id,
                        "type": r.type,
                        "payload": r.payload,
                    }
                    for r in rows
                ]

                for r in rows:
                    session.delete(r)

                session.commit()
            finally:
                session.close()

            if not jobs:
                await asyncio.sleep(self.poll_interval)
                continue

            for job in jobs:
                if job["id"] in self._in_flight:
                    continue
                self._in_flight.add(job["id"])
                asyncio.create_task(self._run_executor_for_message(job))

            await asyncio.sleep(self.poll_interval)


def main() -> None:
    if not QUEUE_RECEIVER_ID:
        raise RuntimeError("QUEUE_RECEIVER_ID env var is required for DB queue mode")

    backend = Backend()

    # STRICT: every sender_id must match one of these prefixes:
    #   - "bss_chat::"
    #   - "jobs::"
    apps = [
        ChatApp(),
    ]
    host = AppHost(GCConnection().build_db_session_factory(), receiver_id=QUEUE_RECEIVER_ID, apps=apps)
    guard = AsyncGuard(
        host=host,
        receiver_id=QUEUE_RECEIVER_ID,
        max_concurrent=CONCURRENT_INSTANCES,  # set your desired cap here
    )
    asyncio.run(guard.run())


if __name__ == "__main__":
    main()
