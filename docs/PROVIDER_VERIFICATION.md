# Live provider verification

Verification separates parser/unit behavior, database/concurrency behavior, full origin streams, and Telegram registration. A green unit suite cannot establish unrestricted access from every server IP.

## October 10, 2026 completeness regression checks

The upstream streaming-playback change `54b8350` was fetched and integrated before
this work. Fresh quality discovery now takes precedence over the durable file cache;
a complete redacted catalog is a temporary outage fallback. Tests cover both a saved
quality and a new quality after restart for each provider, plus 1,000 duplicate
requests and recovery after a catalog fallback.

Generated neutral 18-second audio/video fixtures reproduce an 11-second preview.
The delivery pipeline rejects a short Telegram registration, downloads the full
available origin, and decodes the complete video and audio to EOF. These tests run
against all six provider labels on the shared delivery contract; they are transport
tests, not live site extraction tests. Separate tests reject a short progressive
origin and an 11-second video paired with an 18-second audio track, without publishing
or persisting the incomplete file. Legacy references are not trusted solely because
their advertised Telegram duration looks correct.

The reported Instagram reel `DeR9eU8idi0` was attempted from the workstation before
and after the changes. The workstation connection timed out. The six-provider live
workstation audit completed **23 full Pinterest qualities** with the new MP4 track
validator; the other five providers failed connection/extraction from this egress.
Those failures are recorded as failures. The explicit reel is first in the Linux
live workflow so earlier Instagram requests cannot consume its rate-limit budget.
Instagram fixture tests cover a null product response, failed web pages, and an
authorized numeric media response without a shortcode. They do not establish that
the exact reel is publicly accessible from the production server.

The first [Linux completeness audit](https://github.com/mahdiashtian/SaintCrispy/actions/runs/38066730469)
passed **694 code/database tests** and read the reported reel's complete ordinary
MP4: 1,468,542 bytes and 11.7 seconds against a rounded 12-second origin duration.
It also exposed a validation error for seven VP9 renditions: the duration calculation
omitted media samples stored in the initial `moov`. The calculation now includes
both initial samples and fragments, with a real VP9 decoding regression test.
The final [Linux completeness audit](https://github.com/mahdiashtian/SaintCrispy/actions/runs/38067377477)
at `b8c94b4` passed **698 code/database tests** and completed **60 full public quality
streams** across five providers. The separate [push test run](https://github.com/mahdiashtian/SaintCrispy/actions/runs/38067376736)
passed lint, formatting and the same code/database suite.

| Provider | Full streams completed | Remaining observations |
| --- | ---: | --- |
| SoundCloud | 3 | Both reported protected tracks still require DRM; their original guest download is blocked |
| Instagram | 8 | Every rendition of `DeR9eU8idi0` passed; later carousel/reel requests hit HTTP 429 |
| Pinterest | 29 | Both inputs and every offered quality passed |
| XVideos | 13 | Two working samples passed; two obsolete public/embedded inputs returned 404 |
| XNXX | 7 | The working sample passed all qualities; the other input returned 404 |
| YouTube | 0 | The reported video and public fixture both required login on the runner's egress |

The exact Instagram reel passed its ordinary MP4 and all seven VP9 DASH renditions,
including separate audio. Validated durations were 11.7 to 11.815 seconds against the
origin's rounded 12 seconds; output sizes were 359,898 to 2,232,676 bytes. These live
checks read every byte and validate track duration and container completeness; full
decoding is covered separately by neutral generated codec fixtures. The longest
successful source was approximately 1,238 seconds, with a largest output of
420,388,463 bytes. No preview was substituted for a protected or inaccessible source.

The strict live step remains visibly failed for the listed origin restrictions.
Those failures are not counted as successful downloads. Available samples/formats
change between runs, which explains the different totals. Linux CI did not use
production cookies or verify Telegram registration; it sent no messages and saved
no complete media file to disk. Code changes are published in `38ecbf7` and `b8c94b4`.

The refreshed local 1,000-unique-input pipeline benchmark completed every transfer
in **12.066 seconds**, reached **1,000 HTTP connections**, and kept upload parts at
**64**. It read and uploaded 1,048,699,000 bytes each, with no source errors or dropped
logs. Maximum measured event-loop delay was 1.266 seconds. As above, Telegram and the
repository are simulated in this throughput measurement.

Validation before final publication: **693 tests passed, 5 database integration tests
skipped** locally; PostgreSQL/Redis integration runs additionally on Linux CI.
No production server connection or production Telegram message was used.

## October 9, 2026 observations

The earlier Linux origin run on this date completed 71 public quality streams across five providers: SoundCloud, Instagram, Pinterest, XVideos and XNXX. The test run and sanitized result are available in [GitHub Actions run 37940691580](https://github.com/mahdiashtian/SaintCrispy/actions/runs/37940691580). The overall live check failed visibly for restricted/deleted samples and YouTube's login challenge; those failures were not counted as successes.

The workstation check for this architecture change tried the exact reported YouTube video `l6zflbTGNFQ`, the reported SoundCloud `quavoofficial/away`, public SoundCloud `gdaal/mojezeh`, a Pinterest pin, and public XVideos/XNXX fixtures. The exact YouTube video's metadata extracted, but its CDN connections failed. A separate `visionos` client attempt also failed extraction and was not promoted to a fallback. SoundCloud, XVideos and XNXX connections timed out from this workstation. This is an egress limitation, not proof that their parsers successfully downloaded these samples here.

Pinterest pin `2885187256207927` completed two full qualities and Telegram registration without sending messages. The HLS 720x1280 stream produced 1,014,604 bytes; the progressive 720x1280 file contained 971,092 bytes. A mismatching Telegram URL-cache result triggered a streamed upload fallback, whose registered size matched the origin. Instagram had already been confirmed working by the operator; Linux checks continue to include a carousel and two reels.

The requested `IconsEmoji` set was also read through Telegram: it contains 143 documents and provides matching custom variants for the music and chat emojis used in the start text. UTF-16 entity offsets and rejection fallback are tested. No test message was published to check Premium eligibility.

The fresh [Linux verification run 37950024038](https://github.com/mahdiashtian/SaintCrispy/actions/runs/37950024038) passed **653 code/database tests** and completed **55 full public quality streams** across five providers:

| Provider | Full streams completed | Remaining observations |
| --- | ---: | --- |
| SoundCloud | 3 | Both reported tracks still expose protected full streams; original guest download is 401 |
| Instagram | 13 | Two inputs passed; a third reel hit HTTP 429 during quality resolution |
| Pinterest | 29 | Both inputs and every offered quality passed |
| XVideos | 9 | Working public/embedded samples passed; one obsolete input returned 404 |
| XNXX | 1 | Working public sample passed; one obsolete input returned 404 |
| YouTube | 0 | The exact reported video and standard public fixture both required login on the runner's egress |

The workflow's strict live step failed visibly for those restrictions. Its code/database test step passed, and the separate [push test run 37950007165](https://github.com/mahdiashtian/SaintCrispy/actions/runs/37950007165) is green. Available formats can change between checks, which explains why this run completed 55 streams and the earlier run completed 71. Production-network findings must be measured on that host rather than inferred from either run.

## Concurrent pipeline measurement

A local run with 1,000 distinct, initially uncached inputs, synchronized source starts and real HTTP bodies completed all 1,000 downloads in 11.677 seconds with no failures or dropped performance records. It read 1,048,699,000 bytes and acknowledged the same number of upload bytes, for 2,097,398,000 payload bytes through the pipeline. It reached 1,000 active HTTP connections while the global upload window stayed at 64 parts. The maximum measured event-loop delay was 1.331 seconds. Telegram RPCs and the repository were simulated in this benchmark; this is not production Telegram throughput or a latency guarantee. A separate 1,000-request duplicate-content run produced only 100 origin downloads and reused 900 saved references.

```bash
python tools/load_test_transfers.py --requests 1000 --unique-downloads 1000 \
  --all-new --concurrency 1000 --synchronize-sources --provider-mix \
  --file-mib 1 --rpc-latency 0.005 --no-memory-tracing \
  --log-file .runtime/load-1000.jsonl
python tools/summarize_logs.py .runtime/load-1000.jsonl --files 1000
```

## Reported restricted media

For SoundCloud `quavoofficial/away` and `octobersveryown/quebec`, the earlier origin diagnostic returned full AAC transcodings with SAMPLE-AES / SAMPLE-AES-CTR, Apple/Widevine key formats, broken ordinary MP3 alternatives and a guest original-download response of 401. Legacy origin routes did not provide a working ordinary stream. The provider supports normal complete streams and authorized original downloads, but does not implement DRM license acquisition/decryption. A preview or another track is never substituted for the requested full file.

The [SoundCloud API guide](https://developers.soundcloud.com/docs/api/guide#playing) describes playable, preview and blocked access. The upstream [DRM-only SoundCloud issue](https://github.com/yt-dlp/yt-dlp/issues/17335) tracks this specific CBC/CTR limitation. These are origin access conditions, not missing bitrate labels.

For YouTube, the [upstream extractor guide](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#youtube) and [PO token guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide) distinguish JavaScript challenges, account cookies and GVS tokens. The installed stable release was checked against [upstream releases](https://github.com/yt-dlp/yt-dlp/releases/tag/2026.08.19). Node alone does not authenticate a challenged datacenter connection. Extraction and CDN delivery use the same configured proxy; a returned signed URL is probed before being offered as a quality.

## Repeatable checks

```bash
python -m pytest -q
python tools/audit_providers.py --all-qualities --stream --strict \
  --max-stream-mib 1024 --case-timeout 900 --transfer-timeout 240 \
  --url youtube='https://www.youtube.com/watch?v=l6zflbTGNFQ' \
  --url soundcloud='https://soundcloud.com/quavoofficial/away' \
  --url soundcloud='https://soundcloud.com/gdaal/mojezeh' \
  --output .runtime/provider-audit.json
python tools/probe_soundcloud.py --legacy \
  https://soundcloud.com/quavoofficial/away \
  https://soundcloud.com/octobersveryown/quebec
```

`--strict` exits nonzero for any failed quality, timeout or restricted input. Add `--telegram --stream-mismatches` for registration verification with no messages. Reports remain private under `.runtime/`; no cookies, token values or signed URLs are pushed.
