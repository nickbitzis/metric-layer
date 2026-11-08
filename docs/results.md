## Reference SQL suite

| Query | Rows | Compiled | Without pre-agg | Used pre-agg |
|---|---:|:---:|:---:|:---:|
| gross revenue, total | 1 | pass | pass | yes |
| gross and net revenue by country (items and refunds at different grains) | 6 | pass | pass | yes |
| net revenue by month (refunds booked in the month issued) | 18 | pass | pass | yes |
| average order value by month in 2025 (ratio of sums) | 12 | pass | pass | yes |
| cancellation rate by order device | 3 | pass | pass | no |
| conversion rate by customer country (anonymous sessions kept as NULL) | 7 | pass | pass | no |
| sessions by traffic source, weekly, March 2026 | 30 | pass | pass | no |
| distinct buyers by month (count distinct, not additive) | 18 | pass | pass | no |
| trailing 28 day active buyers, daily, June 2026 | 30 | pass | pass | no |
| trailing 28 day active buyers by country, weekly, Q2 2026 | 72 | pass | pass | no |
| total customers (all-time cumulative) by quarter in 2026 | 2 | pass | pass | no |
| refund rate by customer segment | 2 | pass | pass | no |
| return on ad spend by month | 18 | pass | pass | yes |
| gross revenue by seller region, US and Canada buyers, excluding books | 3 | pass | pass | yes |
| units sold and units per order by country (numerator and denominator from different tables) | 6 | pass | pass | yes |
| refunds and refunded orders by reason in 2025 | 4 | pass | pass | no |
| average unit price by category (average measure) | 6 | pass | pass | no |
| new customers by acquisition channel, yearly | 20 | pass | pass | no |
| shipping fees and orders by device for business buyers | 3 | pass | pass | no |
| gross revenue by category and device, daily, last week of June 2026 (pre-aggregation path) | 122 | pass | pass | yes |

20 of 20 reference queries match.

## Naive joins vs compiled metrics

| Metric | Naive SQL | Compiled | Error | Cause |
|---|---:|---:|---:|---|
| Net revenue, all time | 2,414,666 | 2,486,970 | -2.9% | orders JOIN order_items JOIN refunds: each refund repeats once per line item, each item once per refund |
| Refunds issued, all time | 212,237 | 78,632 | +169.9% | same join, refund side only: a refund on a 4-item order is counted 4 times |
| Average order value | 64.03 | 136.35 | -53.0% | AVG over joined item rows averages line items, not orders |
| Sessions, all time | 67,956 | 100,000 | -32.0% | INNER JOIN to customers to get country drops anonymous sessions (customer_id is NULL) |
| Conversion rate | 0.2763 | 0.2000 | +38.2% | average of per-customer conversion rates (an average of ratios) that also skips anonymous sessions |

## Natural language evaluation

| Interpreter | Questions | Exact match | Execution match | Clarification recall | False clarifications |
|---|---:|---:|---:|---:|---:|
| offline | 67 (53 answerable, 14 need clarification) | 92.5% | 92.5% | 100.0% | 2 |
