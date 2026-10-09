# Live provider verification

Verification separates parser/unit behavior, database/concurrency behavior, full origin streams, and Telegram registration. A green unit suite cannot establish unrestricted access from every server IP.

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
