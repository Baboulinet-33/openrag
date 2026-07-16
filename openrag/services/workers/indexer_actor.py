from __future__ import annotations

import asyncio
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.models.catalog import DocumentStatus
from core.models.document import Document
from services.workers.pipeline_builder import IndexingPipeline


class IndexerWorker:
    """Pure-Python core of the thin indexer actor.

    ``@ray.remote`` is not applied here so the class is directly
    instantiable in tests.  The production Ray actor wraps this class
    (or applies ``@ray.remote`` at startup).

    State transitions reported to *task_state_manager* are compatible
    with the existing queue-monitoring states:

    ``SERIALIZING`` — processing has started (parse + chunk + embed + store)
    ``COMPLETED``   — pipeline finished successfully
    ``FAILED``      — pipeline raised; set via ``set_failed_if_not_cancelled``

    Callers are responsible for setting ``QUEUED`` *before* dispatching
    the task, and for storing the object ref via ``set_object_ref``.

    Since #664 the worker is also the owner of the uploader's reserved
    quota slot: when the caller admitted the upload it already charged one
    ``users.file_count`` slot, and this worker either consumes it (by
    writing the catalog row) or releases it — see ``process_file``.

    Every transition is mirrored to *job_repo* (issue #660) so the outcome of a
    file survives a restart of this worker or of the state actor.
    """

    def __init__(
        self,
        pipeline: IndexingPipeline,
        task_state_manager: Any,
        document_repo: Any = None,
        topic_tag_repo: Any = None,
        user_repo: Any = None,
        job_repo: Any = None,
    ) -> None:
        self._pipeline = pipeline
        self._tsm = task_state_manager
        self._document_repo = document_repo
        self._topic_tag_repo = topic_tag_repo
        self._user_repo = user_repo
        self._job_repo = job_repo

    async def process_file(
        self,
        *,
        task_id: str,
        path: str,
        metadata: dict[str, Any],
        partition: str,
        user: dict[str, Any] | None = None,
        workspace_ids: list[str] | None = None,
        replace: bool = False,
        indexation_config: dict[str, Any] | None = None,
        embedder_name: str | None = None,
        quota_reserved: bool = False,
    ) -> dict[str, Any]:
        """Run one file through the indexing pipeline.

        Returns a plain dict ``{"stored_count": int, "stage": "stored"}``
        on success.  On failure, state is set to FAILED and the exception
        is re-raised so the Ray task is marked as errored.

        When ``quota_reserved`` is set, the admission gate already charged
        one ``users.file_count`` slot to the uploader (#664) and this call
        owns it. The slot is *consumed* the moment ``add_file_to_partition``
        reports a new catalog row; on every other outcome — pipeline
        failure, cancellation, or the duplicate-at-catalog race where the
        insert reports ``False`` — the ``finally`` below hands it back, so a
        rejected upload can never permanently eat a slot.
        """
        # Released in ``finally`` unless the catalog write claims it. A flag
        # plus ``finally`` (rather than an ``except``) is what makes
        # cancellation safe: ray.cancel raises ``asyncio.CancelledError``, a
        # BaseException that ``except Exception`` would sail straight past.
        release_slot = bool(quota_reserved)
        user_id = (user or {}).get("id")
        await self._tsm.set_state.remote(task_id, "SERIALIZING")
        await _update_job(
            self._job_repo,
            task_id,
            status=DocumentStatus.SERIALIZING,
            started_at=datetime.now(UTC),
        )
        try:
            document = await _load_document(path, metadata, partition)
            # One indexation timestamp for this file, shared by the Milvus chunks
            # (via the store stage) and the Postgres catalog row, so they agree.
            row: dict[str, Any] = {
                "task_id": task_id,
                "document": document,
                "partition": partition,
                "filename": document.filename,
                "language": metadata.get("language", "en"),
                "replace": replace,
                "user": user,
                "workspace_ids": workspace_ids,
                "indexation_config": indexation_config,
                "embedder_name": embedder_name,
            }
            row = await self._pipeline.run(row)
            indexed_at = row.get("indexed_at")

            if self._document_repo is not None:
                created = await _write_catalog_record(
                    doc_repo=self._document_repo,
                    metadata=metadata,
                    partition=partition,
                    user=user,
                    replace=replace,
                    indexation_config=indexation_config,
                    indexed_at=indexed_at,
                )
                if created:
                    # The reservation is now a real file row; releasing it here
                    # would undercount and hand the user free quota.
                    release_slot = False
            if self._topic_tag_repo is not None:
                await _replace_topic_tags_if_needed(
                    topic_tag_repo=self._topic_tag_repo,
                    row=row,
                    metadata=metadata,
                    partition=partition,
                    indexation_config=indexation_config,
                )
            await self._tsm.set_state.remote(task_id, "COMPLETED")
            await _update_job(
                self._job_repo,
                task_id,
                status=DocumentStatus.COMPLETED,
                completed_at=datetime.now(UTC),
            )
            return {"stored_count": row.get("stored_count", 0), "stage": row.get("stage", "")}
        except Exception:
            tb = traceback.format_exc()
            # The actor arbitrates FAILED-vs-CANCELLED atomically under its lock;
            # honouring its verdict here keeps the durable row from overwriting a
            # cancellation the user already asked for (and already saw).
            failed = await self._tsm.set_failed_if_not_cancelled.remote(task_id, tb)
            if failed:
                await _update_job(
                    self._job_repo,
                    task_id,
                    status=DocumentStatus.FAILED,
                    error=tb,
                    completed_at=datetime.now(UTC),
                )
            raise
        finally:
            if release_slot:
                await _release_quota_slot(self._user_repo, user_id)
        # The raw upload is purged (when configured) by the enclosing actor, not
        # here: cleanup must also cover failures that happen *before* this method
        # runs (catalog/registry init, the SERIALIZING state update). See
        # ``delete_uploaded_file`` and ``IndexerWorkerActor.process_file``.


async def _update_job(job_repo: Any, task_id: str, **fields: Any) -> None:
    """Mirror a lifecycle transition to the durable ``jobs`` row.

    Best-effort by design: this is bookkeeping about the work, not the work. A
    Postgres blip must not fail a file that indexed correctly (nor mask the real
    exception on the failure path, where this runs inside an ``except`` block).
    The repository truncates the stored traceback.
    """
    if job_repo is None:
        return
    try:
        await job_repo.update_job(task_id, **fields)
    except Exception as exc:  # noqa: BLE001 - durable bookkeeping must not fail indexing
        from core.utils.logging import get_logger

        get_logger().warning(
            "Durable job state write failed; job history for this task may be incomplete",
            task_id=task_id,
            status=fields.get("status"),
            error=str(exc),
        )


async def _write_catalog_record(
    *,
    doc_repo: Any,
    metadata: dict[str, Any],
    partition: str,
    user: dict[str, Any] | None,
    replace: bool,
    indexation_config: dict[str, Any] | None,
    indexed_at: datetime | None = None,
) -> bool:
    """Write the file's catalog row; return whether a *new* row was created.

    ``False`` means no new file exists for this task — either it was a
    ``replace`` re-index of an existing row, or ``add_file_to_partition``
    found a row already there (the duplicate-at-catalog race). Both cases
    leave a reserved quota slot unconsumed (#664).
    """
    file_id = metadata.get("file_id", "")
    file_metadata = {key: value for key, value in metadata.items() if key != "page"}
    config_kwargs = {"indexation_config": indexation_config} if indexation_config is not None else {}
    if replace:
        await doc_repo.update_file_in_partition(
            file_id=file_id,
            partition=partition,
            file_metadata=file_metadata,
            relationship_id=metadata.get("relationship_id"),
            parent_id=metadata.get("parent_id"),
            indexed_at=indexed_at,
            **config_kwargs,
        )
        return False

    return bool(
        await doc_repo.add_file_to_partition(
            file_id=file_id,
            partition=partition,
            file_metadata=file_metadata,
            user_id=user.get("id") if user else None,
            relationship_id=metadata.get("relationship_id"),
            parent_id=metadata.get("parent_id"),
            indexed_at=indexed_at,
            **config_kwargs,
        )
    )


async def _replace_topic_tags_if_needed(
    *,
    topic_tag_repo: Any,
    row: dict[str, Any],
    metadata: dict[str, Any],
    partition: str,
    indexation_config: dict[str, Any] | None,
) -> None:
    file_id = metadata.get("file_id", "")
    if not file_id:
        return

    has_topic_tags = "topic_tags" in row
    topic_tagging_disabled = indexation_config is not None and indexation_config.get("enable_topic_tagging") is False
    if not has_topic_tags and not topic_tagging_disabled:
        return

    raw_tags = row.get("topic_tags", [])
    if not isinstance(raw_tags, list):
        raise TypeError("topic_tags must be a list of strings")

    tags = [tag for tag in raw_tags if isinstance(tag, str) and tag.strip()]
    await topic_tag_repo.delete_by_document(file_id, partition=partition)
    if not tags:
        return
    await topic_tag_repo.bulk_insert(
        [
            {
                "document_id": file_id,
                "partition": partition,
                "tag": tag,
            }
            for tag in tags
        ]
    )


async def _load_document(
    path: str,
    metadata: dict[str, Any],
    partition: str,
) -> Document:
    p = Path(path)
    file_id = metadata.get("file_id")
    if not file_id:
        # file_id is a required route path param, force-set by
        # IndexingService._build_metadata. Missing here means a broken upstream
        # contract — fail loudly rather than silently persisting chunks under a
        # non-queryable id (e.g. the temp upload's basename).
        raise ValueError("_load_document requires metadata['file_id']")
    # ``Document.id`` is the file's identity, not a random uuid: parsers set
    # ``ProcessedDocument.document_id = document.id`` and the chunker uses that as
    # ``Chunk.document_id`` / ``file_id``. If this defaulted to uuid4, chunks would
    # persist under an id the ``/partition/{partition}/file/{file_id}`` lookup
    # never queries by (zero chunks found).
    #
    # Per-partition indexation_config reaches the pipeline via ``row["indexation_config"]``
    # (see IndexerWorker.process_file); it is intentionally not stamped into the
    # document metadata so it never leaks into chunk metadata.
    filename = _display_filename(path, metadata)
    raw_bytes = await asyncio.to_thread(p.read_bytes)
    return Document(
        id=file_id,
        filename=filename,
        raw_bytes=raw_bytes,
        content_type=Document.detect_content_type(filename),
        partition=partition,
        metadata=dict(metadata),
    )


def _display_filename(path: str, metadata: dict[str, Any]) -> str:
    """Return the user-facing filename while falling back to the stored path."""

    filename = metadata.get("original_filename") or metadata.get("filename")
    if filename:
        return str(filename)
    return Path(path).name


async def _release_quota_slot(user_repo: Any, user_id: int | None) -> None:
    """Give the uploader's reserved file slot back, swallowing any error.

    Runs on cleanup paths only. A release that raises would replace the real
    indexing error with a database error, so failures are swallowed — but
    loudly, because a lost release leaves ``file_count`` one too high and
    permanently narrows that user's quota until an admin reconciles it.
    """
    if user_repo is None or user_id is None:
        return
    try:
        await user_repo.release_file_slot(user_id)
    except Exception:  # noqa: BLE001 - cleanup must never mask the indexing error
        import logging

        logging.getLogger(__name__).exception(
            "Failed to release reserved file slot for user %s; file_count is now one too high.",
            user_id,
        )


async def delete_uploaded_file(path: str, logger: Any) -> None:
    """Remove the raw upload from disk, swallowing any cleanup error.

    Called from the actor boundary (``IndexerWorkerActor.process_file``) when
    ``save_uploaded_files`` is off, so a client that manages its own files never
    has a disk copy left behind — even when indexing fails before the pipeline
    runs. A failed delete must never turn indexing into a failure, so the error
    is logged and discarded.
    """
    try:
        await asyncio.to_thread(Path(path).unlink, missing_ok=True)
        logger.debug(f"Deleted input file: {path}")
    except Exception as cleanup_err:  # noqa: BLE001 - cleanup must not fail the task
        logger.warning(f"Failed to delete input file {path}: {cleanup_err}")


__all__ = ["IndexerWorker", "delete_uploaded_file"]
