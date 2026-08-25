"""Semantic catalog data model. Frozen, shared across requests."""

from __future__ import annotations

from dataclasses import dataclass, field


class CatalogError(ValueError):
    """Raised when catalog data cannot be loaded or parsed."""


@dataclass(frozen=True)
class Column:
    name: str
    type: str = ""
    description: str = ""
    # Column-level security. None = readable. "hash" = stable pseudonym (equal
    # values stay equal, so grouping still works). "redact" = constant literal.
    # Sourced from dbt column meta, never guessed from the column name.
    mask: str | None = None

    @property
    def is_masked(self) -> bool:
        return self.mask is not None


@dataclass(frozen=True)
class Join:
    """A known way to join this model to another.

    Sourced from dbt ``relationships`` tests, which is the only place a project
    actually declares foreign keys. Without this the agent has to guess join
    keys from column names, the most common way generated SQL goes quietly wrong
    (a plausible join that silently fans out row counts).
    """

    to_model: str
    from_column: str
    to_column: str


@dataclass(frozen=True)
class Metric:
    """A human-owned metric definition (dbt MetricFlow).

    A person defines what a business term means once ("active customer",
    "completed revenue"), so every query returns the same number instead of the
    model inventing one. The agent is handed these as authoritative and writes
    SQL matching ``expr`` (+ ``filter``).
    """

    name: str
    label: str
    description: str
    model: str  # the CatalogModel this metric is computed on
    expr: str  # the aggregation in business terms, e.g. "sum(amount)"
    filter: str = ""  # optional condition, e.g. "status = 'completed'"
    # Set by the data team in dbt meta. QueryGate never sets it and the agent
    # cannot claim it. See query/certification.py.
    certified: bool = False


@dataclass(frozen=True)
class CatalogModel:
    name: str
    schema: str
    table: str
    kind: str = "table"  # table | view
    governed: bool = False
    tenant_column: str | None = None
    domain: str = ""
    description: str = ""
    columns: tuple[Column, ...] = ()
    joins: tuple[Join, ...] = ()
    # Column the table is partitioned by, from dbt's `partition_by` config. The
    # agent is told about it so it can filter; whether a missing filter is
    # *refused* is a per-deployment call (config.REQUIRE_PARTITION_FILTER),
    # because it only costs real money on engines that prune by partition.
    partition_column: str | None = None

    def relation(self) -> str:
        """Fully-qualified, quoted relation for the engine."""
        return f'"{self.schema}"."{self.table}"'

    def identifiers(self) -> frozenset[str]:
        """Every lowercased reference form that should resolve to this model."""
        return frozenset(
            {
                self.name.lower(),
                self.table.lower(),
                f"{self.schema}.{self.table}".lower(),
                f"{self.schema}.{self.name}".lower(),
            }
        )

    def column_names(self) -> frozenset[str]:
        return frozenset(col.name.lower() for col in self.columns)

    def masked_columns(self) -> dict[str, str]:
        """Lowercased column name → mask policy, for the columns that carry one."""
        return {c.name.lower(): c.mask for c in self.columns if c.mask}


@dataclass(frozen=True)
class SemanticCatalog:
    models: tuple[CatalogModel, ...]
    glossary: dict[str, tuple[str, ...]] = field(default_factory=dict)
    metrics: tuple[Metric, ...] = ()
    build: dict[str, str] = field(default_factory=dict)

    def metrics_for(self, model_name: str) -> tuple[Metric, ...]:
        return tuple(m for m in self.metrics if m.model.lower() == model_name.lower())

    def get_metric(self, name: str) -> Metric | None:
        wanted = name.lower()
        return next((m for m in self.metrics if m.name.lower() == wanted), None)

    def get_model(self, name: str) -> CatalogModel | None:
        wanted = name.lower()
        for model in self.models:
            if wanted in model.identifiers():
                return model
        return None

    def resolve_table(self, table_ref: str) -> CatalogModel | None:
        return self.get_model(table_ref) if table_ref else None
