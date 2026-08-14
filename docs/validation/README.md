# Runtime Validation Records

Validation records are dated evidence snapshots. A later record can extend production
coverage without changing what an earlier observation proved at its own date.

## Current follow-up

- [2026-08-14 WeChat private, group, reply, and media validation](2026-08-14-wechat-private-group-media-runtime.md)
  records authorized private and explicitly mentioned group text paths, thread reuse,
  application-process restart persistence, reply handling, guarded incident recovery, and
  the current inbound image boundary.

## Baseline history

- [2026-08-13 WeChat runtime validation](2026-08-13-wechat-runtime.md) records the
  five-service deployment, `latest` bootstrap, Checkpoint and Message Store behavior, and
  fail-closed rejection before production authorization and routing were configured.

The 2026-08-13 record must not be used as the current authorization-dependent validation
status. Neither record proves the complete AI media path, host-restart recovery, or
automatic `uncertain` recovery capabilities explicitly listed as remaining in the
2026-08-14 record.
