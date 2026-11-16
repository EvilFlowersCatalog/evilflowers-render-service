"""Configuration: the kit's ``RuntimeSettings`` plus the render service's own fields.

``RuntimeSettings`` (evilflowers-worker) brings the broker, the visibility timeout, the shutdown grace,
the delivery cap (``MAX_DELIVERIES``), ``STORAGE_PATH`` with its path guard (``storage_file``),
``IMAGE_TAG`` and ``LOG_FORMAT``. A standalone job holds no catalog credential.
"""

from functools import lru_cache
from pathlib import Path

from evilflowers_worker.settings import RuntimeSettings
from pydantic import AliasChoices, Field, model_validator

#: The digitalizer's queue for this service (issue #1), on its broker (Valkey db 8).
STANDALONE_QUEUE = "evilflowers_render.standalone"


class Settings(RuntimeSettings):
    # -- Broker ---------------------------------------------------------------------------------------------------
    #: The digitalizer's broker. In k8s the Deployment sets BROKER (kv_url.digitalizer_dev, Key-Value note).
    redis_url: str = Field("redis://localhost:6379/8", validation_alias=AliasChoices("BROKER", "REDIS_URL"))
    queue: str = Field(STANDALONE_QUEUE, validation_alias=AliasChoices("RENDER_QUEUE", "QUEUE"))

    # -- Time limits (issue #1: "a long book must finish under the task time limit") ------------------------------
    #: The book's own deadline, checked between pages and bounding every subprocess. Below the orchestrator's
    #: 8 h render deadline (the contract comment), so the outcome says "time limit" before the run is lost.
    task_soft_time_limit: int = Field(27_000, gt=0)
    #: Celery's hard kill (set on the task). The gap to the soft limit covers one veraPDF run, the md5 of the
    #: media and the outcome write.
    task_time_limit: int = Field(28_200, gt=0)

    # -- Scale-in (the kit's `shutdown`): as ocr-service, a book is requeued rather than waited for -----------------
    worker_soft_shutdown_timeout: float = Field(10.0, ge=0)
    worker_cancel_timeout: float = Field(10.0, ge=0)

    # -- Rendering ------------------------------------------------------------------------------------------------
    #: Used when the request carries no `dpi` (the digitalizer's StudioRenderer default).
    default_dpi: int = Field(600, gt=0)
    #: Paths of the tools; None: PATH (recode_pdf: next to the interpreter first, as in the digitalizer).
    recode_pdf: str | None = None
    verapdf: str = "verapdf"
    #: The PDF/A check is part of the outcome; without veraPDF the job fails instead of reporting null.
    verapdf_required: bool = True
    #: Outline bookmarks + page labels on both PDFs (the digitalizer's `finalize`).
    finalize: bool = True

    # -- Logs -----------------------------------------------------------------------------------------------------
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _limits(self) -> "Settings":
        if self.task_time_limit <= self.task_soft_time_limit:
            raise ValueError("TASK_TIME_LIMIT must exceed TASK_SOFT_TIME_LIMIT")
        if self.task_time_limit >= self.broker_visibility_timeout:
            # Redis would redeliver a book that is still rendering: two workers on one output_dir.
            raise ValueError("TASK_TIME_LIMIT must stay below BROKER_VISIBILITY_TIMEOUT")
        return self

    @property
    def storage_root(self) -> Path:
        return Path(self.storage_path).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
