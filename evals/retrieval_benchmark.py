"""Retrieval benchmark on a synthetic catalog of realistic size.

The bundled sample catalog has four models, which is too few to tell any two
retrieval configurations apart: everything reaches the ceiling at once. This
builds forty instead, across ten domains, and asks thirty-two questions in
business language against descriptions written in schema language.

They are fixtures, not a second dbt project. Retrieval reads catalog metadata
and nothing else, so names, domains, descriptions and column names are the whole
of what it needs, and they live below as literals.

Method, stated so the numbers can be discounted properly:

* The catalog is synthetic. It reflects how it was written.
* The questions were written before any configuration was run.
* Three glossaries are compared, and the difference between the last two is the
  point of the whole exercise:
    backwards  keys are business words, which is the intuitive way round and
               does nothing, because expansion fires on the key and no document
               contains "revenue" when the columns say gross_amount
    blind      keys taken from the catalog, values are ordinary business
               synonyms, written without looking at the questions
    tuned      the same, but written after seeing which questions failed, which
               is how a glossary is really maintained and also why its score is
               an upper bound rather than a result

Embeddings need the extra; without it those rows are skipped.

    uv run python evals/retrieval_benchmark.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from querygate import config
from querygate.catalog.models import CatalogModel, Column, SemanticCatalog
from querygate.retrieval.index import CatalogIndex

MODELS = [
    (
        "fct_orders",
        "sales",
        "One row per order, rolled up from line level. Gross amount, "
        "discount, net amount and fulfilment status.",
        ["order_id", "customer_id", "gross_amount", "discount_amount", "net_amount", "status", "ordered_at"],
    ),
    (
        "fct_order_lines",
        "sales",
        "One row per line item within an order. Quantity, unit price and SKU.",
        ["order_line_id", "order_id", "sku", "quantity", "unit_price"],
    ),
    (
        "dim_customers",
        "sales",
        "Customer master. One row per customer account with acquisition source and segment.",
        ["customer_id", "account_name", "segment", "acquisition_source", "created_at"],
    ),
    (
        "fct_invoices",
        "finance",
        "Issued invoices with net, tax and gross totals, and settlement state.",
        ["invoice_id", "customer_id", "net_total", "tax_total", "gross_total", "settled_at"],
    ),
    (
        "fct_payments",
        "finance",
        "Received payments matched to invoices. Includes method and clearing date.",
        ["payment_id", "invoice_id", "amount", "method", "cleared_at"],
    ),
    (
        "fct_refunds",
        "finance",
        "Refunds issued against orders, with reason code.",
        ["refund_id", "order_id", "amount", "reason_code", "issued_at"],
    ),
    (
        "dim_accounts_receivable",
        "finance",
        "Outstanding balances per customer with ageing buckets in days.",
        ["customer_id", "outstanding_balance", "ageing_bucket"],
    ),
    (
        "fct_subscriptions",
        "product",
        "Active and historical subscription terms per account, with plan and billing period.",
        ["subscription_id", "customer_id", "plan_id", "period", "started_at", "ended_at"],
    ),
    (
        "dim_plans",
        "product",
        "Plan catalogue with list price, tier and included seat count.",
        ["plan_id", "plan_name", "tier", "list_price", "included_seats"],
    ),
    (
        "fct_plan_changes",
        "product",
        "Upgrades, downgrades and cancellations between plans over time.",
        ["change_id", "customer_id", "from_plan_id", "to_plan_id", "changed_at"],
    ),
    (
        "fct_sessions",
        "product",
        "Application sessions with duration and entry surface.",
        ["session_id", "user_id", "duration_seconds", "entry_surface", "started_at"],
    ),
    (
        "fct_feature_events",
        "product",
        "Instrumented feature interactions, one row per event.",
        ["event_id", "user_id", "feature_key", "occurred_at"],
    ),
    (
        "dim_users",
        "product",
        "End users within a customer account, with role and activation state.",
        ["user_id", "customer_id", "role", "activated_at", "last_seen_at"],
    ),
    (
        "fct_support_tickets",
        "support",
        "Support tickets with priority, queue and resolution timestamps.",
        ["ticket_id", "customer_id", "priority", "queue", "opened_at", "resolved_at"],
    ),
    (
        "fct_ticket_messages",
        "support",
        "Individual messages on a support ticket, inbound or outbound.",
        ["message_id", "ticket_id", "direction", "sent_at"],
    ),
    (
        "dim_agents",
        "support",
        "Support agents with team assignment and shift pattern.",
        ["agent_id", "agent_name", "team", "shift_pattern"],
    ),
    (
        "fct_csat_responses",
        "support",
        "Post-resolution satisfaction scores on a five point scale.",
        ["response_id", "ticket_id", "score", "submitted_at"],
    ),
    (
        "fct_campaign_sends",
        "marketing",
        "Outbound campaign deliveries with channel and template.",
        ["send_id", "campaign_id", "user_id", "channel", "template_key", "sent_at"],
    ),
    (
        "fct_campaign_engagements",
        "marketing",
        "Opens and clicks attributed to a campaign send.",
        ["engagement_id", "send_id", "engagement_type", "occurred_at"],
    ),
    (
        "dim_campaigns",
        "marketing",
        "Campaign metadata with objective, owner and budget.",
        ["campaign_id", "campaign_name", "objective", "owner", "budget"],
    ),
    (
        "fct_web_visits",
        "marketing",
        "Website visits with referrer, landing path and device class.",
        ["visit_id", "referrer", "landing_path", "device_class", "visited_at"],
    ),
    (
        "fct_attribution_touches",
        "marketing",
        "Multi-touch attribution points on the path to an order.",
        ["touch_id", "order_id", "channel", "position", "touched_at"],
    ),
    (
        "fct_shipments",
        "ops",
        "Physical shipments with carrier, dispatch and delivery timestamps.",
        ["shipment_id", "order_id", "carrier", "dispatched_at", "delivered_at"],
    ),
    (
        "fct_inventory_snapshots",
        "ops",
        "Daily on-hand quantity per SKU per warehouse location.",
        ["snapshot_date", "sku", "location_id", "on_hand_quantity"],
    ),
    (
        "dim_locations",
        "ops",
        "Warehouse and fulfilment locations with region and capacity.",
        ["location_id", "location_name", "region", "capacity"],
    ),
    (
        "fct_returns",
        "ops",
        "Returned goods with condition grade and restock outcome.",
        ["return_id", "order_id", "condition_grade", "restocked", "received_at"],
    ),
    (
        "dim_suppliers",
        "ops",
        "Supplier master with lead time and reliability grade.",
        ["supplier_id", "supplier_name", "lead_time_days", "reliability_grade"],
    ),
    (
        "fct_purchase_orders",
        "ops",
        "Purchase orders raised against suppliers.",
        ["po_id", "supplier_id", "sku", "quantity", "raised_at"],
    ),
    (
        "fct_headcount",
        "people",
        "Monthly headcount snapshot by department and level.",
        ["snapshot_month", "department", "level", "headcount"],
    ),
    (
        "fct_payroll_runs",
        "people",
        "Payroll disbursements per period with gross and deductions.",
        ["run_id", "period", "gross_pay", "deductions"],
    ),
    (
        "dim_employees",
        "people",
        "Employee master with department, manager and tenure.",
        ["employee_id", "department", "manager_id", "hired_at"],
    ),
    (
        "fct_time_off",
        "people",
        "Approved absence records with type and duration.",
        ["request_id", "employee_id", "absence_type", "days"],
    ),
    (
        "fct_cloud_spend",
        "platform",
        "Daily cloud infrastructure cost by service and environment.",
        ["spend_date", "service", "environment", "cost"],
    ),
    (
        "fct_api_requests",
        "platform",
        "API request log with endpoint, latency and response class.",
        ["request_id", "endpoint", "latency_ms", "response_class", "requested_at"],
    ),
    (
        "fct_incidents",
        "platform",
        "Production incidents with severity and time to restore.",
        ["incident_id", "severity", "started_at", "restored_at"],
    ),
    (
        "fct_deployments",
        "platform",
        "Service deployments with commit reference and outcome.",
        ["deployment_id", "service", "commit_sha", "outcome", "deployed_at"],
    ),
    (
        "dim_services",
        "platform",
        "Service registry with owning team and criticality tier.",
        ["service", "owning_team", "criticality_tier"],
    ),
    (
        "fct_contract_terms",
        "legal",
        "Negotiated contract terms per account, including notice period.",
        ["contract_id", "customer_id", "notice_period_days", "auto_renew", "signed_at"],
    ),
    (
        "fct_compliance_checks",
        "legal",
        "Automated policy check outcomes per system.",
        ["check_id", "system", "policy_key", "outcome", "checked_at"],
    ),
    (
        "dim_regions",
        "reference",
        "Geographic regions with parent territory and currency.",
        ["region", "territory", "currency"],
    ),
]


QUESTIONS = [
    ("how much money did we make last quarter", ["fct_orders", "fct_invoices"]),
    ("what is our top line", ["fct_orders", "fct_invoices"]),
    ("which accounts owe us money", ["dim_accounts_receivable", "fct_invoices"]),
    ("how much did we give back to unhappy buyers", ["fct_refunds"]),
    ("who is about to leave us", ["fct_plan_changes", "fct_subscriptions"]),
    ("are people downgrading", ["fct_plan_changes"]),
    ("how sticky is the product", ["fct_sessions", "fct_feature_events"]),
    ("which bits of the app does nobody touch", ["fct_feature_events"]),
    ("how fast do we get back to people who complain", ["fct_support_tickets"]),
    ("are customers happy after we help them", ["fct_csat_responses"]),
    ("how busy is the help desk", ["fct_support_tickets", "fct_ticket_messages"]),
    ("what does it cost us to run the servers", ["fct_cloud_spend"]),
    ("is the site slow", ["fct_api_requests"]),
    ("how often do things break in production", ["fct_incidents"]),
    ("how long does it take to fix an outage", ["fct_incidents"]),
    ("how many people work here", ["fct_headcount", "dim_employees"]),
    ("what do we spend on salaries", ["fct_payroll_runs"]),
    ("who is off next week", ["fct_time_off"]),
    ("which marketing pushes actually worked", ["fct_campaign_engagements", "dim_campaigns"]),
    ("where do our visitors come from", ["fct_web_visits", "fct_attribution_touches"]),
    ("how many emails did we blast out", ["fct_campaign_sends"]),
    ("are parcels arriving on time", ["fct_shipments"]),
    ("do we have enough stock", ["fct_inventory_snapshots"]),
    ("how much gets sent back to us", ["fct_returns"]),
    ("which vendors are unreliable", ["dim_suppliers"]),
    ("what are we buying in", ["fct_purchase_orders"]),
    ("how much does each tier cost", ["dim_plans"]),
    ("who signed up recently", ["dim_customers", "dim_users"]),
    ("which deals auto renew", ["fct_contract_terms"]),
    ("are we breaking any rules", ["fct_compliance_checks"]),
    ("how often do we ship code", ["fct_deployments"]),
    ("which team owns what", ["dim_services"]),
]


GLOSSARY_BACKWARDS = {
    "revenue": ("sales", "income", "money", "turnover"),
    "customer": ("client", "account", "buyer"),
    "churn": ("cancelled", "downgrade", "leaving"),
    "cost": ("spend", "expense"),
    "ticket": ("complaint", "issue"),
    "headcount": ("employees", "staff"),
}


GLOSSARY_BLIND = {
    "orders": ("purchases", "sales", "transactions"),
    "amount": ("value", "revenue", "money", "turnover"),
    "invoices": ("billing", "billed", "receivables"),
    "payments": ("receipts", "collections"),
    "refunds": ("credits", "chargebacks", "returns"),
    "balances": ("debt", "owed", "outstanding"),
    "subscriptions": ("contracts", "recurring", "seats"),
    "plans": ("packages", "tiers", "pricing"),
    "changes": ("upgrades", "downgrades", "churn", "attrition"),
    "sessions": ("visits", "activity", "engagement"),
    "feature": ("functionality", "capability", "adoption"),
    "users": ("people", "accounts", "seats"),
    "customers": ("clients", "accounts", "buyers"),
    "tickets": ("cases", "issues", "requests", "support"),
    "satisfaction": ("csat", "feedback", "sentiment"),
    "campaign": ("marketing", "promotion", "outreach"),
    "engagements": ("opens", "clicks", "responses"),
    "visits": ("traffic", "visitors", "referrals"),
    "attribution": ("sourcing", "channel", "journey"),
    "shipments": ("deliveries", "logistics", "parcels"),
    "inventory": ("stock", "quantity", "availability"),
    "returns": ("rma", "sendbacks"),
    "suppliers": ("vendors", "partners"),
    "purchase": ("procurement", "sourcing"),
    "headcount": ("staffing", "employees", "people"),
    "payroll": ("salaries", "compensation", "wages"),
    "employees": ("staff", "workforce", "people"),
    "absence": ("leave", "holiday", "pto"),
    "spend": ("cost", "expenditure", "bill"),
    "latency": ("performance", "response", "speed"),
    "incidents": ("outages", "failures", "downtime"),
    "deployments": ("releases", "rollouts"),
    "services": ("systems", "components", "ownership"),
    "contract": ("agreement", "terms", "renewal"),
    "compliance": ("policy", "governance", "audit"),
}


GLOSSARY_TUNED = {
    "amount": ("money", "revenue", "made", "earned", "top", "line", "sales"),
    "totals": ("money", "revenue", "top", "line"),
    "invoices": ("billed", "owe", "owing"),
    "balances": ("owe", "owed", "debt"),
    "refunds": ("gave", "back", "returned", "unhappy"),
    "changes": ("churn", "leave", "leaving", "downgrading", "cancelling"),
    "cancellations": ("churn", "leave", "leaving"),
    "sessions": ("sticky", "engagement", "usage", "active"),
    "feature": ("bits", "app", "touch", "used", "usage"),
    "tickets": ("complain", "complaints", "desk", "help", "busy"),
    "satisfaction": ("happy", "csat", "pleased"),
    "spend": ("cost", "costs", "servers", "bill"),
    "latency": ("slow", "fast", "speed"),
    "incidents": ("break", "broke", "outage", "down"),
    "restore": ("fix", "fixed", "recovery"),
    "headcount": ("people", "staff", "employees", "work"),
    "payroll": ("salaries", "wages", "pay"),
    "absence": ("off", "holiday", "leave"),
    "engagements": ("worked", "clicks", "opens"),
    "visits": ("visitors", "traffic", "come"),
    "sends": ("blast", "emails", "sent"),
    "shipments": ("parcels", "arriving", "delivery"),
    "inventory": ("stock", "enough"),
    "returns": ("sent", "back"),
    "suppliers": ("vendors", "unreliable"),
    "purchase": ("buying", "buy"),
    "plans": ("tier", "tiers", "pricing", "cost"),
    "contract": ("deals", "renew"),
    "compliance": ("rules", "breaking"),
    "deployments": ("ship", "shipping", "release"),
    "services": ("team", "owns", "owner"),
}


def _catalog(glossary: dict[str, tuple[str, ...]]) -> SemanticCatalog:
    models = tuple(
        CatalogModel(
            name=name,
            schema="analytics",
            table=name,
            domain=domain,
            description=description,
            columns=tuple(Column(name=c, type="text") for c in columns),
        )
        for name, domain, description, columns in MODELS
    )
    return SemanticCatalog(models=models, glossary=glossary, build={})


def _score(index: CatalogIndex, k: int = 3) -> tuple[int, int]:
    hit1 = hitk = 0
    for question, expected in QUESTIONS:
        ranked = [m.name for m in index.search(question, k)]
        hit1 += bool(ranked) and ranked[0] in expected
        hitk += bool(set(expected) & set(ranked))
    return hit1, hitk


def _embedder() -> object | None:
    try:
        from querygate.retrieval.embedder import FastEmbedEmbedder
    except ImportError:
        return None
    try:
        return FastEmbedEmbedder(config.EMBEDDING_MODEL)
    except Exception as exc:
        print(f"embeddings unavailable, skipping those rows: {exc}")
        return None


def main() -> int:
    total = len(QUESTIONS)
    embedder = _embedder()

    rows: list[tuple[str, dict, object | None]] = [
        ("BM25 alone", {}, None),
        ("Glossary written backwards", GLOSSARY_BACKWARDS, None),
        ("Glossary written blind, from the schema", GLOSSARY_BLIND, None),
        ("Embeddings (RRF), no glossary", {}, embedder),
        ("Blind glossary + embeddings", GLOSSARY_BLIND, embedder),
        ("Tuned glossary (leaked the test set)", GLOSSARY_TUNED, None),
        ("Tuned glossary + embeddings", GLOSSARY_TUNED, embedder),
    ]

    print(f"{len(MODELS)} models, {total} questions, hit@1 and hit@3\n")
    print(f"  {'configuration':42} {'hit@1':>11} {'hit@3':>11}")
    print(f"  {'-' * 42} {'-' * 11} {'-' * 11}")
    for label, glossary, emb in rows:
        if emb is None and "mbedding" in label:
            print(f"  {label:42} {'skipped':>11} {'skipped':>11}")
            continue
        hit1, hitk = _score(CatalogIndex.build(_catalog(glossary), embedder=emb))
        print(f"  {label:42} {hit1:>3}/{total} {hit1 / total:>5.0%} {hitk:>3}/{total} {hitk / total:>5.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
