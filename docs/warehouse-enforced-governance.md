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

## The one-service-user problem

The scenario this was built for, stated plainly: a mid-sized company has exactly
one technical Snowflake user. Everything connects through it. If that user holds
`SELECT` on the whole warehouse, then the moment an agent sits in front of it,
an analyst on team A can read team B's tables by asking nicely. Nothing in the
warehouse objects, because as far as Snowflake is concerned every query is the
same, fully-privileged principal.

Role mapping fixes the **query**. It does not fix the **credential**, and it is
worth being precise about the difference, because the two get conflated and the
gap between them is where an audit finding lives.

**What is fixed.** A query arrives, the caller's IdP role maps to exactly one
warehouse role, the session is pinned to it, and Snowflake's own grants decide
what that query can read. Team A's analyst gets team A's views. There is no
prompt that changes this, because the role is chosen from operator config before
any SQL runs, and `CURRENT_ROLE()` is read back and compared afterwards.

**What is not fixed.** The service user must be granted *every* mapped role for
this to work at all. So the credential itself is a union principal: whoever holds
those connection details holds the whole warehouse. The blast radius of a leaked
secret, a misconfigured pod, or a second process reusing the same account is
total. Role pinning is a control QueryGate applies at runtime; it is not a
property of the account.

That is an honest and often acceptable trade, the same posture as any application
service account, but it should be a decision rather than an accident.

### The ladder, cheapest first

**1. One role per audience, mapped (implemented).** No Snowflake feature beyond
grants. Works today. Residual risk: the union principal above. Right answer for
most mid-sized shops, and the place to start.

**2. One service user per audience.** N credential sets instead of one. Removes
the union principal, since a leaked `QG_FINANCE_SVC` password exposes finance
only, at the cost of managing N secrets and N connection configs. QueryGate would need
per-audience connection settings rather than a single `QG_SF_*` block, which is
a small change to the adapter and a larger one to how you run it. Worth it when
the audiences differ in sensitivity rather than just in subject.

**3. Row access policies instead of per-audience views.** Keeps one set of
tables. The policy reads `CURRENT_ROLE()` and filters. Better than views when the
audience grid is wide: twelve teams times four marts is forty-eight views to keep
in sync, versus four tables with one policy each. Still relies on role pinning
for its input, so it composes with step 1 rather than replacing it.

**4. External OAuth, the caller's own identity.** The honest end state. The human
authenticates to the IdP, QueryGate exchanges that token for a Snowflake one, and
the query runs as *them*. No union principal exists at all. Native row access and
masking policies apply unchanged, and the Snowflake audit log names a person
rather than a service account, which is the thing a compliance reviewer actually
asks for. Cost: an External OAuth security integration, token exchange in the
server, and every analyst needs a Snowflake identity, which is precisely what a
company with one technical user does not have. That last point is usually what
decides it, and it is an org change more than an engineering one.

### Where the sharp edge is

Not in any of the above. It is in step 1 combined with `DEFAULT_SECONDARY_ROLES
= ('ALL')`, covered in detail earlier on this page. That default silently
un-does the entire mechanism while leaving every surface looking correct, and it
is the specific reason `_pin_role` disables secondary roles and then verifies the
result rather than trusting `USE ROLE`.

Check it before anything else:

```sql
SHOW PARAMETERS LIKE 'DEFAULT_SECONDARY_ROLES' FOR USER QUERYGATE_SVC;
```

### What still has to be decided per deployment

- Whether an analyst may hold two mapped roles. QueryGate refuses ambiguity
  today, which is safe but means "finance analyst who also covers ops" needs a
  third role granting both, created deliberately, rather than resolving to a
  silent union.
- Whether the catalog should hide models a role cannot read. Currently it does
  not, so the agent will happily write valid SQL against a view it will then be
  refused. Not a leak, but it burns turns. See the first item below.
- What happens to `dbt docs` if the marts are not tenant-modelled. The catalog
  still describes every model to every caller, so descriptions and column names
  leak even when rows do not. For most shops table names are not sensitive; if
  yours are, that is an argument for per-role catalog filtering rather than a
  reason to avoid this mode.

## Sourcing the map from Snowflake instead of config

`QG_WAREHOUSE_ROLE_MAP` duplicates something the warehouse already knows. The
grants exist in Snowflake, the roles exist in Snowflake, and a team that runs
this properly already keeps that model in one place. Restating it in an env var
gives you two sources of truth and one of them will rot.

Worth doing. The rest of this section is what it takes to do it without moving
the failure mode somewhere worse.

### What the join key would be

The gap is that Snowflake knows its own roles but not which IdP role each one
answers to. Four ways to close it, roughly in order of how much they cost:

**Naming convention.** IdP `finance` maps to Snowflake `QG_FINANCE` by prefix.
Zero configuration. Also completely implicit, so renaming an IdP group silently
unmaps an audience, and there is no place to look up what the convention was.
Fine for a small shop, bad the moment someone new joins.

**SCIM provisioning.** If the IdP already provisions Snowflake through SCIM, the
role names *are* the group names and the map is the identity function. Nothing
to store, because the mapping is a consequence of how the roles got created. Any
shop already doing this should probably start here, and this is the case where
the whole feature is just "stop configuring what SCIM already decided".

**Role comments.** `COMMENT = 'idp_role=finance'` on the role. Readable with
`SHOW ROLES`, survives renames of everything except the comment itself. Cheap and
explicit, though a comment is a string field with no schema and nothing stops a
typo.

**Object tags.** `ALTER ROLE QG_FINANCE SET TAG governance.idp_role = 'finance'`.
The Snowflake-native answer, queryable and governable in its own right. Also
Enterprise-tier, which decides it for a lot of teams.

### The freshness trap

This is the part that would bite, and it is specific enough to be worth writing
down before anybody starts.

Snowflake exposes this information twice, and the convenient copy is the wrong
one. `SNOWFLAKE.ACCOUNT_USAGE.ROLES`, `GRANTS_TO_USERS` and `TAG_REFERENCES` are
the queryable, joinable, pleasant views, and they lag behind reality by up to a
couple of hours. Using them for an authorization decision means an offboarded
analyst keeps their access until the view catches up, and a newly granted role
does not work for reasons nobody can see. The real-time answers come from `SHOW
ROLES` and `SHOW GRANTS TO USER`, which return result sets rather than tables and
are more awkward to consume.

Use the `SHOW` commands. Verify the latency of anything in `ACCOUNT_USAGE` on
your own account before trusting it with an access decision; the documented
figures vary by view and have changed over time.

### Where the trust boundary moves

The current design says the map is an operator allowlist rather than a token
claim, because a claim is attacker-influenced input. Sourcing the map from
Snowflake does not break that argument, but it does change who has to be trusted:
whoever can set a role comment or tag can now change who reads what.

That is very likely fine. Those same people already control the grants, which is
strictly more power than controlling the mapping. It is worth stating explicitly
rather than discovering during a review, and it does mean the tag or comment
should be owned by the same role that owns the grants, not by anyone who happens
to hold `OWNERSHIP` on a role.

### Availability, which is the real cost

An env var cannot be unreachable. A discovered map can, and the failure is total:
no map means no identity resolution means every query is refused. That turns a
Snowflake blip into a full outage of the server.

The pattern already exists in this codebase, in `catalog/sync.py`: load on a TTL,
keep serving the last-known-good copy when a refresh fails, never crash the
serving path on a sync error. A discovered role map should work the same way,
with one difference that matters. The catalog degrades safely when stale, because
an out-of-date description is a cosmetic problem. A stale role map is an access
control decision, so it needs a hard ceiling on staleness after which the server
refuses rather than guesses. Fail-closed on stale, not last-known-good forever.

### Shape it would take

```
QG_WAREHOUSE_ROLE_SOURCE=config | snowflake_tag | snowflake_comment
QG_WAREHOUSE_ROLE_TAG=governance.idp_role     # for the tag variant
QG_WAREHOUSE_ROLE_MAX_STALENESS_SECONDS=900   # refuse rather than use an older map
```

`config` stays the default. The discovered variants resolve at startup, so a
deployment that cannot read its own role model fails to boot rather than failing
per request, which is the same choice `validate_configuration` already makes.

`scripts/snowflake_roles.py` would then invert: instead of reading the env var
and emitting grants, it would emit the grants *and* the tag or comment that makes
them discoverable, which keeps provisioning and discovery in one command.

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

[`scripts/snowflake_roles.py`](../scripts/snowflake_roles.py) generates steps 1
and 2 from the same `QG_WAREHOUSE_ROLE_MAP` the server reads, so the grants and
the mapping cannot drift. It prints the SQL and stops; `--apply` pipes it to the
Snowflake CLI, `--verify` emits the read-only checks instead. It refuses outright
if the map names a role no grant covers, which is the mistake that otherwise
surfaces as a caller being refused in production for no visible reason.

```bash
export QG_WAREHOUSE_ROLE_MAP='analyst=QG_ANALYST,finance=QG_FINANCE'
python scripts/snowflake_roles.py --service-user QUERYGATE_SVC --warehouse QG_WH \
    --grant QG_ANALYST=ANALYTICS.SHARED --grant QG_FINANCE=ANALYTICS.FINANCE
```


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
