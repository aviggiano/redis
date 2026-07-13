# Native bitmap operations and rollout

Redis can store a logical bitmap as either a legacy string or the distinct
native `bitmap` type backed by compressed Roaring containers. `GETBIT`,
`SETBIT`, `BITCOUNT`, `BITPOS`, `BITOP`, `BITFIELD`, and `BITFIELD_RO` accept
both representations. `TYPE` reports `bitmap` and `OBJECT ENCODING` reports
`bitmap-roaring` for native values.

## Conversion and rollback

Use `BITMAP CONVERT key NATIVE` to opt in one existing string bitmap and
`BITMAP CONVERT key STRING` to materialize it back to a string. Both operations
are synchronous O(N) operations, preserve the expiration and all attached
module key metadata, invalidate `WATCH`, and propagate their deterministic
result as `RESTORE` rather than asking replicas to repeat a local type choice.
They emit `type_changed` followed by the bitmap-class `convert` event on the
server executing the command. Conversion to the current representation is a
no-op.

The command is marked `DENYOOM`. Native-to-string rollback is bounded by the
fixed native v1 logical limit and is deliberately independent of the current
`proto-max-bulk-len`; lowering the protocol limit after creating or restoring a
native key therefore does not make rollback impossible. Plan conversion of
large dense keys during a maintenance window and measure main-thread latency
and peak RSS first.

## Compatibility boundaries

Native bitmap is not a string subtype:

- Generic string commands such as `GET`, `STRLEN`, `GETRANGE`, `APPEND`, and
  `SETRANGE` return `WRONGTYPE`.
- `MGET` returns a null element for a native bitmap, as it does for every
  non-string value.
- `SORT` `BY` or `GET` pattern resolution treats a native bitmap like a
  non-string/missing pattern value. `GET #` remains unaffected.
- Lua and Functions see the same command behavior: bitmap commands work and
  generic string commands fail their normal type check.
- Modules observe `REDISMODULE_KEYTYPE_BITMAP`; string DMA and string-pointer
  APIs are unavailable. Subscribe to `REDISMODULE_NOTIFY_BITMAP` for `convert`
  and to `REDISMODULE_NOTIFY_TYPE_CHANGED` for the preceding type transition.
- Cluster hashing is unchanged. `DUMP`/`RESTORE`, `MIGRATE`, slot migration,
  replication, and AOF require every receiving server to understand the native
  RDB type.

Native bitmap v1 deliberately uses the 32-bit Roaring key space, with a 512 MiB
logical-byte limit and maximum bit offset 4,294,967,295. It does not support
redis-roaring `R64` offsets above that boundary. Such module values must be
range-checked or partitioned before migration.

Native DUMP/RDB data uses RDB type 30 and a versioned compact payload. Both
incremental AOF transitions and live replication use metadata-preserving
RESTORE effects, while an AOF rewrite emits the same native persisted type.
Older binaries cannot load or relay this representation.

## Safe rollout

1. Upgrade every replica and every possible cluster migration target before
   creating native values. Keep `bitmap-default-native no` during the rolling
   upgrade.
2. Benchmark representative sparse, dense, and mixed data. Include first
   conversion, repeated writes, all BITOP variants, DUMP/RESTORE, RDB save,
   AOF rewrite, full synchronization, command p99 latency, and peak RSS.
3. Convert selected keys with `BITMAP CONVERT key NATIVE`, verify application
   and module behavior, then expand gradually. Enabling
   `bitmap-default-native yes` is a separate global policy decision: new
   bitmap-command keys become native, writes convert existing strings, and
   all-string BITOP destinations become native.
4. During mixed-version operation, do not migrate native keys to old nodes and
   do not attach old replicas. Local configuration does not affect replayed
   transitions, but old binaries cannot load the new persisted type.

## Downgrade

Before starting an older binary, stop native creation. In every logical
database (use `SELECT` where applicable) and on every cluster primary, iterate
`SCAN 0 TYPE bitmap` to completion and run `BITMAP CONVERT key STRING` on each
returned key. Repeat the scan until it returns none, then perform a fresh RDB
save or AOF rewrite. Account for the dense string size in `maxmemory`, disk,
replication, and fork copy-on-write budgets. Keep the newer binary available
until the rewritten persistence files have loaded successfully on a staging
instance.

`tools/bitmap-bench.py` provides a small reproducible smoke benchmark for
conversion, sparse and dense access, all-string native BITOP, and large mixed
BITOP. Production acceptance should also use workload-specific latency and
memory telemetry.
