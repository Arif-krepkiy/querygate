-- One row per customer with recency + active flag (ordered in last 90 days).
-- Governed by tenant_id. Use for retention / churn questions.
with last_order as (
    select tenant_id, customer_id, max(order_date) as last_order_date
    from {{ ref('customer_orders') }}
    group by 1, 2
)
select
    c.tenant_id,
    c.customer_id,
    c.customer_name,
    c.region,
    p.plan_name,
    lo.last_order_date,
    (lo.last_order_date >= date '2024-06-30' - interval '90 days') as is_active
from {{ ref('stg_customers') }} c
left join last_order lo
    on c.customer_id = lo.customer_id and c.tenant_id = lo.tenant_id
join {{ ref('raw_plans') }} p
    on c.plan_id = p.plan_id
