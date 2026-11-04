#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""env_check.py - 转写环境体检与修复建议（Windows / macOS / Linux）

先体检，再决定装什么。默认只报告，不改环境；加 --fix 才执行安装命令。

    python env_check.py                # 体检
    python env_check.py --json         # 机器可读
    python env_check.py --fix          # 按推荐引擎安装缺失依赖
    python env_check.py --fix --dry-run  # 只打印将执行的命令

已知坑（Windows 尤其常见）：部分较老 CPU 上 ctranslate2 新版会让模型加载直接
段错误退出（exit 139 / 0xC0000005），--fix 会给出可用的降级版本组合。
"""
import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PKGS = ["faster_whisper", "ctranslate2", "onnxruntime", "numpy", "scipy", "av", "static_ffmpeg"]


def ver(mod_name):
    try:
        m = importlib.import_module(mod_name)
    except Exception:
        return None
    return getattr(m, "__version__", "?") if not hasattr(m, "version") else getattr(m, "version", "?")


def find_bin(name):
    hit = shutil.which(name)
    if hit:
        return hit
    try:
        import static_ffmpeg
        base = Path(static_ffmpeg.__file__).parent / "bin"
        exe = name + (".exe" if os.name == "nt" else "")
        for cand in [base / exe, base / "win32" / exe, base / "linux" / exe, base / "darwin" / exe]:
            if cand.is_file():
                return str(cand)
    except Exception:
        pass
    return None


def cached_models():
    """只把「权重已落盘」的模型算作可用：只有 config.json 的半截缓存会让离线模式误判。"""
    root = Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface")) / "hub"
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.glob("models--*")):
        if "whisper" not in d.name.lower():
            continue
        name = d.name.replace("models--", "").replace("--", "/")
        snaps = list(d.glob("snapshots/*"))
        weights = any((s / "model.bin").is_file() or any(s.glob("*.safetensors")) for s in snaps)
        pending = list(d.glob("blobs/*.incomplete"))
        if not weights:
            out.append(f"{name}（权重未下载完）")
        elif pending:
            out.append(f"{name}（有 {len(pending)} 个文件未完成）")
        else:
            out.append(name)
    return out


def main():
    ap = argparse.ArgumentParser(description="转写环境体检")
    ap.add_argument("--engine", default="auto", choices=["auto", "faster-whisper", "funasr", "mlx"])
    ap.add_argument("--fix", action="store_true", help="执行安装/修复命令")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令，不执行")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    rep = {
        "os": f"{os.name} {sys.platform}",
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "in_venv": sys.prefix != getattr(sys, "base_prefix", sys.prefix),
        "ffmpeg": find_bin("ffmpeg"),
        "ffprobe": find_bin("ffprobe"),
        "cli": {k: bool(shutil.which(v)) for k, v in
                [("funasr", "funasr"), ("mlx_whisper", "mlx_whisper")]},
        "pkgs": {p: ver(p) for p in PKGS},
        "cached_models": cached_models(),
        "issues": [],
        "fix": [],
    }
    py_exe = sys.executable
    pip = [py_exe, "-m", "pip", "install"]

    if sys.version_info < (3, 9):
        rep["issues"].append(f"Python {rep['python']} 过低，需 3.9+")
    if not rep["ffmpeg"]:
        rep["issues"].append("缺 ffmpeg（有 PyAV 时仍可解码，但无法 ffprobe 交叉核时长）")
        rep["fix"].append(pip + ["static-ffmpeg"])

    engines = {}
    engines["faster-whisper"] = bool(rep["pkgs"]["faster_whisper"])
    engines["funasr"] = rep["cli"]["funasr"]
    engines["mlx"] = rep["cli"]["mlx_whisper"]
    rep["engines"] = engines

    want = a.engine
    if want == "auto":
        want = "faster-whisper" if engines["faster-whisper"] else ("funasr" if engines["funasr"] else
                                                                   ("mlx" if engines["mlx"] else "faster-whisper"))
    rep["recommended"] = want
    if not any(engines.values()):
        rep["issues"].append("没有任何可用识别引擎")

    if want == "faster-whisper" and not engines["faster-whisper"]:
        rep["fix"].append(pip + ["faster-whisper", "onnxruntime"])
    if not rep["pkgs"]["numpy"] or not rep["pkgs"]["scipy"]:
        rep["fix"].append(pip + ["numpy", "scipy"])   # 仅降噪需要；不影响识别
    if not rep["pkgs"]["av"] and not rep["ffmpeg"]:
        rep["fix"].append(pip + ["av"])
    if want == "mlx" and not engines["mlx"]:
        rep["fix"].append(["uv", "tool", "install", "mlx-whisper"])

    # Windows 老 CPU 上 ctranslate2 新版会让模型加载直接崩溃
    ct = rep["pkgs"].get("ctranslate2")
    if ct and a.engine in ("auto", "faster-whisper"):
        rep["note"] = ("若加载模型时报 exit 139 / 0xC0000005，降级为 "
                       "ctranslate2==4.6.0、setuptools<81、onnxruntime==1.20.1 再试")

    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print("=== 转写环境体检 ===")
        print(f"系统            : {rep['os']}")
        print(f"Python          : {rep['python']}（{'虚拟环境' if rep['in_venv'] else '基础解释器'}）")
        print(f"解释器          : {py_exe}")
        print(f"ffmpeg          : {rep['ffmpeg'] or '缺失'}")
        print(f"ffprobe         : {rep['ffprobe'] or '缺失'}")
        print("引擎            : " + ", ".join(f"{k}={'OK' if v else '缺'}" for k, v in engines.items()))
        print(f"推荐引擎        : {want}")
        print("包版本          : " + ", ".join(f"{k}={v or '未装'}" for k, v in rep["pkgs"].items()))
        print(f"已缓存 whisper 模型: {', '.join(rep['cached_models']) or '（无）'}")
        if rep.get("note"):
            print(f"提示            : {rep['note']}")
        if rep["issues"]:
            print("\n发现问题：")
            for i in rep["issues"]:
                print(f"  - {i}")
        else:
            print("\n[OK] 未发现阻断性问题")
        if rep["fix"]:
            print("\n建议执行（国内网络加 -i https://pypi.tuna.tsinghua.edu.cn/simple 可避免 pythonhosted 超时）：")
            for c in rep["fix"]:
                print("  " + " ".join(c))
        else:
            print("\n无需安装动作")

    if a.fix and rep["fix"]:
        if not rep["in_venv"]:
            print("\n[WARN] 当前不在虚拟环境中，安装会写进全局解释器；建议先建 venv。", file=sys.stderr)
        for cmd in rep["fix"]:
            print("[RUN] " + " ".join(cmd))
            if a.dry_run:
                continue
            r = subprocess.run(cmd)
            if r.returncode != 0:
                print(f"[FAIL] 命令返回 {r.returncode}，后续修复终止", file=sys.stderr)
                sys.exit(1)
        print("[OK] 修复命令执行完毕，重新运行本脚本确认")


if __name__ == "__main__":
    main()
