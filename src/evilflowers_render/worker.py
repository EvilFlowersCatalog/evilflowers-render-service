"""The Celery application: ``celery -A evilflowers_render.worker worker -E --queues=evilflowers_render.standalone``.

==========================  ==============================  ==========================================
Task                        Queue                           Sent by
==========================  ==============================  ==========================================
evilflowers_render.book     ``RENDER_QUEUE``                the books digitalizer (issue #1), one per book
==========================  ==============================  ==========================================

Built by the shared runtime, evilflowers-worker: ``create_app`` sets the Celery configuration every
worker on the broker shares (prefetch 1, the visibility timeout, task events), installs the scale-in
protocol (``shutdown``: SIGTERM is a warm shutdown, the book is cancelled and requeued after
``WORKER_SOFT_SHUTDOWN_TIMEOUT``) and the telemetry. Late acknowledgement and the hard time limit are
per task (``tasks``). The digitalizer learns the end from ``link`` / ``link_error``; there is no
result backend.
"""

import logging
import os

from celery import signals
from evilflowers_worker import observability, runtime

from evilflowers_render.settings import get_settings

TASK_PREFIX = "evilflowers_render"
SERVICE = "render"

ATTRIBUTES = {
    **observability.CATALOG_JOB_ATTRIBUTES,
    # The digitalizer's job within its run, and the IA item.
    "job": "evilflowers.standalone.job",
    "item_id": "evilflowers.item.id",
}

settings = get_settings()

# `service.name` = the k8s app name (Deploy-a-Service); the kit's default would be `evilflowers-render`.
os.environ.setdefault("OTEL_SERVICE_NAME", SERVICE)

app = runtime.create_app(
    SERVICE,
    settings,
    include=[f"{TASK_PREFIX}.tasks"],
    queues=[settings.queue],
    routes={f"{TASK_PREFIX}.*": settings.queue},
    attributes=ATTRIBUTES,
)


def _log_level(logger: logging.Logger | None = None, **_kwargs) -> None:
    """LOG_LEVEL on the worker's loggers: the deployment's command passes no `--loglevel` (Celery: WARNING)."""
    (logger or logging.getLogger()).setLevel(settings.log_level.upper())


signals.after_setup_logger.connect(_log_level, weak=False)
signals.after_setup_task_logger.connect(_log_level, weak=False)
