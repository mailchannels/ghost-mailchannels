# Contributing

Use a feature branch and pull request. Run `bash scripts/test.sh` and all four native scenarios
from README before proposing changes that affect intake, queue, transport or recovery semantics.
Never enable real provider calls in tests. The native fixture stays on an internal Docker network.
Keep the dependency hash lock and pinned container digests current through reviewed changes.

Preserve commit-before-ACK and claim-before-send, fixed HTTPS/no redirects, server-owned routing,
site isolation, no automatic replay of uncertain sends, and atomic audited operator resolution.
Changes to receipt identity, schema, lifecycle or locks require upgrade/backup/recovery review.
Document claims narrowly: native/local tests do not establish live delivery or hosted plan support.
There is no automated publishing/deployment job. Support: dev@mailchannels.com.
