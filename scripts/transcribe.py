#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""transcribe.py - 视频/音频转写：音轨提取 + 本地语音识别（Windows / macOS / Linux 通用）

职责单一：把一段已下载到本地的媒体文件变成「结构化分段 JSON + 带时间戳纯文本」。
不做选题、不写总结、不生成最终 Markdown —— 那些由 agent 按 SKILL.md 完成。

链路：快照校验 -> 抽 16k 单声道（ffmpeg / PyAV 双路）-> 可选谱减降噪
      -> ASR（faster-whisper / funasr / mlx 三选一）-> 质量自检 -> 排他写盘

用法：
    python transcribe.py --input video.mp4 --outdir out
    python transcribe.py --input video.mp4 --engine funasr --hotwords "褚自军,KOSTAL"
    python transcribe.py --input video.mp4 --clip 62,78 --outdir out/r1   # 只重跑异常段

依赖：Python 3.9+，无强制第三方包（引擎按所选路径安装，见 env_check.py）。
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Windows 控制台默认 GBK，emoji 会直接抛 UnicodeEncodeError；统一改用 UTF-8 并容错。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HF_MIRROR = "https://hf-mirror.com"
AUDIO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".m4a", ".mp3", ".wav", ".aac", ".flac", ".ts"}
SR = 16000


def log(msg):
    print(msg, flush=True)


def die(msg, code=2):
    print(f"[FAIL] {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def sha256_file(path, buf=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(buf), b""):
            h.update(blk)
    return h.hexdigest()


def stat_sig(p):
    st = os.stat(p)
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def fmt_ts(sec):
    """秒 -> mm:ss 或 hh:mm:ss（跨小时才带小时，避免时间轴冗长）。"""
    sec = max(0.0, float(sec or 0.0))
    total = int(round(sec))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def exclusive_path(path):
    """排他命名：已存在则加 __r2/__r3，绝不覆盖历史稿。返回可用路径。"""
    path = Path(path)
    if not path.exists():
        return path
    n = 1
    while True:
        n += 1
        cand = path.with_name(f"{path.stem}__r{n}{path.suffix}")
        if not cand.exists():
            return cand


def exclusive_write(path, text):
    p = exclusive_path(path)
    with open(p, "x", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return p


# --------------------------------------------------------------------------
# 1) 定位 ffmpeg / ffprobe：PATH -> static_ffmpeg 包内置 -> 都没有
# --------------------------------------------------------------------------
def find_bin(name):
    hit = shutil.which(name)
    if hit:
        return hit
    try:
        import static_ffmpeg  # Windows 上常用的免装方案
        base = Path(static_ffmpeg.__file__).parent / "bin"
        for sub in ("win32", "linux", "darwin"):
            cand = base / sub / (name + (".exe" if os.name == "nt" else ""))
            if cand.is_file():
                return str(cand)
        cand = base / (name + (".exe" if os.name == "nt" else ""))
        if cand.is_file():
            return str(cand)
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# 2) 抽音轨：ffmpeg 优先，PyAV 兜底（PyAV 自带解码器，不依赖外部二进制）
# --------------------------------------------------------------------------
def extract_with_ffmpeg(ffmpeg, src, dst, clip=None):
    cmd = [ffmpeg, "-y", "-v", "error"]
    if clip:
        cmd += ["-ss", f"{clip[0]}", "-to", f"{clip[1]}"]
    cmd += ["-i", str(src), "-vn", "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(dst)]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not Path(dst).is_file() or Path(dst).stat().st_size <= 44:
        detail = (r.stderr or b"").decode("utf-8", "replace").strip()[-600:]
        raise RuntimeError(f"ffmpeg 抽音轨失败: {detail or '无输出'}")
    return "ffmpeg"


def extract_with_pyav(src, dst, clip=None):
    """PyAV 解码 -> mono 16k WAV。三个坑都在这里处理掉，见 references/pitfalls.md。"""
    import av
    import wave
    try:
        from av.audio.resampler import AudioResampler
    except Exception:
        AudioResampler = None
    import numpy as np

    container = av.open(str(src))
    if not container.streams.audio:
        container.close()
        raise RuntimeError("该文件没有音频轨")
    stream = container.streams.audio[0]
    resampler = AudioResampler(format="s16", layout="mono", rate=SR) if AudioResampler else None

    chunks = []
    for frame in container.decode(stream):
        t = float(frame.time or 0.0)
        if clip and not (clip[0] <= t < clip[1]):
            continue
        outs = [resampler.resample(frame)] if resampler else [[frame]]
        for group in outs:
            for of in (group or []):
                chunks.append(_frame_to_f32(of, np))
    # 坑 1：必须 flush，否则 resampler 缓冲的尾部帧丢失，时长被砍掉 1/3~2/3
    if resampler:
        for of in (resampler.resample(None) or []):
            chunks.append(_frame_to_f32(of, np))
    container.close()
    if not chunks:
        raise RuntimeError("解码结果为空")
    audio = np.concatenate(chunks).astype("float32")
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(dst), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm.tobytes())
    return "pyav"


def _frame_to_f32(frame, np):
    # 坑 3：PyAV 18+ 的 to_ndarray() 不接受 format= 关键字，直接无参调用
    arr = frame.to_ndarray()
    name = frame.format.name
    if name.startswith("s"):
        arr = arr.astype("float32") / float(1 << (8 * arr.dtype.itemsize - 1))
    elif name.startswith(("flt", "dbl")):
        arr = arr.astype("float32")
    else:
        arr = arr.astype("float32")
    if arr.ndim == 2:
        arr = arr[0] if arr.shape[0] == 1 else arr.mean(axis=0)  # 坑 2：resampler 已重采样，只做降混
    return arr.reshape(-1).astype("float32")


def wav_duration(path):
    import wave
    with wave.open(str(path), "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())


def container_duration(ffprobe, src):
    if not ffprobe:
        return None
    r = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(src)],
        capture_output=True, text=True,
    )
    try:
        return float((r.stdout or "").strip())
    except Exception:
        return None


# --------------------------------------------------------------------------
# 3) 可选降噪：谱减法。BGM 明显时帮识别，安静人声时反而伤细节，故默认 auto 判定
# --------------------------------------------------------------------------
def noise_floor_ratio(wav_path):
    """用「最安静 10% 帧」与「最响 10% 帧」的能量比粗估本底噪声。比值越高说明底噪越重。"""
    try:
        import numpy as np
        import wave
    except Exception:
        return None
    with wave.open(str(wav_path), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    x = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
    if x.size < SR:
        return None
    hop, win = 512, 1024
    n = 1 + (x.size - win) // hop
    if n < 16:
        return None
    idx = np.arange(n)[:, None] * hop + np.arange(win)[None, :]
    e = np.sum(x[idx] ** 2, axis=1) + 1e-12
    lo = float(np.mean(np.sort(e)[: max(1, n // 10)]))
    hi = float(np.mean(np.sort(e)[-max(1, n // 10):]))
    return (lo / hi) ** 0.5


def spectral_denoise(src_wav, dst_wav, alpha=1.6, floor=0.06):
    import numpy as np
    import wave
    from scipy import signal  # 缺 scipy 时由调用方降级

    with wave.open(str(src_wav), "rb") as wf:
        x = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").astype("float32") / 32768.0
    n_fft, hop = 1024, 256
    win = signal.windows.hann(n_fft, sym=False)
    n = 1 + max(0, (x.size - n_fft)) // hop
    if n < 8:
        shutil.copyfile(src_wav, dst_wav)
        return False
    idx = np.arange(n)[:, None] * hop + np.arange(n_fft)[None, :]
    frames = x[idx] * win
    energies = np.sum(frames ** 2, axis=1)
    quiet = np.argsort(energies)[: max(1, n // 10)]
    noise_mag = np.mean(np.abs(np.fft.rfft(frames[quiet], n=n_fft)), axis=0)

    out = np.zeros(n_fft + hop * n, dtype="float32")
    wsum = np.zeros_like(out)
    for i in range(n):
        spec = np.fft.rfft(frames[i], n=n_fft)
        mag = np.maximum(np.abs(spec) - alpha * noise_mag, floor * noise_mag)
        seg = np.fft.irfft(mag * np.exp(1j * np.angle(spec)), n=n_fft) * win
        s = i * hop
        out[s:s + n_fft] += seg
        wsum[s:s + n_fft] += win ** 2
    nz = wsum > 1e-8
    out[nz] /= wsum[nz]
    out = np.clip(out[: x.size], -1.0, 1.0)
    with wave.open(str(dst_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes((out * 32767.0).astype("<i2").tobytes())
    return True


# --------------------------------------------------------------------------
# 4) 三个识别引擎，统一归一成 [{start,end,text}]
# --------------------------------------------------------------------------
def hf_model_cached(model_id):
    """权重落盘才算缓存可用；只有 config 的半截目录会让离线模式误判。"""
    root = Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface")) / "hub"
    d = root / ("models--" + model_id.replace("/", "--"))
    if not d.is_dir():
        return False
    if list(d.glob("blobs/*.incomplete")):
        return False
    return any((s / "model.bin").is_file() or any(s.glob("*.safetensors"))
               for s in d.glob("snapshots/*"))


def engine_faster_whisper(wav, a):
    try:
        from faster_whisper import WhisperModel
    except Exception as e:
        raise RuntimeError(f"未安装 faster-whisper（{type(e).__name__}）；先跑 env_check.py --fix") from None
    mid = a.model if "/" in a.model else f"Systran/faster-whisper-{a.model}"
    common = dict(device="cpu", compute_type=a.compute, cpu_threads=max(1, min(8, os.cpu_count() or 4)))
    try:
        model = WhisperModel(mid, local_files_only=True, **common)
        how = "本地缓存"
    except Exception:
        if a.offline:
            die(f"离线模式但本地缓存不可用：{mid}"
                + ("（权重未下载完，去掉 --offline 续传）" if not hf_model_cached(mid) else ""))
        log(f"      模型未缓存，联网下载（{os.environ.get('HF_ENDPOINT')}）…")
        model = WhisperModel(mid, local_files_only=False, **common)
        how = "已下载"
    lang = None if a.language == "auto" else a.language
    kw = dict(language=lang, beam_size=a.beam, condition_on_previous_text=False)
    if a.prompt:
        kw["initial_prompt"] = a.prompt
    try:
        it, info = model.transcribe(wav, vad_filter=True,
                                    vad_parameters=dict(min_silence_duration_ms=300, speech_pad_ms=200), **kw)
        segs = [dict(start=float(s.start), end=float(s.end), text=(s.text or "").strip()) for s in it]
    except Exception as e:
        # 缺少 onnxruntime 时 Silero VAD 不可用，退回不开 VAD 再跑一次
        log(f"      [WARN] 带 VAD 失败（{type(e).__name__}），改用无 VAD 重试")
        it, info = model.transcribe(wav, vad_filter=False, **kw)
        segs = [dict(start=float(s.start), end=float(s.end), text=(s.text or "").strip()) for s in it]
    return segs, getattr(info, "language", None), f"{mid}（{how}, cpu/{a.compute}）"


def engine_funasr(wav, a):
    exe = shutil.which("funasr")
    if not exe:
        raise RuntimeError("未找到 funasr 命令；先跑 env_check.py 查看安装方式")
    outdir = Path(tempfile.mkdtemp(prefix="xhs-funasr-"))
    try:
        cmd = [exe, str(wav), "--model", a.funasr_model, "--language", "zh",
               "--device", "cpu", "--timestamps", "--output-format", "json", "--output-dir", str(outdir)]
        if a.hotwords:
            cmd += ["--hotwords", a.hotwords]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        files = sorted(outdir.rglob("*.json"))
        if r.returncode != 0 or not files:
            raise RuntimeError(f"funasr 执行失败：{(r.stderr or r.stdout or '')[-600:]}")
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        segs = _segments_from_funasr(payload)
        if not segs:
            raise RuntimeError("funasr JSON 结构无法解析（不同版本差异大），改用 faster-whisper 或 mlx")
        return segs, "zh", f"funasr/{a.funasr_model}（cpu）"
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


def _segments_from_funasr(payload):
    """FunASR 不同模型/版本字段不一，按候选键逐层找「有 start/end/text 的列表」。"""
    def walk(node):
        if isinstance(node, list) and node and isinstance(node[0], dict):
            keys = set(node[0])
            if {"text"} <= keys and ({"start", "end"} <= keys or {"timestamp"} & keys):
                out = []
                for it in node:
                    if "start" in it and "end" in it:
                        s, e = float(it["start"]), float(it["end"])
                    else:
                        ts = it.get("timestamp") or [0, 0]
                        s, e = float(ts[0]) / 1000.0, float(ts[-1]) / 1000.0
                    if s > 1000:  # 毫秒制
                        s, e = s / 1000.0, e / 1000.0
                    txt = (it.get("text") or "").strip()
                    if txt:
                        out.append(dict(start=s, end=e, text=txt))
                return out
        if isinstance(node, dict):
            for v in node.values():
                hit = walk(v)
                if hit:
                    return hit
        if isinstance(node, list):
            for v in node:
                hit = walk(v)
                if hit:
                    return hit
        return None
    return walk(payload) or []


def engine_mlx(wav, a):
    exe = shutil.which("mlx_whisper")
    if not exe:
        raise RuntimeError("未找到 mlx_whisper 命令（仅 Apple 芯片可用）")
    model = a.mlx_model
    outdir = Path(tempfile.mkdtemp(prefix="xhs-mlx-"))
    env = dict(os.environ)
    env.setdefault("HF_HUB_DISABLE_XET", "1")
    if a.offline or hf_model_cached(model):
        env["HF_HUB_OFFLINE"] = "1"   # mlx 默认会联网校验版本，代理环境下直接 502
    try:
        cmd = [exe, str(wav), "--model", model, "--output-format", "json",
               "--output-dir", str(outdir), "--output-name", "asr", "--verbose", "False",
               "--condition-on-previous-text", "False"]
        if a.language != "auto":
            cmd += ["--language", a.language]
        if a.prompt:
            cmd += ["--initial-prompt", a.prompt]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        files = sorted(outdir.glob("*.json"))
        if r.returncode != 0 or not files:
            raise RuntimeError(f"mlx_whisper 执行失败：{(r.stderr or r.stdout or '')[-600:]}")
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        segs = [dict(start=float(s["start"]), end=float(s["end"]), text=(s.get("text") or "").strip())
                for s in payload.get("segments", []) if (s.get("text") or "").strip()]
        return segs, payload.get("language"), f"{model}（本地 mlx）"
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


ENGINES = {
    "faster-whisper": engine_faster_whisper,
    "funasr": engine_funasr,
    "mlx": engine_mlx,
}


def pick_engine(pref):
    if pref != "auto":
        return pref
    try:
        import faster_whisper  # noqa: F401
        return "faster-whisper"
    except Exception:
        pass
    if shutil.which("funasr"):
        return "funasr"
    if shutil.which("mlx_whisper"):
        return "mlx"
    die("没有可用识别引擎。先跑 env_check.py 查看安装方式（Windows 建议 faster-whisper）")


# --------------------------------------------------------------------------
# 5) 质量自检：空稿、乱序、覆盖不足、连续重复幻觉
# --------------------------------------------------------------------------
def qa(segs, duration, clip=None):
    flags = []
    if not segs:
        return ["空稿：模型未返回任何片段"], 0.0
    bad = [s for s in segs if not s["text"] or s["end"] <= s["start"]]
    if bad:
        flags.append(f"{len(bad)} 个片段时间为零或为空，已剔除")
        segs[:] = [s for s in segs if s["text"] and s["end"] > s["start"]]
    inv = sum(1 for i in range(1, len(segs)) if segs[i]["start"] < segs[i - 1]["start"])
    if inv > max(1, len(segs) * 0.05):
        flags.append(f"时间戳乱序 {inv}/{len(segs)}，需人工检查")
    base = clip[0] if clip else 0.0
    span = (clip[1] - clip[0]) if clip else duration
    last_end = (max(s["end"] for s in segs) - base) if segs else 0.0
    coverage = min(1.0, (last_end / span)) if span else 0.0
    if span and coverage < 0.85:
        flags.append(f"覆盖不足：末尾 {fmt_ts(last_end)} / 全长 {fmt_ts(span)}，可能尾部丢失")
    run, prev = 1, None
    for s in segs:
        t = s["text"].strip()
        run = run + 1 if (prev is not None and t == prev) else 1
        prev = t
        if run >= 3:
            flags.append(f"连续重复片段（疑似幻觉）出现在 {fmt_ts(s['start'])} 附近：{t[:20]}")
            break
    short = [s for s in segs if len(s["text"]) <= 2]
    if len(short) > max(3, len(segs) * 0.2):
        flags.append(f"{len(short)} 个片段极短（≤2 字），建议检查切分或降噪")
    return flags, coverage


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="小红书视频音轨提取与本地识别")
    ap.add_argument("--input", required=True, help="已下载到本地的媒体文件")
    ap.add_argument("--outdir", default=None, help="输出目录，默认 <输入目录>/transcribe")
    ap.add_argument("--engine", default="auto", choices=["auto", *ENGINES])
    ap.add_argument("--model", default="medium", help="faster-whisper 模型：small/medium/large-v3 或完整仓库名")
    ap.add_argument("--compute", default="int8", help="faster-whisper 计算类型")
    ap.add_argument("--language", default="auto", help="auto/zh/en/...；auto 交模型检测")
    ap.add_argument("--beam", type=int, default=5)
    ap.add_argument("--prompt", default=None, help="initial prompt：人名、品牌、术语，用顿号分隔")
    ap.add_argument("--hotwords", default=None, help="funasr 热词，逗号分隔")
    ap.add_argument("--funasr-model", default="paraformer", help="paraformer/sensevoice/paraformer-en")
    ap.add_argument("--mlx-model", default="mlx-community/whisper-large-v3-turbo")
    ap.add_argument("--denoise", default="auto", choices=["auto", "on", "off"])
    ap.add_argument("--clip", default=None, help="只重跑某段：起始秒,结束秒（异常段复核用）")
    ap.add_argument("--offline", action="store_true", help="不联网，只用本地缓存模型")
    ap.add_argument("--json", action="store_true", help="stdout 只输出最终 JSON")
    a = ap.parse_args()

    os.environ.setdefault("HF_ENDPOINT", HF_MIRROR)
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")       # xet 通道国内常超时
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    if a.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
    # 不要覆盖 HF_HOME，否则看不到已下载缓存，会误判「未缓存」而重下几个 G

    src = Path(a.input).expanduser().resolve()
    if not src.is_file():
        die(f"输入文件不存在：{src}")
    if src.suffix.lower() not in AUDIO_EXT:
        log(f"[WARN] 扩展名 {src.suffix} 不在常见音视频列表中，仍尝试解码")
    outdir = Path(a.outdir).expanduser().resolve() if a.outdir else src.parent / "transcribe"
    outdir.mkdir(parents=True, exist_ok=True)

    clip = None
    if a.clip:
        try:
            s, e = [float(x) for x in a.clip.split(",")]
            clip = (s, e)
        except Exception:
            die("--clip 格式应为 起始秒,结束秒，例如 62,78")

    t0 = time.time()
    # 快照：识别前后都不许源文件变动，否则时间轴与内容对不上
    sig = stat_sig(src)
    digest = sha256_file(src)
    if not a.json:
        log(f"[1/6] 素材 {src.name}  {sig['size']} 字节  sha256={digest[:16]}…")

    ffmpeg, ffprobe = find_bin("ffmpeg"), find_bin("ffprobe")
    tmp = Path(tempfile.mkdtemp(prefix="xhs-transcribe-"))
    try:
        raw_wav = tmp / "raw16k.wav"
        used = None
        last_err = None
        if ffmpeg:
            try:
                used = extract_with_ffmpeg(ffmpeg, src, raw_wav, clip)
            except Exception as e:
                last_err = e
        if used is None:
            try:
                used = extract_with_pyav(src, raw_wav, clip)
            except Exception as e:
                die(f"抽音轨失败。ffmpeg={'有' if ffmpeg else '缺失'}；PyAV 兜底也失败：{e}"
                    + (f"；ffmpeg 错误：{last_err}" if last_err else ""))
        duration = wav_duration(raw_wav)
        if duration < 0.5:
            die(f"音轨时长为 {duration:.2f}s，文件可能损坏或该视频无声音")
        c_dur = container_duration(ffprobe, src)
        if not a.json:
            log(f"[2/6] 抽音轨完成（{used}）：{fmt_ts(duration)}"
                + (f"，容器时长 {fmt_ts(c_dur)}" if c_dur else ""))

        # 降噪决策
        denoised = False
        asr_wav = raw_wav
        if a.denoise != "off":
            ratio = noise_floor_ratio(raw_wav)
            want = a.denoise == "on" or (ratio is not None and ratio > 0.12)
            if want:
                try:
                    den_wav = tmp / "denoised16k.wav"
                    denoised = spectral_denoise(raw_wav, den_wav)
                    if denoised:
                        asr_wav = den_wav
                except Exception as e:
                    log(f"      [WARN] 降噪跳过（{type(e).__name__}，缺 numpy/scipy？）")
            if not a.json:
                log(f"[3/6] 降噪：{'已应用（谱减法）' if denoised else '未应用'}"
                    + (f"，底噪比 {ratio:.3f}" if ratio is not None else ""))

        eng = pick_engine(a.engine)
        if not a.json:
            log(f"[4/6] 识别引擎 {eng} …")
        segs, lang, model_desc = ENGINES[eng](asr_wav, a)
        if clip:
            segs = [dict(s, start=s["start"] + clip[0], end=s["end"] + clip[0]) for s in segs]

        flags, coverage = qa(segs, duration, clip)
        if not segs:
            die("识别结果为空，未生成稿件。检查音轨是否有语音，或换引擎/加大 --model")

        # 快照复核：识别期间源文件被改动，则时间轴与内容不可信（必须在落盘前判定）
        changed = stat_sig(src) != sig or sha256_file(src) != digest
        if changed:
            flags.append("源文件在识别期间发生变化，本次结果需复核")

        # 写盘（排他，绝不覆盖历史稿）
        payload = {
            "source": {"name": src.name, "path": str(src), "sha256": digest, "bytes": sig["size"]},
            "media": {"duration": round(duration, 3), "container_duration": c_dur,
                      "clip": list(clip) if clip else None, "decoder": used},
            "asr": {"engine": eng, "model": model_desc, "language": lang,
                    "denoise": denoised, "offline": bool(a.offline)},
            "stats": {"segments": len(segs), "coverage": round(coverage, 4),
                      "chars": sum(len(s["text"]) for s in segs), "elapsed": round(time.time() - t0, 1)},
            "flags": flags,
            "segments": segs,
        }
        seg_path = exclusive_write(outdir / "segments.json",
                                   json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

        lines = [f"# {src.stem} 原始识别稿",
                 f"# 素材 sha256: {digest}",
                 f"# 时长: {fmt_ts(duration)}    语言: {lang}    引擎: {model_desc}",
                 f"# 片段数: {len(segs)}    降噪: {'是' if denoised else '否'}    耗时: {payload['stats']['elapsed']}s",
                 "# 格式: [开始 - 结束] 文本", ""]
        for s in segs:
            lines.append(f"[{fmt_ts(s['start'])} - {fmt_ts(s['end'])}] {s['text']}")
        if flags:
            lines += ["", "# 自检提示:"] + [f"#   - {f}" for f in flags]
        txt_path = exclusive_write(outdir / "transcript_raw.txt", "\n".join(lines) + "\n")

        receipt = {
            "ok": True, "engine": eng, "model": model_desc, "language": lang,
            "duration": round(duration, 3), "segments": len(segs),
            "coverage": round(coverage, 4), "denoise": denoised,
            "flags": flags, "elapsed": round(time.time() - t0, 1),
            "outputs": {"segments": str(seg_path), "raw": str(txt_path)},
        }
        if a.json:
            print(json.dumps(receipt, ensure_ascii=False))
        else:
            log(f"[5/6] 片段 {len(segs)} 条，覆盖 {coverage*100:.1f}%，用时 {receipt['elapsed']}s")
            for f in flags:
                log(f"      [WARN] {f}")
            log(f"[6/6] 输出：{seg_path}")
            log(f"            {txt_path}")
            log(json.dumps(receipt, ensure_ascii=False))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("已中断")
