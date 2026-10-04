# 开发指南

[English](DEVELOPING.md) | **中文**

本文说明 HLS 测试服务器的结构以及如何扩展它，面向"想读代码、加场景、改 bug"的人，
目标是让你不必先把整个仓库逆向一遍就能上手。

- [1. 架构总览](#1-架构总览)
- [2. 各模块职责](#2-各模块职责)
- [3. 一次请求的完整流程](#3-一次请求的完整流程)
- [4. 新增一个场景（7 步流程）](#4-新增一个场景7-步流程)
- [5. URL 的各个部分从哪来](#5-url-的各个部分从哪来)
- [6. i18n：增改页面文案](#6-i18n增改页面文案)
- [7. 验证框架](#7-验证框架)
- [8. 媒体生成内部原理](#8-媒体生成内部原理)
- [9. ffmpeg 路径解析（real 模式）](#9-ffmpeg-路径解析real-模式)
- [9.1 样例视频（--sample / tools/sample/）](#91-样例视频sample--toolssample)
- [9.2 缓存清理的容错与护栏](#92-缓存清理的容错与护栏)
- [10. 响应预览端点 /preview/{id}](#10-响应预览端点-previewid)
- [11. 踩坑清单（改代码前必读）](#11-踩坑清单改代码前必读)
- [12. 命令速查](#12-命令速查)
- [13. 网页中 / 英文切换](#13-网页中--英文切换)

---

## 1. 架构总览

服务器刻意做了分层，让每一类关注点只存在于一个地方。数据**自上而下**流动：
handler 向 builder 要文本、向 provider 要字节、向 state 对象要可变状态。

```
                HTTP 请求
                     |
                     v
   +-----------------------------------------+
   |  app.py    Handler._route()             |  薄路由层
   |            -> ROUTES 正则 -> handler     |
   +-----------------------------------------+
        |                 |                |
        | 文本            | 字节           | 可变状态
        v                 v                v
  playlists.py        media.py          state.py
  (m3u8 文本)      (TS / fMP4 字节)   (会话、限流、
        |                 |             一次性 token、封禁)
        |                 |
        v                 v
     keys.py  <-----  crypto.py
  (密钥、HMAC)      (AES-128-CBC)
        \                 /
         `---- i18n.py ---'   (页面文案 + 中文翻译，app.py 使用)
```

几条不变量：

- `app.py` **自己不做**任何媒体/播放列表构造。它只把 URL 映射到 handler，
  由 handler 再往下委派。
- `playlists.py` 是**纯函数**：同样的入参永远返回同样的字符串；所有依赖时间
  的行为都必须显式传参进来。
- `media.py` 是**确定性的**：同一个分片序号永远产出同样的字节，因此缓存与
  byte-range 长度在各次请求间保持一致。
- 路由与验证逻辑**完全不依赖**展示语言。

---

## 2. 各模块职责

| 模块 | 行数 | 职责 |
| --- | --- | --- |
| `server.py` | ~67 | 命令行入口。解析 `--host/--port/--cache-dir/--ffmpeg/--sample`，调用 `app.init_media()`，启动服务。 |
| `verify.py` | ~1710 | 全矩阵测试脚本。自建服务器，跑遍所有场景，断言可观测行为。 |
| `hls_test/app.py` | ~2040 | 路由表、各场景 handler、`CATALOG`、索引页、`/preview` 响应预览端点、HTTP `Handler`、Range 模拟、进程内缓存。 |
| `hls_test/playlists.py` | ~950 | 纯 m3u8 构造器——每种场景形态一个函数。 |
| `hls_test/media.py` | ~1010 | MPEG-TS 复用器（PAT/PMT/PES/CRC32）+ fMP4/ISOBMFF box 构造 + ffmpeg/样例路径解析 + 真实内容缓存与清理。 |
| `hls_test/crypto.py` | ~250 | 纯 Python AES-128-CBC；导入时用 FIPS-197 / NIST SP 800-38A 测试向量自检。 |
| `hls_test/keys.py` | ~47 | 固定密钥、正确/错误密钥、轮换密钥、HMAC 签名/验签。 |
| `hls_test/state.py` | ~155 | 会话、一次性 token、验证码、按 IP 计数、封禁、range 首次使用标记。 |
| `hls_test/i18n.py` | ~228 | `LANGS`、`CATEGORIES`、页面 `UI` 文案、`ZH_SCENARIOS`（id→中文）映射、`resolve_lang()`。 |

### 为什么 `CATALOG` 和 `i18n` 要分开

`app.CATALOG` 是机器可读的场景注册表，其元组保留**稳定的英文**字段：

```
(id, category, item, title, path, expect)
```

- `id` —— 稳定键，从不展示给用户；用于串联 CATALOG ↔ 翻译 ↔ 测试。
- `category` —— `i18n.CATEGORIES` 里的英文分类键之一。
- `item` —— 用户最初的需求编号（1–17），用于追溯。
- `title` / `expect` —— **英文原文**，同时也是翻译缺失时的回退值。
- `path` —— 播放列表 URL（`verify.check_catalog` 也会访问它）。

中文文案**只**放在 `i18n.ZH_SCENARIOS` 里，以 `id` 为键。这样每种语言只有一个
真相来源，且翻译缺失时会退化成英文，而不会让页面或测试挂掉。

---

## 3. 一次请求的完整流程

所有路由都在 `app.py` 的 `Handler._route()` 里完成：

1. 取 `self.path`。若是 RFC 7230 的 **absolute-form** 目标（`http://host/path?q`），
   先剥掉 scheme + authority。客户端在 keep-alive 连接上跟随重定向时会出现这种形式。
2. 切出 `?query`，然后对 path 做 `urllib.parse.unquote()`，因此**路由正则匹配的是
   解码后的路径**。
3. 构造 `Req` 包装对象（`req.base`、`req.q()`、`req.qint()`、`req.ip` …）。
4. **HEAD 按 GET 路由匹配**（RFC 7231：HEAD 的应答与 GET 完全相同，只是不带响应体）。
   `match_method = "GET" if method == "HEAD" else method`。
5. 按注册顺序遍历 `ROUTES`，第一个匹配上的 `(method, regex)` 即命中。
   正则的**位置捕获组**会作为位置参数传给 handler（排在 `req` 之后）。
6. handler 返回 `Resp`；非 200 的情况则 raise `Fail(status, body, mime, headers)`
   —— 包括有意构造的 401/403/404/416/429/500。
7. `Handler._send()` 写回响应；当 `Resp` 指定了 `rmode`（Range 行为）或 `kbps`
   （限速）时就地生效。未捕获的异常会变成 500 并打印 traceback。

---

## 4. 新增一个场景（7 步流程）

请按顺序执行。漏掉 README / i18n 这两步，通常就会出现"测试全绿、页面却是坏的"。

### 第 1 步 —— 在 `hls_test/playlists.py` 里生成播放列表文本

新增一个构造器。**第一个参数永远是 `base`**（绝对源站），即使场景用不到它，
这样 handler 就能无条件地把 `base` 传进去。

```python
def my_scenario(base: str) -> str:
    """One-line English summary. / 一行中文说明。"""
    return media_playlist([f"seg/{i}.ts" for i in range(N_SEG)])
```

### 第 2 步 —— 在 `hls_test/app.py` 里加 handler

每个 URL 一个 handler。路由正则里的**捕获组数量必须**与 handler 的形参数量一致
（见 [§5](#5-url-的各个部分从哪来)）。

```python
def h_my_scenario(req: Req) -> Resp:
    return m3u8(playlists.my_scenario(req.base))

def h_my_scenario_seg(req: Req, i: str) -> Resp:
    return ts(MEDIA.segment("myscenario", int(i)))
```

### 第 3 步 —— 注册路由

在 `app.py` 的路由表段里加 `_r(...)` 行，归到该场景的 `# --- N. 名称 ---` 分隔标题下：

```python
_r("GET", r"^/hls/my-scenario/playlist\.m3u8$", h_my_scenario)
_r("GET", r"^/hls/my-scenario/seg/(\d+)\.ts$", h_my_scenario_seg)
```

### 第 4 步 —— 加一条 `CATALOG` 条目（英文）

```python
("my-scenario", "Delivery", "17", "My scenario title",
 "/hls/my-scenario/playlist.m3u8",
 "One sentence describing what a correct client must do."),
```

放在对应分类的分隔标题之下。`path` 必须与路由完全一致、可访问。

### 第 5 步 —— 在 `hls_test/i18n.py` 里加中文翻译

```python
# 放进 ZH_SCENARIOS
"my-scenario": ("分发", "我的场景标题", "一句话说明正确的客户端应如何表现。"),
```

校验覆盖完整性（必须打印空列表）：

```bash
python -c "from hls_test import app, i18n; print(i18n.missing_ids([s[0] for s in app.CATALOG]))"
```

### 第 6 步 —— 加 README 行

在 `README.md`（英文）和 `README.zh-CN.md`（中文）的场景表里**各加一行**，
并同步更新计数（场景数 / 断言数）。

### 第 7 步 —— 在 `verify.py` 里加断言

写一个 `check_my_scenario(c: Client)` 函数，然后从 `main()` 里调用它。
断言**可观测行为**（状态码、能否解密、PID、PTS、字节区间），而不是内部实现细节。

```python
def check_my_scenario(c: Client) -> None:
    group("24. my scenario")
    st, _, body = c.get("/hls/my-scenario/seg/0.ts")
    check("segment status", st, 200)
    ok("segment is valid TS", ts_sync(body))
```

然后跑矩阵：

```bash
python verify.py --fast
```

> **别忘了登记预览。** 新场景必须出现在 `app.PREVIEWABLE`（无状态 → 直接调 builder）
> 或 `app.STATEFUL_NOTES`（有状态 → 一行说明）之一，否则 `check_preview` 的覆盖断言
> 会失败。详见 [§10](#10-响应预览端点-previewid)。

---

## 5. URL 的各个部分从哪来

路由就是 `ROUTES` 里一个 URL 一条正则。微妙之处在于**捕获组是按位置传给 handler 的**
（排在 `req` 之后）：

| 路由正则 | 组数 | handler 签名 |
| --- | --- | --- |
| `^/hls/vod/seg/(\d+)\.ts$` | 1 | `h_vod_seg(req, i)` |
| `^/hls/renditions/audio/(\w+)/seg/(\d+)\.ts$` | 2 | `h_renditions_audio_seg(req, group, i)` |
| `^/hls/nested/(v1\|v2\|shared)/(\d{3}p)\.m3u8$` | 2 | `h_nested_media(req, tier, _res)` |

规则：

- 捕获组数量**必须等于** handler 形参数量（减掉 `req`）。多一个或少一个都会在
  分发器里抛 `TypeError` → HTTP 500。
- 只想匹配、不想接收的备选项，用 `(?:...)` 写成**非捕获组**。
- 某个组只是为了匹配、handler 不需要它的值时，参数名用下划线开头（`_res`、`_prefix`）
  —— 这是约定，不是硬性要求。

`req.base` 是绝对源站（`http://127.0.0.1:8080`），由 `Host` 头推导而来。正是它让
播放列表能写出绝对 URI。播放列表里的相对 URI 由**客户端**相对"包含它的那个文档"
解析 —— 详见 [§9](#9-踩坑清单改代码前必读) 里的嵌套 master 陷阱。

---

## 6. i18n：增改页面文案

语言解析顺序（`i18n.resolve_lang`，索引页使用）：

1. 显式 `?lang=` 查询参数（页内切换器用的就是这个）；
2. `Accept-Language` 请求头；
3. `DEFAULT_LANG`（`en`）。

三个字典，各司其职：

- `CATEGORIES[lang][英文键]` —— 分类标签。
- `UI[lang][键]` —— 页面框架文案（标题、表头、警示条、模式标签）。
- `ZH_SCENARIOS[id]` —— 每个场景的 `(分类, 标题, 预期行为)` 三元组。

**新增一种语言：**

1. 把语言码加进 `LANGS`，给 `LANG_NAMES` 加个名字。
2. 给 `CATEGORIES` 和 `UI` 各加一份字典。
3. 加一份翻译好的场景映射。
4. 跑覆盖校验（`missing_ids()`），再跑 `verify.py --fast`（catalog 分组会断言
   i18n 覆盖情况与每种语言的页面渲染）。

未知键永远回退英文 —— 翻译缺失绝不会导致 404 或断言失败，最坏情况只是本该显示
中文的地方显示了英文。

---

## 7. 验证框架

`verify.py` 在随机端口上自建一个服务器（或用 `--base` 指向已有实例），然后按固定
顺序执行 `check_*` 函数。基础原语：

| 辅助函数 | 用途 |
| --- | --- |
| `group(name)` | 开始一个带标题的断言块。 |
| `check(name, got, want)` | 断言相等；记录并打印。 |
| `ok(name, cond, detail)` | 断言一个布尔条件。 |
| `Server` / `Client` | 后台服务器线程；HTTP 客户端在出错时返回错误而非抛异常，并手动跟随重定向。 |
| `ts_sync(buf)` | `len % 188 == 0` 且每个包首字节是 `0x47`。 |
| `first_video_pts(buf)` | 从 TS 缓冲里解析出第一个视频 PES 的 PTS。 |
| `pids(buf)` | 出现过的 PID 集合。 |
| `boxes(buf)` | 顶层 ISOBMFF fourcc，并校验 box 链恰好闭合到 EOF。 |
| `playlist_lines(text)` | 把 m3u8 切成去空行、去首尾空白后的行列表。 |

运行方式：

```bash
python verify.py                 # 完整矩阵（含直播滑窗计时检查）
python verify.py --fast          # 跳过唯一需要 sleep 的检查
python verify.py --base http://127.0.0.1:8080   # 测一个已在运行的实例
```

**设计原则：** 只断言下载器能观测到的东西。不要断言私有属性或那些一次合理重构
就可能变动的精确字节偏移。

---

## 8. 媒体生成内部原理

### MPEG-TS（`media.make_ts_segment`）

每个分片都是合法的 188 字节包传输流：

- PAT（PID 0）→ PMT（PID `0x1000`）→ PES 视频（`PID_VIDEO`）+ PES 音频（`PID_AUDIO`）。
- PSI 段用 CRC32（`_crc32_mpeg`）。
- PTS 每个分片前进 `SEG_DUR * 90_000`；`pts_start` 可覆盖这个起点
  （discontinuity 和 `clip B` 场景在用）。
- `streams="v"` / `"a"` 产出纯视频 / 纯音频（供分离式 rendition 使用）。

### fMP4（`media.fmp4_init` / `fmp4_segment`）

手工拼装的 ISOBMFF box：`ftyp`/`moov`(trak>mdia>minf>stbl>stsd)/`moof`(mfhd,traf[tfhd,tfdt,trun])/`mdat`。

有两点关乎正确性：

- **`default-base-is-moof`** —— `tfhd` 带 flags `0x020000`，因此每个 `trun.data_offset`
  都是相对 **moof box 起点**、而不是文件起点。搞错这点会让 byte-range 索引指到错误的字节。
- **双轨**（`tracks="va"`）—— `moof` 里包含**两个 `traf` box**（视频 + 音频）。
  单轨输出与双轨代码路径**字节完全一致**；`init` 614 B / `seg` 121320 B（纯视频）
  对比 1026 B / 183632 B（va）。

### AES-128-CBC（`crypto.aes128_cbc_decrypt` / `encrypt`）

纯 Python 实现，导入时用 FIPS-197 与 NIST SP 800-38A 向量自检。
它**很慢**（~3.5 MB ≈ 50 s），所以任何要加密批量数据的地方，要么缓存结果
（`app.enc_segment`、`app.big_file`），要么减少分片数（`aes-byterange` 组合场景
用 `AES_BR_N = 4` 个 1 秒分片）。

---

## 9. ffmpeg 路径解析（real 模式）

默认情况下分片都是**合成**的（纯 Python 生成，确定性、免依赖）。`fmp4-av` 等
需要真实 H.264/AAC 的场景会走 **real 模式**：调用 ffmpeg 现场转码一次并缓存。

为了不让整个项目绑定在"系统 PATH 里必须有 ffmpeg"上，`media._resolve_ffmpeg()`
按以下**优先级**查找（`candidate` 是 `--ffmpeg` 或 `HLS_FFMPEG` 传入的值）：

1. 命令行 `--ffmpeg <path>` —— 显式指定
2. 环境变量 `HLS_FFMPEG`
3. 项目内 `./tools/ffmpeg/ffmpeg[.exe]` 或 `./tools/ffmpeg/bin/ffmpeg[.exe]`
4. 系统 `PATH`（`shutil.which("ffmpeg")`）

`<path>` 既可以是 **exe 本体**，也可以是它**所在的目录**（会自动补上文件名）。

```bash
python server.py                              # 若 PATH 无 ffmpeg，会自动探测 ./tools/ffmpeg
python server.py --ffmpeg /opt/ffmpeg/bin     # 指定目录
python server.py --ffmpeg C:/Dependency/ffmpeg/bin/ffmpeg.exe   # 指定 exe
```

**推荐做法**：把 ffmpeg 目录拷进 `tools/ffmpeg/`（已加进 `.gitignore`，不会入库），
这样"零配置"即可用；不拷也不会报错 —— real 场景自动回退到合成模式并标记
`is_real: False`。启动时会打印实际使用的 ffmpeg 路径。

**索引页顶部横幅**（`h_index` 里的 `ff_banner`）会同步显示这个状态：找到 -> 绿色横幅 +
解析到的路径；没找到 -> 琥珀色横幅 + 查找位置与四种启用方式。文案在 `i18n.UI` 的
`ff_*` 键里；`verify.check_ffmpeg_banner` 会断言横幅存在、变体正确、路径可见。

> 注意：`-hls_fmp4_init_filename` / `-hls_segment_filename` 的**值是文件名**，
> 会被拼到 m3u8 所在目录后面。传绝对路径会得到双重路径而失败。正确做法是把
> 子进程 `cwd` 设为输出目录，只传相对文件名（见 `media._prepare_real`）。

## 9.1 样例视频（`--sample` / `tools/sample/`）

real 模式的内容来源可以换成**用户自己的视频**。`media._resolve_sample()` 按同样的
思路查找，优先级（`candidate` 是 `--sample` 或 `HLS_SAMPLE`）：

1. 命令行 `--sample <path>` —— 视频文件或其所在目录
2. 环境变量 `HLS_SAMPLE`
3. 项目内 `./tools/sample/source.{mp4,mkv,mov,webm,ts}`（取第一个存在的）

`tools/sample/` 已加进 `.gitignore`（`tools/sample/*`，仅放行 `README.md`）。
找到样例后，`_prepare_real()` 转交 `_prepare_real_from_sample()` 转码；找不到则
照旧用内置 `testsrc2`/`sine`。

关键实现约束：

- **`-stream_loop -1` + 固定 `-t 72`**：任意长度/帧率的样例都产出可预期分片数，
  保住 `_real_file()` 的 `index % 12` 复用契约（下游 handler 零改动）。
- **关键帧对齐**：`-sc_threshold 0 -force_key_frames expr:gte(t,n_forced*6)` 让每个
  分片边界都落在 IDR 上，得到干净的 6s 定长分片。**不加这两项时** ffmpeg 只在样例
  自带的关键帧处切，分片时长会飘（实测 5s 样例出 8.33s/片）。
- **无音轨兜底（无 ffprobe）**：本机只有 `ffmpeg.exe`、没有 `ffprobe`。av/audio
  两条命令先用 `-map 0:a:0?`（可选音轨，缺失不报错），整条命令因无音轨失败时
  再回落 `anullsrc` 生成的静音轨。
- **`.source` 标记文件**：缓存目录内的 `.source`（JSON）记录来源（`sample`/`builtin`）
  与样例的 path+mtime+size。`cache_is_stale()` 据此判断"放了样例但旧缓存仍在"
  → `__init__` 时自动先清后重建，避免静默提供旧内容。

端点为 `GET /sample.json`（只读状态）与 `POST /sample/rebuild`（清空并用当前样例
重建，无请求体）。索引页顶部**第二行**是样例面板（`_sample_panel_html`），与 ffmpeg
横幅复用 `.ff`/`button.clr` 样式。断言见 `verify.check_sample`。

> **注意**：样例面板与 ffmpeg 横幅**都用 `.ff ok`/`.ff no` 样式**，因此
> `check_ffmpeg_banner` 必须按 `id="ff-banner"` 限定范围来断言，否则会把样例面板
> 的 class 也算进去而误判"两个变体同时出现"。

## 9.2 缓存清理的容错与护栏

`POST /cache/clear` 走 `media.MediaProvider.clear_cache()`。它有两个必须保留的性质：

1. **逐文件容错**：删除循环用 `except BaseException`（不是 `except OSError`）。
   某些环境会在批量删除超限时抛 `SystemExit` —— 它是 `BaseException` 而非
   `Exception`，只兜 `OSError`/`Exception` 会让它穿透到 HTTP 层、直接把连接掐断
   （前端表现为 **HTTP 000**，而不是可读的错误）。失败的文件记进返回值的
   `errors` 列表。
2. **有残留则不重建**：只要有文件没删干净（`errors` 非空），就跳过重建并置
   `regen_skipped=True`，避免"声称成功却留着旧文件"。

`h_cache_clear` 再包一层 `try/except BaseException` 返回结构化 JSON（`ok`/`errors`/
`error`），前端据此显示"部分清理 + 失败数 + 原因"。`Handler._route` 也补了
`except (KeyboardInterrupt, SystemExit)` 分支作为纵深防御（转成 503 而非断连）。

> **环境陷阱**：本仓库的测试环境通过 `sitecustomize.py` 把 `os.remove` 换成带批量
> 计数护栏的版本，超阈值抛 `SystemExit`。**真实用户机器上没有这个注入**，清理会
> 正常工作。测试里不要拿真实缓存做删除实验 —— 用 `tempfile.mkdtemp()` 造小目录，
> 并用 `unittest.mock` 打桩 `os.remove` 来模拟单文件失败与 `SystemExit`（见
> `verify.check_cache_panel`）。

---

## 10. 响应预览端点 /preview/{id}

索引页每一行右侧有一个"查看响应"按钮：点击后前端 `fetch('/preview/<id>')`，
把该场景的**响应形态**展开显示在行下方（状态码、关键响应头、正文、说明）。

后端 `h_preview(req, sid)` 返回 JSON：

```json
{
  "id": "vod",
  "title": "普通 VOD（完整列表分层）",
  "path": "/hls/vod/playlist.m3u8",
  "status": 200,
  "content_type": "application/vnd.apple.mpegurl",
  "content_length": 304,
  "headers": { "Content-Type": "...", "Content-Length": "304" },
  "body": "#EXTM3U\n...",
  "note": null
}
```

关键在于**预览绝不产生副作用**。实现方式是一张白名单表：

- `app.PREVIEWABLE` —— id → 一个**纯 builder 调用**（如 `_pv_vod(base)`）。
  只登记**无状态**场景；预览直接调用 builder 拿文本，不经过 handler、不碰 `STATE`。
- `app.STATEFUL_NOTES` —— id → 一行**静态说明**（为什么不能预览、以及正确的取法示例）。
  有状态场景（`onetime` / `ratelimit` / `ban` / `captcha` / `session` / `sign` …）
  走这个分支，`body` 为空、`status` 为 `null`，只回 `note` 与 `expect`。

`verify.py` 的 `check_preview()` 用三条断言守住这个契约：

1. 无状态场景 → 200，且 `body` 以 `#EXTM3U` 开头；
2. 有状态场景 → 带 `note`、`body` 为空，且**连续两次调用字节完全一致**（证明没改状态）；
3. `PREVIEWABLE ∪ STATEFUL_NOTES` **恰好覆盖 `CATALOG` 的每一个 id**（不漏、不拼错）。

新增场景时，**别忘了把它登记进这两张表之一** —— 否则 `check_preview` 会失败。

---

## 11. 踩坑清单（改代码前必读）

1. **捕获组数量必须与 handler 签名一致。** 这是 500 的头号成因。数清楚组数；
   不需要的组用 `(?:...)`。

2. **CATALOG 保留英文，中文放 `i18n.ZH_SCENARIOS`。** 千万别往 `CATALOG` 里写中文
   —— 它同时是回退值。

3. **嵌套 master 每一层都会改变解析基准。** 相对 URI 相对*包含它的那个文档*解析，
   所以顶层 master 里合法的 URI，往下一两层就可能 404。各层路由基准为：
   `/hls/nested/master.m3u8` → `/hls/nested/{tier}/sub.m3u8` →
   `/hls/nested/{tier|shared}/{res}.m3u8`。

4. **播放列表里的 SCTE-35 是 hex 字符串，不是 base64。** `SCTE35-OUT`/`SCTE35-IN`
   承载的是大端二进制 `hex` 序列；不存在 `SCTE-35=` 这个属性名。

5. **`CLOSED-CAPTIONS` 不能带 `URI`。** 它通过 `INSTREAM-ID` 引用带内字幕；
   给它加 `URI` 是违反规范的。

6. **`fmp4_segment` 的偏移是相对 moof 的。** 见上面的 `default-base-is-moof`。

7. **HEAD 必须按 GET 路由。** 若只对 GET 特判，HEAD 探测就会挂。

8. **一切依赖时间的东西都要传参进来，而不是在 builder 内部读时钟。**
   保持 `playlists.py` 纯净，测试才可复现。

9. **畸形播放列表以 `bytes` 返回。** BOM / CRLF / 小写标签需要字节级控制；
   返回 `str` 会把它们规范化掉。

10. **未知 range mode → 404，不是 500。** 每个接受 `mode` 的 handler 都要用
    `RANGE_MODES` 校验它。

---

## 12. 命令速查

```bash
# 启动服务器
python server.py                    # http://127.0.0.1:8080/

# 指定 ffmpeg（real 模式）；不指定则依次找 HLS_FFMPEG、./tools/ffmpeg、PATH
python server.py --ffmpeg C:/Dependency/ffmpeg/bin

# 指定样例视频；不指定则找 HLS_SAMPLE、./tools/sample/source.{mp4,mkv,...}
python server.py --sample /path/to/clip.mkv

# 语法检查全部模块
python -c "import py_compile,glob; [py_compile.compile(f,doraise=True) for f in glob.glob('hls_test/*.py')+['server.py','verify.py']]; print('OK')"

# 跑验证矩阵
python verify.py --fast

# 体检：路由数与场景数
python -c "from hls_test import app; print(len(app.ROUTES), len(app.CATALOG))"

# i18n 覆盖（必须是空列表）
python -c "from hls_test import app, i18n; print(i18n.missing_ids([s[0] for s in app.CATALOG]))"

# 预览覆盖（必须是空列表）：每个场景都要登记进 PREVIEWABLE 或 STATEFUL_NOTES
python -c "from hls_test import app; c=set(app.PREVIEWABLE)|set(app.STATEFUL_NOTES); print([s[0] for s in app.CATALOG if s[0] not in c])"

# 手动看某个场景的响应预览
curl "http://127.0.0.1:8080/preview/vod?lang=zh"
```

---

## 13. 网页中 / 英文切换

索引页（`http://127.0.0.1:8080/`）**右上角**有语言切换，三种方式任选：

1. **点右上角切换链接** —— 页面上方右侧显示 `语言: English · 中文`，点一下即可切换。
   底层就是把 URL 换成 `?lang=zh` 或 `?lang=en`，所以可以直接加书签。
2. **手动加查询参数** —— `http://127.0.0.1:8080/?lang=zh`。`/scenarios.json` 同理：
   `/scenarios.json?lang=zh`。
3. **不改 URL** —— 只要不带 `?lang=`，服务器就按浏览器发来的 `Accept-Language`
   自动协商（浏览器是中文环境就默认显示中文）。

优先级：`?lang=` > `Accept-Language` > 英文。
