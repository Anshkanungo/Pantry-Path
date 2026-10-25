# Pipeline runs: Gantt summary

| DAG | Run | State | Wall time | Task time (sum) |
|---|---|---|---|---|
| `build_reference_data_serial` | `manual__2026-10-25T01:40:38.796951+00:00` | success | 11.2 min | 9.2 min |
| `build_reference_data` | `scheduled__2026-10-25T00:00:00+00:00` | success | 4.6 min | 11.0 min |

Wall time change from the first to the second run: -59%.

## `build_reference_data_serial`: slowest tasks

| Task | Instances | Span | Busy time | Max parallel |
|---|---|---|---|---|
| `refine_dish` | 254 | 6.5 min | 4.7 min | 1 |
| `download_off` | 1 | 2.0 min | 2.0 min | 1 |
| `bias_report` | 1 | 1.5 min | 1.5 min | 1 |
| `filter_off` | 1 | 0.4 min | 0.4 min | 1 |
| `clean_recipes` | 1 | 0.2 min | 0.2 min | 1 |
| `group_dishes` | 1 | 0.2 min | 0.2 min | 1 |

## `build_reference_data`: slowest tasks

| Task | Instances | Span | Busy time | Max parallel |
|---|---|---|---|---|
| `refine_dish` | 254 | 2.3 min | 6.4 min | 4 |
| `download_off` | 1 | 2.1 min | 2.1 min | 1 |
| `bias_report` | 1 | 1.5 min | 1.5 min | 1 |
| `filter_off` | 1 | 0.4 min | 0.4 min | 1 |
| `clean_recipes` | 1 | 0.2 min | 0.2 min | 1 |
| `group_dishes` | 1 | 0.2 min | 0.2 min | 1 |
