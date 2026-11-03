# Operating evidence and recovery

The early outcome-platform release includes the tools below. A healthy HTTP endpoint and a persistent disk do not prove complete collection or recoverability.

## Encrypted operator snapshots

`scripts/backup.py` is an offline **operator-entire-database** tool. It reads the operator's local database path, including every workspace, account, credential hash, event, debug ciphertext and definition. It is not a customer export, MCP tool, workspace-key action or project-scoped API. Do not give its archive or key to a project member as if it contained only their project.

Load a separate random 32-byte URL-safe base64 key into `AFTERSPAN_BACKUP_KEY` from the operator's secret store. Never use a command-line secret or replace `AFTERSPAN_CONNECTION_KEY` to make a restore work. Store that existing connection key separately too: encrypted provider credentials and debug snapshots still need their original key after restoring the database.

```sh
python scripts/backup.py create --database /data/analytics.db --archive /private/backup.asb
python scripts/backup.py restore --archive /private/backup.asb --destination /private/isolated-restore/analytics.db
```

Create the isolated restore directory with mode 700 first. The destination must not exist. The tool refuses overwrites; it never swaps the running database. Keep the isolated app bound to loopback, with network egress disabled and no mail, billing, provider or deployment workers. Do not start the normal application against an old backup before reviewing those side effects.

Creation uses SQLite's backup API to include committed WAL pages in one consistent snapshot, without copying live database/WAL files independently. A five-minute snapshot limit fails rather than waiting forever on a blocked source. The private temporary snapshot is removed on completion/failure. Encrypted archives and restored database files use mode 600. Bounded AES-GCM frames authenticate their sequence and contents; an authenticated final receipt verifies byte count, chunk count and full plaintext hash. Wrong key, changed/truncated/reordered data or extra bytes reject restoration. Failed restores remove the newly created destination. SQLite consistency, foreign keys and the basic Afterspan schema are checked before success.

Encryption protects an archive from readers without its separate key. It does not protect a compromised operator, the live plaintext database, OS swap or a deliberately decrypted copy. This local command creates no off-site copy, retention policy, schedule, recovery-time guarantee or backup success alert. Set those separately in the deployment's infrastructure. Verify a retained archive on an isolated machine and record its actual time and size. Do not infer disaster-recovery readiness from these unit tests.

A backup can contain data erased since its creation and grants revoked afterward. Keep archives under a bounded retention/erasure policy. Before any production restoration, reconcile subsequent erasures and access revocations, expired captures/fixtures, external provider writes and idempotency records. Restoring a backup is not permission to replay payments, messages, releases or other business writes. Test recovery without those integrations enabled.

## Current local proof

### Scheduled object-storage copies — local candidate

The optional operator job reuses the same encrypted snapshot and isolated restore. It is off by default. When enabled on the running service, a background worker checks the persisted schedule every minute; the default interval is 24 hours. A shared-volume file lock prevents overlapping workers during process replacement. This requires a POSIX host and one retained SQLite volume, not a laptop timer. It provides no customer export or MCP backup tool.

Configure a private S3-compatible bucket and a unique deployment-owned prefix. Set `AFTERSPAN_BACKUP_ENABLED=true`, `AFTERSPAN_BACKUP_BUCKET`, `AFTERSPAN_BACKUP_ENDPOINT` (HTTPS), `AFTERSPAN_BACKUP_REGION`, `AFTERSPAN_BACKUP_PREFIX` (for example `afterspan-production/`), `AFTERSPAN_BACKUP_ACCESS_KEY_ID` and `AFTERSPAN_BACKUP_SECRET_ACCESS_KEY` through the server secret store. Set the separate `AFTERSPAN_BACKUP_KEY` and escrow it outside both this service and the bucket before enabling anything. The job never creates a bucket or grants access. Bucket credentials must be limited to this backup destination; normal customer or producer credentials are not used.

Optional bounds: `AFTERSPAN_BACKUP_RETAIN` defaults to seven verified copies (2–31); `AFTERSPAN_BACKUP_INTERVAL_SECONDS` defaults to 86,400 (3,600–604,800); `AFTERSPAN_BACKUP_MAX_BYTES` defaults to 1 GiB (1 MiB–10 GiB). `AFTERSPAN_BACKUP_ADDRESSING_STYLE` is `virtual` by default; use `path` only when the configured S3 service requires it. Storage must fit the live database plus one pending encrypted archive. Temporary private space must also fit the snapshot and isolated restore checks. A retained archive can contain since-erased data; choosing retention is an operator privacy decision.

The worker authenticates the local archive, uploads it under a unique key, then downloads that exact object, verifies its complete size/hash and authenticates a restore into a new private temporary path. No app, listener, mail, billing or provider worker starts against that restore. Only then does it save success and retire older known verified copies. Verification time remains separate from the conservative snapshot-start clock: reconciling an old archive does not make its recovery point fresh. An overdue recovered copy leaves a fresh capture due on the next worker tick. It never lists or deletes unrelated bucket objects. A changed destination, prefix, addressing mode or encryption key requires operator reconciliation; it does not silently repurpose the old state.

An interrupted upload retains one immutable encrypted archive and its exact object key beside the database. After a 15-minute backoff, the next worker first reads back that exact object. Only a definite not-found response permits uploading the saved bytes again; timeouts, authorization failures and corrupt readback remain failed. It does not rerun application work. Retention failure retries cleanup without taking another snapshot. State and pending archives therefore need the same persistent-volume protection as the database. After total volume loss, operators must inspect the bucket directly and reconcile the copy inventory before re-enabling automatic retention; the SQLite archive does not include these sidecar files.

`GET /api/operator/backup` accepts only the operator credential and returns bounded status, last attempt/verification clocks, last recovery-point clock, verified-copy count and an overdue flag. It cannot run a backup or return an archive, key, destination, object key or private content. Accounts, workspace keys, producer keys and remote MCP cannot use it. Normal `/healthz` stays independent of backup state. Treat a failed result or `overdue: true` as actionable; a historical succeeded result alone is not current recovery health. Poll this operator status through the deployment's monitoring. No external notification channel is configured automatically.

Local tests cover real committed-WAL recovery, corrupt and uncertain readback, authenticated-restore failure, immutable upload retry, retention isolation/retry, restart clocks, process locks, default-off behavior and operator-only access. S3 transfer concurrency is one with bounded request retries/timeouts. This is not live bucket/schedule proof. The current Railway dashboard requires Pro for its managed volume scheduler; the separate object-storage design does not enable that feature or upgrade a plan. Production activation, destination permissions, key escrow, scheduled execution, monitoring and isolated recovery remain gates.

### Existing operator snapshot proof

Six tests cover committed WAL, encryption, isolated destination permissions, overwrite refusal, wrong keys, archive corruption/truncation/extra bytes/reordering, real CLI backup/restore in separate processes, and the actual app schema. The app test restores pinned datasets, exact release receipts and workflow evidence, reproduces the same frozen outcome-report fingerprint, accepts a new event in the restored database and still denies unauthenticated reads. This uses synthetic fixtures and fake local credentials. A separate production-database snapshot and isolated restore were verified on 2 November; see the release evidence below. Scheduled off-site retention and full application recovery remain pending.

## Production snapshot evidence (2026-11-02)

Before deploying the platform, the reviewed tool read a consistent snapshot from the production SQLite volume. The encrypted archive was copied through bounded, hash-checked transport ranges to a private operator store outside that service. Its separate key and archive remain private. An isolated restore authenticated all frames and passed SQLite consistency and foreign-key checks: 131,153,920 database bytes, 105.84 seconds for the successful capture/transfer/restore operation. No application, email, billing or provider worker was started against that copy; it was never promoted.

Two earlier large streaming transfers failed and remain failed evidence, not successful backups. The bounded transfer retried only ranges and did not replay business work. This one checked recovery point is not a scheduled backup service, erasure-retention policy, a recovery-time guarantee.

## Post-release restore and restart evidence (2026-11-02)

A second snapshot captured the deployed platform after both live Tracker validations. Its 131,817,472-byte database restored in isolation; capture, bounded transfer and initial restore took 125.94 seconds. A separate process cleared all inherited service credentials, blocked external sockets, started no listener or lifecycle workers, and used in-memory API dispatch against only that restored copy. Both complete frozen production outcome reports matched, including counts, tasks, goals, evidence, cost and coverage—not merely their query fingerprints. The original account and isolated-project producer credentials still worked. One new synthetic event was accepted; repeating it was a duplicate. SQLite consistency and foreign keys passed. No outbound network attempt occurred and the copy was never promoted. Provider-connection secret decryption was not exercised at that checkpoint; the later bounded check below covers it with the original key still on the running server.

Separately, production Afterspan was explicitly redeployed at the same reviewed commit: deployment bcfd264d-71c9-44d8-8c83-6d4e10bed407 became 8f4af5cc-c0cb-43f6-849c-96534b8e72a3, with a changed runtime identity. One saved chart, one profile with traits, both validation goals, complete frozen outcome contents and retained trace contents survived. Identical OTLP re-export remained deduplicated and scoped audit keys still worked. A fresh real read-only Tracker comparison arrived after the restart, resolved its source check and was accepted by Slack. This is bounded volume/restart/delivery proof, not a lossless-collection guarantee or recovery-time SLA.

## Collection trust remains separate

The existing Python collector has a durable SQLite outbox and pipeline pulses; the Node collector has a bounded in-memory queue and cannot survive process termination. Queue/disk/network failures and requests blocked before instrumentation can leave gaps. An absent pulse is not zero drops. The branch now has a local typed per-source collection-health screen/API/CLI/MCP; see [collection health](collection-health.md). An isolated real-process test now proves the bounded outage/restart/ambiguous-receipt path below. The local benchmark, incomplete original probe, completed paced warm-connection production run and access assessment are recorded in [capacity and access](capacity-and-access.md). Sustained/cold-connection capacity, wider workloads and full operating readiness remain unverified. The release comparison therefore reports collection completeness as not established.

## Isolated crash and ambiguous-delivery proof

`python -m unittest discover -s tests -p test_recovery_processes.py` starts a real loopback Uvicorn service with a new private database, fake operator credentials and no inherited cloud/mail/billing credentials. It uses actual producer write keys and separate project-scoped read keys. No production process is stopped.

The test kills the analytics process, completes one synthetic business write while its telemetry remains queued, and crashes the producer after persistence. A fresh producer process reopens the durable outbox. A loopback proxy forwards the telemetry to the restarted service, then drops the HTTP reply after commit. The producer retains the same event for retry. The test kills/restarts analytics again before the next producer process retries, and verifies the duplicate acknowledgement clears the outbox.

| Checkpoint | Business actions | Queued events | Stored completion events |
|---|---:|---:|---:|
| Service offline; producer crashed after queue persistence | 1 | 1 | 0 |
| Service accepted event; acknowledgement discarded | 1 | 1 | 1 |
| Service restarted; same event acknowledged as duplicate | 1 | 0 | 1 |

Project-scoped API reads verify the event and chart after restart. The saved goal and credential remain usable, traits persist in SQLite, unauthenticated reads are denied and cross-project reads fail. Backend ingestion counters also survive. This proves this synthetic application's retry path and these stored records on the local filesystem. It does not prove every business application's idempotency, Node memory durability, host disk-loss recovery, production volume configuration or recovery time. The separate production restart/volume/delivery verification remains a release gate.

## Private provider storage after restore (2026-11-03)

A separately reviewed offline check on running release c6333da4079752bc414d62cc7484af012ff28306 restored a fresh, authenticated 141,688,832-byte snapshot into a private temporary directory on the same server. Both configured provider connections decrypted through the existing connection resolver using the original private-storage key. Both refused a wrong key and a missing key. No latest revoked provider connection existed in that snapshot, so live revoked-connection recovery was not exercised; the actual-app regression uses a revoked synthetic connection and checks that it remains unavailable after restore.

The source and restored connection reads were read-only. No application, listener or lifecycle worker started, socket/DNS operations were blocked and no network attempt occurred. The archive key was generated only in the child process; neither it nor the original private-storage key left that process. All temporary snapshot/archive/restore copies were removed before success. On-server capture, restore and these checks took 3.704 seconds; this excludes off-site transfer and is not a disaster-recovery time estimate. No archive was retained or promoted, so this test created no retained recovery point.

This closes only the on-server provider-decryption check while the original key is available. It does not verify recovery after losing the runtime, external key escrow, an off-site destination, a schedule, alerts or retention. The live scheduler remains disabled and its prepared destination is still staged. Those activation gates remain open. The production probe source was independently reviewed at SHA256 52ebb9f83cdca2b8522d486d5a92406628c615a15a4f214d7dc37ed9db6242d8; only aggregate verification metadata was retained.
