"""Object-storage config resolution for the S3 nodes.

These nodes are meant to talk to YOUR bucket — the one on the node's saved
connection, or named in its config, or supplied via env for a single-bucket
deployment. There is deliberately no built-in bucket: the previous default was
the literal string ``"citra-ai"``, so a workflow with no bucket configured
silently read from / wrote into the vendor's own bucket instead of failing.

Same reasoning for region: the old default was ``ap-south-1`` (the vendor's
region). Left unset, boto3 resolves the region through its own normal chain
(AWS_DEFAULT_REGION, ~/.aws/config, instance metadata), which is what an
operator running this on their own infrastructure expects.
"""

from __future__ import annotations

import os
from typing import Optional


class BucketNotConfiguredError(ValueError):
    """Raised when an S3 node has no bucket to talk to."""


def require_bucket(*candidates: Optional[str], node: str) -> str:
    """Return the first non-empty bucket name, or fail loudly.

    Pass candidates in priority order, e.g. the resolved connection's bucket,
    then the node's own config, then ``BUCKET_NAME``.
    """
    for value in candidates:
        if value:
            return str(value)
    raise BucketNotConfiguredError(
        f"{node}: no S3 bucket configured. Set it on the node's connection, "
        f"in the node's `bucket_name` config, or via the BUCKET_NAME "
        f"environment variable. There is no default bucket."
    )


def resolve_region(*candidates: Optional[str]) -> Optional[str]:
    """First non-empty region, else None so boto3 uses its own resolution."""
    for value in candidates:
        if value:
            return str(value)
    return os.getenv("AWS_S3_REGION") or None
