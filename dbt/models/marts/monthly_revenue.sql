-- Revenue per tenant, month, region. Completed orders only. Governed by tenant_id.
select
    tenant_id,
    date_trunc('month', order_date)::date as month,
    region,
    sum(amount) as revenue,
    count(*) as order_count
from {{ ref('customer_orders') }}
where status = 'completed'
group by 1, 2, 3
