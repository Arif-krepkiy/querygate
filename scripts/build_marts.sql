CREATE SCHEMA IF NOT EXISTS analytics;

-- staging (inlined from dbt models)
DROP TABLE IF EXISTS analytics.customer_orders, analytics.monthly_revenue, analytics.active_customers, analytics.plan_catalog CASCADE;

CREATE TABLE analytics.customer_orders AS
WITH stg_orders AS (
  SELECT order_id, tenant_id, customer_id, order_date::date AS order_date, amount::numeric AS amount, status
  FROM raw_orders
),
stg_customers AS (
  SELECT customer_id, tenant_id, customer_name, plan_id, region FROM raw_customers
)
SELECT o.order_id, o.tenant_id, o.customer_id, c.customer_name, c.region,
       o.order_date, o.amount, o.status, p.plan_name
FROM stg_orders o
JOIN stg_customers c ON o.customer_id = c.customer_id AND o.tenant_id = c.tenant_id
JOIN raw_plans p ON c.plan_id = p.plan_id;

CREATE TABLE analytics.monthly_revenue AS
SELECT tenant_id, date_trunc('month', order_date)::date AS month, region,
       sum(amount) AS revenue, count(*) AS order_count
FROM analytics.customer_orders WHERE status = 'completed'
GROUP BY 1,2,3;

CREATE TABLE analytics.active_customers AS
WITH last_order AS (
  SELECT tenant_id, customer_id, max(order_date) AS last_order_date
  FROM analytics.customer_orders GROUP BY 1,2
)
SELECT c.tenant_id, c.customer_id, c.customer_name, c.region, p.plan_name,
       lo.last_order_date,
       (lo.last_order_date >= DATE '2024-06-30' - INTERVAL '90 days') AS is_active
FROM raw_customers c
LEFT JOIN last_order lo ON c.customer_id = lo.customer_id AND c.tenant_id = lo.tenant_id
JOIN raw_plans p ON c.plan_id = p.plan_id;

CREATE TABLE analytics.plan_catalog AS
SELECT plan_id, plan_name, monthly_price::numeric AS monthly_price, tier FROM raw_plans;
