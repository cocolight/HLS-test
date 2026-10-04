"""Media generation: synthetic MPEG-TS muxer, pseudo-fMP4 boxes, optional ffmpeg.
媒体生成：合成 MPEG-TS 复用器、伪 fMP4 box 构造、可选的 ffmpeg 真实内容。

Two content modes / 两种内容模式:
- "synthetic" (default, zero deps): structurally valid MPEG-TS segments
  (0x47 sync, PAT/PMT/PCR/PES with controllable PTS) filled with deterministic
  pseudo-random bytes. Not decodable as real video, but perfectly fine for
  exercising HLS downloader logic (fetch, decrypt -> 0x47 sync byte check,
  concat, integrity verify).
  "合成"（默认，零依赖）：结构合法的 MPEG-TS 分片（0x47 同步字节、PAT/PMT/PCR/PES，
  PTS 可控），载荷是确定性伪随机字节。它不是可解码的真实视频，但足以考验
  HLS 下载器逻辑（拉取、解密后查 0x47 同步字节、拼接、完整性校验）。
- "real" (ffmpeg): real H.264/AAC segments produced with ffmpeg testsrc,
  decodable and playable in players. ffmpeg is located in this order:
  explicit path passed to MediaProvider > $HLS_FFMPEG > ./tools/ffmpeg >
  system PATH.
  "真实"（ffmpeg）：用 ffmpeg testsrc 生成的 H.264/AAC 分片，可解码、可在
  播放器里播放。ffmpeg 按以下顺序查找：显式传入的路径 > $HLS_FFMPEG >
  项目内 ./tools/ffmpeg > 系统 PATH。

Deterministic filler means every synthetic segment can be re-derived and
verified byte-for-byte: sha256-based, keyed by (group, index, stream, k).
确定性填充意味着每个合成分片都能被重新推导并逐字节校验：
基于 sha256，以 (group, index, stream, k) 为键。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time

TS_PACKET = 188          # MPEG-TS 固定包长
PID_PAT = 0x0000         # 节目关联表 PID（固定为 0）
PID_PMT = 0x1000         # 节目映射表 PID
PID_VIDEO = 0x1001       # 视频基本流 PID
PID_AUDIO = 0x1002       # 音频基本流 PID

_STREAM_TYPE_H264 = 0x1B  # PMT 中的视频流类型：H.264
_STREAM_TYPE_AAC = 0x0F   # PMT 中的音频流类型：AAC（ADTS）

_CLOCK = 90000  # Hz，MPEG-TS 的 PTS/PCR 时钟频率


# ---------------------------------------------------------------------------
# MPEG CRC-32 (poly 0x04C11DB7, init 0xFFFFFFFF, no reflect / no xorout)
# MPEG 专用 CRC-32（多项式 0x04C11DB7，初值全 1，不反转输入输出）
# ---------------------------------------------------------------------------

def _crc32_mpeg(data: bytes) -> int:
    """PSI 段尾部校验用的 CRC-32（与 IEEE 以太网 CRC 参数不同）。"""
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if (crc & 0x80000000) else (crc << 1)
            crc &= 0xFFFFFFFF
    return crc


# ---------------------------------------------------------------------------
# TS packetisation / TS 打包
# ---------------------------------------------------------------------------

def pcr_bytes(pcr_base: int) -> bytes:
    """把 33 位 PCR base 编码成 6 字节（42 位字段，中间位固定填充）。"""
    base = pcr_base & 0x1FFFFFFFF
    return bytes([
        (base >> 25) & 0xFF,
        (base >> 17) & 0xFF,
        (base >> 9) & 0xFF,
        (base >> 1) & 0xFF,
        ((base & 1) << 7) | 0x7E,  # 6 个保留位 + PCR 扩展高位
        0x00,                      # PCR 扩展低 8 位
    ])


def _af(pcr: int | None, total: int) -> bytes:
    """Adaptation field of exactly `total` bytes (length byte included).
    构造恰好 total 字节的适配字段（含长度字节本身）。

    适配字段用来把 TS 包补齐到 188 字节；带 PCR 时前 6 字节放 PCR，
    其余填 0xFF（填充字节）。
    """
    body = bytearray()
    body.append(0x10 if pcr is not None else 0x00)
    if pcr is not None:
        body += pcr_bytes(pcr)
    while len(body) < total - 1:
        body.append(0xFF)
    if len(body) != total - 1:
        raise ValueError("adaptation field size mismatch")
    return bytes([total - 1]) + bytes(body)


def ts_packet(pid: int, payload: bytes = b"", unit_start: bool = False,
              pcr: int | None = None) -> bytes:
    """One 188-byte TS packet. / 构造一个 188 字节的 TS 包。

    unit_start=True 置 payload_unit_start_indicator，表示这是 PES/PSI 首包。
    payload 最多 184 字节，多出来的空间用适配字段补足。
    """
    n = len(payload)
    if n > 184:
        raise ValueError("payload too large for one TS packet")
    header = bytearray(4)
    header[0] = 0x47  # 同步字节
    header[1] = (0x40 if unit_start else 0x00) | ((pid >> 8) & 0x1F)
    header[2] = pid & 0xFF

    if pcr is not None:
        if n > 176:
            raise ValueError("need >= 8 bytes of adaptation field for PCR")
        header[3] = 0x30  # AF + payload（带适配字段）
        return bytes(header) + _af(pcr, 188 - 4 - n) + payload
    if n == 184:
        header[3] = 0x10  # 只有 payload
        return bytes(header) + payload
    header[3] = 0x30
    return bytes(header) + _af(None, 184 - n) + payload


def _psi_packet(pid: int, table: bytes) -> bytes:
    """Packetise a PSI section (pointer_field on the first packet).
    把一个 PSI 段打包成 TS 包（首包带 pointer_field）。
    """
    payload = b"\x00" + table  # pointer_field=0，段紧跟在后面
    out = b""
    first = True
    while payload:
        chunk, payload = payload[:184], payload[184:]
        out += ts_packet(pid, chunk, unit_start=first)
        first = False
    return out


def _psi_section(table_id: int, ext: bytes, body: bytes) -> bytes:
    """构造完整 PSI 段：头 + 扩展 + 体 + CRC-32。"""
    sec_len = len(body) + len(ext) + 4
    hdr = bytes([table_id, 0xB0 | ((sec_len >> 8) & 0x0F), sec_len & 0xFF]) + ext
    data = hdr + body
    return data + _crc32_mpeg(data).to_bytes(4, "big")


def _pat() -> bytes:
    """PAT：把节目号 1 关联到 PMT 的 PID。"""
    ext = (1).to_bytes(2, "big") + b"\xc1\x00\x00"  # TSID + 版本 + 段号 + 末段号
    prog = (1).to_bytes(2, "big") + bytes(
        [0xE0 | ((PID_PMT >> 8) & 0x1F), PID_PMT & 0xFF])
    return _psi_section(0x00, ext, prog)


def _pmt(with_audio: bool) -> bytes:
    """PMT：声明视频流（必选）与音频流（可选）及其 PID / 流类型。"""
    ext = (1).to_bytes(2, "big") + b"\xc1\x00\x00"
    head = bytes([0xE0 | ((PID_VIDEO >> 8) & 0x1F), PID_VIDEO & 0xFF]) + b"\xf0\x00"
    streams = bytes([_STREAM_TYPE_H264,
                     0xE0 | ((PID_VIDEO >> 8) & 0x1F), PID_VIDEO & 0xFF]) + b"\xf0\x00"
    if with_audio:
        streams += bytes([_STREAM_TYPE_AAC,
                          0xE0 | ((PID_AUDIO >> 8) & 0x1F), PID_AUDIO & 0xFF]) + b"\xf0\x00"
    return _psi_section(0x02, ext, head + streams)


def _pes(pts: int, stream_id: int, data: bytes) -> bytes:
    """PES packet carrying PTS (no DTS).
    构造只带 PTS（无 DTS）的 PES 包——HLS 测试只关心 PTS 时间轴。

    PTS 是 33 位字段，按 0b0010 + 3 位 + marker + 15 位 + marker + 15 位 + marker
    的格式拆成 5 字节编码。
    """
    p = pts & 0x1FFFFFFFF
    length = 7 + len(data)  # 标志 + 头长度 + PTS 共 7 字节
    out = bytearray()
    out += b"\x00\x00\x01" + bytes([stream_id])   # PES 起始码 + stream_id
    out += length.to_bytes(2, "big") if length < 0x10000 else b"\x00\x00"
    out.append(0x85)  # '10' + data_alignment + original
    out.append(0x80)  # PTS 存在标志
    out.append(5)     # PES 头数据长度
    out.append(0x21 | ((p >> 29) & 0x0E))   # '0010' + PTS[32:30] + marker
    out.append((p >> 22) & 0xFF)            # PTS[29:22]
    out.append(0x01 | ((p >> 14) & 0xFE))   # marker + PTS[21:15]
    out.append((p >> 7) & 0xFF)             # PTS[14:7]
    out.append(0x01 | ((p << 1) & 0xFE))    # marker + PTS[6:0]
    return bytes(out) + data


def _filler(group: str, index: int, stream: str, k: int, n: int) -> bytes:
    """确定性伪随机载荷：以 (group,index,stream,k) 为种子反复 sha256 展开。
    同样输入永远得到同样字节，便于 verify.py 逐字节复算。
    """
    out = bytearray()
    h = hashlib.sha256(f"{group}|{index}|{stream}|{k}".encode()).digest()
    while len(out) < n:
        h = hashlib.sha256(h).digest()
        out += h
    return bytes(out[:n])


def make_ts_segment(group: str, index: int, duration: float = 6.0,
                    pts_start: int | None = None, streams: str = "av") -> bytes:
    """Synthetic but structurally valid TS segment with controllable PTS.
    合成但结构合法的 TS 分片，PTS 可控。

    streams: "av" (audio+video), "v" (video only), "a" (audio only).
    pts_start: None -> continuous timeline (90k ticks/sec from segment index).
    pts_start 为 None 时表示连续时间轴（按分片序号 × 时长 × 90k 推算起点）。
    """
    if pts_start is None:
        pts_start = 90000 + int(index * duration * _CLOCK)
    has_video = "v" in streams
    has_audio = "a" in streams
    out = bytearray()
    out += _psi_packet(PID_PAT, _pat())          # 先发 PAT
    out += _psi_packet(PID_PMT, _pmt(has_audio))  # 再发 PMT

    fps = 25
    pes_payload = 1700
    n_video = int(duration * fps)
    for i in range(n_video if has_video else 0):
        pts = pts_start + i * (_CLOCK // fps)     # 每帧推进 1/25 秒
        data = _filler(group, index, "v", i, pes_payload)
        pes = _pes(pts, 0xE0, data)
        first = True
        while pes:
            # 第一个视频包需要容纳 PCR，故有效载荷少 8 字节。
            take = (176 if i == 0 else 184) if first else 184
            chunk, pes = pes[:take], pes[take:]
            if first:
                out += ts_packet(PID_VIDEO, chunk, unit_start=True,
                                 pcr=pts if i == 0 else None)  # 仅首帧带 PCR
                first = False
            else:
                out += ts_packet(PID_VIDEO, chunk)
    n_audio = int(duration * 2)
    for i in range(n_audio if has_audio else 0):
        pts = pts_start + i * (_CLOCK // 2)       # 每帧推进 0.5 秒
        data = _filler(group, index, "a", i, 8000)
        pes = _pes(pts, 0xC0, data)
        first = True
        want_pcr = i == 0 and not has_video       # 纯音频流才由音频承载 PCR
        while pes:
            take = (176 if want_pcr else 184) if first else 184
            chunk, pes = pes[:take], pes[take:]
            if first:
                out += ts_packet(PID_AUDIO, chunk, unit_start=True,
                                 pcr=pts if want_pcr else None)
                first = False
            else:
                out += ts_packet(PID_AUDIO, chunk)
    return bytes(out)


def verify_ts(buf: bytes) -> bool:
    """Every 188th byte must be the 0x47 sync byte.
    校验：长度是 188 的整数倍，且每个包首字节都是 0x47 同步字节。"""
    return len(buf) % TS_PACKET == 0 and all(
        buf[i] == 0x47 for i in range(0, len(buf), TS_PACKET)
    )


# ---------------------------------------------------------------------------
# Pseudo-fMP4: structurally valid box tree (not decodable without real SPS/PPS)
# 伪 fMP4：结构合法的 box 树（没有真实 SPS/PPS，因此不可解码）
# ---------------------------------------------------------------------------

def _box(fcc: bytes, *payload: bytes) -> bytes:
    """构造一个 ISOBMFF box：4 字节长度 + 4 字节类型（fourcc）+ 载荷。"""
    body = b"".join(payload)
    return (8 + len(body)).to_bytes(4, "big") + fcc + body


def _full(fcc: bytes, version: int, flags: int, *payload: bytes) -> bytes:
    """构造 FullBox：在 box 载荷前插入 1 字节 version + 3 字节 flags。"""
    return _box(fcc, bytes([version]) + flags.to_bytes(3, "big"), *payload)


def _u16(v: int) -> bytes:
    """大端 16 位无符号整数。"""
    return v.to_bytes(2, "big")


def _u32(v: int) -> bytes:
    """大端 32 位无符号整数。"""
    return v.to_bytes(4, "big")


def _u64(v: int) -> bytes:
    """大端 64 位无符号整数。"""
    return v.to_bytes(8, "big")


# 单位矩阵（16.16 / 2.30 定点混合格式），视频轨 transform 用。
_IDENTITY_MATRIX = (
    _u32(0x00010000) + _u32(0) * 2 +
    _u32(0) + _u32(0x00010000) + _u32(0) +
    _u32(0) + _u32(0) + _u32(0x40000000)
)


def _avcc_dummy() -> bytes:
    """avcC box 内容：AVCDecoderConfigurationRecord（含样例 SPS/PPS）。
    仅结构合法，不是真实可解码的 SPS/PPS。"""
    sps = b"\x67\x42\x00\x1e\x95\x8c\x1e\xdb\x01\x40\x16\xec\x04\x40\x00\x00\x03\x00\x40\x00\x00\x07\x81\x85\x4a\x64"
    pps = b"\x68\xce\x3c\x80"
    return (bytes([1, 66, 0, 30, 0xFF, 0xE1]) + _u16(len(sps)) + sps +
            b"\x01" + _u16(len(pps)) + pps)


def _esds_dummy() -> bytes:
    """Minimal MPEG-4 ES_Descriptor for an AAC track.
    AAC 轨道所需的最小 MPEG-4 ES_Descriptor。

    Tag hierarchy: ES_Descriptor(0x03) -> DecoderConfigDescriptor(0x04) ->
    DecoderSpecificInfo(0x05). Lengths use the one-byte short form.
    描述符层级：ES_Descriptor(0x03) -> DecoderConfigDescriptor(0x04) ->
    DecoderSpecificInfo(0x05)（另有 SLConfigDescriptor 0x06）。长度用单字节短格式。
    """
    aac_cfg = b"\x12\x10"                 # AAC-LC, 48 kHz, stereo
    dsi = bytes([0x05, len(aac_cfg)]) + aac_cfg
    dec_specific = dsi
    dec_cfg_body = (
        bytes([0x40]) +                     # objectTypeIndication：MPEG-4 Audio
        bytes([0x15]) +                     # streamType=5(音频)<<2 | upStream
        b"\x00\x00\x00" +                   # bufferSizeDB
        _u32(96000) + _u32(96000) +         # maxBitrate, avgBitrate
        dec_specific
    )
    dec_cfg = bytes([0x04, len(dec_cfg_body)]) + dec_cfg_body
    sl_cfg = bytes([0x06, 0x01, 0x02])      # SLConfigDescriptor, predefined=2
    es_body = _u16(2) + bytes([0x00]) + dec_cfg + sl_cfg  # ES_ID + flags + 描述符
    es = bytes([0x03, len(es_body)]) + es_body
    return _full(b"esds", 0, 0, es)


def _visual_stsd() -> bytes:
    """视频 sample entry（avc1）+ 内嵌 avcC。"""
    visual = (
        b"\x00" * 6 + b"\x00\x01" +      # reserved + data_reference_index
        _u16(0) + b"\x00" * 12 +          # pre_defined + reserved
        _u16(416) + _u16(234) +           # width, height
        _u16(0x0048) + _u16(0x0048) +     # dpi
        _u16(0) + b"\x00" * 31 + b"\x00" +  # frame_count + compressorname(32)
        _u16(0x0018) + _u16(0xFFFF)       # depth, pre_defined(-1)
    )
    return _box(b"avc1", visual, _box(b"avcC", _avcc_dummy()))


def _audio_stsd() -> bytes:
    """音频 sample entry（mp4a）+ 内嵌 esds。"""
    # AudioSampleEntry: 8 reserved + 2 data_reference_index + 8 reserved +
    # channelcount(2) + samplesize(2) + pre_defined(2) + reserved(2) +
    # samplerate(4, 16.16 fixed point)
    audio = (
        b"\x00" * 6 + b"\x00\x01" +       # reserved + data_reference_index
        b"\x00" * 8 +                      # reserved
        _u16(2) + _u16(16) +               # channelcount, samplesize
        _u16(0) + _u16(0) +                # pre_defined, reserved
        _u32(48000 << 16)                  # samplerate 48 kHz，16.16 定点
    )
    return _box(b"mp4a", audio, _esds_dummy())


def _video_trak() -> bytes:
    """视频轨：trak -> tkhd + mdia(mdhd/hdlr/minf(stbl))。"""
    avc1 = _visual_stsd()
    # stbl 用空表：本工程不需要真实的采样索引表（moof 里自带 trun）。
    stbl = _box(b"stbl",
                _full(b"stsd", 0, 0, _u32(1), avc1),
                _full(b"stts", 0, 0, _u32(0)),
                _full(b"stsc", 0, 0, _u32(0)),
                _full(b"stsz", 0, 0, _u32(0), _u32(0)),
                _full(b"stco", 0, 0, _u32(0)))
    minf = _box(b"minf",
                _full(b"vmhd", 0, 1, _u16(0), b"\x00" * 6),   # 视频媒体头
                _box(b"dinf", _full(b"dref", 0, 0, _u32(1), _full(b"url ", 0, 1))),
                stbl)
    mdia = _box(b"mdia",
                _full(b"mdhd", 0, 0, _u32(0), _u32(0), _u32(_CLOCK), _u32(0),
                      _u16(0x55C4), _u16(0)),                  # 时基 90 kHz
                _full(b"hdlr", 0, 0, _u32(0), b"vide", _u32(0) * 3,
                      b"VideoHandler\x00"),
                minf)
    return _box(b"trak",
                _full(b"tkhd", 0, 7,
                      _u32(0), _u32(0),            # creation, modification
                      _u32(1), _u32(0),           # track_id=1, reserved
                      _u32(0),                    # duration
                      _u32(0), _u32(0),           # reserved
                      _u16(0), _u16(0),           # layer, alternate_group
                      _u16(0), _u16(0),           # volume, reserved
                      _IDENTITY_MATRIX,
                      _u32(416 << 16), _u32(234 << 16)),  # width, height (16.16)
                mdia)


def _audio_trak() -> bytes:
    """音频轨：结构与视频轨相同，媒体类型为 soun、时基 48 kHz、track_id=2。"""
    mp4a = _audio_stsd()
    stbl = _box(b"stbl",
                _full(b"stsd", 0, 0, _u32(1), mp4a),
                _full(b"stts", 0, 0, _u32(0)),
                _full(b"stsc", 0, 0, _u32(0)),
                _full(b"stsz", 0, 0, _u32(0), _u32(0)),
                _full(b"stco", 0, 0, _u32(0)))
    minf = _box(b"minf",
                _full(b"smhd", 0, 0, _u16(0), _u16(0)),   # balance + reserved
                _box(b"dinf", _full(b"dref", 0, 0, _u32(1), _full(b"url ", 0, 1))),
                stbl)
    mdia = _box(b"mdia",
                _full(b"mdhd", 0, 0, _u32(0), _u32(0), _u32(48000), _u32(0),
                      _u16(0x55C4), _u16(0)),                 # 时基 48 kHz
                _full(b"hdlr", 0, 0, _u32(0), b"soun", _u32(0) * 3,
                      b"SoundHandler\x00"),
                minf)
    return _box(b"trak",
                _full(b"tkhd", 0, 7,
                      _u32(0), _u32(0),            # creation, modification
                      _u32(2), _u32(0),           # track_id = 2, reserved
                      _u32(0),                    # duration
                      _u32(0), _u32(0),           # reserved
                      _u16(0), _u16(0),           # layer, alternate_group
                      _u16(0x0100), _u16(0),      # volume = 1.0（音频）, reserved
                      _IDENTITY_MATRIX,
                      _u32(0), _u32(0)),          # width, height = 0 for audio
                mdia)


def fmp4_init(tracks: str = "v") -> bytes:
    """Init segment. ``tracks`` contains "v" and/or "a".
    初始化段（ftyp + moov）。``tracks`` 可含 "v"（视频）与 "a"（音频）。

    With only "v" the output is byte-identical to before (single visual trak).
    只传 "v" 时输出与旧版逐字节一致（单条视频 trak），保证向后兼容。
    """
    ftyp = _box(b"ftyp", b"isom", _u32(0x200), b"isomiso2avc1mp41")
    has_video = "v" in tracks
    has_audio = "a" in tracks
    traks = b""
    if has_video:
        traks += _video_trak()
    if has_audio:
        traks += _audio_trak()
    # next_track_id = 已用轨道数 + 1（本工程最多 2 条轨）。
    next_track_id = 1 + (1 if has_video else 0) + (1 if has_audio else 0)
    moov = _box(b"moov",
                _full(b"mvhd", 0, 0,
                      _u32(0), _u32(0),           # creation, modification
                      _u32(1000), _u32(0),        # timescale, duration
                      _u32(0x00010000),           # rate 1.0
                      _u16(0x0100), _u16(0),      # volume, reserved
                      _u32(0), _u32(0),           # reserved
                      _IDENTITY_MATRIX,
                      _u32(0) * 6,                 # pre_defined
                      _u32(next_track_id)),        # next_track_id
                traks)
    return ftyp + moov


def fmp4_segment(group: str, index: int, seq: int, pts_start: int | None = None,
                 duration: float = 6.0, tracks: str = "v") -> bytes:
    """Media segment. ``tracks`` mirrors fmp4_init: "v", "a" or "va".
    媒体分片（styp + moof + mdat）。``tracks`` 与 fmp4_init 一致："v"/"a"/"va"。

    With only "v" the output is byte-identical to before (single traf).
    只传 "v" 时输出与旧版逐字节一致（单个 traf）。

    注意 default-base-is-moof（tfhd flags 0x020000）：trun 的 data_offset 是
    相对 **moof 起点** 的偏移，而不是文件起点。双轨时第二条 traf 的 offset
    要跳过第一条轨的媒体数据。
    """
    if pts_start is None:
        pts_start = 90000 + int(index * duration * _CLOCK)
    has_video = "v" in tracks
    has_audio = "a" in tracks

    # --- video traf 视频轨的 traf ---
    v_samples = int(duration * 25)
    v_dur = _CLOCK // 25
    mdat_v = _filler(group, index, "m4s", 0, 120_000)
    # 把载荷尽量均匀分给每个采样点，余数摊到前几个采样点上。
    v_base = len(mdat_v) // v_samples if v_samples else 0
    v_sizes = [v_base + (1 if i < len(mdat_v) - v_base * v_samples else 0)
               for i in range(v_samples)]

    def video_traf(data_offset: int) -> bytes:
        payload = _u32(v_samples) + _u32(data_offset)
        for s in v_sizes:
            payload += _u32(v_dur) + _u32(s)
        return _box(b"traf",
                    _full(b"tfhd", 0, 0x020000, _u32(1)),   # default-base-is-moof
                    _full(b"tfdt", 1, 0, _u64(pts_start)),  # 解码时间基准
                    _full(b"trun", 0, 0x000301, payload))   # 采样计数/偏移/时长/大小

    # --- audio traf (48 kHz timescale, 1024-sample frames) 音频轨 traf ---
    A_CLOCK = 48000
    a_samples = int(duration * A_CLOCK / 1024)          # 6 秒约 281 帧
    a_dur = 1024
    a_count = max(1, a_samples)
    a_total = 60_000
    a_mdat = _filler(group, index, "m4sa", 0, a_total)
    a_base = a_total // a_count
    a_sizes = [a_base + (1 if i < a_total - a_base * a_count else 0)
               for i in range(a_count)]

    def audio_traf(data_offset: int) -> bytes:
        payload = _u32(a_count) + _u32(data_offset)
        for s in a_sizes:
            payload += _u32(a_dur) + _u32(s)
        return _box(b"traf",
                    _full(b"tfhd", 0, 0x020000, _u32(2)),   # track_id=2
                    _full(b"tfdt", 1, 0, _u64(pts_start)),
                    _full(b"trun", 0, 0x000301, payload))

    # --- assemble: moof must be sized before data_offset can be computed ----
    # 组装：先算出 moof 长度，才能算 data_offset。
    # Layout: styp | moof | mdat. The mdat payload holds video bytes then the
    # audio bytes, so the second traf's data_offset points past the first.
    # 布局 styp | moof | mdat；mdat 载荷先放视频再放音频，
    # 所以第二条 traf 的 offset 要跳过视频数据。
    mdat_payload = (mdat_v + a_mdat) if (has_video and has_audio) else (
        mdat_v if has_video else a_mdat)

    def build_moof(v_off: int, a_off: int) -> bytes:
        trafs = b""
        if has_video:
            trafs += video_traf(v_off)
        if has_audio:
            trafs += audio_traf(a_off)
        return _box(b"moof", _full(b"mfhd", 0, 0, _u32(seq)), trafs)

    # First pass picks up the moof size; the encoded moof length is stable
    # regardless of the (4-byte) offset values, so one refinement suffices.
    # 第一遍只是量出 moof 长度；moof 编码长度与 4 字节 offset 的取值无关，
    # 所以修正一次即可收敛。
    moof = build_moof(0, 0)
    payload_start = len(moof) + 8                       # mdat 头 8 字节
    if has_video and has_audio:
        v_off = payload_start
        a_off = payload_start + len(mdat_v)
    elif has_video:
        v_off, a_off = payload_start, payload_start
    else:
        v_off, a_off = payload_start, payload_start
    moof = build_moof(v_off, a_off)

    styp = _box(b"styp", b"msdh", _u32(0x200), b"msdhmsix")
    return styp + moof + _box(b"mdat", mdat_payload)


# ---------------------------------------------------------------------------
# Media provider (synthetic, with optional ffmpeg-upgraded cache)
# 媒体提供器：合成内容，可选自动升级为 ffmpeg 真实内容并缓存
# ---------------------------------------------------------------------------

def _exe_name() -> str:
    """平台上的 ffmpeg 可执行文件名（Windows 带 .exe）。"""
    return "ffmpeg.exe" if os.name == "nt" else "ffmpeg"


def human_size(n: int) -> str:
    """把字节数格式化成人类可读的字符串（B / KB / MB / GB）。

    用于索引页缓存面板与 /cache.json。二进制单位（1024 进制），保留一位小数；
    0 与小于 1024 都按整数 B 显示。
    """
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} GB"


def _resolve_ffmpeg(candidate: str | None) -> str | None:
    """定位 ffmpeg 可执行文件的绝对路径；找不到返回 None。

    查找顺序（优先级由高到低）：
      1. 显式传入的 candidate —— 可以是可执行文件路径，也可以是它所在的目录；
      2. 环境变量 HLS_FFMPEG（同样支持文件或目录）；
      3. 项目内 tools/ffmpeg/ 与 tools/ffmpeg/bin/（便于把 ffmpeg 随项目携带）；
      4. 系统 PATH。

    传入目录时会自动补上 ffmpeg / ffmpeg.exe（同时支持 <dir>/bin 布局）。
    """

    def as_file(path: str) -> str | None:
        """把一个候选字符串规整成可执行文件的绝对路径，失败返回 None。"""
        if not path:
            return None
        p = os.path.expanduser(path.strip().strip('"'))
        if os.path.isdir(p):
            # 目录：先看目录本身，再看它的 bin/ 子目录。
            for base in (p, os.path.join(p, "bin")):
                for name in (_exe_name(), "ffmpeg"):
                    f = os.path.join(base, name)
                    if os.path.isfile(f):
                        return os.path.abspath(f)
            return None
        if os.path.isfile(p):
            return os.path.abspath(p)
        # 既不是文件也不是目录：交给 PATH 解析（例如只给了 "ffmpeg"）。
        return shutil.which(p)

    # 1 + 2：显式参数 > 环境变量。
    for explicit in (candidate, os.environ.get("HLS_FFMPEG")):
        got = as_file(explicit) if explicit else None
        if got:
            return got

    # 3：项目内自带目录（相对本文件定位到仓库根，再找 tools/ffmpeg）。
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ("tools/ffmpeg", "tools/ffmpeg/bin"):
        got = as_file(os.path.join(root, rel))
        if got:
            return got

    # 4：系统 PATH。
    return shutil.which("ffmpeg")


# 项目内样例视频的候选文件名（按顺序取第一个存在的）。
_SAMPLE_NAMES = ("source.mp4", "source.mkv", "source.mov",
                 "source.webm", "source.ts")


def _resolve_sample(candidate: str | None) -> str | None:
    """定位样例视频的绝对路径；找不到返回 None。

    查找顺序（优先级由高到低）：
      1. 显式传入的 candidate —— 可以是视频文件路径，也可以是它所在的目录；
      2. 环境变量 HLS_SAMPLE（同样支持文件或目录）；
      3. 项目内 tools/sample/ 下的 source.{mp4,mkv,mov,webm,ts}。

    传入目录时会在 _SAMPLE_NAMES 里挑第一个存在的文件。找不到任何样例时返回
    None —— 此时真实模式回落到内置 testsrc2/sine 测试图，行为与旧版一致。
    """

    def as_file(path: str) -> str | None:
        """把一个候选字符串规整成视频文件的绝对路径，失败返回 None。"""
        if not path:
            return None
        p = os.path.expanduser(path.strip().strip('"'))
        if os.path.isdir(p):
            for name in _SAMPLE_NAMES:
                f = os.path.join(p, name)
                if os.path.isfile(f):
                    return os.path.abspath(f)
            return None
        return os.path.abspath(p) if os.path.isfile(p) else None

    # 1 + 2：显式参数 > 环境变量。
    for explicit in (candidate, os.environ.get("HLS_SAMPLE")):
        got = as_file(explicit) if explicit else None
        if got:
            return got

    # 3：项目内 tools/sample/（相对本文件定位到仓库根）。
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return as_file(os.path.join(root, "tools", "sample"))


class MediaProvider:
    """统一出口：handler 只调 segment()/fmp4_init()/fmp4_segment()，
    不关心底层是合成还是真实内容，也不关心缓存。"""

    def __init__(self, cache_dir: str | None = None,
                 ffmpeg_path: str | None = None,
                 sample_path: str | None = None):
        self._cache: dict[tuple, bytes] = {}   # 合成内容缓存
        self._lock = threading.Lock()
        self._real = False
        self._real_dir: str | None = None
        self._using_sample = False
        # 先解析 ffmpeg 可执行文件（可为空 -> 说明没有可用 ffmpeg）。
        self._ffmpeg = _resolve_ffmpeg(ffmpeg_path)
        # 再解析样例视频（可为空 -> 用内置 testsrc2/sine 测试图）。
        self._sample = _resolve_sample(sample_path)
        if cache_dir:
            self._real_dir = os.path.join(cache_dir, "real")
            if os.path.isdir(self._real_dir) and os.listdir(self._real_dir):
                if self.cache_is_stale():
                    # 已有缓存但样例变了（新增/替换/删除）-> 先清后重建，
                    # 否则会静默继续提供旧的 testsrc 内容。
                    self.clear_cache(regenerate=True)
                else:
                    # 缓存目录已有内容 -> 直接用真实模式。
                    self._real = True
                    self._using_sample = self._cache_from_sample()
            elif self._ffmpeg:
                # 找到 ffmpeg 就现编一次并落盘；失败则安静回退合成模式。
                try:
                    self._prepare_real()
                    self._real = True
                except BaseException:
                    # BaseException：环境的删除护栏会抛 SystemExit，
                    # 这里兜住以免整个进程被拖垮。
                    self._real = False
                self._using_sample = bool(self._sample) and self._real

    @property
    def is_real(self) -> bool:
        return self._real

    @property
    def ffmpeg(self) -> str | None:
        """实际使用的 ffmpeg 路径（没有则为 None）。"""
        return self._ffmpeg

    @property
    def real_dir(self) -> str | None:
        """真实内容缓存目录（未启用缓存时为 None）。"""
        return self._real_dir

    @property
    def sample(self) -> str | None:
        """样例视频的绝对路径；未提供样例时为 None。"""
        return self._sample

    @property
    def using_sample(self) -> bool:
        """当前真实缓存是否由样例视频转码而来（内置测试图则为 False）。"""
        return self._using_sample

    def cache_stats(self) -> dict:
        """统计真实内容缓存：文件数 + 总字节 + 目录。供索引页与 /cache.json 使用。

        只读、不产生副作用。目录不存在时返回 0/0（而不是报错）——首次启动、
        缓存尚未生成时就是这种情况。
        """
        files, total = 0, 0
        if self._real_dir and os.path.isdir(self._real_dir):
            for name in os.listdir(self._real_dir):
                fp = os.path.join(self._real_dir, name)
                if os.path.isfile(fp):
                    files += 1
                    try:
                        total += os.path.getsize(fp)
                    except OSError:
                        pass
        return {
            "dir": os.path.abspath(self._real_dir) if self._real_dir else None,
            "files": files,
            "bytes": total,
            "real": self._real,
        }

    def clear_cache(self, regenerate: bool = True) -> dict:
        """删除真实内容缓存目录下的所有文件（保留目录本身）。

        返回 {removed, bytes_freed, regenerated, regen_skipped, errors, stats}。
        只删 ``_real_dir`` 内的**文件**，不递归、不跟随目录，避免误删。

        ``regenerate=True``（默认）时，清空后立刻用 ffmpeg 重新生成一遍，
        这样运行中的服务器清完缓存仍能继续提供真实内容（与索引页横幅的承诺一致）；
        重生成失败则回落合成模式。传 False 只清不重建。

        鲁棒性：单个文件删除失败**不会中断整批**——失败的文件记进 ``errors``。
        注意用 ``BaseException`` 而非 ``OSError``：某些环境会在批量删除超限时
        抛 ``SystemExit``（进程级信号），只有 ``BaseException`` 能兜住它，否则
        异常会穿透到 HTTP 层把连接掐断（表现为前端收到 HTTP 000）。有任何文件
        没删干净时**不重建**（``regen_skipped=True``），以免"声称成功却残留旧文件"。
        """
        removed, freed = 0, 0
        errors: list[dict] = []
        if self._real_dir and os.path.isdir(self._real_dir):
            for name in os.listdir(self._real_dir):
                fp = os.path.join(self._real_dir, name)
                if not os.path.isfile(fp):
                    continue
                try:
                    size = os.path.getsize(fp)
                    os.remove(fp)
                    removed += 1
                    freed += size
                except BaseException as e:
                    # 文件被占用（PermissionError）或环境删除护栏（SystemExit）等。
                    errors.append({"file": name,
                                   "error": f"{type(e).__name__}: {e}"})

        regenerated = False
        regen_skipped = False
        # 有残留 -> 不重建，保持现状并如实上报；缓存已空才重建。
        if regenerate and errors:
            regen_skipped = True
        elif regenerate and self._real_dir and self._ffmpeg:
            try:
                self._prepare_real()
                self._real = True
                regenerated = True
            except BaseException:
                self._real = False
        else:
            self._real = False
        return {"removed": removed, "bytes_freed": freed,
                "regenerated": regenerated, "regen_skipped": regen_skipped,
                "errors": errors, "stats": self.cache_stats()}

    # -- sample marker / staleness ------------------------------------------

    def _marker_path(self) -> str | None:
        """缓存来源标记文件 <real_dir>/.source 的路径（无缓存目录时为 None）。"""
        return os.path.join(self._real_dir, ".source") if self._real_dir else None

    def _write_source_marker(self, kind: str) -> None:
        """在缓存目录写 .source 标记，记录本次真实内容由谁生成。

        kind="sample" 时额外记录样例路径 + 大小 + mtime，用于判断样例是否被换过；
        kind="builtin" 表示来自内置 testsrc2/sine。失败静默忽略（标记是尽力而为）。
        """
        path = self._marker_path()
        if not path:
            return
        info: dict = {"kind": kind, "built_at": time.time()}
        if kind == "sample" and self._sample:
            try:
                st = os.stat(self._sample)
                info.update(path=self._sample, mtime=st.st_mtime, size=st.st_size)
            except OSError:
                pass
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(info, f)
        except OSError:
            pass

    def _cache_from_sample(self) -> bool:
        """读 .source 判断现有缓存是否来自样例，且样例未被改动过。"""
        path = self._marker_path()
        if not path or not self._sample:
            return False
        try:
            with open(path, encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            return False
        if info.get("kind") != "sample" or info.get("path") != self._sample:
            return False
        try:
            st = os.stat(self._sample)
        except OSError:
            return False
        return info.get("mtime") == st.st_mtime and info.get("size") == st.st_size

    def cache_is_stale(self) -> bool:
        """缓存存在但与当前样例不符（新增/替换/删除样例）-> 需要重建。"""
        if not (self._real_dir and os.path.isdir(self._real_dir)
                and os.listdir(self._real_dir)):
            return False
        return bool(self._sample) and not self._cache_from_sample()

    def _prepare_real(self) -> None:
        """调用 ffmpeg 生成一批真实 TS / fMP4 分片，缓存到 _real_dir。

        所有文件名都用**相对名**，并把 ffmpeg 的工作目录设为 _real_dir。原因：
        ffmpeg 的 -hls_fmp4_init_filename / -hls_segment_filename 会把值当作
        *文件名*，再拼到 m3u8 所在目录后面；若直接传绝对路径就会得到
        "<out>/<abs path>" 这种双重路径而打开失败。用 cwd + 相对名最稳。
        """
        os.makedirs(self._real_dir, exist_ok=True)
        outdir = os.path.abspath(self._real_dir)

        def ff(*args: str) -> None:
            # 用解析出来的绝对路径调用，而不是依赖 PATH 里的裸 "ffmpeg"；
            # cwd=outdir 配合相对文件名，避免上面的双重路径问题。
            subprocess.run(
                [self._ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args],
                check=True, capture_output=True, timeout=900, cwd=outdir)

        src_v = ["-f", "lavfi", "-i", "testsrc2=size=416x234:rate=25"]
        src_a = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
        enc_v = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "30"]
        enc_a = ["-c:a", "aac", "-b:a", "96k"]
        hls = ["-f", "hls", "-hls_time", "6", "-hls_playlist_type", "vod"]

        if self._sample:
            self._prepare_real_from_sample(ff, enc_v, enc_a, hls, src_v)
            self._write_source_marker("sample")
            return

        # A/V TS segments  音视频 TS 分片
        ff(*src_v, *src_a, "-t", "72", *enc_v, *enc_a,
           "-map", "0:v", "-map", "1:a",
           *hls, "-hls_segment_filename", "seg_%02d.ts", "av.m3u8")
        # video-only TS  纯视频 TS
        ff(*src_v, "-t", "72", *enc_v,
           *hls, "-hls_segment_filename", "segv_%02d.ts", "v.m3u8")
        # audio-only TS  纯音频 TS
        ff(*src_a, "-t", "72", *enc_a,
           *hls, "-hls_segment_filename", "sega_%02d.ts", "a.m3u8")
        # fMP4 (video only)  仅视频 fMP4
        ff(*src_v, "-t", "72", *enc_v,
           *hls, "-hls_segment_type", "fmp4",
           "-hls_fmp4_init_filename", "init.mp4",
           "-hls_segment_filename", "f_%02d.m4s", "f.m3u8")
        # clip B for discontinuity (separate encode -> PTS restarts near 0)
        # 不连续场景用的 clip B：单独编码，使 PTS 大致从头开始
        ff(*src_v, *src_a, "-t", "12", *enc_v, *enc_a,
           "-map", "0:v", "-map", "1:a",
           *hls, "-hls_segment_filename", "b_%02d.ts", "b.m3u8")
        self._write_source_marker("builtin")

    def _prepare_real_from_sample(self, ff, enc_v, enc_a, hls, src_v) -> None:
        """用样例视频转码出与内置源**完全相同**的那 5 组输出。

        关键点：``-stream_loop -1`` + 固定 ``-t 72`` 让任意长度/帧率的样例都产出
        可预期的分片数，从而保住 ``_real_file`` 的 ``index % 12`` 复用契约
        （下游 handler 与 segment()/fmp4_*() 无需任何改动）。

        强迫每个分片边界都落在关键帧上：``-force_key_frames`` 按 6s 步长强制
        IDR + ``-sc_threshold 0`` 关闭场景切换切分，否则 ffmpeg 只会在样例
        自带的稀疏关键帧处切，导致分片时长飘忽（实测 8.33s/片）。这两项配合
        ``-r 25`` 得到干净的 6s 定长分片。

        样例可能没有音轨，而本机没有 ffprobe 可探测。做法：av/audio 两条命令先用
        ``-map 0:a:0?``（可选音轨，缺失不报错），整条命令失败时再回落
        ``anullsrc`` 生成的静音轨。视频统一 scale 到内置源的 416x234@25，保证
        与合成/内置内容在分辨率上一致。
        """
        inp = ["-stream_loop", "-1", "-i", self._sample]
        anull = ["-f", "lavfi", "-i",
                 "anullsrc=channel_layout=stereo:sample_rate=48000"]
        # 编码参数：加关键帧对齐（见 docstring），其余与内置源一致。
        enc_v = [*enc_v, "-sc_threshold", "0",
                 "-force_key_frames", "expr:gte(t,n_forced*6)"]

        def ff_try(primary, fallback) -> None:
            """先跑 primary；失败（多半是无音轨）再跑 fallback。"""
            try:
                ff(*primary)
            except subprocess.CalledProcessError:
                ff(*fallback)

        # 1) A/V TS segments  音视频 TS 分片（样例无音轨 -> 补静音）
        ff_try(
            [*inp, "-t", "72", *enc_v, *enc_a,
             "-map", "0:v:0", "-map", "0:a:0?",
             *hls, "-hls_segment_filename", "seg_%02d.ts", "av.m3u8"],
            [*inp, *anull, "-t", "72", *enc_v, *enc_a,
             "-map", "0:v:0", "-map", "1:a",
             *hls, "-hls_segment_filename", "seg_%02d.ts", "av.m3u8"],
        )
        # 2) video-only TS  纯视频 TS
        ff(*inp, "-t", "72", *enc_v, "-an",
           *hls, "-hls_segment_filename", "segv_%02d.ts", "v.m3u8")
        # 3) audio-only TS  纯音频 TS（样例无音轨 -> 用静音源）
        ff_try(
            [*inp, "-t", "72", *enc_a, "-vn",
             *hls, "-hls_segment_filename", "sega_%02d.ts", "a.m3u8"],
            [*anull, "-t", "72", *enc_a,
             *hls, "-hls_segment_filename", "sega_%02d.ts", "a.m3u8"],
        )
        # 4) fMP4 (video only)  仅视频 fMP4
        ff(*inp, "-t", "72", *enc_v, "-an",
           *hls, "-hls_segment_type", "fmp4",
           "-hls_fmp4_init_filename", "init.mp4",
           "-hls_segment_filename", "f_%02d.m4s", "f.m3u8")
        # 5) clip B for discontinuity  不连续场景用的 clip B（-t 12）
        ff_try(
            [*inp, "-t", "12", *enc_v, *enc_a,
             "-map", "0:v:0", "-map", "0:a:0?",
             *hls, "-hls_segment_filename", "b_%02d.ts", "b.m3u8"],
            [*inp, *anull, "-t", "12", *enc_v, *enc_a,
             "-map", "0:v:0", "-map", "1:a",
             *hls, "-hls_segment_filename", "b_%02d.ts", "b.m3u8"],
        )

    # -- public API ---------------------------------------------------------

    def _synth(self, key: tuple, builder) -> bytes:
        """按 key 缓存合成结果（双检锁：锁外读、锁内写）。"""
        with self._lock:
            got = self._cache.get(key)
        if got is None:
            got = builder()
            with self._lock:
                self._cache[key] = got
        return got

    def _real_file(self, pattern: str, index: int) -> bytes | None:
        """读取真实缓存文件；越界时对 12 取模复用（只有 12 个预生成分片）。"""
        if not self._real:
            return None
        for idx in (index, index % 12):
            path = os.path.join(self._real_dir, pattern % idx)
            if os.path.exists(path):
                with open(path, "rb") as f:
                    return f.read()
        return None

    def segment(self, group: str, index: int, streams: str = "av",
                pts_start: int | None = None, duration: float = 6.0) -> bytes:
        """取一个 TS 分片：优先真实缓存，回落合成。"""
        if self._real:
            if group == "clipb":
                got = self._real_file("b_%02d.ts", index)
            elif streams == "a":
                got = self._real_file("sega_%02d.ts", index)
            elif streams == "v":
                got = self._real_file("segv_%02d.ts", index)
            else:
                got = self._real_file("seg_%02d.ts", index)
            if got is not None:
                return got
        return self._synth(
            ("ts", group, index, streams, pts_start, duration),
            lambda: make_ts_segment(group, index, duration, pts_start, streams))

    def fmp4_init(self, tracks: str = "v") -> bytes:
        """取 fMP4 初始化段（真实模式仅支持单轨 "v"）。"""
        if self._real and tracks == "v":
            path = os.path.join(self._real_dir, "init.mp4")
            if os.path.exists(path):
                with open(path, "rb") as f:
                    return f.read()
        return self._synth(("fmp4init", tracks), lambda: fmp4_init(tracks))

    def fmp4_segment(self, index: int, pts_start: int | None = None,
                      duration: float = 6.0, tracks: str = "v") -> bytes:
        """取一个 fMP4 媒体分片（真实模式仅支持单轨 "v"）。"""
        if self._real and tracks == "v":
            got = self._real_file("f_%02d.m4s", index)
            if got is not None:
                return got
        return self._synth(("m4s", index, pts_start, duration, tracks),
                           lambda: fmp4_segment("m4s", index, index, pts_start,
                                                duration, tracks))
