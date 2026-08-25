"""Environment-derived configuration, read once at import."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.environ.get("ENV_FILE", ".env"), override=False)


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    return int(raw) if raw else default


def _env_map(name: str, default: str) -> dict[str, str]:
    """Parse "key=value,key2=value2" into a dict, skipping malformed entries."""
    raw = os.environ.get(name) or default
    mapping: dict[str, str] = {}
    for item in raw.split(","):
        key, _, value = item.strip().partition("=")
        if key.strip() and value.strip():
            mapping[key.strip()] = value.strip()
    return mapping


def _env_list(name: str, default: str) -> tuple[str, ...]:
    raw = os.environ.get(name) or default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes")


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    return float(raw) if raw else default


def _env_weights(name: str, default: str) -> dict[str, float]:
    """Parse ``"run_query=5,get_filter_values=3"`` into a name → weight map."""
    raw = os.environ.get(name) or default
    weights: dict[str, float] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, _, value = item.partition("=")
        try:
            weights[key.strip()] = float(value)
        except ValueError:
            continue
    return weights


# ── Server ────────────────────────────────────────────────────────────
MCP_HOST: str = os.environ.get("QG_HOST", "0.0.0.0")  # noqa: S104 (container bind)
MCP_PORT: int = _env_int("QG_PORT", 8765)
MCP_ENV: str = os.environ.get("QG_ENV", "local")

# ── Tunable limits ────────────────────────────────────────────────────
DEFAULT_ROW_LIMIT: int = _env_int("QG_DEFAULT_ROW_LIMIT", 100)
MAX_ROW_LIMIT: int = _env_int("QG_MAX_ROW_LIMIT", 1000)
MAX_SQL_LENGTH: int = _env_int("QG_MAX_SQL_LENGTH", 20000)
MAX_SEARCH_LENGTH: int = _env_int("QG_MAX_SEARCH_LENGTH", 200)

# ── Warehouse ─────────────────────────────────────────────────────────
# Which engine adapter to load. Adding one means writing a module under
# warehouse/ and listing it here. The query pipeline is engine-agnostic.
# Who applies row security: this server (inject) or the engine's own grants
# and policies (warehouse). See docs/warehouse-enforced-governance.md.
GOVERNANCE_MODE: str = os.environ.get("QG_GOVERNANCE_MODE", "inject").strip().lower()
# IdP role -> warehouse role, an explicit allowlist. Never the token's role claim
# passed through: that is attacker-influenced input.
WAREHOUSE_ROLE_MAP: dict[str, str] = _env_map("QG_WAREHOUSE_ROLE_MAP", "")

WAREHOUSE: str = os.environ.get("QG_WAREHOUSE", "postgres").strip().lower()

# sqlglot dialect used to parse / transform / render every query. Defaults to
# the warehouse name (true for postgres, duckdb, bigquery, snowflake, trino);
# override when the engine and its dialect name diverge. This lives in config
# rather than constants because it is exactly what changes per deployment.
SQL_DIALECT: str = os.environ.get("QG_SQL_DIALECT", "").strip().lower() or WAREHOUSE

PG_DSN: str = os.environ.get("QG_PG_DSN", "postgresql://querygate:querygate@localhost:5432/warehouse")
# DuckDB database file; ":memory:" is handy for demos and tests.
DUCKDB_PATH: str = os.environ.get("QG_DUCKDB_PATH", ":memory:")
# Hard kill switch for a runaway query (ms). Postgres has no dry-run byte
# estimate, so the timeout is the primary cost control; EXPLAIN cost is the
# advisory pre-check.
STATEMENT_TIMEOUT_MS: int = _env_int("QG_STATEMENT_TIMEOUT_MS", 15000)
# Refuse queries whose EXPLAIN total cost exceeds this (0 disables the check).
MAX_PLAN_COST: float = float(os.environ.get("QG_MAX_PLAN_COST") or 0)

# Schemas a query may read. Tables anywhere else are rejected at validation.
BQ_PROJECT: str = os.environ.get("QG_BQ_PROJECT", "").strip()
BQ_LOCATION: str = os.environ.get("QG_BQ_LOCATION", "").strip()
# Hard byte ceiling BigQuery itself enforces, on top of the plan-cost gate.
BQ_MAX_BYTES_BILLED: int = _env_int("QG_BQ_MAX_BYTES_BILLED", 0)

SF_ACCOUNT: str = os.environ.get("QG_SF_ACCOUNT", "").strip()
SF_USER: str = os.environ.get("QG_SF_USER", "").strip()
SF_PASSWORD: str = os.environ.get("QG_SF_PASSWORD", "")
SF_AUTHENTICATOR: str = os.environ.get("QG_SF_AUTHENTICATOR", "").strip()
# Service role used in inject mode. In warehouse mode the caller's mapped role
# replaces it per query.
SF_ROLE: str = os.environ.get("QG_SF_ROLE", "").strip()
SF_WAREHOUSE: str = os.environ.get("QG_SF_WAREHOUSE", "").strip()
SF_DATABASE: str = os.environ.get("QG_SF_DATABASE", "").strip()
SF_SCHEMA: str = os.environ.get("QG_SF_SCHEMA", "").strip()

ALLOWED_SCHEMAS: tuple[str, ...] = _env_list("QG_ALLOWED_SCHEMAS", "analytics")

# Column that scopes governed tables to a tenant. Tables without it are public.
TENANT_COLUMN: str = os.environ.get("QG_TENANT_COLUMN", "tenant_id")

# Refuse a query that reads a partitioned table without narrowing the partition
# column. Off by default: Postgres and DuckDB barely care, but on BigQuery,
# Snowflake or Iceberg this is the difference between reading one day and the
# whole table, so turn it on there.
REQUIRE_PARTITION_FILTER: bool = _env_bool("QG_REQUIRE_PARTITION_FILTER")

# Column-level security. Masks are declared per column in the catalog (from dbt
# column meta), never inferred from names: "email_preference" is not PII and a
# regex would say otherwise.
MASKING_ENABLED: bool = _env_bool("QG_MASKING_ENABLED", default=True)
# A principal holding this role reads masked columns in the clear. Empty means
# nobody does, which is the safer default for a shared deployment.
PII_UNMASK_ROLE: str = os.environ.get("QG_PII_UNMASK_ROLE", "pii_reader")

# Roles that may read certified metrics and nothing else: no ad-hoc SQL, no
# uncertified metric. Orthogonal to row security. See query/certification.py.
CERTIFIED_ONLY_ROLES: tuple[str, ...] = _env_list("QG_CERTIFIED_ONLY_ROLES", "")

# ── Catalog source (precedence: S3 → dbt target dir → bundled sample) ─
CATALOG_S3_URI: str | None = os.environ.get("QG_CATALOG_S3_URI") or None
CATALOG_S3_ENDPOINT: str | None = os.environ.get("QG_CATALOG_S3_ENDPOINT") or None  # MinIO et al.
CATALOG_DBT_TARGET_PATH: str | None = os.environ.get("QG_CATALOG_DBT_TARGET_PATH") or None
_BUNDLED_SAMPLE = str(Path(__file__).resolve().parent / "references" / "sample_catalog.json")
CATALOG_LOCAL_PATH: str = os.environ.get("QG_CATALOG_LOCAL_PATH", _BUNDLED_SAMPLE)
CATALOG_REFRESH_TTL_SECONDS: int = _env_int("QG_CATALOG_REFRESH_TTL_SECONDS", 900)

# ── Retrieval ─────────────────────────────────────────────────────────
EMBEDDINGS_ENABLED: bool = _env_bool("QG_EMBEDDINGS_ENABLED")
EMBEDDING_MODEL: str = os.environ.get("QG_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
# Where embedding vectors live: memory (default, right up to ~50k models) or
# qdrant (shared across replicas, metadata filtering). BM25 and rank fusion
# always stay in-process. See retrieval/vector_store.py.
VECTOR_BACKEND: str = os.environ.get("QG_VECTOR_BACKEND", "memory").strip().lower()
QDRANT_URL: str | None = os.environ.get("QG_QDRANT_URL") or None
QDRANT_COLLECTION: str = os.environ.get("QG_QDRANT_COLLECTION", "querygate_catalog")

# ── Result cache ──────────────────────────────────────────────────────
# TTL for cached get_filter_values results; 0 disables caching entirely.
# Uses QG_REDIS_URL when set (shared across replicas), else an in-process
# cache. Note that a result cache in a multi-tenant server is a security
# surface: keys are tenant-scoped by construction. See cache/base.py.
CACHE_TTL_SECONDS: int = _env_int("QG_CACHE_TTL_SECONDS", 0)

# ── Rate limiting ─────────────────────────────────────────────────────
# Calls per minute per bucket; 0 disables limiting entirely.
RATE_LIMIT_RPM: float = _env_float("QG_RATE_LIMIT_RPM", 0)
# Bucket capacity: how big a burst a caller may spend at once.
RATE_LIMIT_BURST: int = _env_int("QG_RATE_LIMIT_BURST", 10)
# Who shares a bucket: principal (per user) | tenant (per org) | global.
RATE_LIMIT_SCOPE: str = os.environ.get("QG_RATE_LIMIT_SCOPE", "principal").strip().lower()
# Per-tool token costs. Warehouse-touching tools are charged more than
# in-memory catalog lookups, so one RPM knob still reflects real load.
RATE_LIMIT_COSTS: dict[str, float] = _env_weights(
    "QG_RATE_LIMIT_COSTS",
    "run_query=5,get_metric=5,get_table_stats=4,get_filter_values=3,search_catalog=1,describe_model=1,reload_catalog=10",
)
# When the limiter's store is unreachable: allow (default) or reject. Rate
# limiting protects availability, so failing open beats a self-inflicted outage.
# Governance is the opposite, and always fails closed.
RATE_LIMIT_FAIL_OPEN: bool = _env_bool("QG_RATE_LIMIT_FAIL_OPEN", default=True)
# Shared limiter store. Unset → in-process limiter (single replica only).
REDIS_URL: str | None = os.environ.get("QG_REDIS_URL") or None

# ── Auth ──────────────────────────────────────────────────────────────
# Which verifier to load: static (fixed tokens, for the demo) or oidc.
AUTH_PROVIDER: str = os.environ.get("QG_AUTH_PROVIDER", "static").strip().lower()

# Static tokens: "token=tenantA|tenantB:role1|role2,token2=tenantC".
DEMO_TOKENS: str = os.environ.get("QG_DEMO_TOKENS", "")

# OIDC (Keycloak/Auth0/Okta). The issuer is used both to validate `iss` and to
# discover the JWKS endpoint. Audience is optional but worth setting: without
# it, any token from the same realm is accepted here.
OIDC_ISSUER: str = os.environ.get("QG_OIDC_ISSUER", "")
OIDC_AUDIENCE: str = os.environ.get("QG_OIDC_AUDIENCE", "")
# Where tenancy and roles live in the token. Provider-specific, so configurable
# rather than guessed; dotted paths work for nested claims.
OIDC_TENANT_CLAIM: str = os.environ.get("QG_OIDC_TENANT_CLAIM", "tenant_ids")
OIDC_ROLES_CLAIM: str = os.environ.get("QG_OIDC_ROLES_CLAIM", "realm_access.roles")

# ── Observability ─────────────────────────────────────────────────────
SERVICE_NAME: str = os.environ.get("QG_SERVICE_NAME", "querygate")
# OTel tracing (needs: uv sync --extra otel). Without an OTLP endpoint spans go
# to the console, which is enough to see the shape locally.
TRACING_ENABLED: bool = _env_bool("QG_TRACING_ENABLED")
OTLP_ENDPOINT: str = os.environ.get("QG_OTLP_ENDPOINT", "")
# Put SQL text and tenant identifiers into spans. OFF by default: traces are
# shipped to backends with none of this server's access controls, so the
# governed query path would leak through telemetry. Local debugging only.
TRACE_SENSITIVE: bool = _env_bool("QG_TRACE_SENSITIVE")
# Serve Prometheus metrics at /metrics (needs prometheus_client).
METRICS_ENABLED: bool = _env_bool("QG_METRICS_ENABLED", default=True)

# ── Debug ─────────────────────────────────────────────────────────────
# Attach the provenance footer (tables, plan cost, tenant_scoped) to results.
# Dev-only: a deployed pod keeps this off so internals cannot leak.
INCLUDE_PROVENANCE: bool = _env_bool("QG_INCLUDE_PROVENANCE")
