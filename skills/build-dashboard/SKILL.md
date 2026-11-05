---
name: build-dashboard
description: Answer usage questions in Afterspan and optionally save useful charts without duplicates or invented metrics.
---
Call connection_status, event_catalog and saved_charts first. Usage reads need analytics:read; charts:write is only needed when the user asks to save a chart. Ask what decision the charts should support if it is not clear. Discover each event's properties before choosing filters or breakdowns. Preview every definition with query_metric, using one frozen start/end/as_of when comparing results. Return its view_url and open that native Afterspan query when asked to pull up usage. Do not replace the product with a detached HTML report unless the user asks for an export. A project dashboard link is not proof that the exact query is visible.

Use count for actions and users only for explicit opaque identities; a shared integration credential is not one person. Separate known test traffic using explicit evidence. Do not invent a production/test label for historical events or infer a person from a client or IP. Do not sum daily uniques to estimate people or divide unrelated chart totals to claim funnel conversion.

For an authorized save, confirm charts:write and reuse an existing equivalent chart. For each new chart generate a UUID, call save_chart with chart_id, title and query, then read saved_charts and compare the saved definition. Reuse the identical UUID and payload on a timeout; inspect before changing anything. A conflicting ID is not permission to overwrite. Keep saved queries relative (days) so they continue to update; fixed windows are for the verification comparison.

Use titles that explain the unit and breakdown. Explain test traffic and measurement limits. For saved charts return connection_status.dashboard_url; usage reads keep their exact query_metric.view_url. This release saves individual charts in Saved charts, not a dashboard with editable sections or layout. Do not describe it as richer than it is.
