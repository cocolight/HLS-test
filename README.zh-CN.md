# HLS 测试源

一个自包含的 HLS 源站，**故意复现协议里那些麻烦的部分**：47 个场景，覆盖加密、  
密钥分发、访问控制、源端缺陷、容器格式与 HTTP 断点续传行为。

它的用途是测试**客户端/下载器的处理逻辑**，而不是播放。每个场景都有稳定的 URL  
和明确的预期行为，可以直接把下载器指过来，逐项验证它对各种异常的处理是否正确。

- 零依赖 —— 只用 Python 3.10+ 标准库
- 不联网，不需要任何外部媒体文件
- `verify.py` 会自建实例，断言全部 552 项可观测行为

English: [README.md](README.md)

## 快速开始

```bash
python server.py                 # http://127.0.0.1:8080/
python verify.py                 # 跑完整验证矩阵（自带服务器）
python verify.py --base http://127.0.0.1:8080   # 或测一个已在运行的实例
```

打开 `http://127.0.0.1:8080/` 看场景索引，`/scenarios.json` 是机器可读的目录。

索引页每一行右侧有「查看响应」按钮：点击后会在该行下方就地展开该场景的
**响应形态**（状态码、关键响应头、正文），数据来自 `/preview/{id}`。有状态场景
（`onetime`、`ratelimit`、`ban`、`captcha` 等）只给一行说明、不实际执行，
因此预览**绝不产生副作用**。

## 场景清单

| 分类     | 场景                                | URL                                         | 预期行为                                                                                          |
| ------ | --------------------------------- | ------------------------------------------- | --------------------------------------------------------------------------------------------- |
| 加密     | AES-128，显式 IV                     | `/hls/aes128/playlist.m3u8`                 | 可解密；明文首字节必须是 0x47（TS 同步字）。                                                                    |
| 加密     | AES-128，IV 取自媒体序号                 | `/hls/aes128/msn-iv/playlist.m3u8`          | 无 IV 属性 → IV 必须等于分片的媒体序号。                                                                     |
| 加密     | AES-128 多码率 + SESSION-KEY         | `/hls/aes128/master.m3u8`                   | master 含 `EXT-X-SESSION-KEY`；两个变体均加密。                                                         |
| 加密     | 密钥 URL 带时效 + 签名                   | `/hls/key-expiry/playlist.m3u8?ttl=30`      | `?ttl` 秒后密钥接口 403。TTL 可配（最小 5）。                                                               |
| 加密     | SAMPLE-AES（identity keyformat）    | `/hls/sample-aes/playlist.m3u8`             | 下载器应拒绝：不支持的加密方式。                                                                              |
| 加密     | SAMPLE-AES + FairPlay（`skd://`）   | `/hls/fairplay/playlist.m3u8`               | DRM；应判定为不可解密，而非解析错误。                                                                          |
| 加密     | Widevine KEYFORMAT + `data:` pssh | `/hls/widevine/playlist.m3u8`               | DRM；该 `data:` URI 是 pssh box，不是密钥。                                                            |
| 加密     | 密钥需要特定请求头                         | `/hls/key-header/playlist.m3u8`             | 密钥接口需 `X-Api-Key`，否则 403。                                                                     |
| 加密     | 每片轮换 AES 密钥                       | `/hls/key-rotation/playlist.m3u8`           | 每片一个 `EXT-X-KEY`；沿用旧密钥会失败。                                                                    |
| 加密     | AES-128 + fMP4（init 明文）           | `/hls/aes-fmp4/playlist.m3u8`               | `EXT-X-KEY` 在 `EXT-X-MAP` 之前；init 段明文，只有 `.m4s` 加密。                                          |
| 加密     | AES-128 + `EXT-X-BYTERANGE`         | `/hls/aes-byterange/playlist.m3u8`          | 区间索引的是**密文**；每段独立 PKCS7 填充，合并后再解密必然失败。                                                      |
| 加密     | `EXT-X-DISCONTINUITY` 处换密钥         | `/hls/aes-disc/playlist.m3u8`               | Clip A/B 用不同密钥；跨边界沿用同一密钥会解出乱码。                                                            |
| 播放列表结构 | 混合 URI 形态 + 重定向                   | `/hls/paths/master.m3u8`                    | 相对 / 点段 / 百分号编码 / 查询串 / 绝对 / 协议相对 / 302，变体与分片 URI 都有。                                         |
| 播放列表结构 | 多层嵌套 master（三层）                   | `/hls/nested/master.m3u8`                   | `master -> sub-master -> media`。sub-master 内混用相对、点段与父级（`../`）URI；沿用顶层 base 的客户端会 404。            |
| 播放列表结构 | 用 `EXT-X-BYTERANGE` 切 TS          | `/hls/byterange/playlist.m3u8`              | 所有分片都是同一个 `big.ts` 的字节区间。                                                                     |
| 播放列表结构 | `EXT-X-DEFINE` 变量 URI             | `/hls/define/playlist.m3u8`                 | 分片 URI 使用 `{$segbase}` 替换（v8 特性）。                                                             |
| 播放列表结构 | 变长 `EXTINF` + `TARGETDURATION` 不符 | `/hls/variable/consistent/playlist.m3u8`    | 时长从 0.8s 到 12.0s 不等。`/mismatch/` 声明的 `TARGETDURATION:6` 小于真实最大值 12.0，属常见源端缺陷。        |
| 播放列表结构 | 不规范 m3u8                          | `/hls/malformed/crlf/playlist.m3u8`         | 变体：`crlf bom no-version unknown-tags spacing lowercase no-extm3u bad-attrs all`。分片本身正常，考的是解析。 |
| 播放列表结构 | 剧集：多集 + 上/下一集 + 共享密钥        | `/hls/series/master.m3u8`                   | 3 集分片数各不相同，全集共用一把 AES-128 密钥；用自定义 `EXT-X-SERIES-PREV` / `EXT-X-SERIES-NEXT` 标签表达先/后集关系，客户端需能忽略未知标签。 |
| 分发     | 普通 VOD（`ENDLIST`）                 | `/hls/vod/playlist.m3u8`                    | 基准正常路径。                                                                                       |
| 分发     | 直播滑窗（6 × 6s）                      | `/hls/live/playlist.m3u8`                   | 无 `ENDLIST`；`MEDIA-SEQUENCE` 递增；含 PDT。                                                        |
| 分发     | Event 列表 → 转为 VOD                 | `/hls/live/event.m3u8`                      | 窗口增长到 12 片后追加 `ENDLIST`。                                                                      |
| 访问控制   | Referer / UA / Cookie 校验          | `/hls/guard/playlist.m3u8?auth=all`         | `?auth=referer\|ua\|cookie\|all`；缺失或错误 → 403。                                                 |
| 访问控制   | 签名 URL + 时效                       | `/hls/sign/playlist.m3u8?ttl=60`            | HMAC 签名的 `?exp&s`；超时 403（默认 60s，最小 5）。                                                        |
| 访问控制   | 必须带 Session                       | `/hls/session/playlist.m3u8`                | 先 `GET /hls/session/start`；分片需 `Cookie HLSSESS=` 或 `?session=`，否则 401。                        |
| 访问控制   | 限速                                | `/hls/ratelimit/playlist.m3u8?n=5`          | 30 秒内超过 `?n` 次分片请求 → 429 + `Retry-After`。                                                     |
| 访问控制   | 带宽限流                              | `/hls/throttle/playlist.m3u8?kbps=64`       | 分片按 `?kbps` 速率下发（默认 64）。                                                                      |
| 访问控制   | 突发后 IP 封禁                         | `/hls/ban/playlist.m3u8?n=8`                | 60 秒内超 `?n` 次 → 封禁 403 持续 60 秒。                                                               |
| 访问控制   | 验证码拦截                             | `/hls/captcha/playlist.m3u8`                | 首个分片返回 403 HTML；在 `/hls/captcha/solve?code=1234` 解锁。                                          |
| 源端缺陷   | 缺失 / 500 / 乱码 / 截断                | `/hls/broken/playlist.m3u8`                 | seg2=404，seg4=500，seg6=乱码，seg8=截断一半。                                                          |
| 源端缺陷   | 含错误密钥分片的 AES 流                    | `/hls/broken-enc/playlist.m3u8`             | seg6 解出乱码（下游 PKCS7 去填充报错）。                                                                    |
| 源端缺陷   | `EXT-X-GAP` 占位                    | `/hls/gap/playlist.m3u8`                    | 第 3、4 片标记 GAP；应跳过而非请求。                                                                        |
| 源端缺陷   | 同一广告区间用三种信令标记                 | `/hls/ads/playlist.m3u8`                    | 用 `EXT-X-DATERANGE`（`SCTE35-OUT`/`SCTE35-IN`，十六进制二进制）、`EXT-X-CUE-OUT`/`CUE-IN` 与 `EXT-X-GAP` 同时标记。 |
| 源端缺陷   | PTS 跳变 + `EXT-X-DISCONTINUITY`    | `/hls/discontinuity/playlist.m3u8`          | Clip B 的 PTS 回退到 Clip A 末值之下。                                                                 |
| 源端缺陷   | 一次性分片 URL                         | `/hls/onetime/playlist.m3u8`                | 每个分片 URL 只能用一次，之后 403；只能重新拉列表。                                                                |
| 断点续传   | RFC 正确的 `Range`                   | `/hls/range/ok/playlist.m3u8`               | `Accept-Ranges: bytes`，`Content-Range` 正确，越界返回 416。                                           |
| 断点续传   | 不支持 Range                         | `/hls/range/none/playlist.m3u8`             | `Accept-Ranges: none`；永远 200 + 完整响应体。                                                         |
| 断点续传   | 声称支持却忽略 Range                     | `/hls/range/ignored/playlist.m3u8`          | `Accept-Ranges: bytes` 但永远 200 + 完整响应体，且无 `Content-Range`。                                    |
| 断点续传   | `206` 但总长度是错的                     | `/hls/range/badtotal/playlist.m3u8`         | `Content-Range` 的总长只有真实值一半；信了就会损坏文件。                                                          |
| 断点续传   | 多区间响应损坏                           | `/hls/range/multipart-broken/playlist.m3u8` | `bytes=a-b,c-d` 只返回第一段，且不带 `multipart/byteranges`。                                            |
| 断点续传   | 只能续一次，之后归零                        | `/hls/range/restarts/playlist.m3u8`         | 首次 `Range` 生效，之后返回 200 完整响应体。                                                                 |
| 断点续传   | 续传不可能（三重机制叠加）                  | `/hls/resume-triple/playlist.m3u8`          | 一次性 URL + 限时密钥 + 一次性 `Range`。正确行为是识别出无法续传并重下，而非拼接出损坏文件。                              |
| 容器     | 音视频分离                             | `/hls/av-separate/master.m3u8`              | `EXT-X-MEDIA` 音频组 + 纯视频变体 + 纯音频变体。                                                            |
| 容器     | 多音轨 + 字幕 + 隐藏字幕                   | `/hls/renditions/master.m3u8`               | 两个 `AUDIO` 组（`aud`/`comm`）、一个 `SUBTITLES` 组（`en`/`zh`）、一个 `CLOSED-CAPTIONS` 组（**无** URI）。字幕为带 `X-TIMESTAMP-MAP` 的 WebVTT。 |
| 容器     | fMP4（`EXT-X-MAP` + `.m4s`）        | `/hls/fmp4/playlist.m3u8`                   | 必须先取 init 段，并与媒体分片重新封装。                                                                       |
| 容器     | fMP4 双轨（视频 + 音频）                  | `/hls/fmp4/av/playlist.m3u8`                | init 含两条 `trak`（`avc1`+`mp4a`）；每个 `.m4s` 的 `moof` 含两个 `traf`（track_ID 1、2），data offset 各自独立。 |
| 容器     | fMP4 MAP + 字节区间分片                 | `/hls/fmp4/byterange/playlist.m3u8`         | init 与媒体分片都是同一个 `combined.m4s` 的区间。                                                           |

## 凭据

固定值，索引页上同样列出。

| 用途                      | 值                                  |
| ----------------------- | ---------------------------------- |
| `X-Api-Key`（key-header） | `hls-test-api-key`                 |
| Referer（guard）          | `https://portal.example.com/`      |
| User-Agent 前缀（guard）    | `HLSClient/1.0`                    |
| Cookie（guard）           | `access=granted`                   |
| 验证码                     | `1234`                             |
| AES-128 密钥              | `000102030405060708090a0b0c0d0e0f` |
| AES-128 IV              | `000102030405060708090a0b0c0d0e0f` |

## 内容模式

分片是程序化生成的**结构合法但不可解码**的 MPEG-TS（0x47 同步、PAT/PMT/PES、  
PTS 可控）或 fMP4（box 链在文件末尾精确闭合）。这是有意为之：这些场景考的是  
下载器逻辑，不是解码器。

只要找得到 `ffmpeg`，服务器就会在首次使用时编码真实的 H.264/AAC 内容并缓存到  
`media_cache/`；删掉该目录即可重新生成。

索引页**顶部有一块状态横幅**，明确告诉你是否找到了 ffmpeg；找到时还会显示实际使用的  
路径。没找到时，横幅会列出所有查找位置和四种启用方式——不用再猜"有没有真实视频"。

ffmpeg 按以下顺序查找（优先级由高到低）：

1. `--ffmpeg PATH` —— 可执行文件，或它所在的目录；
2. `$HLS_FFMPEG` —— 同上，作为环境变量；
3. `./tools/ffmpeg` 或 `./tools/ffmpeg/bin` —— 把 ffmpeg 放进项目内；
4. 系统 `PATH`。

所以**不受系统 PATH 限制**，项目内自带或绝对路径都能用：

```bash
python server.py --ffmpeg /opt/ffmpeg/bin
python server.py --ffmpeg C:/Dependency/ffmpeg/bin/ffmpeg.exe
```

启动横幅会打印实际解析到的 ffmpeg 路径；没找到时显示 `synthetic`。

### 样例视频

除了内置测试图，你还可以用自己的视频驱动真实内容缓存。在 `tools/sample/` 里放  
**一个**视频（按顺序取第一个存在的：`source.mp4`、`source.mkv`、`source.mov`、  
`source.webm`、`source.ts`），然后在索引页点「**处理 / 重建**」——服务器会用  
ffmpeg 把它转码进 `media_cache/real/`，所有真实模式场景就都提供你的内容了。  
删掉文件再重建即可回到内置测试图。

可用 `--sample <路径>` 或 `$HLS_SAMPLE` 覆盖位置（文件或目录均可）。输出统一为  
416x234 @ 25fps、关键帧对齐的 6 秒定长分片；样例没有音轨时会自动补静音轨，  
纯音频场景不受影响。视频不入库——只有 `tools/sample/README.md` 会被跟踪。

### 缓存面板

索引页顶部还会显示真实内容缓存大小与「**清理缓存**」按钮。清理是逐文件容错的：  
某个文件删不掉时（被播放器占用，或被沙箱的删除护栏拦下），其余文件照删，界面会  
如实显示已删数量与首个失败原因，而不是笼统的「失败」。端点为 `GET /cache.json`  
（只读统计）与 `POST /cache/clear`（可加 `?regenerate=0` 只清不重建）。

## 验证

`verify.py` 会在随机端口起一个服务器，逐项检查每个场景的可观测行为——状态码、  
AES 分片可解密性、PTS 时间线、Range 语义、TS 同步对齐、fMP4 box 结构、  
不规范列表的形态、带宽限流，以及无副作用的 `/preview/{id}` 预览端点：

```
checks: 552   passed: 552   failed: 0
```

`--fast` 会跳过直播滑窗的计时检查（唯一需要 sleep 的部分），报告 550 项。

## 目录结构

```
server.py               入口（argparse，启动 HTTP 服务）
verify.py               全矩阵验证脚本
docs/DEVELOPING.md      架构说明、新增场景流程、踩坑清单（英文）
docs/DEVELOPING.zh-CN.md  同上，中文版
hls_test/app.py         路由、场景处理器、目录、索引页
hls_test/playlists.py   m3u8 生成（每个场景一个函数）
hls_test/media.py       MPEG-TS 复用器 + fMP4 box 构造
hls_test/crypto.py      纯 Python AES-128-CBC（导入时自检）
hls_test/keys.py        密钥派生 + HMAC 签名
hls_test/state.py       session、一次性 token、限流、封禁
hls_test/i18n.py        页面文案 + 按 id 索引的中文翻译
```

## 网页中英文切换

索引页与 `/scenarios.json` 支持中英切换，三种方式（按优先级）：

1. **点切换链接** —— 索引页**右上角**显示 `语言: English · 中文`，点一下即可切换。
   底层就是把 URL 换成 `?lang=zh` / `?lang=en`，所以可以直接加书签。
2. **手动加参数** —— `http://127.0.0.1:8080/?lang=zh`
   （`/scenarios.json?lang=zh` 同理）。
3. **什么都不做** —— 不带 `?lang=` 时，服务器按浏览器的 `Accept-Language` 自动协商。

优先级：`?lang=` > `Accept-Language` > 英文。

## 参与开发

架构分层、新增场景的分步流程与易错点，见 [docs/DEVELOPING.zh-CN.md](docs/DEVELOPING.zh-CN.md)（中文，推荐）。
英文版见 [docs/DEVELOPING.md](docs/DEVELOPING.md)。

## 许可

MIT —— 见 [LICENSE](LICENSE)。
