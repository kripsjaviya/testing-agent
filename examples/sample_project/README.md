# sample_project — a target for the agent

A small orders service with **six planted bugs**, one per category in the
assessment brief. This is a *target*, not part of the agent: nothing under
`src/bugfixer/` refers to it, and the agent discovers everything here at runtime
from test output and source.

Standard library only (`sqlite3`, `asyncio`), so it runs with no install step.

## Running its tests directly

```bash
cd examples/sample_project
python -m pytest          # 28 tests, 7 failing, 6 bugs
```

## The planted bugs

Listed here for a human reviewer. The agent is never told any of this.

| # | Category | File | Failing test(s) |
|---|---|---|---|
| 1 | Off-by-one / logic | `app/pagination.py` | `test_partial_last_page_is_counted` |
| 2 | Missing input validation | `app/orders.py` | `test_negative_quantity_is_rejected` |
| 3 | Incorrect async handling | `app/inventory.py` | `test_concurrent_reservations_do_not_lose_updates` |
| 4 | Data transformation | `app/serialisers.py` | `test_serialised_order_exposes_every_field` |
| 5 | Security (SQL injection) | `app/repository.py` | `test_crafted_customer_name_cannot_read_other_rows` **and** `test_customer_names_with_apostrophes_are_supported` |
| 6 | Resource leak | `app/audit.py` | `test_counting_entries_releases_the_file_handle` |

**Seven failing tests, six bugs.** Bug 5 surfaces twice: the interpolated query is
both injectable *and* broken for any legitimate name containing an apostrophe.
That is deliberate — it exercises the agent's re-run-after-each-fix behaviour,
where one patch clears two failures and the second is never sent to a model.

The other 21 tests pass and must keep passing, so a patch that overreaches is
caught by the full-suite regression gate.
