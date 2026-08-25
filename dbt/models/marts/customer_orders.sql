-- One row per order, enriched with customer + plan. Governed by tenant_id.
select
    o.order_id,
    o.tenant_id,
    o.customer_id,
    c.customer_name,
    c.region,
    o.order_date,
    o.amount,
    o.status,
    p.plan_name
from {{ ref('stg_orders') }} o
join {{ ref('stg_customers') }} c
    on o.customer_id = c.customer_id and o.tenant_id = c.tenant_id
join {{ ref('raw_plans') }} p
    on c.plan_id = p.plan_id
