---
name: connect-and-verify
description: Connect an Afterspan project and verify that real application activity is collected.
---
Use the deployment's /.well-known/afterspan to discover /mcp. Register the remote URL in the user's MCP client and use its OAuth flow. The user chooses a project and approves scopes in the browser. Never copy credentials into chat or tool arguments. Start with analytics:read; request charts:write only for an authorized chart task, and audit:read for request investigations. Connections can be revoked at /assets/mcp-connect.html.

Call connection_status and confirm the project. Call event_catalog with a bounded window, then setup_status. Distinguish configured collection from observed application activity. If authorized, exercise one safe real application interaction, then query_metric for its expected event. Do not manufacture production events to claim a working integration. Name test traffic separately, and do not claim that unrelated historical events prove the new interaction. If the event cannot be isolated, report that limit.

Finish with the project, scopes, collection evidence, time window and one supported next step. This connection cannot deploy, issue producer keys or administer accounts. Existing local CLI setup remains available for those separately authorized workflows.
