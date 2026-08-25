# Warehouse-enforced governance

For deployments where the warehouse, not QueryGate, decides who may read what:
Snowflake roles granted over per-department views, Snowflake row access
policies, Postgres RLS. Common in shops whose marts were never modelled
multi-tenant, so there is no tenant column to filter on.

Set `QG_GOVERNANCE_MODE=warehouse`. This document is the design behind that
switch, and the work still outstanding.

---

## The two modes are alternatives, not layers

| | `inject` (default) | `warehouse` |
|---|---|---|
| Who enforces row security | QueryGate | the engine's own grants / policies |
| Requires | a tenant column on governed models | a warehouse role per audience |
| Mechanism | predicate injected into every governed scope, then independently re-proved | the query runs **as the caller's role** |
| Fails closed when | the caller has no tenant scopes | the caller maps to no warehouse role |
| Guarantee rests on | our AST rewrite being correct | your grants being correct |

Running both is only meaningful when the tenant column genuinely exists; if it
does not, `inject` silently degrades. See below.

## The failure this mode exists to prevent

With the dbt catalog loader, `governed = QG_TENANT_COLUMN in columns`. A model
without that column is therefore **public by construction**. That is deliberate, so a
lookup table like `ref_codes` stays queryable.

But if *most* marts lack the column, the consequence is not an error. It is:

1. no predicate is injected anywhere, and
2. every query runs under one service identity.

Each caller then receives the union of everything that identity can read, the
query succeeds, and nothing anywhere reports a problem. **Wrong rows, no error**
which is the worst failure mode a data-access layer can have.

`warehouse` mode replaces that with a mandatory mapping and a refusal.

## What is implemented

- **`QG_GOVERNANCE_MODE`**: `inject` | `warehouse`. In `warehouse` mode the
  pipeline skips injection *explicitly* (the trace records "skipped: enforced by
  the warehouse" rather than quietly showing nothing) and stops treating empty
  tenant scopes as a refusal, because scopes are meaningless here.
- **`QG_WAREHOUSE_ROLE_MAP`**: `analyst=QG_ANALYST,finance=QG_FINANCE`. An
  explicit allowlist, *not* the token's role claim passed through: a claim is
  attacker-influenced input, and forwarding it verbatim would let a loose IdP
  configuration name any role in the warehouse.
- **Fail-fast at startup** (`validate_configuration`, called from `main`):
  `warehouse` mode without a role map refuses to boot. A deployment that would
  serve everyone as one identity must never accept traffic.
- **Refusal, never a fallback**, per request: unmapped role, no roles, anonymous
  caller, or *several* mapped roles (ambiguous identity, and picking one silently
  would grant or withhold access by accident) all raise `IdentityError`, surfaced
  to the agent as `kind: "identity"`.
- **Identity attached at the warehouse seam** (`warehouse.estimate` /
  `warehouse.execute`), not in the tools. Every query path (ad-hoc SQL, metrics,
  profiling, filter values) funnels through there, so no path can reach an
  engine without the caller's identity.
- **Snowflake adapter opens the session under that role**
  (`_connect(prepared.warehouse_role)`), then pins and verifies it. See below.

### The secondary-roles trap

This design requires the service user to be granted **every** mapped role. That
makes a Snowflake default dangerous: when the user's `DEFAULT_SECONDARY_ROLES`
is `('ALL')`, which is common and the default for users created through some flows,
the session activates *all* granted roles no matter which primary role you
connect with. `role=QG_ANALYST` then narrows nothing: the caller reads every
audience's views, the connection looks correctly scoped, and nothing errors.
Wrong rows, no error: the same failure this mode exists to prevent, reappearing
one layer down.

`_pin_role` therefore does three things in order, before any query runs:

1. `USE SECONDARY ROLES NONE`. This, not the primary role, is what actually
   narrows the session;
2. `USE ROLE <role>`;
3. `SELECT CURRENT_ROLE()` and compare. A silent fallback to a default role
   fails closed here rather than answering as somebody else.

The role name is matched against an identifier whitelist first: it comes from
operator config rather than a token, but it still reaches SQL as text.

Verify on a real account before trusting it:

```sql
-- as the service user, after QueryGate connects with role=QG_ANALYST
SELECT CURRENT_ROLE(), CURRENT_SECONDARY_ROLES();
-- expect: QG_ANALYST, and no secondary roles
SHOW PARAMETERS LIKE 'DEFAULT_SECONDARY_ROLES' FOR USER QUERYGATE_SVC;
```

## What is still missing

**1. Catalog filtering by role.** Retrieval currently offers every model in the
catalog. Under warehouse enforcement a caller will be shown views they cannot
read, write valid SQL against them, and get a permission error from the engine,
then try again. Not a security hole (the engine refuses), but it burns turns and
looks broken. Needs: per-model visibility in the catalog (role → models) and
filtering in `search_catalog` / `describe_model`.

**2. Provenance must record the role.** Every answer should carry which warehouse
identity produced it, and it should reach the trace. Otherwise an incident review
ends at "querygate ran it".

**3. Snowflake OAuth / token pass-through.** Role mapping is the pragmatic step;
the honest end state is the caller's own identity reaching Snowflake via External
OAuth. Then native row access and masking policies apply unchanged, and the
Snowflake audit log shows the human rather than a service account. Cost: an
External OAuth integration and token exchange in the server.

**4. Per-role connection reuse.** Connecting per call is fine at current volumes
and avoids a pool that would have to be invalidated per role, but a busy
deployment will want pooling keyed by role.

**5. Postgres/BigQuery equivalents.** The mode is engine-neutral in the pipeline,
but only Snowflake acts on `warehouse_role` today. Postgres would `SET ROLE`;
BigQuery has no per-query role and would need impersonation of a per-audience
service account.

## What QueryGate still provides in this mode

Row security is delegated, so the isolation demo is no longer *our* guarantee and
should not be pitched as one. Everything else is unaffected and is the actual
value here:

- read-only, single-statement enforcement (AST, before the engine sees anything);
- the cost gate and statement timeout. On Snowflake, billed by warehouse time,
  the timeout *is* the spend ceiling;
- grounding: the agent cannot invent tables or columns;
- **certified metrics** (`QG_CERTIFIED_ONLY_ROLES`), orthogonal to row security
  and the direct answer to "what if it computes a key metric wrong for an
  executive";
- audit, rate limiting, response compaction.

The pitch changes shape accordingly, and improves: *we do not touch your
governance. It stays in Snowflake, where your CDO already owns it. We close what
you do not have: grounding, cost control, certified numbers, and an audit trail
of what the agent asked.*

## Migration checklist for a role-and-view Snowflake shop

1. Create one Snowflake role per audience if they do not already exist, granted
   `SELECT` on that audience's views only.
2. Grant every mapped role to the QueryGate service user (`GRANT ROLE … TO USER …`).
3. Map IdP roles to warehouse roles in `QG_WAREHOUSE_ROLE_MAP`; ensure each
   principal resolves to exactly one.
4. Set `QG_GOVERNANCE_MODE=warehouse`. The server refuses to start if the map is
   missing.
5. Verify with two accounts in different roles that the same question returns
   different, correct rows, and that an unmapped account is refused rather than
   served.
6. Keep `QG_MAX_PLAN_COST` and `QG_STATEMENT_TIMEOUT_MS` set: they are the only
   spend controls on this engine.
