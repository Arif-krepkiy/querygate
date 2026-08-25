-- Public reference: subscription plans + pricing. No tenant_id → not governed.
select
    plan_id,
    plan_name,
    monthly_price,
    tier
from {{ ref('raw_plans') }}
