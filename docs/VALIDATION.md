# Ghost editorial notification intake

This is the initial signed intake and durable queue component for an external `post.published`
editorial notification integration. It does not replace Ghost newsletters, which retain their native
provider requirements. An ASGI HTTP receiver is implemented. A one-shot sending worker is also implemented. **There is no deployment or published directory integration yet.** Support contact: dev@mailchannels.com. Company publication and forum account setup are pending.

`Site` is trusted server configuration: a connection ID, exact HTTPS origin, one or more webhook
signing secrets (minimum32 bytes) and a separate stable receipt HMAC key. Map requests to this
configuration server-side. Never take connection identity, sender or notification recipients from
payload fields. Signing secrets must be distinct across sites; the class cannot check other sites'
configuration. Rotation can temporarily supply old and new signing secrets. Do not rotate the
receipt key without a coordinated migration/drain, since doing so changes deduplication keys.

`Queue.accept(raw_bytes, signature_header_values, site)` verifies Ghost6.67.0's signature over the
original body bytes concatenated with the signed millisecond timestamp. It requires one strictly
formatted signature header, a five-minute freshness window and a body no larger than1MiB. It parses
JSON only after verification, rejects duplicate keys and accepts only `post.published` with a
published current post and a link on the configured origin.

The receiver in `server.py` exposes `POST /hooks/<connection-id>` and a liveness-only `GET /health`.
It streams at most the declared body length (maximum1MiB) with a0.75-second total body-read deadline,
requires JSON, rejects content/transfer encodings and duplicate signature headers, and returns202
only after `accept` commits. Uvicorn/h11 validates HTTP framing before ASGI; identical duplicate
Content-Length values may be normalized by that parser, while conflicting framing is rejected.
The application additionally rejects duplicate length headers if they reach ASGI. Bodies are not
logged or echoed. Unknown routes return404, unsupported methods405, invalid input400/401,
oversize413, unsupported media415, read timeout408 and storage/queue failures503. No path uses410,
because Ghost can remove the webhook on410 (transport-dependent in6.67.0). A disconnected client may leave a committed job if disconnect
occurs during the database transaction; Ghost replay deduplicates that job.

Install the hash-locked requirements in a Python3.13 environment. Copy `config.example.json` outside
the checkout, replace the example keys with independently generated secrets, configure the actual
publication origin and provision a private writable database directory. Run with `GHOST_CONFIG`
pointing to that private JSON file: `python server.py`. It binds127.0.0.1:8186 (PORT can change the
port), disables access logs, limits concurrency to32 and uses h11 with a16KiB incomplete-event
limit. Startup sets umask077 for newly created database files. Do not use example secrets in a site.

Place it behind a maintained HTTPS reverse proxy with header/body size and absolute connection
read deadlines, request-rate limits and appropriate trusted forwarding configuration. The app's
body deadline starts after HTTP headers; it does not protect slow header uploads at the proxy.
Keep the source port private. The request body plus SQLite lock budget is up to1.75 seconds before
other overhead; native Ghost's two-second budget still needs end-to-end validation. Native failure tests find no POST retries for503/timeouts; proxy behavior and hosted latency remain unverified. Do not point a production
Ghost webhook at this intake until native validation and a reconciliation operator are in place.

The receipt identity is site + event + post ID + canonical published_at/updated_at. Different
signature timestamps and JSON whitespace do not create another queued job. Different versions are
retained, including out-of-order arrivals, so a delayed publication notification is not silently
lost. Equal versions with changed title/link are rejected as conflicts. This version-based policy
has been tested against native edit, unpublish/republish and two collection routes; verify the
company site's own routing before deployment, since
Ghost does not supply a documented globally unique event ID here.

SQLite uses full synchronous commits and an immediate transaction with a one-second lock timeout.
A site can hold at most10,000 receipt rows by default. Duplicates still succeed at capacity; new
jobs fail closed. The minimal title/link, HMAC identifiers and attempt/operational metadata are stored, not full post HTML,
membership data, signature or secrets. Store the database/WAL in a private directory on reliable
local storage with appropriate backups. Titles/URLs may still be sensitive editorial data. Define
retention and deletion before deployment; deleting receipts makes an old event eligible again.
SQLite and local tests are not proof of a multi-host or cloud storage guarantee.

## Sending worker

`worker.py` processes at most one queued job per invocation. It commits `pending` under an immediate
SQLite transaction before touching the provider. Other workers cannot claim the same job. The sender
and up to50 distinct internal-team recipients are trusted server configuration, never webhook input.
The notification contains the stored title/link as plain text and escaped HTML and never fetches the
post URL. The subject is fixed. No subscriber list or bulk-newsletter behavior is implemented.

The transport makes one fixed-endpoint HTTPS POST, verifies certificates, disables environment
proxies and redirects, and discards the response body. Its ten-second socket timeout is per blocking
operation, not a guaranteed total wall-clock deadline. This runs outside Ghost's HTTP request.
HTTP202 records `accepted` (not delivered);4xx except408 records `rejected`;408,5xx, redirects,
unexpected success and transport failures leave `pending`. No state is automatically reset to
`queued`, including after worker restart. An exception or process crash after claim also leaves the
job pending. No durable exactly-once guarantee is claimed.

Copy the following structure into a private file outside source control and set `GHOST_WORKER_CONFIG`
to its path when running `python worker.py`:

```json
{
  "database": "/var/lib/mailchannels-ghost/queue.db",
  "site": "editorial",
  "sender": "configured-sender@example.test",
  "recipients": ["internal-team@example.test"],
  "api_key": "replace-with-company-secret"
}
```

An invocation can send real email if queued jobs and real credentials are supplied. Local tests never
run that configuration. Production/provider validation needs separate authorization. Keep receiver
signing secrets separate from worker API credentials. Changes to server-owned delivery configuration
apply to still-queued jobs, so drain/review the queue before changing recipients or sender.

For operations, monitor queued/pending/accepted/rejected counts and capacity. On any pending job,
inspect authorized provider records before deciding whether it was accepted. Do not delete a receipt
or change its state just to retry. Coordinate site revocation by disabling its webhook/receiver mapping
and stopping its worker before changing credentials. Accepted/rejected receipts remain for dedupe;
retention, provider reconciliation and deployment monitoring still require company operating ownership.

Nine worker tests use synthetic provider results, including simultaneous claims, committed state at
send time, restart suppression, escaped content and acceptance followed by failed receipt update.
Seven additional tests exercise the unchanged transport against a real loopback TLS server,
with certificate verification and the API hostname preserved. They cover empty202, untrusted CA,
redirect refusal, single requests for rejection/server errors, disconnect after request receipt and
an actual socket timeout. Test-only socket routing asserts the fixed API destination before connecting
locally; no external provider traffic occurs. Three subprocess SIGKILL scenarios stop after durable
claim, after provider202 but before receipt update, and after the accepted receipt commit. Fresh
processes leave those jobs pending/pending/accepted respectively and do not resend. This demonstrates
local process-crash behavior, not native Ghost, real-provider or power-loss guarantees.

After installing requirements, run `python -m unittest discover -s test` from this directory.
The maintained tests cover real HMAC,
rotation, timestamp/body tampering, site isolation, minimal persistence, SQLite concurrency,
restart/deduplication, capacity, conflict and out-of-order versions. Nine tests additionally run a real Uvicorn HTTP listener on loopback to check commit-before-ACK,
duplicates, malformed input, oversized declarations before upload, partial-body timeout/disconnect,
shared-secret configuration rejection and capacity/SQLite lock failures. The unit-test payload fixtures are derived from upstream serialization source. A separate native
Ghost6.67.0 Docker harness now verifies actual signed publish/republish payloads through the receiver.

Remaining: company Ghost plan/routing validation and separately authorized real-provider validation,
company hosting/account ownership, installation/health/revocation documentation and actual publication.
Ghost's directory is curated and closed to submissions; a completed integration can have a forum
handoff, but that is not official directory acceptance. No forum post has been sent.

Sources:
- https://docs.ghost.org/webhooks
- https://github.com/TryGhost/Ghost/blob/v6.67.0/ghost/core/core/server/services/webhooks/webhook-trigger.js
- https://github.com/TryGhost/Ghost/blob/v6.67.0/ghost/core/core/server/services/webhooks/serialize.js

## Native Ghost validation

From the repository root:

```sh
docker pull ghost:6.67.0@sha256:9482099b1c8700764dc3d38c293e09c16d70f8bacc14af589c47d90b4bebd017
docker build -t visibility-ghost-receiver:test -f native/Dockerfile .
python native/run.py
```

Image preparation needs network access. The harness runs Ghost and the actual receiver on a new
internal Docker network without internet egress or published ports, creates a disposable integration
key through Ghost's model, then uses the Admin API to register a signed webhook and publish, edit,
unpublish and republish a post. No provider worker runs. It removes the fixture containers, anonymous
Ghost content volume, network, captured payloads and queue afterward. It requires local Docker bind
mount support; the orchestration Python needs only the standard library.

Ghost's private-IP webhook exception is enabled only in this isolated fixture. The production
receiver needs a public HTTPS endpoint; do not copy that exception into production Ghost. Native
Admin UI setup, actual HTTPS ingress and hosted plan entitlement are not tested here. The additional
custom-route scenario below covers two collection routes.
The fixture uses SQLite/development mode rather than Ghost's production MySQL deployment.

The tested sequence produces two signed `post.published` callbacks, both202 with committed queue
rows. Editing while published and unpublishing do not produce that subscribed event in this sequence.
Republish retains `published_at` and changes `updated_at`; both notifications remain distinct.
Replaying either captured callback returns duplicate. Local receiver processing was below6ms;
this is not a hosted network/proxy latency guarantee. Two collection routing variants are tested below; other site routing requires deployment validation. Failure tests are described below. Reproduce with `python native/run.py events` from the repository root.

The original host virtual environment is Python3.12.8 (earlier evidence called it3.13).
The maintained Docker Python3.13 suite now has69tests. It emits ResourceWarnings for test-owned
SQLite connections; no failing test. Run `bash scripts/test.sh` from the repository root.

### Failed delivery and recovery limits

After building the receiver image above, run:

```sh
python native/run.py failures
python native/run.py failures-default
```

These use fixture-only routes (not shipped receiver routes) returning503, taking2.5seconds to
respond, and returning410. Two publications exercise each hook; the tests inspect Ghost's terminal
webhook receipts after each publication and count real HTTP requests. No notification is enqueued.
Both HTTP transports make one request per hook per publication for503 and timeout, despite the
webhook's configured retry.limit5. The installed Got defaults exclude POST from retryable methods.
A503 therefore signals intake failure but does not ensure Ghost will retry it. The next publication
is a different event, not recovery of the missed event.

With `security.allowWebhookInternalIPs=true`, Ghost's `@tryghost/request` wrapper records503 and
deletes the410 hook. With the default `request-external` transport, HTTP failure status is null and
the410 hook remains and receives the next publication. Its errors expose `response.statusCode`,
while WebhookTrigger reads `err.statusCode`; the alternate wrapper flattens response fields.
Timeouts record `ETIMEDOUT` in both cases. These are observed6.67.0 behaviors, not intentional
integration guarantees. Keep this receiver's no410 rule and explicitly delete webhooks on uninstall.
The default-transport fixture uses development mode's local-address exception; production DNS/TLS
and SSRF enforcement are not covered by this local test.

The queue protects only callbacks that reach it and commit. Receiver outages, capacity failures,
expired/misconfigured signatures and failed callbacks before commit can lose editorial notifications.
Monitor Ghost's webhook error field as well as status (null does not mean healthy), and alert on
receiver errors/capacity. Investigate both Ghost publication history and queue receipts after an
outage. A current-post Admin API listing cannot necessarily reconstruct all historical publish,
edit and republish versions. Do not claim lossless recovery or auto-republish posts as a retry.
Constrained audited resolution is implemented; company provider evidence, snapshot-gap reconciliation and an agreed missed-event policy remain deployment gates.

Reproduce with `python native/run.py failures` and `python native/run.py failures-default`; local observations only, no real provider traffic.

## Receipt diagnostics

`receipts.py` provides site-scoped read-only JSON counts and bounded receipt pages. It uses an
existing database and omits titles/URLs unless `--include-content` is supplied. It never sends or
changes states. See [OPERATIONS.md](../OPERATIONS.md) for invocation, pagination limits, monitoring,
uncertain/missed-event investigation, revocation and retention gates. The diagnostics tests pass
 including real WAL visibility, site isolation, pagination, redaction,
missing-file protection and sanitized CLI failures.


## Queue snapshots

`backup.py` creates a new mode0600 standalone SQLite snapshot, including committed WAL state, with
an integrity check and checksum report. It never overwrites, restores, requeues or sends. See
[OPERATIONS.md](../OPERATIONS.md#create-and-verify-a-queue-backup) for the command and an isolated restore
drill. An old snapshot can make previously sent work eligible again; workers must remain stopped
until an operator reconciles the snapshot gap. Company backup storage, secret-key backup, retention
and production recovery approval are still pending.


## Native collection routing

```sh
python native/run.py routes
```

This scenario starts Ghost6.67.0 with a temporary `routes.yaml`: posts tagged `news` use
`/news/{slug}/`, and the inverse collection uses `/archive/{slug}/`. An Admin API draft is published,
then its tag and slug change while published; it is unpublished and republished. The two signed
callbacks contain `/news/native-route/` and `/archive/moved-route/` respectively. Both commit with202
and replay as duplicates, including the earlier version after the later URL has been stored.
The published tag/slug edit itself produces no `post.published` callback in this sequence.

Both versions also have an external `canonical_url`. Ghost preserves it separately; `post.url`
continues to identify the configured publication, and only that URL enters the notification queue.
This validates these collection changes without allowing external webhook-supplied notification
links. It does not validate all routing patterns, custom frontends, actual page rendering, a Ghost(Pro)
plan or a production reverse proxy. Follow the exact-origin rule when configuring the company site.

Reproduce with `python native/run.py routes`. The ordinary publish/edit/republish
scenario also passed again after the harness change. All fixture
traffic remains on an internal Docker network with no MailChannels transport or real credentials.


## Attempt diagnostics

Claims now persist their start time and trusted sender/recipient snapshot before transport.
Returned HTTP status and observation time are recorded without storing response bodies or keys;
unknown outcomes still remain pending and never automatically retry. The additive migration preserves
old receipts with null metadata. Read [OPERATIONS.md](../OPERATIONS.md#attempt-evidence-and-schema-upgrade)
for the coordinated upgrade, privacy, backup and interpretation requirements. This does not prove provider delivery. A constrained audited pending-receipt resolution
command is documented below; broader recovery and reconciliation remain operating gates.


## Conclusive receipt resolution

`recover.py` can resolve an existing pending receipt to accepted or rejected only after an operator
has confirmed the exact provider outcome and supplied attribution/evidence reference. It commits an
audit record atomically, never sends/requeues, and excludes updated workers using Unix advisory locks.
Read [the required operating procedure](../OPERATIONS.md#resolve-a-conclusively-investigated-pending-receipt)
before use. Unknown outcomes stay pending; no production recovery or provider access is automated.


### Restored-queue quarantine

The suite now includes seven restore-quarantine tests, bringing the total to 76. A real SQLite
snapshot is taken before a mocked 202 acceptance, restored to a separate file, quarantined and
checked against the actual worker selection path: no resend occurs. Replay remains a duplicate,
unknown attempt fields remain null, and quarantine plus later operator-resolution audits survive
another backup. Tests cover bounded site-scoped batches, unchanged terminal states, repeated runs,
state/audit failure rollback, invalid input, missing files, cross-process worker locks and CLI output.
A separate test explicitly demonstrates the limitation: a post-snapshot event absent from the backup
can still enqueue after reopening intake. Company recovery must resolve that missing history first.
All provider behavior in these tests is local/mocked; no live send or deployment is established.
