# HLS test source

A self-contained HLS origin server that reproduces the awkward parts of the
protocol on purpose: 47 scenarios covering encryption, key delivery, access
control, source defects, container formats and HTTP resume behaviour.

It exists to test **client/downloader logic**, not playback. Every scenario has
a stable URL and a documented expected behaviour, so a downloader can be pointed
at it and its handling of each failure mode verified.

- Zero dependencies — Python 3.10+ standard library only
- No network access, no external media files
- `verify.py` asserts all 552 observable behaviours against a self-hosted instance

中文文档：[README.zh-CN.md](README.zh-CN.md)

## Quick start

```bash
python server.py                 # http://127.0.0.1:8080/
python verify.py                 # run the full matrix (self-hosts a server)
python verify.py --base http://127.0.0.1:8080   # ...or test a running one
```

Open `http://127.0.0.1:8080/` for the scenario index, or
`/scenarios.json` for the machine-readable catalog.

Each row on the index page has a **response** button: click it to expand that
scenario's response shape (status code, key headers and body) inline, fetched
from `/preview/{id}`. Stateful scenarios (`onetime`, `ratelimit`, `ban`,
`captcha`, …) show a note instead of executing, so previewing **never produces
side effects**.

## Scenarios

| Category | Scenario | URL | Expected behaviour |
| --- | --- | --- | --- |
| Encryption | AES-128, explicit IV | `/hls/aes128/playlist.m3u8` | Decryptable; first plaintext byte must be 0x47 (TS sync). |
| Encryption | AES-128, IV from media sequence number | `/hls/aes128/msn-iv/playlist.m3u8` | No IV attribute -> IV must be the segment's media sequence number. |
| Encryption | AES-128 multivariant + SESSION-KEY | `/hls/aes128/master.m3u8` | `EXT-X-SESSION-KEY` in master; both variants AES-128. |
| Encryption | Key URL expires (TTL, signed) | `/hls/key-expiry/playlist.m3u8?ttl=30` | Key endpoint 403s after `?ttl` seconds. TTL configurable (min 5). |
| Encryption | SAMPLE-AES (identity keyformat) | `/hls/sample-aes/playlist.m3u8` | Downloader should refuse: unsupported encryption method. |
| Encryption | SAMPLE-AES + FairPlay (`skd://`) | `/hls/fairplay/playlist.m3u8` | DRM; reject as undecryptable, not as a parse error. |
| Encryption | Widevine KEYFORMAT + `data:` pssh URI | `/hls/widevine/playlist.m3u8` | DRM; the `data:` URI is a pssh box, not a key. |
| Encryption | Key requires a custom request header | `/hls/key-header/playlist.m3u8` | Key endpoint needs `X-Api-Key` (403 otherwise). |
| Encryption | AES key rotates per segment | `/hls/key-rotation/playlist.m3u8` | One `EXT-X-KEY` per segment; sticky-key handling is wrong here. |
| Encryption | AES-128 + fMP4 (clear init) | `/hls/aes-fmp4/playlist.m3u8` | `EXT-X-KEY` precedes `EXT-X-MAP`; the init segment is clear, only `.m4s` fragments are encrypted. |
| Encryption | AES-128 + `EXT-X-BYTERANGE` | `/hls/aes-byterange/playlist.m3u8` | Ranges index the **ciphertext**; each range is independently PKCS7-padded, so merging before decrypt fails. |
| Encryption | Key changes at `EXT-X-DISCONTINUITY` | `/hls/aes-disc/playlist.m3u8` | Clips A/B use different keys; one key across the boundary yields garbage. |
| Playlist structure | Mixed URI forms + redirect | `/hls/paths/master.m3u8` | Relative / dot-segments / percent-encoded / query / absolute / protocol-relative / 302, in variants **and** segment URIs. |
| Playlist structure | Multi-level nested master (3 tiers) | `/hls/nested/master.m3u8` | `master -> sub-master -> media`. Sub-masters use relative, dot-segment and parent (`../`) URIs; a client that keeps the top-level base URL 404s. |
| Playlist structure | TS segments via `EXT-X-BYTERANGE` | `/hls/byterange/playlist.m3u8` | All segments are byte ranges of one `big.ts`. |
| Playlist structure | `EXT-X-DEFINE` variable URIs | `/hls/define/playlist.m3u8` | Segment URIs use `{$segbase}` substitution (v8). |
| Playlist structure | Variable `EXTINF` + `TARGETDURATION` | `/hls/variable/consistent/playlist.m3u8` | Durations span 0.8s..12.0s. `/mismatch/` declares `TARGETDURATION:6`, below the real 12.0 max (a common origin bug). |
| Playlist structure | Malformed playlists | `/hls/malformed/crlf/playlist.m3u8` | Variants: `crlf bom no-version unknown-tags spacing lowercase no-extm3u bad-attrs all`. Segments are clean; parsing is the test. |
| Playlist structure | Series / episodes (multi-episode VOD) | `/hls/series/master.m3u8` | 3 episodes with unequal segment counts, one shared AES-128 key, and custom `EXT-X-SERIES-PREV` / `EXT-X-SERIES-NEXT` tags a client must ignore. |
| Delivery | Plain VOD (`ENDLIST`) | `/hls/vod/playlist.m3u8` | Baseline happy path. |
| Delivery | Live sliding window (6 x 6s) | `/hls/live/playlist.m3u8` | No `ENDLIST`; `MEDIA-SEQUENCE` advances; PDT included. |
| Delivery | Event playlist -> becomes VOD | `/hls/live/event.m3u8` | Window grows to 12 segments, then appends `ENDLIST`. |
| Access control | Referer / UA / Cookie checks | `/hls/guard/playlist.m3u8?auth=all` | `?auth=referer\|ua\|cookie\|all`; wrong or missing header -> 403. |
| Access control | Signed segment URLs with expiry | `/hls/sign/playlist.m3u8?ttl=60` | HMAC-signed `?exp&s`; 403 after TTL (default 60s, min 5). |
| Access control | Session required | `/hls/session/playlist.m3u8` | `GET /hls/session/start` first; segments need `Cookie HLSSESS=` or `?session=` (401 otherwise). |
| Access control | Rate limited | `/hls/ratelimit/playlist.m3u8?n=5` | More than `?n` segment fetches per 30s -> 429 + `Retry-After`. |
| Access control | Bandwidth throttled | `/hls/throttle/playlist.m3u8?kbps=64` | Segments delivered at `?kbps` (default 64). |
| Access control | IP ban after burst | `/hls/ban/playlist.m3u8?n=8` | Burst over `?n` in 60s -> 403 for 60s. |
| Access control | Captcha gate | `/hls/captcha/playlist.m3u8` | First segment 403 HTML; solve at `/hls/captcha/solve?code=1234`. |
| Source defects | Missing / 500 / garbage / truncated | `/hls/broken/playlist.m3u8` | seg2=404, seg4=500, seg6=garbage, seg8=truncated to half. |
| Source defects | AES stream with a wrong-key segment | `/hls/broken-enc/playlist.m3u8` | seg6 decrypts to garbage (PKCS7 unpad fails downstream). |
| Source defects | `EXT-X-GAP` placeholders | `/hls/gap/playlist.m3u8` | Segments 3-4 marked GAP; skip, don't fetch. |
| Source defects | Ad break signalled 3 ways | `/hls/ads/playlist.m3u8` | One break marked by `EXT-X-DATERANGE` (`SCTE35-OUT`/`SCTE35-IN`, hex-encoded binary), `EXT-X-CUE-OUT`/`CUE-IN` **and** `EXT-X-GAP`. |
| Source defects | PTS jump + `EXT-X-DISCONTINUITY` | `/hls/discontinuity/playlist.m3u8` | Clip B restarts its PTS below clip A's last value. |
| Source defects | One-time segment URLs | `/hls/onetime/playlist.m3u8` | Every segment URL works exactly once, then 403; re-fetch the playlist. |
| Resume | RFC-correct `Range` | `/hls/range/ok/playlist.m3u8` | `Accept-Ranges: bytes`, correct `Content-Range`, 416 for unsatisfiable ranges. |
| Resume | Range unsupported | `/hls/range/none/playlist.m3u8` | `Accept-Ranges: none`; always 200 + full body. |
| Resume | Advertises ranges but ignores them | `/hls/range/ignored/playlist.m3u8` | `Accept-Ranges: bytes` yet always 200 + full body, no `Content-Range`. |
| Resume | `206` with a wrong total length | `/hls/range/badtotal/playlist.m3u8` | `Content-Range` total claims half the real size; trusting it corrupts the file. |
| Resume | Broken multi-range response | `/hls/range/multipart-broken/playlist.m3u8` | `bytes=a-b,c-d` returns only the first range as a plain 206, no `multipart/byteranges`. |
| Resume | Resume works once, then resets | `/hls/range/restarts/playlist.m3u8` | First `Range` request is honoured; later ones get a 200 full body. |
| Resume | Resume impossible (3 mechanisms) | `/hls/resume-triple/playlist.m3u8` | One-time URLs + expiring key + one-shot `Range`. Correct behaviour is to detect this and restart, not splice. |
| Container | Audio/video in separate renditions | `/hls/av-separate/master.m3u8` | `EXT-X-MEDIA` audio group + video-only variants + audio-only variant. |
| Container | Multi-audio + subtitles + captions | `/hls/renditions/master.m3u8` | Two `AUDIO` groups (`aud`/`comm`), a `SUBTITLES` group (`en`/`zh`) and a `CLOSED-CAPTIONS` group (**no** URI). Subtitles are WebVTT with `X-TIMESTAMP-MAP`. |
| Container | fMP4 (`EXT-X-MAP` + `.m4s`) | `/hls/fmp4/playlist.m3u8` | Init segment must be fetched first and remuxed with the media. |
| Container | fMP4 with video + audio traks | `/hls/fmp4/av/playlist.m3u8` | Init holds two `trak`s (`avc1`+`mp4a`); each `.m4s` `moof` holds two `traf`s (track_ID 1 and 2) with independent data offsets. |
| Container | fMP4 MAP + segments via byterange | `/hls/fmp4/byterange/playlist.m3u8` | Init and media segments are ranges of one `combined.m4s`. |

## Credentials

Fixed values, also listed on the index page.

| Used by | Value |
| --- | --- |
| `X-Api-Key` (key-header) | `hls-test-api-key` |
| Referer (guard) | `https://portal.example.com/` |
| User-Agent prefix (guard) | `HLSClient/1.0` |
| Cookie (guard) | `access=granted` |
| Captcha code | `1234` |
| AES-128 key | `000102030405060708090a0b0c0d0e0f` |
| AES-128 IV | `000102030405060708090a0b0c0d0e0f` |

## Content modes

Segments are real MPEG-TS (0x47 sync, PAT/PMT/PES, controllable PTS) or fMP4
(box chain closing exactly at EOF), produced programmatically — **structurally
valid, not decodable video**. That is deliberate: these scenarios test
downloader logic, not a decoder.

If `ffmpeg` is available, the server instead encodes real, playable H.264/AAC
content on first use and caches it under `media_cache/`. Delete that directory
to regenerate.

The index page shows a **status banner at the top** telling you whether ffmpeg
was found, and if so the exact path in use. When it is missing, the banner lists
every location that was searched and the four ways to enable it — so you never
have to guess whether real video is active.

ffmpeg is located in this order (highest priority first):

1. `--ffmpeg PATH` — an executable, or the directory containing it;
2. `$HLS_FFMPEG` — same, as an environment variable;
3. `./tools/ffmpeg` or `./tools/ffmpeg/bin` — ship ffmpeg inside the project;
4. system `PATH`.

So you are not tied to `PATH`; a project-local or absolute ffmpeg works:

```bash
python server.py --ffmpeg /opt/ffmpeg/bin
python server.py --ffmpeg C:/Dependency/ffmpeg/bin/ffmpeg.exe
```

The startup banner prints the resolved ffmpeg path, or `synthetic` when none
was found.

### Sample video

Instead of the built-in test pattern, you can drive the real-content cache from
your own clip. Drop **one** video in `tools/sample/` (first match wins:
`source.mp4`, `source.mkv`, `source.mov`, `source.webm`, `source.ts`) and click
**Process / rebuild** on the index page — the server transcodes it with ffmpeg
into `media_cache/real/` and every real-mode scenario serves your content.
Delete the file and rebuild to return to the built-in pattern.

Override the location with `--sample <path>` or `$HLS_SAMPLE` (a file or a
directory). Output is normalised to 416x234 @ 25fps with keyframe-aligned 6s
segments; a clip with no audio track gets a silent one, so audio-only scenarios
keep working. Videos are gitignored — only `tools/sample/README.md` is tracked.

### Cache panel

The top of the index page also shows the real-content cache size and a **Clear
cache** button. Clearing is per-file fault-tolerant: if a file cannot be removed
(locked by a player, or blocked by a sandbox delete guard), the others are still
deleted and the UI reports the count and the first reason instead of a bare
"failed". The endpoints are `GET /cache.json` (read-only stats) and
`POST /cache/clear` (optional `?regenerate=0` to clear without rebuilding).

## Verification

`verify.py` starts a server on an ephemeral port and checks the observable
behaviour of every scenario — status codes, decryptability of AES segments,
PTS timelines, Range semantics, TS sync alignment, fMP4 box structure,
malformed-playlist shapes, bandwidth throttling, and the side-effect-free
`/preview/{id}` endpoint:

```
checks: 552   passed: 552   failed: 0
```

`--fast` skips the live-window timing checks (the only ones that sleep) and
reports 550 checks.

## Layout

```
server.py          entry point (argparse, starts the HTTP server)
verify.py          full-matrix verification harness
docs/DEVELOPING.md      architecture, how to add a scenario, pitfalls
docs/DEVELOPING.zh-CN.md  same, in Chinese
hls_test/app.py    routing, scenario handlers, catalog, index page
hls_test/playlists.py   m3u8 generation (one function per scenario)
hls_test/media.py       MPEG-TS muxer + fMP4 box builder
hls_test/crypto.py      pure-Python AES-128-CBC (self-tested at import)
hls_test/keys.py        key derivation + HMAC signing
hls_test/state.py       sessions, one-time tokens, rate limits, bans
hls_test/i18n.py        page text + id-keyed Chinese translations
```

## Switching the page language

The index page and `/scenarios.json` are available in English and Chinese. Three
ways to switch, in priority order:

1. **Click the switcher** at the top-right of the index page (`Language: English · 中文`).
   It just changes the URL to `?lang=zh` / `?lang=en`, so the choice is bookmarkable.
2. **Append `?lang=`** yourself: `http://127.0.0.1:8080/?lang=zh`
   (or `/scenarios.json?lang=zh`).
3. **Do nothing** — with no `?lang=`, the server negotiates from the browser's
   `Accept-Language` header.

Priority: `?lang=` > `Accept-Language` > English.

## Contributing

See [docs/DEVELOPING.md](docs/DEVELOPING.md) for the architecture, the step-by-step
recipe for adding a scenario, and the list of pitfalls to avoid.

## License

MIT — see [LICENSE](LICENSE).
