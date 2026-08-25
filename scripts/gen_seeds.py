"""Generate the demo seed CSVs (multi-tenant SaaS orders).

Fixed seed, so the committed CSVs and the eval ground-truth numbers stay
reproducible.

    python scripts/gen_seeds.py
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

random.seed(42)

SEEDS = Path(__file__).resolve().parent.parent / "dbt" / "seeds"
TENANTS = [
    ("acme", "Acme Corp", "Retail"),
    ("globex", "Globex", "Manufacturing"),
    ("initech", "Initech", "Software"),
]
PLANS = [(1, "Free", 0, "t0"), (2, "Pro", 49, "t1"), (3, "Enterprise", 299, "t2")]
REGIONS = ["NA", "EU", "APAC"]
STATUSES = ["completed", "completed", "completed", "pending", "refunded"]  # weighted
TODAY = date(2024, 6, 30)


def _write(name: str, header: list[str], rows: list[list]) -> None:
    with (SEEDS / f"{name}.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"seeds/{name}.csv: {len(rows)} rows")


def main() -> None:
    SEEDS.mkdir(parents=True, exist_ok=True)

    _write("raw_plans", ["plan_id", "plan_name", "monthly_price", "tier"], [list(p) for p in PLANS])
    _write("raw_tenants", ["tenant_id", "tenant_name", "industry"], [list(t) for t in TENANTS])

    customers, orders = [], []
    cid, oid = 0, 0
    for tenant_id, _, _ in TENANTS:
        n_customers = random.randint(20, 40)
        for _ in range(n_customers):
            cid += 1
            plan_id = random.choice([1, 2, 2, 3])
            region = random.choice(REGIONS)
            signup = TODAY - timedelta(days=random.randint(30, 700))
            customers.append([cid, tenant_id, f"Customer {cid}", plan_id, signup.isoformat(), region])
            for _ in range(random.randint(0, 12)):
                oid += 1
                od = signup + timedelta(days=random.randint(0, (TODAY - signup).days))
                amount = round(random.uniform(20, 900), 2)
                orders.append([oid, tenant_id, cid, od.isoformat(), amount, random.choice(STATUSES)])

    _write(
        "raw_customers",
        ["customer_id", "tenant_id", "customer_name", "plan_id", "signed_up_at", "region"],
        customers,
    )
    _write("raw_orders", ["order_id", "tenant_id", "customer_id", "order_date", "amount", "status"], orders)


if __name__ == "__main__":
    main()
