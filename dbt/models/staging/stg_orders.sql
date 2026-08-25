select
    order_id,
    tenant_id,
    customer_id,
    order_date::date as order_date,
    amount::numeric as amount,
    status
from {{ ref('raw_orders') }}
