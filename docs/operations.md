# Operating evidence and recovery

This outcome-platform work is unreleased. A healthy HTTP endpoint and a persistent disk do not prove complete collection or recoverability.

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

Six tests cover committed WAL, encryption, isolated destination permissions, overwrite refusal, wrong keys, archive corruption/truncation/extra bytes/reordering, real CLI backup/restore in separate processes, and the actual app schema. The app test restores pinned datasets, exact release receipts and workflow evidence, reproduces the same frozen outcome-report fingerprint, accepts a new event in the restored database and still denies unauthenticated reads. This uses synthetic fixtures and fake local credentials. Production snapshots, off-site retention and real recovery timings remain pending.

## Collection trust remains separate

The existing Python collector has a durable SQLite outbox and pipeline pulses; the Node collector has a bounded in-memory queue and cannot survive process termination. Queue/disk/network failures and requests blocked before instrumentation can leave gaps. An absent pulse is not zero drops. The branch now has a local typed per-source collection-health screen/API/CLI/MCP; see [collection health](collection-health.md). Measured capacity, real fault/restart proof and production verification are still required. The release comparison therefore reports collection completeness as not established.
