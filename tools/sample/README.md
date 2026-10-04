# Sample video / 样例视频

Drop **one** video file in this directory and click **Process / rebuild** on the
index page (or `POST /sample/rebuild`). The server transcodes it with ffmpeg
into the real-content cache (`media_cache/real`), replacing the built-in
`testsrc2` test pattern. Delete the file and rebuild to go back to the built-in
pattern.

把**一个**视频文件放进本目录，然后在索引页点「**处理 / 重建**」（或调
`POST /sample/rebuild`）。服务器会用 ffmpeg 把它转码进真实内容缓存
（`media_cache/real`），替换内置的 `testsrc2` 测试图。删掉文件再重建即可回到
内置测试图。

## Accepted names / 识别的文件名

The first match on disk wins / 按以下顺序取第一个存在的：

- `source.mp4`
- `source.mkv`
- `source.mov`
- `source.webm`
- `source.ts`

## Overrides / 覆盖方式

Precedence: `--sample <path>` > `$HLS_SAMPLE` > `./tools/sample/`.

优先级：`--sample <路径>` > `$HLS_SAMPLE` > `./tools/sample/`。

```bash
python server.py --sample /path/to/my-clip.mkv
HLS_SAMPLE=/path/to/my-clip.mp4 python server.py
```

## Notes / 说明

- Videos are **not** committed (`tools/sample/*` is gitignored except this file).
  视频不入库（`tools/sample/*` 已 gitignore，仅本文件除外）。
- Any resolution/framerate is accepted; output is scaled to **416x234 @ 25fps**
  and looped to a fixed 12 segments, so the cache stays a predictable size.
  任意分辨率/帧率都可以；输出统一缩放为 **416x234 @ 25fps** 并循环到固定
  12 个分片，缓存体积可预期。
- If the sample has no audio track, a silent track is synthesised
  (`anullsrc`) so the audio-only scenarios still work.
  若样例没有音轨，会自动合成静音轨（`anullsrc`），纯音频场景仍可用。
- Transcoding runs synchronously and may take a few seconds.
  转码是同步执行的，可能需要几秒。
