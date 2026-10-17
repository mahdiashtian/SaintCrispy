# YouTube batch diagnosis — October 17, 2026

The reported `youtube_public_unavailable` message is a public-resolver deadline,
not evidence that YouTube itself is down. The path includes YouTube watch metadata,
exact-ID search, a resolver request key, conversion calls and media header probes.
Measure each dependency separately before attributing a failure to the origin.

## Defects corrected

- HTTPX's 12-second setting did not bound the Curl-backed session, whose transport
  supplies a separate 30-second timeout. An explicit async deadline now bounds both
  response headers and body. Safe logs identify operation, phase, status, elapsed
  time and transport/DNS/TLS failure codes without exception text or signed URLs.
- Thirty-two striped public locks serialized unrelated video IDs after hash
  collisions. Identity-specific, reference-counted locks now coalesce only the
  same video, clean up on cancellation and bound distinct pending identities at
  1024. A bounded lock wait reports `youtube_public_busy`, separately from network
  timeouts. A waiting resolver cannot invalidate its active owner's catalog.
- Failed discoveries now coalesce too: a bounded 15-second negative cache stores
  only safe error code/copy, preventing a thousand duplicate followers from each
  retrying an already failed upstream call. Expiry allows recovery; cancellation
  is never cached and successful selected-source resolution clears the failure.
  A proven native guest extraction timeout also has a bounded, per-video 15-second recovery window,
  preventing failed public followers from spawning another stalled native worker
  each time. Explicit player/helper/account configurations retain their recovery.
- Waiting for every quality discarded already verified choices when one
  conversion/probe stalled until the discovery deadline. Completed choices now
  survive; unfinished owned tasks are cancelled and awaited. Such partial catalogs
  keep the existing short 15-second TTL and do not masquerade as full discoveries.
- An exact playable, complete H264/audio origin catalog capped at 360p needs one
  video conversion plus MP3, rather than three equivalent video conversions plus
  MP3. A catalog capped at 720p needs two video requests. Unknown, incomplete or
  inconsistent catalogs still receive ordinary full discovery. Selected bytes and
  complete-file duration/audio integrity remain mandatory.

Conversion concurrency stays at two. No library replacement, proxy rotation,
account access, HTTP 429 retry storm or removal of integrity validation is involved.

## Reproduce complete downloads

```bash
python tools/benchmark_youtube.py
python tools/benchmark_youtube.py --url-file public-video-urls.txt --cold \
  --stages 20 100 --concurrency 4 --output .runtime/youtube-cold-batch.json
```

The first stage is sequential. The second has four workers by default. Each case
calls the real provider's inspect and resolve contracts and consumes the selected
file to EOF. Video/audio alternate where offered. MP4 passes the production
stream validator; MP3 passes FFmpeg and full-duration validation. Report hashes,
byte counts and completion flags describe consumed media, not just HTTP 200 or a
small prefix. There are no Telegram sends or uploads in this benchmark.

The default input is one short public video, reused across samples; it does not
claim distinct files. Supply one public URL per line for diversity. `--cold` clears
menu/source snapshots before samples but retains origin metadata caches, shared
in-flight work and rate cooldowns. Repeated concurrent identities can still
coalesce. Report `unique_inputs` makes this scope explicit. Existing Telegram file
references are not used: even warm-menu samples download the complete source again.

The default cap is 256 MiB and 180 seconds per case. A limit, empty stream or
incomplete file is a failure. HTTP 429 stops both remaining stages; three
consecutive failures also stop queued cases. `not_attempted` is never counted as
success. Reports contain no cookie/proxy values, request keys or signed sources.

## Diagnosis and throughput boundaries

`youtube_public_response` records header arrival. Compare it with
`youtube_public_operation_complete` to distinguish slow bodies from slow headers.
`youtube_public_operation_failed` includes the operation (`request_key`, `convert`,
`metadata`), phase and a safe failure classification. `youtube_public_queue` measures
lock/conversion-slot waiting. The benchmark separately reports its own worker queue.
DNS, TCP, TLS, HTTP 403/429 and a local queue delay are different failure conditions.
Successful public responses additionally log DNS, TCP, TLS and first-byte wait
times from Curl's request metrics. Reused connections can have zero setup times;
first-byte waiting still includes network round trips and upstream processing.

On this workstation, a fresh reported-video inspection failed after 11.019 seconds:
watch metadata reached its three-second bound and exact-ID search reached its
three-second bound, while the resolver key completed in 1.883 seconds. Separate
short-video connectivity probes found YouTube watch headers unavailable after
eight seconds, but search HTTP 200 in 1.307 seconds and resolver-key HTTP 200 in
1.996 seconds. This establishes partial connectivity here, not a global YouTube
outage or the production server's network state. Neither a key nor search HTTP 200
proves that a media file can be consumed.

Connectivity varied during the investigation. A later final-code short-video
check read exact playable origin metadata in 1.212 seconds and requested only two
conversions for its capped source resolution. The fresh menu took 10.371 seconds;
complete video took another 1.049 seconds to resolve and 2.456 seconds to consume.
Two warm-menu video/audio downloads then completed in 2.318/2.186 seconds each.
Curl reported key DNS/TCP/TLS at 0.012/0.113/0.124 seconds, but conversion first-byte
waits at 5.838/7.713 seconds on reused connections with zero setup time. This isolates
substantial waiting beyond connection setup; it does not distinguish every part
of the remote service's processing from network round trips.

The initial 20-case diverse run also recorded HTTP **502** from a converter POST.
Another video's four converter requests each reached the 12-second header deadline
despite a successful key request on the same host. Do not classify these as a
global YouTube outage, or invent HTTP 429 when no such response was received.

Exact-ID search for `FUKmyRLOlAA` returned no usable matching metadata even with a
separate 12-second budget (response completed after 3.955 seconds). Later, the
origin-first path recovered this same video and consumed complete video/audio:
189,845/160,621 bytes, durations 10.000/10.032 seconds. A missing search hit must
not permanently blacklist a video. A fresh anonymous native-only check after
origin connectivity recovered still hit its 12-second extraction deadline; it
did not establish an alternative accepted download route.
The workstation had Node **22.23.2** and the pinned yt-dlp nightly/EJS/Curl packages;
that native failure was not caused by a missing JavaScript executable.

## Workstation batch evidence

| Workload | Distinct inputs | Completed downloads | Elapsed | Interpretation |
| --- | ---: | ---: | ---: | --- |
| Baseline, forced cold menu/source snapshots, sequential | 1 short video | 14/20 | 567.260 s | Six inspection failures; both video and audio consumed completely on successful cases |
| Updated discovery, sequential public timer videos | 20 | 16/20 | 465.496 s | Two metadata and two conversion failures; 4,885,504 complete bytes |
| Updated discovery, repeated mixed set, four workers | 20 | 73/100 | 823.729 s | All 100 attempted; 13 inspection and 14 selected-link failures; 47,850,016 complete bytes |

The workloads differ; these rows are not a controlled speedup comparison. Baseline
100-case work was interrupted after the already-failing 20-case baseline, rather
than repeating the known failure unchanged. The updated 100-case stage cycles the
same 20 diverse inputs with four workers; metadata caches/coalescing are retained
and every successful selected file is still downloaded completely. Nineteen of the
20 inputs completed at least one file in this stage. There were no failed partial
file transfers, skipped cases or observed HTTP 429 responses. All 27 failures
occurred before file consumption, while obtaining metadata or refreshing a selected
URL. Successful file consumption averaged 1.942 seconds; that does not include
inspection, resolution or the worker queue. Stage p95 service time was 42.023 seconds
and p95 queue wait was 745.752 seconds. More waiting capacity is not more throughput.

The batch ran while fixes were being developed. Subsequent final-code Curl timing
and per-video failure/native recovery guards were verified separately by the live
smoke checks above and simulated 1000-follower regressions; the batch numbers are
not a controlled before/after measurement of those final guards. The 15-second
negative cache cannot solve upstream failure for new distinct inputs or failures
recurring after its expiry. The live results do **not** establish reliable 1000
distinct cold YouTube downloads per minute, or complete resolution of external
resolver availability.

Independent internal admission testing used dedicated real PostgreSQL and a
simulated 50 ms origin plus simulated Telegram. It processed **1020/1020** distinct
requests at **34 arrivals/second for 30 seconds**, with 1020 durable menus/history
views, zero exceptions/capacity rejections/log drops, and **94 ms p95** completion.
This is a request-processing test, not 1020 actual YouTube downloads or a sustained
production bandwidth result. The random test schema and disposable database/cache
containers were removed after validation.

The complete regression suite passed 2577 tests with real dedicated PostgreSQL and
Redis. The final affected suite passed **230 tests**, additionally covering transport
metrics, same-video native backoff, cancellation, provider boundaries and
transfer-source handling; Ruff lint/format and whitespace checks passed. Tests
with simulated origins are kept separate from the live file results above.

The existing SQL file-reference cache avoids repeated origin downloads, and
metadata/in-flight coalescing avoids repeated extraction. These paths can serve
many repeated requests with little upstream work. A thousand **different cold**
downloads per minute is a different target: 16.7 new videos per second. Four
conversions per video would require about 66.7 conversion calls/second before
metadata and probes. At 10 MiB per file, origin download alone needs approximately
1.40 Gbit/s, with similar host upload bandwidth if Telegram cannot fetch the URL.

The maintained [yt-dlp extractor guidance](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#youtube)
documents guest/session request limits and recommends pacing downloads. Neither a
different Python library nor increasing task count guarantees unlimited accepted
requests. Use verified file reuse, bounded admission/queues and provisioned upstream
capacity; validate a measured mix of distinct cold requests before increasing
workers. A rate-limited or unavailable external resolver has no guaranteed
code-only bypass. A configured accepted native route remains independent recovery;
an authorized alternative resolver must first pass exact-ID and full-file checks.

See [request-arrival testing](REQUEST_THROUGHPUT.md) for separate SQL/Telegram-handler
load tests. Simulated-origin successes do not establish YouTube/CDN throughput.
