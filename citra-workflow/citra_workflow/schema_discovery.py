"""Schema discovery for AI Agent tool connections — read-only introspection.

Each function resolves a connection, introspects the remote system, and returns
a plain dict describing the schema / structure.  All operations are strictly
read-only.  Errors are returned as ``{"error": "..."}`` so callers never crash.
"""

from __future__ import annotations
import asyncio
import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Limits to avoid bloating the LLM prompt
_MAX_TABLES = 100
_MAX_COLUMNS_PER_TABLE = 200
_MAX_COLLECTIONS = 50
_MAX_SAMPLE_DOCS = 5
_MAX_BUCKET_PREFIXES = 50
_MAX_BUCKET_SAMPLE_OBJECTS = 20
_MAX_SFTP_ENTRIES = 50


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

async def discover_sql_schema(
    connection_id: str,
    org_id: str,
    owner_id: str = "",
    environment: str = "test",
    owner_type: str = "",
) -> Dict[str, Any]:
    """Introspect an SQL database via SQLAlchemy ``inspect()`` (read-only).

    Returns::

        {
            "type": "sql",
            "dialect": "postgresql",
            "tables": [
                {"name": "orders", "columns": [{"name": "id", "type": "INTEGER", "nullable": false}, ...]},
                ...
            ]
        }
    """
    try:
        from .connection_resolver import resolve_connection
        resolved = await resolve_connection(
            connection_id, org_id=org_id, owner_id=owner_id,
            owner_type=owner_type, environment=environment,
        )
        conn_str = resolved.get("connection_string", "")
        if not conn_str:
            return {"error": "SQL connection has no connection_string configured."}

        import sqlalchemy
        from sqlalchemy import inspect as sa_inspect

        def _introspect():
            engine = sqlalchemy.create_engine(conn_str, pool_pre_ping=True)
            try:
                insp = sa_inspect(engine)
                dialect = engine.dialect.name  # e.g. "postgresql", "mysql", "mssql"
                tables: List[Dict] = []
                for tbl in insp.get_table_names()[:_MAX_TABLES]:
                    cols = []
                    for col in insp.get_columns(tbl)[:_MAX_COLUMNS_PER_TABLE]:
                        cols.append({
                            "name": col["name"],
                            "type": str(col["type"]),
                            "nullable": col.get("nullable", True),
                        })
                    tables.append({"name": tbl, "columns": cols})
                return {"type": "sql", "dialect": dialect, "tables": tables}
            finally:
                engine.dispose()

        return await asyncio.get_event_loop().run_in_executor(None, _introspect)
    except Exception as e:
        logger.warning("SQL schema discovery failed for %s: %s", connection_id, e)
        return {"error": f"SQL schema discovery failed: {e}"}


# ---------------------------------------------------------------------------
# NoSQL (MongoDB / DocumentDB)
# ---------------------------------------------------------------------------

async def discover_nosql_schema(
    connection_id: str,
    org_id: str,
    owner_id: str = "",
    environment: str = "test",
    owner_type: str = "",
) -> Dict[str, Any]:
    """Introspect a MongoDB database — list collections, sample fields.

    Returns::

        {
            "type": "nosql",
            "database": "mydb",
            "collections": [
                {"name": "users", "sample_fields": ["_id", "name", "email"], "estimated_count": 15000},
                ...
            ]
        }
    """
    try:
        from .connection_resolver import resolve_connection
        from motor.motor_asyncio import AsyncIOMotorClient
        from .config import MONGO_CONNECT_TIMEOUT_MS

        resolved = await resolve_connection(
            connection_id, org_id=org_id, owner_id=owner_id,
            owner_type=owner_type, environment=environment,
        )
        conn_str = resolved.get("connection_string", "")
        db_name = resolved.get("database", "")
        if not conn_str or not db_name:
            return {"error": "NoSQL connection is missing connection_string or database."}

        client = AsyncIOMotorClient(conn_str, serverSelectionTimeoutMS=MONGO_CONNECT_TIMEOUT_MS)
        try:
            db = client[db_name]
            coll_names = await db.list_collection_names()
            collections: List[Dict] = []
            for coll_name in coll_names[:_MAX_COLLECTIONS]:
                # Sample a few documents to infer field names
                sample_fields: set = set()
                async for doc in db[coll_name].aggregate([{"$sample": {"size": _MAX_SAMPLE_DOCS}}]):
                    sample_fields.update(doc.keys())
                est_count = await db[coll_name].estimated_document_count()
                collections.append({
                    "name": coll_name,
                    "sample_fields": sorted(sample_fields),
                    "estimated_count": est_count,
                })
            return {"type": "nosql", "database": db_name, "collections": collections}
        finally:
            client.close()
    except Exception as e:
        logger.warning("NoSQL schema discovery failed for %s: %s", connection_id, e)
        return {"error": f"NoSQL schema discovery failed: {e}"}


# ---------------------------------------------------------------------------
# Bucket (S3-compatible / MinIO)
# ---------------------------------------------------------------------------

async def discover_bucket_structure(
    connection_id: str,
    org_id: str,
    owner_id: str = "",
    environment: str = "test",
    owner_type: str = "",
) -> Dict[str, Any]:
    """List top-level prefixes and a sample of objects in a bucket.

    Returns::

        {
            "type": "bucket",
            "bucket": "my-bucket",
            "top_level_prefixes": ["data/", "logs/"],
            "sample_objects": [{"key": "data/report.csv", "size": 1024}]
        }
    """
    try:
        from .connection_resolver import resolve_connection
        import boto3

        resolved = await resolve_connection(
            connection_id, org_id=org_id, owner_id=owner_id,
            owner_type=owner_type, environment=environment,
        )
        bucket = resolved.get("bucket", "")
        if not bucket:
            return {"error": "Bucket connection has no bucket configured."}

        boto_kwargs: dict = {
            "aws_access_key_id": resolved.get("access_key_id") or None,
            "aws_secret_access_key": resolved.get("secret_access_key") or None,
            "region_name": resolved.get("region") or None,
        }
        endpoint_url = resolved.get("endpoint_url")
        if endpoint_url:
            boto_kwargs["endpoint_url"] = endpoint_url

        def _introspect():
            s3 = boto3.client("s3", **boto_kwargs)
            # Top-level prefixes
            resp = s3.list_objects_v2(Bucket=bucket, Delimiter="/", MaxKeys=_MAX_BUCKET_PREFIXES)
            prefixes = [p["Prefix"] for p in resp.get("CommonPrefixes", [])]
            # Sample objects
            resp2 = s3.list_objects_v2(Bucket=bucket, MaxKeys=_MAX_BUCKET_SAMPLE_OBJECTS)
            sample = [
                {"key": obj["Key"], "size": obj["Size"]}
                for obj in resp2.get("Contents", [])
            ]
            return {
                "type": "bucket",
                "bucket": bucket,
                "top_level_prefixes": prefixes,
                "sample_objects": sample,
            }

        return await asyncio.get_event_loop().run_in_executor(None, _introspect)
    except Exception as e:
        logger.warning("Bucket structure discovery failed for %s: %s", connection_id, e)
        return {"error": f"Bucket structure discovery failed: {e}"}


# ---------------------------------------------------------------------------
# SFTP / FTP
# ---------------------------------------------------------------------------

async def discover_sftp_structure(
    connection_id: str,
    org_id: str,
    owner_id: str = "",
    environment: str = "test",
    owner_type: str = "",
) -> Dict[str, Any]:
    """List the root directory of an SFTP/FTP server.

    Returns::

        {
            "type": "sftp",
            "host": "sftp.example.com",
            "protocol": "sftp",
            "root_entries": [{"name": "reports", "is_dir": true, "size": 0}, ...]
        }
    """
    try:
        from .connection_resolver import resolve_connection
        from .config import FTP_CONNECT_TIMEOUT

        resolved = await resolve_connection(
            connection_id, org_id=org_id, owner_id=owner_id,
            owner_type=owner_type, environment=environment,
        )
        host = resolved.get("host", "")
        if not host:
            return {"error": "SFTP connection has no host configured."}

        port = int(resolved.get("port", 22))
        username = resolved.get("username", "")
        password = resolved.get("password", "")
        private_key = resolved.get("private_key", "")
        protocol = resolved.get("protocol", "sftp")

        if protocol == "sftp":
            def _introspect():
                import io
                import paramiko
                transport = paramiko.Transport((host, port))
                try:
                    if private_key:
                        key_file = io.StringIO(private_key)
                        pkey = paramiko.RSAKey.from_private_key(key_file)
                        transport.connect(username=username, pkey=pkey)
                    else:
                        transport.connect(username=username, password=password)
                    sftp = paramiko.SFTPClient.from_transport(transport)
                    try:
                        entries = []
                        for attr in sftp.listdir_attr("/")[:_MAX_SFTP_ENTRIES]:
                            entries.append({
                                "name": attr.filename,
                                "is_dir": bool(attr.st_mode and (attr.st_mode & 0o40000)),
                                "size": attr.st_size or 0,
                            })
                        return entries
                    finally:
                        sftp.close()
                finally:
                    transport.close()
        else:
            def _introspect():
                if protocol == "ftps":
                    from ftplib import FTP_TLS
                    ftp = FTP_TLS()
                else:
                    from ftplib import FTP
                    ftp = FTP()
                ftp.connect(host, port, timeout=FTP_CONNECT_TIMEOUT)
                ftp.login(username or "anonymous", password or "")
                if protocol == "ftps":
                    ftp.prot_p()
                try:
                    entries = [{"name": n, "is_dir": None, "size": None} for n in ftp.nlst("/")[:_MAX_SFTP_ENTRIES]]
                    return entries
                finally:
                    ftp.quit()

        root_entries = await asyncio.get_event_loop().run_in_executor(None, _introspect)
        return {
            "type": "sftp",
            "host": host,
            "protocol": protocol,
            "root_entries": root_entries,
        }
    except Exception as e:
        logger.warning("SFTP structure discovery failed for %s: %s", connection_id, e)
        return {"error": f"SFTP structure discovery failed: {e}"}


# ---------------------------------------------------------------------------
# HTTP / REST API
# ---------------------------------------------------------------------------

async def discover_api_capabilities(
    connection_id: str,
    org_id: str,
    owner_id: str = "",
    environment: str = "test",
    owner_type: str = "",
) -> Dict[str, Any]:
    """Return basic metadata about an API connection.

    No automated endpoint discovery is performed — crawling enterprise APIs
    without explicit permission is unsafe.

    Returns::

        {
            "type": "api",
            "base_url": "https://api.example.com",
            "auth_type": "bearer" | "api_key" | "none"
        }
    """
    try:
        from .connection_resolver import resolve_connection
        resolved = await resolve_connection(
            connection_id, org_id=org_id, owner_id=owner_id,
            owner_type=owner_type, environment=environment,
        )
        url = resolved.get("url", "")
        headers = resolved.get("headers", {})

        # Infer auth type from headers (don't expose secrets)
        auth_type = "none"
        for k in headers:
            kl = k.lower()
            if kl == "authorization":
                val = headers[k].lower()
                if val.startswith("bearer"):
                    auth_type = "bearer"
                else:
                    auth_type = "token"
                break
            if kl in ("x-api-key", "api-key", "apikey"):
                auth_type = "api_key"
                break

        return {"type": "api", "base_url": url, "auth_type": auth_type}
    except Exception as e:
        logger.warning("API capability discovery failed for %s: %s", connection_id, e)
        return {"error": f"API capability discovery failed: {e}"}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

# Maps agent tool name → discovery function
DISCOVERY_FUNCTIONS = {
    "sql_query": discover_sql_schema,
    "nosql_query": discover_nosql_schema,
    "bucket_read": discover_bucket_structure,
    "sftp_read": discover_sftp_structure,
    "http_request": discover_api_capabilities,
}
