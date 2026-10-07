# MailChannels editorial notifications for Ghost

**Unreleased development preview. Not deployed, listed in the Ghost integration directory,
or validated against a live MailChannels account.** Support: dev@mailchannels.com.

Receive signed Ghost `post.published` webhooks and queue a minimal title/link notification
for an internal editorial team. This external Python service does not replace Ghost newsletters
or their provider requirements. It does not send to subscribers or implement bulk campaigns.

## What works locally

- Signed, size-limited ASGI intake with site isolation, freshness checks and durable SQLite deduplication.
- A one-shot Email API worker with server-owned sender/recipients, fixed HTTPS endpoint, no redirects
  and no automatic retries. Ambiguous sends remain pending, including after process crashes.
- Read-only receipt diagnostics, private consistent snapshots, attempt metadata and an audited
  operator-only resolution command that excludes active updated workers and never requeues.
- Native Ghost 6.67.0 fixtures covering publish/edit/republish, collection routes and callback failures.

Provider acceptance is not delivery. Ghost callbacks that fail before durable intake can be lost;
local testing found no POST retries for timeout/503. Company reconciliation and operating ownership
are required before production use. See [operations](OPERATIONS.md) and [validation](docs/VALIDATION.md).

## Local setup

Use Python 3.13 and Unix/Linux with reliable local SQLite storage. Windows and remote filesystem
locking semantics are unsupported. From a clean checkout:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements.txt
python -m unittest discover -s test
```

Tests use synthetic credentials and local fixtures. The native TLS tests require the `openssl`
command. Run them with Docker networking disabled using `bash scripts/test.sh` for isolation.
Image/dependency preparation uses the internet; the tests do not call MailChannels.

For an operator-reviewed deployment, copy `config.example.json` outside the checkout, generate
independent signing and stable ledger secrets, set the exact publication origin and private database
path, then set `GHOST_CONFIG` to that file and run `python server.py`. The receiver listens only on
127.0.0.1:8186 by default. A maintained HTTPS reverse proxy with request limits and absolute deadlines
is required. `/health` indicates liveness only. Do not use the example secrets.

The worker requires a separate private `GHOST_WORKER_CONFIG` containing database/site, authorized
sender, internal recipients and API key; see [configuration details](docs/VALIDATION.md#sending-worker).
**Running `python worker.py` with real credentials and queued jobs can send real email.** This service
has no provider dry-run mode. Validate company sender/domain/account permissions before enabling it.
Never place production credentials in source, command arguments, tests or public issue reports.

## Native Ghost fixture

```sh
docker pull ghost:6.67.0@sha256:9482099b1c8700764dc3d38c293e09c16d70f8bacc14af589c47d90b4bebd017
docker build -t visibility-ghost-receiver:test -f native/Dockerfile .
python native/run.py events
python native/run.py routes
python native/run.py failures
python native/run.py failures-default
```

The harness uses disposable containers on an internal Docker network with no published ports.
It never runs the MailChannels worker, and cleans up its generated credentials/database afterward.
Plan entitlement, native Admin UI, public HTTPS ingress and company routing still need validation.

## Release and directory status

There is no automated deployment or package release. Review company hosting, secret rotation,
monitoring, backup/retention, provider evidence, missed-event/snapshot-gap recovery and a named
operator before deployment. Keep workers stopped after restoring an old snapshot until its gap is
reconciled; an old queued row may already have been sent.

Ghost's integration directory is curated and closed to submissions at preparation time. Public
source or a future forum post does not establish official directory inclusion. No forum post or
partner message has been sent. See [publisher handoff](docs/PUBLISHING.md).

MIT license. Maintained by MailChannels; support: dev@mailchannels.com.


Restoring a backup requires stopped services and snapshot-gap reconciliation. The bounded
[`quarantine.py`](quarantine.py) command prevents existing restored queued receipts from resending
and records a separate audit; it cannot reconstruct missing post-snapshot receipts. Follow the
[restore quarantine runbook](OPERATIONS.md#quarantine-restored-queued-receipts-before-gap-reconciliation)
before considering resumption. No release or production-recovery approval is implied.
