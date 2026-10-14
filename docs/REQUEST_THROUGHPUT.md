# Request throughput investigation — October 14, 2026

The requested target is 1,000 incoming links per second, not merely 1,000 waiting
tasks. This working-tree investigation is not a production capacity guarantee.
Publication was originally held until both reported downloads and the requested
throughput were established. The later explicit reference-layout task authorizes
publication of the structural work and these improvements, with unsuccessful
origin/cold-throughput findings preserved rather than reported as fixed.

## Causes found and changes

- The cached-link workflow made eight SQL calls, including redundant user/state
  writes and a successful-inspection cleanup that did nothing. The native pool had
  eight connections, one held by the coordinator. Increasing the pool alone did
  not remove the write and cache overhead.
- Admission, user registration and INSPECTING ownership now commit in one SQL
  statement. The pending history insert remains durable before origin work. Menu,
  inspected history and CHOOSING_QUALITY now commit together. The cached path uses
  three statements; the cold path adds one atomic catalog/alias statement.
- Request ownership guards prevent an older completion from changing a newer
  inspection. SQL rollback does not consume admission or create a user. Per-user
  in-process leases reject repeats but can never grant without SQL.
- The hot menu cache rebuilt every entry on each request. Ordered expiration and
  eviction remove this linear scan. Persistent metadata snapshots now explicitly
  project immutable scalar fields instead of recursively copying each quality
  twice. Signed sources, authorization and thumbnails remain excluded from SQL.
- Metadata capacity previously counted duplicate followers as separate work. It
  now counts distinct keys. Lock and worker waits are included in the 90-second
  deadline, and cancellation releases keyed locks.
- A separate active-handler cap now covers database admission and pool waits,
  before the first await. Overflow does not consume quota. Error replies are
  bounded to 32 concurrent RPCs and five seconds, with repeated warnings suppressed
  for five seconds per user/chat/code in a bounded cache. A failed error RPC cannot
  escape into unrelated handlers. Accepted menus are not suppressed.

The default native pool is 32 connections (`POSTGRES_POOL_SIZE`, range 2-64).
`METADATA_CONCURRENCY` remains eight by default, range 1-64; increasing it must be
justified by origin acceptance and available extraction resources.

## Reproducible request-arrival test

`tools/load_test_requests.py` needs an explicit **dedicated** `TEST_DATABASE_URL`.
It creates, migrates and drops only a random private schema. It exercises real
PostgreSQL, one held coordinator connection, the actual YouTube/Instagram Telegram
handler code, SQL admission, histories, durable menus and all four log writers.
Sources and Telegram responses are simulated; it sends no actual messages or
downloads media. Each menu contains eight qualities by default.

```bash
export TEST_DATABASE_URL='postgresql://TEST_USER:TEST_PASSWORD@127.0.0.1:TEST_PORT/TEST_DB'
python tools/load_test_requests.py --rate 1000 --seconds 15 --strict-rate
python tools/load_test_requests.py --rate 1000 --seconds 15 \
  --mode coalesced --origin-delay 0.05 --strict-rate
python tools/load_test_requests.py --rate 1000 --seconds 5 \
  --same-user --strict-rate
python tools/load_test_requests.py --rate 1000 --seconds 5 \
  --mode cold --origin-delay 0.05 --strict-rate
```

`cached` prewarms 100 identities. `coalesced` starts with those identities uncached;
followers must share extraction. `cold` uses a distinct identity for every input.
The open-loop sender schedules arrivals independently of completion. Reports
separate arrivals, completions, menu/history counts, origin calls, capacity
rejections, SQL admission checks, p95 response time, loop delay and log loss.
`passed` describes completion reliability; `--strict-rate` also requires a p95
response below `--response-sla` (default one second), arrival lag below 100 ms and
zero log drops/write errors. A controlled rejection fails the all-menu criterion.

## Measurements and failed attempts

Native Linux tests ran on the workstation's Ubuntu WSL, Python 3.14, with a
dedicated PostgreSQL 17 container. Logs used the Linux filesystem. Origin and
Telegram delays were simulated at 50 ms and 1 ms where specified below. This is
not the remote deployment server. All rows below had zero escaped handler
exceptions and zero dropped/error log writes.

Initial runs used 50,000 queued log records per stream and disabled stdout; the
final run uses the application default of 10,000 records per stream. All runs
retain file logging. Terminal/stdout throughput is not included in these numbers.

| Workload | Inputs | Pool / extraction workers | Menus / capacity rejections | Elapsed / p95 seconds | Result |
| --- | ---: | --- | --- | --- | --- |
| Committed baseline `3ed52af`, cached, eight qualities | 15,000 at 1,000/s | 8 / 8 | 15,000 / 0 | 49.960 / 42.096 | Failed response target |
| Optimized cached path, before handler cap | 15,000 at 1,000/s | 32 / 8 | 15,000 / 0 | 15.028 / 0.044 | Passed simulated request target |
| Optimized cached path with bounded handlers | 15,000 at 1,000/s | 32 / 8 | 15,000 / 0 | 15.029 / 0.146 | Passed simulated request target |
| Final cached path, explicit snapshots, default 10,000-record log queues | 15,000 at 1,000/s | 32 / 8 | 15,000 / 0 | 15.029 / 0.055 | Passed; peak 125 handlers, maximum loop delay 7.8 ms |
| Initially cold 100 identities, coalesced | 15,000 at 1,000/s | 32 / 8 | 15,000 / 0 | 15.036 / 0.048 | Passed; exactly 100 origin inspections |
| Same user repeated | 5,000 at 1,000/s | 32 / 8 | 1 / 0 | 5.002 / 0.00022 | Correct one-minute admission; one SQL check, one menu and one warning |
| All distinct, 50 ms simulated origin | 5,000 at 1,000/s | 32 / 8 | 1,776 / 3,224 | 11.410 / 6.383 | Failed all-menu/response target; peak handlers bounded at 1,000 |
| All distinct; increased workers only | 15,000 at 1,000/s | 32 / 64 | 12,575 / 2,425 | 15.878 / 2.167 | Failed; increasing extraction workers alone insufficient |
| All distinct; explicit metadata snapshots | 15,000 at 1,000/s | 32 / 64 | 13,645 / 1,355 | 15.832 / 1.956 | Improved, but failed target |
| All distinct; snapshots and larger SQL pool | 15,000 at 1,000/s | 64 / 64 | 14,673 / 327 | 15.738 / 1.434 | Improved, but failed target |

An earlier Windows/Python 3.13 investigation used one quality per menu. Its
5,000-request cached baseline took 22.452 seconds (p95 19.779); pool-size 32 alone
took 13.120 (p95 9.937). Cache fixes alone remained 13.076 (p95 10.004), compound
admission/menu writes reached 8.204 (p95 4.638), and atomic history completion
reached 5.849 (p95 1.344). A longer 15,000-request Windows run took 18.668 seconds
(p95 5.607), failing the steady response target. Windows and Linux rows cannot be
compared as identical environments; changing platform did not fix origin access.

A cold 1,500-request CPU profile at 500/s identified 3,000 recursive dataclass
copies, including two metadata snapshots per inspection. Explicit field projection
removed that redundant work and improved the subsequent cold run, but did not
establish universal 1,000/s throughput. Profiling timings are not benchmark results.

## Remaining constraints

The cached/coalesced results establish only the request-processing path. Distinct
cold requests at 1,000/s did **not** meet the all-menu target on this test machine,
including the tuned run. More buffering cannot make origin or database work
faster; increasing limits without throughput evidence only increases latency and
resource use. The tests demonstrate bounded overload rather than silently claiming
success after rejection. Sustained cold throughput needs additional capacity and
measurement against accepted origins and actual deployment resources.

An extraction taking `L` seconds requires approximately `1000 * L` simultaneously
executing extractions to finish 1,000 independent requests/s, before storage and
Telegram costs. The default eight workers cannot achieve that when origins are
slow. Real YouTube/Instagram account/network limits and Telegram outbound limits
remain separate from the bot's incoming rate. No paid broadcasting is enabled.
See [yt-dlp's YouTube access/rate guidance](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#youtube)
and [Telegram's outbound limits](https://core.telegram.org/bots/faq#how-can-i-message-all-of-my-bot-39s-subscribers-at-once).

The exact reported reel was previously verified to EOF in eight qualities on an
independent runner. The exact YouTube input still returned `youtube_login_required`
there. New workstation WSL HTTP probes for both inputs failed before receiving an
HTTP response: `URLError` wrapping `OSError`, errno 101 (`ENETUNREACH`). No TLS
certificate error was reported. Authorized cookies/proxies/helper access remain
unconfigured locally. A missing route, an origin login challenge and a saturated
local queue are different failures; concurrency tuning does not remove either of
the first two. No production server was accessed or updated.

Read [the access report](YOUTUBE_INSTAGRAM_ACCESS.md) and
[the knowledge base](KNOWLEDGE_BASE.md) before another live attempt. Retry failed
access methods only with new code, a working authorized route/session or changed
origin conditions. Preserve every failed load run alongside successful evidence.
