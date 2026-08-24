# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""
Citra-Worker env loader.

Mirrors Citra-Service/vault_env_loader.py:
  1. Load local `.env` (dev / fallback for vars Vault doesn't provide).
  2. If VAULT_ADDR is set, fetch secrets from HashiCorp Vault and overlay
     them onto os.environ. Production deployments rely on this — there is
     NO file-based cross-service fallback.

Each service owns its env. The fact that Citra-Worker happens to use the
same Redis + Mongo as Citra-Service in dev is a coincidence of the values
in `.env`, NOT a feature of the loader.

Idempotent: safe to call from multiple modules; later calls become no-ops
once the env is already populated.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_LOADED = False


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if (v is not None and v != "") else default


def _split_kv2_path(secret_path: str) -> tuple[str, str]:
    """'env/citra-ai' -> ('env', 'citra-ai')."""
    p = secret_path.strip().strip("/")
    if "/" not in p:
        raise ValueError("VAULT_SECRET_PATH must be '<mount>/<name>', e.g. 'env/citra-worker'")
    return p.split("/", 1)


def _is_production_mount(mount: str) -> bool:
    return mount.lower().strip() in ("prod", "production")


def _approle_login(client, vault_addr: str, role_id: str, secret_id: str) -> str:
    url = f"{vault_addr.rstrip('/')}/v1/auth/approle/login"
    resp = client.post(url, json={"role_id": role_id, "secret_id": secret_id})
    if resp.status_code != 200:
        raise RuntimeError(f"AppRole login failed: HTTP {resp.status_code} {resp.text}")
    token = (resp.json().get("auth") or {}).get("client_token")
    if not token:
        raise RuntimeError("AppRole login succeeded but no client_token in response")
    return token


def _kv2_read(client, vault_addr: str, token: str, mount: str, name: str) -> dict:
    """Read KV v2 secret. In prod (mount=prod) any non-200 is fatal.
    In dev/test, 403/404 is logged and returns {} so developers without
    read perms can still work off the local .env."""
    url = f"{vault_addr.rstrip('/')}/v1/{mount}/data/{name}"
    resp = client.get(url, headers={"X-Vault-Token": token})
    if resp.status_code == 200:
        return ((resp.json().get("data") or {}).get("data")) or {}
    if resp.status_code in (403, 404):
        if _is_production_mount(mount):
            raise RuntimeError(
                f"❌ FATAL: Vault KV read failed in PRODUCTION (HTTP {resp.status_code}). "
                f"Cannot start without secrets. Check Vault token permissions for '{mount}/{name}'."
            )
        logger.warning(
            "Vault KV read not permitted or not found (HTTP %s). Falling back to .env values.",
            resp.status_code,
        )
        return {}
    raise RuntimeError(f"KV read failed: HTTP {resp.status_code} {resp.text}")


def load_env() -> dict:
    """Resolve and load env: local .env first, then overlay Vault if configured.

    Call once at process startup (worker.py does this before any other import
    that reads os.getenv). Returns a dict of secrets pulled from Vault (or
    empty when not configured / not permitted).
    """
    global _LOADED
    if _LOADED:
        return {}
    _LOADED = True

    # ─── 1. Local .env (dev + fallback for non-Vault vars) ──────────────
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.info("env_loader: python-dotenv not installed; relying on process env")
    else:
        # CITRA_WORKER_ENV_FILE lets prod containers point at a mounted secrets file.
        explicit = os.getenv("CITRA_WORKER_ENV_FILE")
        if explicit and Path(explicit).exists():
            load_dotenv(explicit, override=False)
            logger.info(f"env_loader: loaded {explicit} (CITRA_WORKER_ENV_FILE)")
        else:
            local_env = Path(__file__).resolve().parent / ".env"
            if local_env.exists():
                load_dotenv(local_env, override=False)
                logger.info(f"env_loader: loaded {local_env}")
            else:
                logger.info(
                    f"env_loader: no .env at {local_env}; relying on process env"
                )

    # ─── 2. Vault overlay (prod) ────────────────────────────────────────
    vault_addr = _env("VAULT_ADDR")
    if not vault_addr:
        logger.info("🔧 VAULT_ADDR not set — using .env only (dev mode)")
        return {}

    try:
        import httpx
    except ImportError:
        logger.warning("env_loader: VAULT_ADDR set but httpx not installed; skipping Vault overlay")
        return {}

    vault_token = _env("VAULT_TOKEN")
    role_id = _env("VAULT_ROLE_ID")
    secret_id = _env("VAULT_SECRET_ID")
    secret_path = _env("VAULT_SECRET_PATH", "env/citra-worker")
    timeout = float(_env("VAULT_TIMEOUT", "10"))

    with httpx.Client(timeout=timeout) as client:
        # Get a token if one wasn't supplied directly.
        if not vault_token:
            if role_id and secret_id:
                logger.info("🔐 Logging into Vault with AppRole")
                vault_token = _approle_login(client, vault_addr, role_id, secret_id)
                logger.info("🔐 AppRole login successful")
            else:
                logger.info(
                    "🔧 VAULT_ADDR set but no VAULT_TOKEN / VAULT_ROLE_ID — skipping Vault read"
                )
                return {}

        # Best-effort health check.
        try:
            health = client.get(f"{vault_addr.rstrip('/')}/v1/sys/health")
            if health.status_code not in (200, 429):  # 200 active, 429 standby
                logger.warning("⚠️ Vault health not OK (HTTP %s)", health.status_code)
        except httpx.RequestError as exc:
            logger.warning("⚠️ Could not check Vault health: %s", exc)

        # Pull secrets.
        mount, name = _split_kv2_path(secret_path)
        logger.info("📦 Fetching KV v2 secret at %s (mount=%s, name=%s)", secret_path, mount, name)
        try:
            secrets = _kv2_read(client, vault_addr, vault_token, mount, name)
        except httpx.RequestError as exc:
            logger.error("❌ Vault connection error: %s", exc)
            raise

        if secrets:
            for k, v in secrets.items():
                os.environ[k] = str(v)
            logger.info("✅ Loaded %d Vault secrets from %s", len(secrets), secret_path)
        elif _is_production_mount(mount):
            raise RuntimeError(
                f"❌ FATAL: No secrets loaded from Vault in PRODUCTION (mount={mount}). "
                f"Worker cannot start without MONGODB_CONN_STRING / REDIS_HOST. "
                f"Check Vault path '{secret_path}' and token permissions."
            )
        else:
            logger.info("ℹ️ No secrets returned from Vault — running on .env values only.")

        return {k: str(v) for k, v in secrets.items()}


# Optional alias matching Citra-Service's public name, for code that wants
# the two services to look symmetric.
load_environment_variables = load_env


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env()
