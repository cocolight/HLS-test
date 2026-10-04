"""Bilingual UI strings for the index page and /scenarios.json.
索引页与 /scenarios.json 的中英双语文案。

Design: the machine-readable catalog (:data:`hls_test.app.CATALOG`) keeps its
stable English ``id`` / ``category`` / ``path`` / ``item`` fields. This module
maps ``id`` -> {category, title, expect} per language, so nothing about routing
or verification depends on the display language.

设计：机器可读目录（:data:`hls_test.app.CATALOG`）保留稳定的英文字段
``id`` / ``category`` / ``path`` / ``item``。本模块按语言把 ``id`` 映射到
{category, title, expect}。因此路由与验证逻辑完全不依赖展示语言——翻译
缺失只会回退到英文，不会导致 404 或断言失败。

Language is resolved in this order / 语言解析优先级:
1. explicit ``?lang=`` query parameter (what the on-page switcher uses)
   显式 ``?lang=`` 查询参数（页内切换器用的就是这个）
2. ``Accept-Language`` request header (browser preference)
   ``Accept-Language`` 请求头（浏览器偏好）
3. ``DEFAULT_LANG`` (English) / 默认英文

Adding a language means adding one dict to :data:`TRANSLATIONS`; the English
entry is the schema reference and every id in CATALOG must be present.
新增语言只需在 :data:`TRANSLATIONS` 里加一份字典；英文是 schema 基准，
CATALOG 中的每个 id 都必须出现（可用 :func:`missing_ids` 校验）。
"""

from __future__ import annotations

LANGS = ("en", "zh")
DEFAULT_LANG = "en"
LANG_NAMES = {"en": "English", "zh": "中文"}

# Category labels (translated once, reused by the table's section rows).
# 分类标签：翻一次，表格的分节行复用（同一分类下的行共享）。
CATEGORIES = {
    "en": {
        "Encryption": "Encryption",
        "Playlist structure": "Playlist structure",
        "Delivery": "Delivery",
        "Access control": "Access control",
        "Source defects": "Source defects",
        "Resume": "Resume",
        "Container": "Container",
    },
    "zh": {
        "Encryption": "加密",
        "Playlist structure": "播放列表结构",
        "Delivery": "分发",
        "Access control": "访问控制",
        "Source defects": "源端缺陷",
        "Resume": "断点续传",
        "Container": "容器",
    },
}

# Chrome / page-level strings. / 页面框架级文案（标题、表头、警示语等）。
UI = {
    "en": {
        "title": "HLS test scenarios",
        "lang_label": "Language",
        "mode_label": "Content mode",
        "mode_real": "real (ffmpeg)",
        "mode_synthetic": "synthetic",
        "json_label": "machine-readable",
        "warn": ("Segments are valid MPEG-TS (0x47 sync). In <b>synthetic</b> "
                 "mode they are not decodable video &mdash; this server tests "
                 "<i>downloader logic</i>, not playback. Put <code>ffmpeg</code> "
                 "on PATH and delete <code>media_cache/</code> to get real, "
                 "playable content."),
        "th_no": "#",
        "th_scenario": "Scenario",
        "th_url": "URL",
        "th_expect": "Expected behaviour",
        "th_use": "Used by",
        "th_value": "Value",
        "h_credentials": "Credentials / constants",
        "h_manual": "Verify an AES segment by hand",
        "url_tip": ("copy the absolute URL from the address bar after opening"),
        "manual_note": "# first byte must be 47",
        "manual_comment": "# first byte must be 47",
        "btn_show": "response",
        "btn_hide": "hide",
        "ph_loading": "loading...",
        "ph_error": "could not load preview",
        "ph_note": "note",
        "ph_body": "body",
        # --- ffmpeg status banner / ffmpeg 状态横幅 ---
        "ff_h": "ffmpeg check",
        "ff_ok": "ffmpeg found &mdash; real H.264/AAC video mode is available",
        "ff_ok_path": "using",
        "ff_ok_note": ("Real <code>fmp4-av</code> scenarios will transcode once "
                       "and cache the result."),
        "ff_no": "ffmpeg not found &mdash; serving synthetic content only",
        "ff_no_note": ("Real video is unavailable, but every downloader-logic "
                       "scenario still works. To enable it, do any one of these:"),
        "ff_no_hint1": ("drop an <code>ffmpeg</code> build at "
                        "<code>tools/ffmpeg/ffmpeg[.exe]</code> or "
                        "<code>tools/ffmpeg/bin/ffmpeg[.exe]</code>"),
        "ff_no_hint2": "set the <code>HLS_FFMPEG</code> environment variable",
        "ff_no_hint3": "run <code>python server.py --ffmpeg &lt;dir|exe&gt;</code>",
        "ff_no_hint4": "put <code>ffmpeg</code> on your <code>PATH</code>",
        "ff_no_tail": ("then delete <code>media_cache/</code> and reload this page."),
        "ff_look": "looked in",
        # --- media cache panel / media 缓存面板 ---
        "cache_h": "Media cache",
        "cache_files": "files",
        "cache_dir": "dir",
        "cache_note": ("ffmpeg-generated real content, cached under this dir. "
                       "Clearing regenerates it right away."),
        "cache_btn": "Clear cache",
        "cache_confirm": ("Delete every cached media file? It will be "
                          "regenerated immediately (may take a few seconds)."),
        "cache_busy": "clearing & regenerating...",
        "cache_done": "cleared",
        "cache_partial": "partially cleared",
        "cache_errs": "failed",
        "cache_fail": "clear failed",
        # --- sample video panel / 样例视频面板 ---
        "sample_h": "Sample video",
        "sample_found": ("sample found &mdash; rebuild transcodes it into "
                         "the real cache"),
        "sample_none": "no sample &mdash; using the built-in test pattern",
        "sample_path": "using",
        "sample_active": "current cache built from sample",
        "sample_inactive": "current cache is built-in",
        "sample_hint": ("drop a video at tools/sample/source.mp4 "
                        "(.mkv/.mov/.webm/.ts also accepted), then click "
                        "rebuild"),
        "sample_btn": "Process / rebuild",
        "sample_busy": "transcoding sample...",
        "sample_done": "rebuilt from sample",
        "sample_fail": "rebuild failed",
    },
    "zh": {
        "title": "HLS 测试场景",
        "lang_label": "语言",
        "mode_label": "内容模式",
        "mode_real": "真实（ffmpeg）",
        "mode_synthetic": "合成",
        "json_label": "机器可读目录",
        "warn": ("分片是结构合法的 MPEG-TS（0x47 同步字节）。在 <b>合成</b> "
                 "模式下它们不是可解码的视频——本服务器测的是<i>下载器逻辑</i>，"
                 "不是播放。把 <code>ffmpeg</code> 放进 PATH 并删除 "
                 "<code>media_cache/</code> 即可获得真实可播放内容。"),
        "th_no": "#",
        "th_scenario": "场景",
        "th_url": "URL",
        "th_expect": "预期行为",
        "th_use": "用途",
        "th_value": "值",
        "h_credentials": "凭据 / 常量",
        "h_manual": "手工校验一个 AES 分片",
        "url_tip": "打开后从地址栏复制完整 URL",
        "manual_note": "# 首字节必须是 47",
        "manual_comment": "# 首字节必须是 47",
        "btn_show": "查看响应",
        "btn_hide": "收起",
        "ph_loading": "加载中…",
        "ph_error": "无法加载预览",
        "ph_note": "说明",
        "ph_body": "响应体",
        # --- ffmpeg 状态横幅 ---
        "ff_h": "ffmpeg 检测",
        "ff_ok": "已找到 ffmpeg —— 真实 H.264/AAC 视频模式可用",
        "ff_ok_path": "正在使用",
        "ff_ok_note": "真实内容场景（如 <code>fmp4-av</code>）会转码一次并缓存结果。",
        "ff_no": "未找到 ffmpeg —— 当前只用合成内容",
        "ff_no_note": ("真实视频不可用，但所有下载器逻辑场景仍照常工作。"
                       "要启用它，任选一种方式："),
        "ff_no_hint1": ("把 <code>ffmpeg</code> 放到 "
                        "<code>tools/ffmpeg/ffmpeg[.exe]</code> 或 "
                        "<code>tools/ffmpeg/bin/ffmpeg[.exe]</code>"),
        "ff_no_hint2": "设置环境变量 <code>HLS_FFMPEG</code>",
        "ff_no_hint3": "用 <code>python server.py --ffmpeg &lt;目录|exe&gt;</code> 启动",
        "ff_no_hint4": "把 <code>ffmpeg</code> 加入 <code>PATH</code>",
        "ff_no_tail": "然后删除 <code>media_cache/</code> 并刷新本页。",
        "ff_look": "查找位置",
        # --- media 缓存面板 ---
        "cache_h": "media 缓存",
        "cache_files": "个文件",
        "cache_dir": "目录",
        "cache_note": "ffmpeg 生成的真实内容缓存在此目录；清理后会立即重建。",
        "cache_btn": "清理缓存",
        "cache_confirm": "要删除全部缓存媒体文件吗？清理后会立即重新生成（可能需要几秒）。",
        "cache_busy": "正在清理并重建…",
        "cache_done": "已清理",
        "cache_partial": "部分清理",
        "cache_errs": "个失败",
        "cache_fail": "清理失败",
        # --- 样例视频面板 ---
        "sample_h": "样例视频",
        "sample_found": "已找到样例 —— 点“处理”即可转码进真实缓存",
        "sample_none": "无样例 —— 使用内置测试图",
        "sample_path": "正在使用",
        "sample_active": "当前缓存来自样例",
        "sample_inactive": "当前缓存为内置源",
        "sample_hint": "把视频放到 tools/sample/source.mp4（也支持 .mkv/.mov/.webm/.ts），再点“处理”",
        "sample_btn": "处理 / 重建",
        "sample_busy": "正在转码样例…",
        "sample_done": "已用样例重建",
        "sample_fail": "重建失败",
    },
}

# Chinese display metadata, keyed by CATALOG id.
# 中文展示元数据，键为 CATALOG 的 id：id -> (分类, 标题, 预期行为)。
# 缺条目时 scenario() 会优雅回退到英文，不会抛错。
ZH_SCENARIOS = {
    "aes128": ("加密", "AES-128，显式 IV", "可解密；明文首字节必须是 0x47（TS 同步字节）。"),
    "aes128-msn": ("加密", "AES-128，IV 取自媒体序列号", "无 IV 属性 —— IV 必须取分片的 media sequence number。"),
    "aes128-master": ("加密", "AES-128 多码率 + SESSION-KEY", "master 中含 EXT-X-SESSION-KEY；两个变体均为 AES-128。"),
    "key-expiry": ("加密", "密钥 URL 带时效（TTL + 签名）", "过了 ?ttl 秒后密钥端点返回 403；考察重试/重拉行为。TTL 可配（最小 5）。"),
    "sample-aes": ("加密", "SAMPLE-AES（identity keyformat）", "下载器应拒绝：不支持的加密方式。"),
    "fairplay": ("加密", "SAMPLE-AES + FairPlay keyformat（skd://）", "DRM；应判定为不可解密，而不是解析错误。"),
    "widevine": ("加密", "Widevine KEYFORMAT + data: pssh URI", "DRM；该 data: URI 是 pssh box，不是密钥。"),
    "key-header": ("加密", "密钥需要自定义请求头", "密钥端点需要请求头 X-Api-Key（否则 403）。"),
    "key-rotation": ("加密", "AES 密钥逐片轮换", "每片一个 EXT-X-KEY；沿用上一片密钥必错。"),
    "aes-fmp4": ("加密", "AES-128 + fMP4（init 明文）", "EXT-X-KEY 在 EXT-X-MAP 之前；init 段明文，仅 .m4s 分片加密。"),
    "aes-byterange": ("加密", "AES-128 + EXT-X-BYTERANGE", "区间索引的是密文；每段独立 PKCS7 填充，合并后再解密必然失败。"),
    "aes-disc": ("加密", "EXT-X-DISCONTINUITY 处更换密钥", "Clip A/B 使用不同密钥；跨边界沿用同一密钥会解出乱码。"),

    "paths": ("播放列表结构", "混合 URI 形态 + 重定向", "相对 / 点段 / 百分号编码 / 查询串 / 绝对 / 协议相对 / 302，变体与分片 URI 都有。"),
    "nested": ("播放列表结构", "多层嵌套 master（三层）", "master -> sub-master -> media。sub-master 内混用相对、点段与父级（../）URI；沿用顶层 base 的客户端会 404。"),
    "variable": ("播放列表结构", "变长 EXTINF + TARGETDURATION 不符", "分片时长从 0.8s 到 12.0s 不等。/mismatch/ 声明 TARGETDURATION:6，小于真实最大值 12.0（常见源端缺陷）。"),
    "byterange": ("播放列表结构", "用 EXT-X-BYTERANGE 切 TS", "所有分片都是同一个 big.ts 的字节区间。"),
    "define": ("播放列表结构", "EXT-X-DEFINE 变量 URI", "分片 URI 使用 {$segbase} 替换（v8 特性）。"),
    "malformed": ("播放列表结构", "不规范 m3u8", "变体：crlf bom no-version unknown-tags spacing lowercase no-extm3u bad-attrs all。分片本身正常，考的是解析。"),

    "vod": ("分发", "普通 VOD（ENDLIST）", "基准正常路径。"),
    "live": ("分发", "直播滑窗（6 × 6s）", "无 ENDLIST；MEDIA-SEQUENCE 递增；含 PDT。"),
    "live-event": ("分发", "Event 列表 → 转为 VOD", "窗口增长到 12 片后追加 ENDLIST。"),

    "guard": ("访问控制", "Referer / UA / Cookie 校验", "?auth=referer|ua|cookie|all。取值：Referer 'https://portal.example.com/'，User-Agent 前缀 'HLSClient/1.0'，Cookie 'access=granted'。"),
    "sign": ("访问控制", "签名 URL + 时效", "HMAC 签名的 ?exp&s 参数；超时 403（默认 60s，最小 5）。"),
    "session": ("访问控制", "必须带 Session", "先 GET /hls/session/start；分片需 Cookie HLSSESS=... 或 ?session=...，否则 401。"),
    "ratelimit": ("访问控制", "限流（429 + Retry-After）", "30 秒内超过 ?n 次分片请求 → 429。"),
    "throttle": ("访问控制", "带宽限流下发", "分片按 ?kbps 速率下发（默认 64）。"),
    "ban": ("访问控制", "突发后 IP 封禁", "60 秒内超 ?n 次 → 403 封禁 60 秒（作用域仅 /hls/ban）。"),
    "captcha": ("访问控制", "验证码拦截", "首个分片返回 403 HTML；用 /hls/captcha/solve?code=1234 解锁。"),

    "broken": ("源端缺陷", "缺失 / 500 / 乱码 / 截断", "seg2=404，seg4=500，seg6=乱码字节，seg8=截断一半。"),
    "broken-enc": ("源端缺陷", "含错误密钥分片的 AES 流", "seg6 用不同密钥加密 → 解出乱码（下游 PKCS7 去填充报错）。"),
    "gap": ("源端缺陷", "EXT-X-GAP 占位", "第 3、4 片标记 GAP；应跳过而非请求。"),
    "ads": ("源端缺陷", "同一广告区间用三种信令标记", "同一广告区间同时被 EXT-X-DATERANGE（SCTE35-OUT/SCTE35-IN，十六进制二进制）、EXT-X-CUE-OUT/CUE-IN 与 EXT-X-GAP 标记。"),
    "discontinuity": ("源端缺陷", "PTS 跳变 + EXT-X-DISCONTINUITY", "5 片之后 PTS 从初值重新开始。"),
    "onetime": ("源端缺陷", "一次性分片 URL（续传极难）", "每次拉列表都会签发新 token；每个分片 URL 只能成功一次，之后 403。只能重新拉列表恢复。"),

    "range-ok": ("断点续传", "RFC 正确的 Range（206/416）", "Accept-Ranges: bytes，Content-Range 正确，越界返回 416 + Content-Range: bytes */len。"),
    "range-none": ("断点续传", "不支持 Range（Accept-Ranges: none）", "服务端声明不支持区间，永远 200 完整响应体；续传必须从 0 重来。"),
    "range-ignored": ("断点续传", "声称支持却忽略 Range", "Accept-Ranges: bytes 但每次请求都返回 200 完整响应体，且无 Content-Range。"),
    "range-badtotal": ("断点续传", "206 但总长度是错的", "Content-Range 的总长只有真实值一半；信了就会损坏拼出的文件。"),
    "range-multipart": ("断点续传", "多区间响应损坏", "bytes=a-b,c-d 只返回第一段，且不带 multipart/byteranges。"),
    "range-restarts": ("断点续传", "只能续一次，之后归零", "每个 URL 首次 Range 生效；之后再请求返回 200 完整响应体（进度丢失）。"),
    "resume-triple": ("断点续传", "续传不可能：三重机制叠加", "一次性分片 URL + 限时密钥 + 一次性 Range。正确的下载器应识别出无法续传并重新下载，而不是拼接出损坏文件。"),

    "av-separate": ("容器", "音视频分离的 rendition", "EXT-X-MEDIA 音频组 + 纯视频变体 + 纯音频变体。"),
    "renditions": ("容器", "多音轨 + 字幕 + 隐藏字幕", "两个 AUDIO 组（aud/comm）、一个 SUBTITLES 组（en/zh，带 URI）、一个 CLOSED-CAPTIONS 组（无 URI，INSTREAM-ID=CC1）。字幕分片是带 X-TIMESTAMP-MAP 的 WebVTT。"),
    "fmp4": ("容器", "fMP4 分片（EXT-X-MAP + .m4s）", "必须先取 init 段（并与媒体分片重新封装）。"),
    "fmp4-av": ("容器", "fMP4 视频 + 音频双轨", "init 含两条 trak（avc1 视频 + mp4a 音频）；每个 .m4s 的 moof 含两个 traf（track_ID 1 和 2），data offset 各自独立。"),
    "fmp4-byterange": ("容器", "fMP4 MAP + 字节区间分片", "init 与媒体分片都是同一个 combined.m4s 的区间。"),
    "series": ("播放列表结构", "剧集：多集 + 上/下一集 + 共享密钥", "3 集分片数各不相同，全集（含上/下一集）共用一把 AES-128 密钥；用自定义的 EXT-X-SERIES-PREV / EXT-X-SERIES-NEXT 标签表达先/后集关系——客户端必须能忽略这些未知标签并继续下载。"),
}


def resolve_lang(req) -> str:
    """Pick a language from ``?lang=`` then ``Accept-Language``, else default.
    依次从 ``?lang=``、``Accept-Language`` 决定语言，否则用默认值。"""
    q = (req.q("lang") or "").strip().lower()
    if q in LANGS:
        return q
    header = (req.header("Accept-Language") or "").lower()
    # crude but sufficient: honour an explicit zh preference over en
    # 简化但够用：zh 出现在 en 之前（或只有 zh）就选中文。
    zh = header.find("zh")
    en = header.find("en")
    if zh != -1 and (en == -1 or zh < en):
        return "zh"
    if en != -1:
        return "en"
    return DEFAULT_LANG


def category(cat: str, lang: str) -> str:
    """把分类英文键翻译成目标语言；未知分类原样返回。"""
    return CATEGORIES.get(lang, CATEGORIES[DEFAULT_LANG]).get(cat, cat)


def scenario(sid: str, cat: str, title: str, expect: str,
             lang: str) -> tuple[str, str, str]:
    """Return ``(category, title, expect)`` in ``lang``.
    返回目标语言下的 ``(分类, 标题, 预期行为)`` 三元组。

    Unknown ids fall back to the English values baked into CATALOG, so adding a
    scenario without a translation degrades gracefully instead of crashing.
    未知 id 回退到 CATALOG 里的英文原值，因此新增场景即使漏了翻译也只是
    显示英文，而不会崩。
    """
    if lang == "zh" and sid in ZH_SCENARIOS:
        return ZH_SCENARIOS[sid]
    return category(cat, lang), title, expect


def ui(key: str, lang: str) -> str:
    """取页面框架文案；缺 key 时回退英文，再缺就返回 key 本身。"""
    table = UI.get(lang, UI[DEFAULT_LANG])
    if key in table:
        return table[key]
    return UI[DEFAULT_LANG].get(key, key)


def missing_ids(catalog_ids) -> list[str]:
    """Ids present in CATALOG but lacking a Chinese translation (for tests).
    CATALOG 中存在但缺中文翻译的 id（供测试校验翻译完整性）。"""
    return [i for i in catalog_ids if i not in ZH_SCENARIOS]
