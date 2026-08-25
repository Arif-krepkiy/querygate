select
    customer_id,
    tenant_id,
    customer_name,
    plan_id,
    signed_up_at::date as signed_up_at,
    region
from {{ ref('raw_customers') }}
