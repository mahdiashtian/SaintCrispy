# Live provider verification

Verification separates parser/unit behavior, database/concurrency behavior, full origin streams, and Telegram registration. A green unit suite cannot establish unrestricted access from every server IP.

## October 9, 2026 observations

The earlier Linux origin run on this date completed 71 public quality streams across five providers: SoundCloud, Instagram, Pinterest, XVideos and XNXX. The test run and sanitized result are available in [GitHub Actions run 37940691580](https://github.com/mahdiashtian/SaintCrispy/actions/runs/37940691580). The overall live check failed visibly for restricted/deleted samples and YouTube's login challenge; those failures were not counted as successes.

The workstation check for this architecture change tried the exact reported YouTube video `l6zflbTGNFQ`, the reported SoundCloud `quavoofficial/away`, public SoundCloud `gdaal/mojezeh`, a Pinterest pin, and public XVideos/XNXX fixtures. The exact YouTube video's metadata extracted, but its CDN connections failed. A separate `visionos` client attempt also failed extraction and was not promoted to a fallback. SoundCloud, XVideos and XNXX connections timed out from this workstation. This is an egress limitation, not proof that their parsers successfully downloaded these samples here.

Pinterest pin `2885187256207927` completed two full qualities and Telegram registration without sending messages. The HLS 720x1280 stream produced 1,014,604 bytes; the progressive 720x1280 file contained 971,092 bytes. A mismatching Telegram URL-cache result triggered a streamed upload fallback, whose registered size matched the origin. Instagram had already been confirmed working by the operator; Linux checks continue to include a carousel and two reels.

The optional Linux workflow now includes the operator's exact YouTube URL alongside the standard public fixture and separately probes SoundCloud's reported tracks and legacy origin routes. Fresh deployment-network findings should be recorded from its uploaded sanitized report; they should not be inferred from another network's success.

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
