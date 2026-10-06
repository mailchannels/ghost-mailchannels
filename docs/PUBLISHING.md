# Company setup and publication handoff

Unreleased development preview; no company deployment, release tag, Ghost directory listing or
live MailChannels validation. Support is dev@mailchannels.com. A named operator is still needed.
Assume no existing Ghost test publication or company forum account.

1. Establish a company-controlled Ghost test publication and forum identity. For a hosted plan,
   confirm Custom Integration/webhook entitlement; native fixtures do not establish it.
2. Assign Linux hosting, HTTPS ingress, monitoring, protected secrets/database, backup/retention
   and a named release/operator owner. Follow README and OPERATIONS.md. Recipient snapshots and
   titles/URLs are personal/editorial data. Use an isolated publication with synthetic content.
3. Use Ghost's native Custom Integration setup to register a signed post.published webhook.
   Verify the exact origin/signing-secret format; configure sender and internal recipients only
   on the server. Keep signing and stable ledger keys separate. Never use example secrets.
4. Validate company routing, Admin UI, plan, public HTTPS ingress, callback latency, rotation,
   revoke/uninstall, failure handling and missed-event/snapshot-gap reconciliation. Retain
   authorized provider evidence for any audited manual pending-receipt resolution.
5. Obtain explicit authorization for a real-provider test with a configured sender and allowed
   internal recipient. The worker sends real email and has no dry-run mode. Verify acceptance
   and delivery records;202alone is not delivery. Review the final release source/dependencies.
6. Tag a maintained release only after those gates pass. No automated deployment/publishing job
   exists. Ghost's directory is curated and closed to submissions at preparation time; recheck
   current policy before seeking inclusion. Public source or a forum post is not directory approval.
7. Prepare accurate installation, support and version details plus an actual fixture demonstration
   for any company-approved forum sharing. Obtain outreach approval before posting. No public
   forum/partner message has been sent. Verify any accepted directory entry independently.

Never claim newsletter/subscriber mail support. Production access, operating policy and directory
acceptance remain separate from source publication and local tests.

Official entry points: https://ghost.org/integrations/ · https://forum.ghost.org/ ·
https://docs.ghost.org/webhooks · https://ghost.org/pricing/
