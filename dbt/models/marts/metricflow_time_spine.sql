-- Required by dbt MetricFlow for time-based metric aggregation. Internal
-- helper. QueryGate filters models named metricflow_* out of the served catalog.
{{ config(materialized='table') }}

with days as (
    select generate_series(
        date '2020-01-01', date '2025-12-31', interval '1 day'
    )::date as date_day
)
select date_day from days
