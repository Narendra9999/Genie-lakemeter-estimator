# DCP configuration source

This branch derives its DCP policy and function contracts from the working agent `01f1c809927f1586bbd2566786d9c455` exported on November 2, 2026. It is a DCP-only projection, not a full copy of the broader agent: only DCP functions, tables, SQL examples and benchmarks are retained. A five-engine menu replaces cross-product catalog discovery. Asset IDs for retained examples are preserved.

The current agent policy presents monthly and one-time totals. Lower-level line-item/detail functions remain part of the calculator API and its regression tests. `live_function_definitions.json` records only the nine DCP function contracts used to validate the source and generated SQL. No live agent or workspace object was changed for this branch.
