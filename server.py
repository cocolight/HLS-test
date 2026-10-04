#!/usr/bin/env python3
"""HLS test server entry point. / HLS 测试服务器入口。

Usage / 用法:
    python server.py [--port 8080] [--bind 127.0.0.1] [--cache-dir ./media_cache]
                     [--ffmpeg /path/to/ffmpeg-or-bin-dir]
                     [--sample /path/to/video-or-dir]

Zero dependencies (pure standard library). If an ffmpeg is available the server
automatically generates real, playable H.264/AAC content into the cache dir;
otherwise it serves synthetic (structurally valid, not decodable) MPEG-TS /
fMP4 segments.

ffmpeg is located in this order / ffmpeg 按以下顺序查找:
    1. --ffmpeg PATH (a file or its directory) / 显式指定的文件或目录
    2. $HLS_FFMPEG                               环境变量
    3. ./tools/ffmpeg[/bin]                      项目内自带
    4. system PATH                               系统 PATH

零依赖（纯标准库）。只要找到 ffmpeg，服务器就会自动生成真实可播放的
H.264/AAC 内容并缓存到 cache dir；否则提供合成的（结构合法但不可解码）
MPEG-TS / fMP4 分片。本服务器测的是下载器逻辑，而非播放。
"""

from __future__ import annotations

import argparse
import sys

# 允许以 `python server.py` 直接从仓库根目录运行（把根目录加进模块搜索路径）。
sys.path.insert(0, ".")

from hls_test import app  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="HLS test server")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--cache-dir", default="media_cache",
                        help="directory for optional ffmpeg-generated content"
                             " / 可选的 ffmpeg 真实内容缓存目录")
    parser.add_argument("--ffmpeg", default=None,
                        help="path to an ffmpeg executable or its bin directory;"
                             " overrides $HLS_FFMPEG and PATH"
                             " / ffmpeg 可执行文件或其所在目录的路径；"
                             "优先级高于 $HLS_FFMPEG 与系统 PATH")
    parser.add_argument("--sample", default=None,
                        help="path to a sample video file (or its directory);"
                             " overrides $HLS_SAMPLE and ./tools/sample/"
                             " / 样例视频文件或其目录的路径；"
                             "优先级高于 $HLS_SAMPLE 与 ./tools/sample/")
    args = parser.parse_args()

    # 先初始化媒体提供器（决定 real / synthetic 模式），再启动 HTTP 服务。
    app.init_media(cache_dir=args.cache_dir, ffmpeg_path=args.ffmpeg,
                   sample_path=args.sample)
    mode = "real (ffmpeg)" if app.MEDIA.is_real else "synthetic"
    httpd = app.make_server(args.bind, args.port)
    print(f"HLS test server")
    print(f"  mode : {mode}")
    if app.MEDIA.ffmpeg:
        print(f"  ffmpeg: {app.MEDIA.ffmpeg}")
    if app.MEDIA.sample:
        src = "sample" if app.MEDIA.using_sample else "built-in test pattern"
        print(f"  sample: {app.MEDIA.sample} (cache from {src})")
    else:
        print(f"  sample: (none - using built-in test pattern)")
    print(f"  listen: http://{args.bind}:{args.port}/")
    print(f"  catalog: http://{args.bind}:{args.port}/scenarios.json")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
