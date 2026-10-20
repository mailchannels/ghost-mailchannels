# Ghost editorial notification operations

This candidate has no company deployment or public release yet. Support contact is
dev@mailchannels.com; a named operator, hosting, monitoring and missed-event policy still need
assignment. These procedures describe the implemented local queue and its limits.

## Inspect receipts

Run locally as the service operator, with access to the private database directory:

```sh
python receipts.py --database /var/lib/mailchannels-ghost/queue.db --site editorial
```

The command opens an existing SQLite database with `mode=ro` and `query_only`, reads one transaction
snapshot, and closes it. It does not load API credentials, call Ghost/MailChannels, instantiate a
queue, create a missing database, send email, or change receipt state. SQLite may use its ordinary
WAL/shared-memory files; do not copy only the main database file while it is live.

JSON includes all four state counts, total receipts, oldest intake time per state, and a bounded page
of receipt IDs, state and intake time. Timestamps are Unix seconds. `oldest_intake_at_by_state` is
not the time a worker started or finished. Pending can mean an active request or an unknown outcome;
it is not proof of failure. Accepted means provider acceptance, not delivery. An unknown site yields
zero rows; this does not verify the configured connection exists or is healthy.

For the next page, pass the exact returned `next_cursor` with `--after`. `--limit` accepts1–500
(default50). IDs sort lexically, not chronologically. Every invocation is a fresh snapshot; a full
walk is not a frozen export, and new rows sorting before a cursor may not appear in later pages.
Counts always describe all receipts for that site, not just the page.

Use `--include-content` only when needed to compare stored titles, URLs and sender/recipient snapshots
with publication history and authorized provider records.
Treat that output as editorial data; do not paste it into public tickets or general logs. API keys,
webhook signatures, raw post HTML and member data are not stored by this queue. A receipt ID is an
HMAC identifier, not the original Ghost post ID.

## Monitor and investigate

1. Check receiver availability and reverse-proxy errors, worker exits, receipt counts, and Ghost's
   webhook error field. `/health` reports receiver liveness only. Ghost6.67.0 may record a null HTTP
   status for actual HTTP failures; null does not mean success.
2. Compare receipt totals with the configured capacity (the current receiver uses10,000 per site).
   All states consume capacity. Alert before exhaustion; capacity failures may lose notifications
   because native503 callbacks were not retried in testing. Automatic retention is not implemented.
3. If queued work grows, check the scheduler/worker, delivery configuration and filesystem access.
   A queued receipt has not yet been claimed by this worker. Do not start a second unrelated sending
   implementation against the same data.
4. For pending receipts, first determine whether a worker is still running. After it stops, compare
   available authorized provider records, service logs and the stored content/time. The current queue
   retains attempt timestamps and routing snapshots for new claims, plus an observed HTTP status
   when available. It still has no provider request ID, so matching can remain ambiguous. Keep uncertain receipts pending; do not delete or requeue them.
5. For rejected receipts, inspect the service/API configuration and known provider error records.
   The worker records the observed HTTP status, but no provider response body or detailed error code. Fixing credentials
   does not automatically retry rejected work. Use the audited pending-resolution command below only after conclusive investigation;
   rejected receipts are never automatically retried.

A callback lost before intake has no receipt. Compare Ghost publication history with the queue after
an outage, but do not treat current-post API results as a complete event history. Published edits and
republish transitions can overwrite timestamps or content. Do not automatically republish a post,
forge a callback, or resend an uncertain message to make the counts match. Define the company's
acceptable missed-notification policy and recovery process before deploying.

## Pause, revoke and uninstall

Stop the worker scheduler and let an in-flight worker finish or retain its pending receipt after a
forced stop. Disable or explicitly delete the Ghost webhook, remove the matching trusted receiver
configuration, and stop the service before changing its credentials. Do not use an HTTP410 response
as the uninstall mechanism: tested Ghost6.67.0 transports disagree about deletion. Verify no further
callbacks arrive. Revoke the site's signing secret and sender API credential where appropriate.

Preserve the stable ledger HMAC key with protected backups. Changing it produces different receipt
identities and may defeat deduplication. A backup/restore must include consistent committed SQLite
state; restoring an older queue can turn previously sent jobs back into eligible work. Keep workers
stopped after restore until the snapshot gap has been reconciled with operator/provider records.
The snapshot and isolated restore drill below are tested locally. Company backup storage, recovery
approval and snapshot-gap reconciliation remain deployment gates.

## Retention

No automated purge is provided. Deleting accepted/rejected receipts permits a replay to enqueue
again; deleting pending receipts removes an unresolved acceptance record. Do not purge simply to
free capacity. Establish a retention window, revocation/replay policy, protected backup lifecycle and
auditable deletion process before production. The five-minute signature tolerance alone is not a
business-event retention guarantee, since a newly signed callback can describe an older event.


## Create and verify a queue backup

Use an existing private directory on a reliable local filesystem, owned by the service operator.
The source directory must also be trusted; this is not a tool for processing attacker-controlled
SQLite files. Choose a new destination name for each run:

```sh
python backup.py --database /var/lib/mailchannels-ghost/queue.db \
  --output /private/backups/ghost-2026-10-06T080000Z.db
```

The command opens only an existing source in read-only mode and uses SQLite's online backup API.
Committed WAL changes are included; uncommitted changes are excluded. It checks the resulting
schema/state values and SQLite integrity, switches the copy to DELETE journal mode, and closes it
before publishing a standalone file. It syncs the file and parent directory and refuses to overwrite
an existing destination, including a symlink or a concurrently created file. New snapshot files are
mode0600. This does not encrypt them: protect remote copies and storage separately.

The output contains a SHA256 checksum, byte size, state counts and completion time, never titles,
URLs or credentials. Save that report beside the protected backup. All sites in the database are
included, and the database itself contains editorial titles/URLs. Completion time is not an exact
publication-event boundary. `--timeout` defaults to30 seconds (maximum300); progress callbacks abort
backup copying if it exceeds this budget. It is not a hard deadline for every filesystem operation.
A busy system may require a quieter backup window. A failed run returns nonzero; investigate before
using any resulting file. In particular, a directory-sync failure can occur after a complete snapshot
was published. Never replace it blindly with a retry.

Ordinary failures clean up temporary files. Abrupt process/power loss can leave mode0600
`.ghost-backup-*` files in the destination directory. These are not completed snapshots. Once no backup
process is active, the operator may remove those temporary files. Do not treat filename existence,
a checksum alone or these local tests as proof of remote retention or power-loss durability.

Back up the stable ledger HMAC key and matching site configuration separately in the company's secret
store, with a version reference in the protected backup record. The snapshot command does not read,
export or validate keys, signing secrets or provider credentials. A queue copy without its matching
ledger key cannot preserve callback deduplication.

## Isolated restore drill and production recovery gate

1. Stop the receiver, worker scheduler and all in-flight workers before a production recovery. Retain
   the damaged/current database and its WAL/SHM for investigation; do not copy over a live database.
2. Verify the stored snapshot checksum against the separately saved protected report. Restore the
   standalone snapshot to a **new private directory**. Do not mix it with old WAL/SHM files. For a drill,
   use a machine/container with no external network and no provider credentials.
3. Run `python receipts.py --database /private/restore/queue.db --site editorial` against the copy.
   Compare counts and, in a controlled environment, the needed receipt details to the backup record.
   Do not invoke `worker.py` as a validation step: it can send queued notifications.
4. Quarantine restored queued receipts using the bounded command below, then reconcile the snapshot
   gap using retained current receipts and authorized provider/operator records.
   A row that was queued at backup time may have been accepted after the snapshot. A later event may
   be absent entirely. An intact restored database does not resolve either case. Keep workers stopped
   whenever the outcome is uncertain. No automated merge, requeue or production restore is provided.
5. Only a designated operator's documented recovery decision may select the production queue/config
   and resume service. Confirm matching ledger-key identity and trusted site/delivery configuration,
   record unresolved or missed events, and monitor the first callbacks/worker invocations.

The automated test copies a live WAL database while another connection has an uncommitted change,
restores only the completed snapshot in a fresh directory, checks database integrity and all four
states, then proves claim selection excludes pending/accepted/rejected work. A separate test
explicitly demonstrates that an older snapshot reintroduces queued work after the current queue has
claimed it. No transport is invoked. Tests also cover existing/racing destinations, missing sources,
invalid states/schema, timeout cleanup, permissions, checksum and sanitized CLI output.

## Attempt evidence and schema upgrade

Each worker claim now atomically records `attempt_started_at`, `attempt_sender` and
`attempt_recipients` with the transition to pending, before it invokes the transport.
The addresses come from that invocation's trusted delivery configuration, not webhook content.
They remain unchanged if configuration changes later. Treat the database and backups as containing
recipient personal data. Default diagnostics omit routing addresses; `--include-content` includes them.

`response_observed_at` and `http_status` record a returned status. Terminal state and these fields
commit together; ambiguous statuses such as503 leave the receipt pending. A missing response, a crash,
or failure to commit the result leaves these fields null. Null does not prove that the provider did
not respond or accept the request. Unix timestamps are wall-clock seconds, not monotonic timing or
proof of delivery. No provider request ID, API key, exception text or response body is retained.

Opening a queue performs an additive, transactional five-column migration under a write lock.
Stop receiver/workers, take a protected backup, and upgrade all processes together. Existing receipt
states, identities and content remain intact; legacy attempts get null metadata rather than invented
evidence. Read-only diagnostics can still inspect the old seven-column schema without migrating it.
A backup includes the new columns; restore must retain this evidence with the corresponding queue.
Do not run older workers after upgrading: they can claim/send without recording these fields.

Attempt evidence alone does not resolve an outcome. The constrained resolution command below is
implemented; provider reconciliation, snapshot-gap recovery, automatic retention and company
deployment remain operating gates. Do not manually reset jobs to queued.

## Resolve a conclusively investigated pending receipt

This local operator command performs no network calls and never sends, deletes, retries or requeues.
Use it only when authorized provider records conclusively identify this exact attempt as accepted or
rejected. Accepted does not mean delivered. Missing provider records, elapsed time, a matching subject,
or a timeout alone are not conclusive rejection evidence. Leave uncertain receipts pending.

1. Stop the worker scheduler and let every in-flight worker finish. All worker processes must run the
   updated locking implementation. Never run an older worker or another sending implementation against
   this queue. Receiver intake may continue, but stop all processes for upgrades or restore operations.
2. Inspect the receipt with `receipts.py --include-content` only in an authorized private environment.
   Reconcile site, time, sender and recipients with independently retained provider/operator records.
   Have the designated operator document the decision in the company's protected evidence system.
3. Use a stable operator identifier and a non-secret evidence reference, not copied provider logs,
   API keys, recipient addresses or message content. The confirmation flag attests that investigation
   has already established the selected provider outcome; this tool cannot verify that evidence.

```sh
python recover.py --database /var/lib/mailchannels-ghost/queue.db --site editorial \
  --receipt 'REPLACE_WITH_64_CHARACTER_RECEIPT_ID' --outcome accepted \
  --actor operator-id --evidence-reference CASE-123 --confirm-provider-evidence
```

Only an existing pending receipt can become accepted or rejected. Its state change and one resolution
record commit in the same FULL-synchronous SQLite transaction. The audit retains the operator,
evidence reference, selected outcome and wall-clock resolution time. It preserves original attempt
metadata and never fills unknown HTTP status/response time with guessed values. The command refuses
repeat resolution and all queued/accepted/rejected receipts. There is no undo or retry action.
After interruption, inspect the receipt before repeating: a commit may have completed before output.

`receipts.py` exposes an `operator_resolution` entry on manually resolved receipts. By default it
includes only audit ID, time and outcome; `--include-content` also shows operator and evidence reference.
Backups preserve resolution records. This is an operational audit, not tamper-proof storage: trusted
administrators with database access can alter it. Retain the external evidence separately.

Updated workers hold shared Unix advisory locks through claim, transport and receipt finalization;
recovery requires a nonblocking exclusive lock. If either conflicts, it fails rather than waiting or
sending. The private `<canonical-database-path>.workers.lock` file persists intentionally. Never delete,
replace or rename it while any process may hold it, and never use hard-linked database aliases.
Use the same canonical queue path and a reliable local filesystem for every process. Locks do not
protect older/noncooperating programs, another host with an independent copy, or remote filesystems
with unverified locking semantics. This implementation targets Unix/Linux; Windows is unsupported.

Resolving a receipt is not snapshot-gap reconciliation or missed-event recovery, and cannot establish
whether an older restored queued row was already sent. Company evidence access, operator ownership,
retention, deployment and real-provider acceptance remain required before production operation.

## Quarantine restored queued receipts before gap reconciliation

On an isolated restored copy, a row marked queued may already have been sent after the snapshot.
With **receiver, schedulers and all workers stopped**, use the following command for each known site
in the database. Retain the original snapshot unchanged and its checksum in protected storage.

```sh
python quarantine.py --database /private/restore/queue.db --site editorial \
  --actor operator-id --evidence-reference RESTORE-123 \
  --limit 100 --confirm-services-stopped
```

This command moves at most 100 queued receipts (configurable 1–500) to pending in one transaction,
with a per-receipt restore audit. Repeat until `remaining_queued` is zero for every site being
restored; `queued_by_site` counts queued receipts across every site in the database, so only an
empty object shows that no site was missed. It preserves original content, identity and attempt
metadata; it does not invent an attempt or provider outcome. Pending here means the snapshot
outcome needs investigation, not that this process tried to send. Existing pending/accepted/rejected receipts and other sites are
untouched. Repeating a finished batch changes no rows. An unknown or empty site is refused so a
mistyped site does not look like completed recovery; verify genuinely empty sites independently.

The command takes the same exclusive advisory lock as manual resolution, so cooperating active
workers block it. It also uses a SQLite write transaction for atomic state/audit changes. This lock
does not stop receiver intake and is released between batches. The confirmation flag is an operator
attestation, not automatic proof that services stopped. Keep all services stopped throughout the
entire restore and reconciliation procedure, including between invocations. Never use an older
worker, another copy of the database or a hard-linked alias to bypass these constraints.

Read-only diagnostics expose `restore_quarantine` with audit ID/time. Actor and evidence reference
are shown only with `--include-content`. Backups preserve this audit. After conclusive provider
investigation, `recover.py` may resolve the pending outcome and adds a separate resolution audit;
it never resends. No automatic unquarantine or requeue operation exists. If evidence remains
inconclusive, keep the receipt pending and apply the company's missed-notification policy.

**Zero remaining queued rows is not permission to resume.** Events first received after the backup
are absent from the copy; a replay can create a new queued receipt even after quarantine. Independently
recover/reconcile the missing receipt history and retained provider evidence before reopening intake
or workers. Current Ghost post snapshots are not a complete event history. The tool does not merge
ledgers or make an older snapshot current. Record an explicit company recovery decision for unresolved
history. Quarantine protects only existing restored rows; it does not establish lossless recovery.

Audit failure rolls back the complete batch. After interruption or nonzero exit, inspect state/audits
before repeating: a commit may have completed before output. Trusted administrators can modify these
SQLite records; retain the external evidence separately. This is not tamper-proof audit storage.
