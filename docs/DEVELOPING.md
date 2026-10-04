# Developing / 开发指南

**English** | [中文](DEVELOPING.zh-CN.md)

This document explains how the HLS test server is put together and how to extend
it. It is aimed at someone who wants to **read the code, add a scenario, or fix a
bug** without reverse-engineering the whole repository first.

本文说明 HLS 测试服务器的结构与扩展方式，面向"想读代码 / 加场景 / 改 bug"的人，
目标是让你不必先把整个仓库逆向一遍。**中文完整版见 [DEVELOPING.zh-CN.md](DEVELOPING.zh-CN.md)。**

- [1. Architecture at a glance](#1-architecture-at-a-glance)
- [2. Module responsibilities](#2-module-responsibilities)
- [3. Request lifecycle](#3-request-lifecycle)
- [4. Adding a scenario (the 7-step recipe)](#4-adding-a-scenario-the-7-step-recipe)
- [5. Where the parts of a URL come from](#5-where-the-parts-of-a-url-come-from)
- [6. i18n: adding or changing UI text](#6-i18n-adding-or-changing-ui-text)
- [7. Verification harness](#7-verification-harness)
- [8. Media generation internals](#8-media-generation-internals)
- [9. ffmpeg path resolution (real mode)](#9-ffmpeg-path-resolution-real-mode)
- [9.1 Sample video (--sample / tools/sample/)](#91-sample-video---sample--toolssample)
- [9.2 Cache-clear fault tolerance and the delete guard](#92-cache-clear-fault-tolerance-and-the-delete-guard)
- [10. Response preview endpoint `/preview/{id}`](#10-response-preview-endpoint-previewid)
- [11. Pitfalls (read before you edit)](#11-pitfalls-read-before-you-edit)
- [12. Quick command reference](#12-quick-command-reference)

---

## 1. Architecture at a glance

The server is deliberately layered so each concern lives in exactly one place.
Data flows **top-down**: a handler asks a builder for text, a provider for bytes,
and a state object for mutable bits.

```
                HTTP request
                     |
                     v
   +-----------------------------------------+
   |  app.py    Handler._route()             |  thin routing layer
   |            -> ROUTES regex -> handler   |
   +-----------------------------------------+
        |                 |                |
        | text           | bytes          | mutable state
        v                 v                v
  playlists.py        media.py          state.py
  (m3u8 text)      (TS / fMP4 bytes)  (sessions, rate limits,
        |                 |             one-time tokens, bans)
        |                 |
        v                 v
     keys.py  <-----  crypto.py
  (keys, HMAC)     (AES-128-CBC)
        \                 /
         `---- i18n.py ---'   (page text + zh translation, used by app.py)
```

Key invariants:

- `app.py` has **no** media/playlist construction logic of its own. It maps a
  URL to a handler; the handler delegates.
- `playlists.py` is **pure**: given the same arguments it returns the same
  string, and `time`-dependent behaviour is always passed in explicitly.
- `media.py` is **deterministic**: a segment index always yields the same bytes,
  so caches and byte-range lengths stay consistent across requests.
- Nothing about routing or verification depends on the display language.

---

## 2. Module responsibilities

| Module | Lines | Responsibility |
| --- | --- | --- |
| `server.py` | ~67 | CLI entry point. Parses `--host/--port/--cache-dir/--ffmpeg`, calls `app.init_media()`, runs the server. |
| `verify.py` | ~1710 | Full-matrix test harness. Self-hosts a server, drives every scenario, asserts observable behaviour. |
| `hls_test/app.py` | ~2040 | Routing table, per-scenario handlers, `CATALOG`, index page, the `/preview` response-preview endpoint, HTTP `Handler`, Range emulation, in-process caches. |
| `hls_test/playlists.py` | ~950 | Pure m3u8 builders — one function per scenario shape. |
| `hls_test/media.py` | ~1010 | MPEG-TS muxer (PAT/PMT/PES/CRC32) + fMP4/ISOBMFF box builder + ffmpeg/sample path resolution + real-content cache and clearing. |
| `hls_test/crypto.py` | ~250 | Pure-Python AES-128-CBC; self-tests against FIPS-197 / NIST SP 800-38A at import. |
| `hls_test/keys.py` | ~47 | Fixed keys, correct/wrong keys, rotating keys, HMAC sign/verify. |
| `hls_test/state.py` | ~155 | Sessions, one-time tokens, captcha, per-IP hit counting, bans, range-first-use. |
| `hls_test/i18n.py` | ~228 | `LANGS`, `CATEGORIES`, page `UI` strings, `ZH_SCENARIOS` id→text map, `resolve_lang()`. |

### Why `CATALOG` and `i18n` are separate

`app.CATALOG` is the machine-readable scenario registry. Its tuples keep
**stable English** values:

```
(id, category, item, title, path, expect)
```

- `id` — stable key, never shown to the user; joins CATALOG ↔ translations ↔ tests.
- `category` — one of the English category keys in `i18n.CATEGORIES`.
- `item` — the user's original requirement number (1–17), for traceability.
- `title` / `expect` — **English originals**, and also the fallback used when a
  translation is missing.
- `path` — the playlist URL (also exercised by `verify.check_catalog`).

Chinese text lives **only** in `i18n.ZH_SCENARIOS`, keyed by `id`. This keeps one
source of truth per language and means a missing translation degrades to English
instead of breaking a page or a test.

---

## 3. Request lifecycle

All routing happens in `Handler._route()` (`app.py`):

1. Read `self.path`. If it is an RFC 7230 **absolute-form** target
   (`http://host/path?q`), strip scheme + authority. This happens when a client
   follows a redirect on a kept-alive connection.
2. Split off `?query`, then `urllib.parse.unquote()` the path, so **routing
   regexes match the decoded path**.
3. Build a `Req` wrapper (`req.base`, `req.q()`, `req.qint()`, `req.ip`, …).
4. **HEAD is matched against GET routes** (RFC 7231: HEAD is answered exactly
   like GET minus the body). `match_method = "GET" if method == "HEAD" else method`.
5. Walk `ROUTES` in registration order; the first `(method, regex)` that matches
   wins. Positional regex capture groups are passed to the handler after `req`.
6. The handler returns a `Resp`, or raises `Fail(status, body, mime, headers)`
   for non-200 outcomes — including intentional 401/403/404/416/429/500.
7. `Handler._send()` writes the response, applying `rmode` (Range behaviour) and
   `kbps` (throttle) when the `Resp` requested them. Unhandled exceptions become
   a 500 with a traceback logged.

---

## 4. Adding a scenario (the 7-step recipe)

Follow these in order. Skipping the README/i18n steps is the usual cause of a
"green tests, broken page" state.

### Step 1 — build the playlist text in `hls_test/playlists.py`

Add a builder. **The first parameter is always `base`** (the absolute origin),
even when the scenario does not need it, so handlers can pass it unconditionally.

```python
def my_scenario(base: str) -> str:
    """One-line English summary. / 一行中文说明。"""
    return media_playlist([f"seg/{i}.ts" for i in range(N_SEG)])
```

### Step 2 — add handlers in `hls_test/app.py`

One handler per URL. The capture-group count in the route regex **must** match
the handler's parameter count (see [§5](#5-where-the-parts-of-a-url-come-from)).

```python
def h_my_scenario(req: Req) -> Resp:
    return m3u8(playlists.my_scenario(req.base))

def h_my_scenario_seg(req: Req, i: str) -> Resp:
    return ts(MEDIA.segment("myscenario", int(i)))
```

### Step 3 — register the routes

Add `_r(...)` lines in the routing table section of `app.py`, grouped under the
scenario's `# --- N. name ---` banner:

```python
_r("GET", r"^/hls/my-scenario/playlist\.m3u8$", h_my_scenario)
_r("GET", r"^/hls/my-scenario/seg/(\d+)\.ts$", h_my_scenario_seg)
```

### Step 4 — add a `CATALOG` entry (English)

```python
("my-scenario", "Delivery", "17", "My scenario title",
 "/hls/my-scenario/playlist.m3u8",
 "One sentence describing what a correct client must do."),
```

Place it under the matching category banner. Keep `path` exactly as routable.

### Step 5 — add the Chinese translation in `hls_test/i18n.py`

```python
# inside ZH_SCENARIOS
"my-scenario": {
    "category": "分发",
    "title": "我的场景标题",
    "expect": "一句话说明正确的客户端应如何表现。",
},
```

Validate coverage (must print an empty list):

```bash
python -c "from hls_test import app, i18n; print(i18n.missing_ids([s[0] for s in app.CATALOG]))"
```

### Step 6 — add README rows

Add one row to the scenario table in **both** `README.md` (English) and
`README.zh-CN.md` (Chinese), and bump the counts (scenarios / assertions).

### Step 7 — add assertions in `verify.py`

Write a `check_my_scenario(c: Client)` function, then call it from `main()`.
Assert **observable behaviour** (status, decryptability, PIDs, PTS, byte ranges),
not internal implementation.

```python
def check_my_scenario(c: Client) -> None:
    group("24. my scenario")
    st, _, body = c.get("/hls/my-scenario/seg/0.ts")
    check("segment status", st, 200)
    ok("segment is valid TS", ts_sync(body))
```

Then run the matrix:

```bash
python verify.py --fast
```

> **Don't forget the preview registration.** The new scenario must appear in
> either `app.PREVIEWABLE` (stateless → call the builder) or
> `app.STATEFUL_NOTES` (stateful → one-line note), or `check_preview`'s coverage
> assertion will fail. See [§10](#10-response-preview-endpoint-previewid).

---

## 5. Where the parts of a URL come from

Routing is one regex per URL in `ROUTES`. The subtlety is that **capture groups
are passed positionally** to the handler, after `req`:

| Route regex | Groups | Handler signature |
| --- | --- | --- |
| `^/hls/vod/seg/(\d+)\.ts$` | 1 | `h_vod_seg(req, i)` |
| `^/hls/renditions/audio/(\w+)/seg/(\d+)\.ts$` | 2 | `h_renditions_audio_seg(req, group, i)` |
| `^/hls/nested/(v1\|v2\|shared)/(\d{3}p)\.m3u8$` | 2 | `h_nested_media(req, tier, _res)` |

Rules:

- Group count **must equal** handler parameter count (minus `req`). One too many
  or one too few raises `TypeError` inside the dispatcher → HTTP 500.
- Use `(?:...)` for **non-capturing** alternatives you don't want to receive.
- If a route needs a group only to match but the handler doesn't need the value,
  name the parameter with a leading underscore (`_res`, `_prefix`) — a convention,
  not a requirement.

`req.base` is the absolute origin (`http://127.0.0.1:8080`), derived from the
`Host` header. It is what lets a playlist emit absolute URIs. Relative URIs in a
playlist are resolved by the **client** relative to the document that contains
them — see the nested-master pitfall in [§9](#9-pitfalls-read-before-you-edit).

---

## 6. i18n: adding or changing UI text

Language resolution order (`i18n.resolve_lang`, used by the index page):

1. explicit `?lang=` query parameter (the on-page switcher uses this);
2. `Accept-Language` request header;
3. `DEFAULT_LANG` (`en`).

Three dicts, one per concern:

- `CATEGORIES[lang][english_key]` — section labels.
- `UI[lang][key]` — page chrome (title, table headers, warning banner, mode label).
- `ZH_SCENARIOS[id]` — per-scenario `{category, title, expect}`.

**Adding a language:**

1. Add the code to `LANGS` and a name to `LANG_NAMES`.
2. Add a dict to `CATEGORIES` and `UI`.
3. Add a translated scenario map.
4. Run the coverage check (`missing_ids()`), then `verify.py --fast` (the
   catalog group asserts i18n coverage and page rendering for each language).

Unknown keys always fall back to English — a missing translation never causes a
404 or a failed assertion, only an English string where Chinese was expected.

---

## 7. Verification harness

`verify.py` self-hosts a server on an ephemeral port (or targets `--base`), then
runs `check_*` functions in a fixed order. Primitives:

| Helper | Purpose |
| --- | --- |
| `group(name)` | Start a labelled assertion block. |
| `check(name, got, want)` | Assert equality; records and prints. |
| `ok(name, cond, detail)` | Assert a boolean condition. |
| `Server` / `Client` | Background server thread; HTTP client that returns errors instead of raising, and follows redirects manually. |
| `ts_sync(buf)` | `len % 188 == 0` and every packet starts with `0x47`. |
| `first_video_pts(buf)` | Parse the first video PES PTS out of a TS buffer. |
| `pids(buf)` | Set of PIDs present. |
| `boxes(buf)` | Top-level ISOBMFF fourccs, validating the box chain closes at EOF. |
| `playlist_lines(text)` | Split m3u8 into non-empty stripped lines. |

Run modes:

```bash
python verify.py                 # full matrix (includes live-window timing)
python verify.py --fast          # skip the only sleeping checks
python verify.py --base http://127.0.0.1:8080   # test a running instance
```

**Design principle:** assert what a downloader can observe. Do not assert private
attributes or exact byte offsets that a refactor could legitimately change.

---

## 8. Media generation internals

### MPEG-TS (`media.make_ts_segment`)

Each segment is a valid 188-byte-packet transport stream:

- PAT (PID 0) → PMT (PID `0x1000`) → PES video (`PID_VIDEO`) + PES audio (`PID_AUDIO`).
- CRC32 (`_crc32_mpeg`) for PSI sections.
- PTS advances by `SEG_DUR * 90_000` per segment; `pts_start` can override this
  (used by discontinuity and `clip B` scenarios).
- `streams="v"` / `"a"` emit video-only / audio-only (for separated renditions).

### fMP4 (`media.fmp4_init` / `fmp4_segment`)

Hand-built ISOBMFF boxes: `ftyp`/`moov`(trak>mdia>minf>stbl>stsd)/`moof`(mfhd,traf[tfhd,tfdt,trun])/`mdat`.

Two things matter for correctness:

- **`default-base-is-moof`** — `tfhd` carries flags `0x020000`, so every
  `trun.data_offset` is relative to the **start of the moof box**, not the file
  start. Getting this wrong makes byte-range indexing point at the wrong bytes.
- **dual track** (`tracks="va"`) — the `moof` contains **two `traf` boxes**
  (video + audio). Single-track output is byte-for-byte unchanged by the dual-track
  code path; `init` is 614 B / `seg` 121320 B (video only) vs 1026 B / 183632 B (va).

### AES-128-CBC (`crypto.aes128_cbc_decrypt` / `encrypt`)

Pure Python, self-tested at import against FIPS-197 and NIST SP 800-38A vectors.
It is **slow** (~3.5 MB ≈ 50 s), which is why anything that encrypts bulk data
either caches the result (`app.enc_segment`, `app.big_file`) or uses a reduced
segment count (the `aes-byterange` combo uses `AES_BR_N = 4` one-second segments).

---

## 9. ffmpeg path resolution (real mode)

By default every segment is **synthetic** (pure Python, deterministic, zero
dependencies). Scenarios that need real H.264/AAC — `fmp4-av` and friends — use
**real mode**: ffmpeg is invoked once to transcode, and the result is cached.

To avoid binding the project to "ffmpeg must be on PATH", `media._resolve_ffmpeg()`
looks up the binary in this order (`candidate` is the value from `--ffmpeg` or
`HLS_FFMPEG`):

1. CLI `--ffmpeg <path>` — explicit
2. environment variable `HLS_FFMPEG`
3. in-project `./tools/ffmpeg/ffmpeg[.exe]` or `./tools/ffmpeg/bin/ffmpeg[.exe]`
4. system `PATH` (`shutil.which("ffmpeg")`)

`<path>` may be either the **executable** or the **directory** that contains it
(the filename is filled in).

```bash
python server.py                              # if PATH has no ffmpeg, ./tools/ffmpeg is probed
python server.py --ffmpeg /opt/ffmpeg/bin     # directory
python server.py --ffmpeg C:/Dependency/ffmpeg/bin/ffmpeg.exe   # executable
```

**Recommended**: drop an ffmpeg build into `tools/ffmpeg/` (already in
`.gitignore`, so it never gets committed) for a zero-config setup. Not doing so is
also fine — real scenarios fall back to synthetic output and report
`is_real: False`. The resolved path is printed at startup.

A **status banner at the top of the index page** (`ff_banner` in `h_index`)
mirrors this state: found → a green banner plus the resolved path; not found →
an amber banner listing the searched locations and the four ways to enable it.
The strings live under the `ff_*` keys in `i18n.UI`; `verify.check_ffmpeg_banner`
asserts the banner exists, uses the right variant, and shows the path.

> Note: the **values** of `-hls_fmp4_init_filename` / `-hls_segment_filename` are
> *filenames*, appended to the directory of the m3u8. Passing an absolute path
> yields a doubled path and fails. The correct approach is to set the child
> process `cwd` to the output directory and pass only relative filenames (see
> `media._prepare_real`).

## 9.1 Sample video (`--sample` / `tools/sample/`)

The real-mode content can be driven by **your own video**. `media._resolve_sample()`
mirrors the ffmpeg lookup, highest priority first (`candidate` is `--sample` or
`HLS_SAMPLE`):

1. `--sample <path>` — a video file or its directory;
2. `$HLS_SAMPLE`;
3. `./tools/sample/source.{mp4,mkv,mov,webm,ts}` (first match wins).

`tools/sample/` is gitignored (`tools/sample/*`, only `README.md` is tracked).
When a sample is found, `_prepare_real()` delegates to
`_prepare_real_from_sample()`; otherwise it falls back to the built-in
`testsrc2`/`sine`.

Implementation constraints:

- **`-stream_loop -1` + fixed `-t 72`** so any length/framerate yields a
  predictable segment count, preserving `_real_file()`'s `index % 12` reuse
  contract (no downstream change).
- **Keyframe alignment**: `-sc_threshold 0 -force_key_frames
  expr:gte(t,n_forced*6)` puts every segment boundary on an IDR, giving clean
  6s segments. **Without it** ffmpeg only cuts on the sample's own keyframes and
  durations drift (a 5s sample produced 8.33s segments in testing).
- **No-ffprobe audio fallback**: only `ffmpeg.exe` exists here, no `ffprobe`.
  The av/audio commands use `-map 0:a:0?` (optional audio) and fall back to an
  `anullsrc` silent track if the command fails for lack of audio.
- **`.source` marker**: a JSON file in the cache dir recording the source
  (`sample`/`builtin`) and the sample's path+mtime+size. `cache_is_stale()` uses
  it to detect "sample added but old cache still present" and rebuild on
  `__init__`, avoiding silently serving stale content.

Endpoints: `GET /sample.json` (read-only status) and `POST /sample/rebuild`
(clear and rebuild from the current sample, no body). The index page shows a
sample panel on a **second row** (`_sample_panel_html`), reusing the `.ff` /
`button.clr` styles. Assertions live in `verify.check_sample`.

> **Note**: the sample panel and the ffmpeg banner **share the `.ff ok`/`.ff no`
> classes**, so `check_ffmpeg_banner` must scope its assertions by
> `id="ff-banner"`; otherwise it counts the sample panel's class too and
> falsely reports "both variants present".

## 9.2 Cache-clear fault tolerance and the delete guard

`POST /cache/clear` goes through `media.MediaProvider.clear_cache()`, which must
keep two properties:

1. **Per-file fault tolerance**: the delete loop uses `except BaseException`
   (not `except OSError`). Some environments raise `SystemExit` when a bulk
   delete exceeds a threshold — it derives from `BaseException`, not
   `Exception`, so catching only `OSError`/`Exception` lets it escape to the
   HTTP layer and tear down the connection (the client sees **HTTP 000**, not a
   readable error). Failed files are recorded in the returned `errors` list.
2. **No rebuild when files remain**: if anything could not be deleted
   (`errors` non-empty), regeneration is skipped and `regen_skipped=True`, so
   the server never claims success while stale files linger.

`h_cache_clear` wraps this in `try/except BaseException` and returns structured
JSON (`ok`/`errors`/`error`); the front-end then shows "partially cleared +
failure count + reason". `Handler._route` also gained an
`except (KeyboardInterrupt, SystemExit)` branch as defence-in-depth (a 503
instead of a dropped connection).

> **Environment caveat**: this repo's test sandbox injects a `sitecustomize.py`
> that replaces `os.remove` with a bulk-count-guarded version that raises
> `SystemExit` past the threshold. **Real user machines have no such injection**
> and clearing works normally. Never experiment on the real cache in tests —
> use `tempfile.mkdtemp()` and stub `os.remove` via `unittest.mock` to simulate
> a single-file failure and the `SystemExit` guard (see
> `verify.check_cache_panel`).

---

## 10. Response preview endpoint `/preview/{id}`

Every row on the index page has a "response" button. Clicking it makes the
front-end `fetch('/preview/<id>')` and expands the scenario's **response shape**
below the row (status code, key headers, body, note).

The backend `h_preview(req, sid)` returns JSON:

```json
{
  "id": "vod",
  "title": "Plain VOD (full manifest ladder)",
  "path": "/hls/vod/playlist.m3u8",
  "status": 200,
  "content_type": "application/vnd.apple.mpegurl",
  "content_length": 304,
  "headers": { "Content-Type": "...", "Content-Length": "304" },
  "body": "#EXTM3U\n...",
  "note": null
}
```

The key invariant is that **a preview never produces side effects**. This is
enforced with a whitelist:

- `app.PREVIEWABLE` — id → a **pure builder call** (e.g. `_pv_vod(base)`). Only
  **stateless** scenarios are registered here; the preview calls the builder
  directly, never the handler, and never touches `STATE`.
- `app.STATEFUL_NOTES` — id → a one-line **static note** (why it can't be
  previewed, plus the correct way to fetch it). Stateful scenarios (`onetime` /
  `ratelimit` / `ban` / `captcha` / `session` / `sign` …) take this branch:
  `body` is empty, `status` is `null`, only `note` and `expect` are returned.

`verify.py`'s `check_preview()` guards the contract with three assertions:

1. stateless scenario → 200, and `body` starts with `#EXTM3U`;
2. stateful scenario → carries a `note`, empty `body`, and **two consecutive
   calls are byte-identical** (proving no state was mutated);
3. `PREVIEWABLE ∪ STATEFUL_NOTES` **covers every `CATALOG` id** (nothing missing,
   nothing misspelled).

When you add a scenario, **register it in one of these two tables** — otherwise
`check_preview` fails.

---

## 11. Pitfalls (read before you edit)

1. **Capture-group count must match the handler signature.** The single most
   common cause of a 500. Count the groups; use `(?:...)` when you don't need one.

2. **CATALOG keeps English; Chinese lives in `i18n.ZH_SCENARIOS`.** Never write
   Chinese into `CATALOG` — it is also the fallback value.

3. **Nested masters change the resolution base at every level.** A relative URI
   resolves against *the document that contains it*, so a URI that is valid at
   the top-level master can 404 one or two levels down. Route bases are:
   `/hls/nested/master.m3u8` → `/hls/nested/{tier}/sub.m3u8` →
   `/hls/nested/{tier|shared}/{res}.m3u8`.

4. **SCTE-35 in playlists is a hex string, not base64.** `SCTE35-OUT`/`SCTE35-IN`
   carry a big-endian binary `hex` sequence; there is no `SCTE-35=` attribute.

5. **`CLOSED-CAPTIONS` cannot carry a `URI`.** It references in-band captions by
   `INSTREAM-ID`; putting a `URI` on it is a spec violation.

6. **`fmp4_segment` offsets are moof-relative.** See `default-base-is-moof` above.

7. **HEAD must route like GET.** If you special-case GET only, HEAD probes break.

8. **Everything time-dependent is passed in, not read from the clock inside
   builders.** Keep `playlists.py` pure so tests are reproducible.

9. **Malformed playlists are returned as `bytes`.** BOM / CRLF / lowercase tags
   need byte-level control; returning `str` would normalise them away.

10. **Unknown range mode → 404, not 500.** Validate `mode` against `RANGE_MODES`
    in every handler that accepts one.

---

## 12. Quick command reference

```bash
# run the server
python server.py                    # http://127.0.0.1:8080/

# point real mode at a specific ffmpeg; otherwise HLS_FFMPEG, ./tools/ffmpeg, PATH are tried in order
python server.py --ffmpeg C:/Dependency/ffmpeg/bin

# point real mode at a sample video; otherwise HLS_SAMPLE, ./tools/sample/source.{mp4,mkv,...} are tried
python server.py --sample /path/to/clip.mkv

# syntax check everything
python -c "import py_compile,glob; [py_compile.compile(f,doraise=True) for f in glob.glob('hls_test/*.py')+['server.py','verify.py']]; print('OK')"

# run the matrix
python verify.py --fast

# sanity: routes + catalog counts
python -c "from hls_test import app; print(len(app.ROUTES), len(app.CATALOG))"

# i18n coverage (must be an empty list)
python -c "from hls_test import app, i18n; print(i18n.missing_ids([s[0] for s in app.CATALOG]))"

# preview coverage (must be an empty list): every scenario in PREVIEWABLE or STATEFUL_NOTES
python -c "from hls_test import app; c=set(app.PREVIEWABLE)|set(app.STATEFUL_NOTES); print([s[0] for s in app.CATALOG if s[0] not in c])"

# inspect a scenario's response preview by hand
curl "http://127.0.0.1:8080/preview/vod?lang=en"
```
