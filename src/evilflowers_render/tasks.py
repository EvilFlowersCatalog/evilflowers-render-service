"""The Celery task. Its name is a contract with the books digitalizer and must not change.

- ``acks_late`` + ``reject_on_worker_lost`` (``runtime.JOB_TASK_OPTIONS``): a worker that dies mid-book (OOM,
  node drain) gets the message redelivered. The redelivery is safe: the task writes only into its own
  ``output_dir`` and outcome file, atomically;
- ``time_limit``: Celery's hard kill. There is no Celery soft limit: the soft limit is the book's own deadline
  (``deadline``), checked between pages and bounding every subprocess.

The body is ``runtime.run_task``: it validates the kwargs, counts the delivery and, past ``MAX_DELIVERIES``,
calls ``book.give_up`` instead of rendering again.
"""

from typing import Any

from celery import shared_task
from evilflowers_worker import runtime
from pydantic import ValidationError

from evilflowers_render import book
from evilflowers_render import worker as _worker  # noqa: F401 - create_app must run in a spawned pool child too
from evilflowers_render.contract import TASK_NAME, BookJob, OutcomeAddress
from evilflowers_render.settings import get_settings


@shared_task(bind=True, name=TASK_NAME, time_limit=get_settings().task_time_limit, **runtime.JOB_TASK_OPTIONS)
def render_book(self, *_args, **kwargs) -> dict[str, Any]:
    """Kwargs only (``BookJob``: run_id, job, token, inputs, output_dir, outcome, request). Sent with
    ``link`` / ``link_error``; the return value stays compact (the event's ``result`` is capped)."""
    return runtime.run_task(
        self,
        lambda job, _delivery: book.run(job),
        BookJob,
        kwargs,
        on_capped=lambda job, delivery: book.give_up(job, delivery),
        on_invalid=_invalid,
    )


def _invalid(kwargs: dict[str, Any], error: ValidationError) -> dict[str, Any]:
    """Fail at once, never retried. When the outcome can still be written, it says why (the orchestrator shows it)."""
    message = f"task kwargs do not match the evilflowers_render.book contract: {error}"
    try:
        address = OutcomeAddress.model_validate(kwargs)
        book.check_outcome_path(address, get_settings())
    except (ValidationError, book.InvalidRequest):
        pass
    else:
        book.write_failed(address, get_settings(), message)
    raise book.InvalidRequest(message) from error
