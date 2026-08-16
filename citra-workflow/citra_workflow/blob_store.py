"""Durable binary-blob store for workflow nodes (images, audio, files).

The node-to-node envelope is a list of small JSON dicts that get persisted into
the Mongo execution document. Binary media (an uploaded image, a recorded
audio clip, a fetched PDF) is far too large and opaque to carry inline as
base64 there — it would bloat every checkpoint and the execution record.

So binary content is stored ONCE in GridFS and passed between nodes BY
REFERENCE as a compact descriptor item:

    {"_blob": {"id": "<gridfs-id>", "mime": "image/png",
               "filename": "scan.png", "size": 12345}}

Any node that produces media (file-upload trigger, file/URL fetch) calls
``put_blob`` and emits the descriptor; any node that consumes it (OCR, audio
transcribe, vision-LLM) calls ``get_blob`` to pull the bytes back. Blobs are
tagged with their ``execution_id`` so a completed/failed run's media can be
swept later.

This module fails loud (raises) on misuse or a missing store — there is no
silent fallback to inline bytes.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Reserved envelope key that marks an item as a blob reference.
BLOB_KEY = "_blob"

# GridFS bucket name (collections: workflow_blobs.files / workflow_blobs.chunks).
_BUCKET_NAME = "workflow_blobs"


def _bucket():
    """Build a GridFS bucket on the shared async Mongo client. Raises if the
    mongo client is unavailable — callers must not swallow this."""
    try:
        from citra_mongo import get_async_mongo_client, MONGODB_DATABASE
    except ImportError:  # pragma: no cover - packaging fallback
        from mongodb_manager import get_async_mongo_client, MONGODB_DATABASE  # type: ignore
    from motor.motor_asyncio import AsyncIOMotorGridFSBucket

    client = get_async_mongo_client()
    db = client[MONGODB_DATABASE]
    return AsyncIOMotorGridFSBucket(db, bucket_name=_BUCKET_NAME)


def is_blob(item: Any) -> bool:
    """True if ``item`` is a blob-reference descriptor."""
    return (
        isinstance(item, dict)
        and isinstance(item.get(BLOB_KEY), dict)
        and bool(item[BLOB_KEY].get("id"))
    )


def blob_descriptor(grid_id: str, *, mime: str, filename: str, size: int,
                    extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build the ``{"_blob": {...}}`` envelope item for a stored blob."""
    desc = {
        "id": str(grid_id),
        "mime": mime or "application/octet-stream",
        "filename": filename or "blob",
        "size": int(size),
    }
    if extra:
        desc.update(extra)
    return {BLOB_KEY: desc}


async def put_blob(
    data: bytes,
    *,
    mime: str = "application/octet-stream",
    filename: str = "blob",
    execution_id: str = "",
) -> Dict[str, Any]:
    """Store ``data`` in GridFS and return its ``{"_blob": {...}}`` descriptor.

    Raises TypeError if ``data`` is not bytes.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"put_blob expects bytes, got {type(data).__name__}")
    bucket = _bucket()
    metadata = {"mime": mime, "execution_id": execution_id or ""}
    grid_id = await bucket.upload_from_stream(filename or "blob", bytes(data), metadata=metadata)
    logger.info(
        "blob_store: stored %d bytes (%s, %s) as %s for execution %s",
        len(data), mime, filename, grid_id, execution_id or "-",
    )
    return blob_descriptor(grid_id, mime=mime, filename=filename, size=len(data))


async def get_blob(item: Dict[str, Any]) -> bytes:
    """Fetch the bytes for a blob-reference item. Raises if not a blob."""
    if not is_blob(item):
        raise ValueError("get_blob: item is not a blob reference (missing '_blob.id')")
    from bson import ObjectId

    grid_id = item[BLOB_KEY]["id"]
    bucket = _bucket()
    stream = await bucket.open_download_stream(ObjectId(grid_id))
    data = await stream.read()
    return data


async def delete_blobs_for_execution(execution_id: str) -> int:
    """Delete every blob tagged with ``execution_id``. Returns the count.

    Used by lifecycle cleanup once a run is terminal so media doesn't linger.
    """
    if not execution_id:
        return 0
    from citra_mongo import get_async_mongo_client, MONGODB_DATABASE  # type: ignore
    bucket = _bucket()
    client = get_async_mongo_client()
    db = client[MONGODB_DATABASE]
    files_col = db[f"{_BUCKET_NAME}.files"]
    deleted = 0
    cursor = files_col.find({"metadata.execution_id": execution_id}, {"_id": 1})
    async for doc in cursor:
        try:
            await bucket.delete(doc["_id"])
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("blob_store: failed to delete blob %s: %s", doc.get("_id"), exc)
    return deleted


async def list_blob_files(execution_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Return lightweight metadata for stored blobs — never the chunk bytes.

    Each item: ``{id, execution_id, mime, filename, size, upload_date}``. Pass
    ``execution_ids`` to restrict to specific runs; omit for the whole bucket.
    Reads only the ``.files`` collection (one doc per blob), so it's cheap even
    with large media. Used by the maintenance orphan-sweep to decide what to
    reclaim without loading any binary content.
    """
    from citra_mongo import get_async_mongo_client, MONGODB_DATABASE  # type: ignore
    client = get_async_mongo_client()
    db = client[MONGODB_DATABASE]
    files_col = db[f"{_BUCKET_NAME}.files"]
    query: Dict[str, Any] = {}
    if execution_ids is not None:
        query["metadata.execution_id"] = {"$in": list(execution_ids)}
    out: List[Dict[str, Any]] = []
    cursor = files_col.find(
        query,
        {"_id": 1, "length": 1, "filename": 1, "metadata": 1, "uploadDate": 1},
    )
    async for doc in cursor:
        meta = doc.get("metadata") or {}
        out.append({
            "id": str(doc["_id"]),
            "execution_id": meta.get("execution_id") or "",
            "mime": meta.get("mime") or "",
            "filename": doc.get("filename") or "",
            "size": int(doc.get("length") or 0),
            "upload_date": doc.get("uploadDate"),
        })
    return out


async def delete_blob(grid_id: str) -> bool:
    """Delete one blob by its GridFS id. Returns True on success.

    A blob that's already gone is treated as success-by-other-means and logged,
    not raised — concurrent sweeps or a prior per-run sweep can race us, and a
    missing target means the goal (no such blob) is already met.
    """
    from bson import ObjectId
    bucket = _bucket()
    try:
        await bucket.delete(ObjectId(grid_id))
        return True
    except Exception as exc:  # noqa: BLE001 - missing/already-deleted is non-fatal here
        logger.warning("blob_store: delete_blob(%s) failed (treating as gone): %s", grid_id, exc)
        return False


def find_blobs(items: List[Any]) -> List[Dict[str, Any]]:
    """Return the subset of ``items`` that are blob references."""
    return [it for it in (items or []) if is_blob(it)]
