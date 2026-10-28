---
name: build-dashboard
description: Create useful Afterspan saved charts from observed events without duplicates or invented metrics.
---
Call connection_status, event_catalog and saved_charts first. Confirm charts:write. Ask what decision the charts should support if it is not clear. Discover each event's properties before choosing filters or breakdowns. Preview every definition with query_metric, using one frozen start/end/as_of when comparing results. Use count for actions and users only for explicit opaque identities; label browser tabs honestly. Do not sum daily uniques to estimate people or divide unrelated chart totals to claim funnel conversion.

Reuse an existing equivalent chart. For each new chart generate a UUID, call save_chart with chart_id, title and query, then read saved_charts and compare the saved definition. Reuse the identical UUID and payload on a timeout; inspect before changing anything. A conflicting ID is not permission to overwrite. Keep saved queries relative (days) so they continue to update; fixed windows are for the verification comparison.

Use titles that explain the unit and breakdown. Explain test traffic and measurement limits. Return connection_status.dashboard_url. This release saves individual charts in Saved charts, not a dashboard with editable sections or layout. Do not describe it as richer than it is.
