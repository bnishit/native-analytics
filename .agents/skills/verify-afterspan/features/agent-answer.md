# Agent answer

## Sub-features

Committed goal inspection, dirty/draft denial, zero and nonzero facts, source/revision references, limits, supported next action and uncertain-write reconciliation.

## How to get to it (user POV)

Connect the fixed-checkout product-loop MCP to a fresh Codex session using one scoped workspace editor key. Ask it to inspect the goal, read its evidence and explain the next step.

## Driving it with the MCP client

Read the actual tool transcript. Only configured loop tools may appear. Have Codex return source, measurement revision, mature, qualified, waiting, unassessed, rate, limitations and next_action for the requested stored observation. Start the measured connection using the actual CLI before the fresh client runs. Have the fresh client reconnect with `loop_connect`, then submit its actual answer using `loop_deliver` with connection, evidence and stable answer UUIDs. Inspect the completed tool result; do not create a producer event in the verification harness. Replay the same answer and confirm its completion receipt does not change. Read back the stored start/completion events and measure the current cohort: the new workspace must remain waiting, with mature 0 and rate null. The service records submission for presentation; this is not proof a person read it. Confirm correct facts pass and a deliberately wrong count fails. Failure is an extra tool/credential, invented denominator, producer quality flag treated as proof, wrong source.

## Gotchas

Published GitHub commits can activate source; local unpushed commits remain unverifiable to the service. No owner or GitHub token belongs in MCP. The first deterministic rubric checks faithful aggregate interpretation; subjective usefulness is outside its proof.
