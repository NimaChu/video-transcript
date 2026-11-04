#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""merge_segments.py - 把 segments.json 合并成段落稿，并生成 Markdown 骨架

只做「排版与校验」，不摘要、不改写、不润色：段落只按停顿/句末标点/长度切开，
逐字保留全部识别内容，合并前后做字符总量比对，不一致立即停止（防漏字）。

输出（均排他创建，不覆盖历史稿）：
    paragraphs.json   段落结构（供 agent 复核与引用）
    draft.md          Markdown 骨架：文件信息 + 待填总结/导图 + 按 2~3 分钟分节的正文

用法：
    python merge_segments.py --segments out/segments.json --outdir out \
        --title "视频标题" --source "https://www.xiaohongshu.com/..."
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SENT_END = set("。！？!?…；;")
CJK = r"\u4e00-\u9fff\u3400-\u4dbf"
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


def safe_name(name, limit=80):
    """Windows 非法字符会直接导致写盘失败，统一替换；过长则截断。"""
    out = _ILLEGAL.sub("_", str(name)).strip(" .")
    return (out[:limit] or "视频")


def norm_punct(text):
    """只做排版规范化，不改字：中文语境下的半角标点换全角，去掉汉字间多余空格。"""
    text = re.sub(rf"(?<=[{CJK}]),(?=[{CJK}])", "，", text)
    text = re.sub(rf"(?<=[{CJK}]);(?=[{CJK}])", "；", text)
    text = re.sub(rf"(?<=[{CJK}])\?(?=[{CJK}])", "？", text)
    text = re.sub(rf"(?<=[{CJK}])!(?=[{CJK}])", "！", text)
    text = re.sub(rf"(?<=[{CJK}])\.(?=[{CJK}])", "。", text)
    text = re.sub(rf"(?<=[{CJK}]) +(?=[{CJK}])", "", text)
    return text


def log(m):
    print(m, flush=True)


def die(m, code=2):
    print(f"[FAIL] {m}", file=sys.stderr, flush=True)
    sys.exit(code)


def fmt_ts(sec):
    sec = max(0.0, float(sec or 0.0))
    total = int(round(sec))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def exclusive_write(path, text):
    path = Path(path)
    if path.exists():
        n = 1
        while True:
            n += 1
            cand = path.with_name(f"{path.stem}__r{n}{path.suffix}")
            if not cand.exists():
                path = cand
                break
    with open(path, "x", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def load_segments(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        die(f"读不了 {path}：{e}")
    segs = data.get("segments") if isinstance(data, dict) else None
    if not isinstance(segs, list) or not segs:
        die("segments.json 里没有可用片段")
    clean = []
    for i, s in enumerate(segs):
        try:
            st, en, tx = float(s["start"]), float(s["end"]), (s.get("text") or "").strip()
        except Exception:
            die(f"第 {i+1} 个片段字段缺失或非数值：{s}")
        if en <= st:
            die(f"第 {i+1} 个片段时间非正：{st}->{en}")
        if tx:
            clean.append(dict(start=st, end=en, text=tx))
    if not clean:
        die("所有片段文本都为空")
    inv = sum(1 for i in range(1, len(clean)) if clean[i]["start"] < clean[i - 1]["start"] - 0.01)
    if inv > max(1, len(clean) * 0.05):
        die(f"时间戳严重乱序 {inv}/{len(clean)}，先修识别结果再合并")
    return data, clean


def merge(segs, gap, maxlen):
    paras, cur = [], None
    for s in segs:
        if cur is None:
            cur = dict(start=s["start"], end=s["end"], parts=[s["text"]])
            continue
        span = s["start"] - cur["end"]
        acc = sum(len(p) for p in cur["parts"])
        brk = (span >= gap) or (cur["parts"][-1][-1:] in SENT_END and acc >= maxlen * 0.6) \
            or (acc + len(s["text"]) > maxlen)
        if brk:
            paras.append(cur)
            cur = dict(start=s["start"], end=s["end"], parts=[s["text"]])
        else:
            cur["parts"].append(s["text"])
            cur["end"] = s["end"]
    if cur:
        paras.append(cur)
    for p in paras:
        p["text"] = "".join(p.pop("parts"))
    return paras


def group_sections(paras, target):
    sections, cur = [], None
    for p in paras:
        if cur is None:
            cur = dict(start=p["start"], end=p["end"], paras=[p])
            continue
        if p["end"] - cur["start"] > target:
            sections.append(cur)
            cur = dict(start=p["start"], end=p["end"], paras=[p])
        else:
            cur["paras"].append(p)
            cur["end"] = p["end"]
    if cur:
        sections.append(cur)
    return sections


def main():
    ap = argparse.ArgumentParser(description="合并识别片段并生成 Markdown 骨架")
    ap.add_argument("--segments", required=True, help="transcribe.py 产出的 segments.json")
    ap.add_argument("--outdir", default=None, help="默认与 segments.json 同目录")
    ap.add_argument("--gap", type=float, default=0.8, help="停顿 ≥ N 秒即分段（默认 0.8）")
    ap.add_argument("--maxlen", type=int, default=60, help="单段超过 N 字即分段（默认 60）")
    ap.add_argument("--section", type=int, default=150, help="每节目标秒数（默认 150s，即 2.5 分钟）")
    ap.add_argument("--punct", default="zh", choices=["zh", "keep"],
                    help="zh：中文语境的半角标点转全角（不改字）；keep：原样保留")
    ap.add_argument("--title", default=None, help="视频标题，用于文件名与 H1")
    ap.add_argument("--source", default=None, help="原始分享链接")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    seg_path = Path(a.segments).expanduser().resolve()
    if not seg_path.is_file():
        die(f"找不到 {seg_path}")
    outdir = Path(a.outdir).expanduser().resolve() if a.outdir else seg_path.parent
    outdir.mkdir(parents=True, exist_ok=True)

    data, segs = load_segments(seg_path)
    paras = merge(segs, a.gap, a.maxlen)

    raw_chars = "".join(s["text"] for s in segs)
    merged_chars = "".join(p["text"] for p in paras)
    if len(raw_chars) != len(merged_chars) or raw_chars != merged_chars:
        die(f"合并前后不一致（片段 {len(raw_chars)} 字 / 合并 {len(merged_chars)} 字），疑似漏字，已停止")

    sections = group_sections(paras, a.section)
    punct_applied = False
    if a.punct == "zh":
        changed = 0
        for p in paras:
            new = norm_punct(p["text"])
            if new != p["text"]:
                changed += 1
            p["text"] = new
        punct_applied = changed > 0
    meta = data.get("media", {}) if isinstance(data, dict) else {}
    asr = data.get("asr", {}) if isinstance(data, dict) else {}
    stats = data.get("stats", {}) if isinstance(data, dict) else {}
    src = data.get("source", {}) if isinstance(data, dict) else {}
    duration = meta.get("duration") or (segs[-1]["end"] if segs else 0)
    title = safe_name(a.title or Path(src.get("name") or "视频").stem)

    # paragraphs.json
    p_payload = {
        "source": src, "media": meta, "asr": asr,
        "merge": {"gap": a.gap, "maxlen": a.maxlen, "section": a.section,
                  "segments": len(segs), "paragraphs": len(paras), "sections": len(sections),
                  "chars": len(merged_chars), "char_check": "passed"},
        "sections": [{"start": round(s["start"], 3), "end": round(s["end"], 3),
                      "startText": fmt_ts(s["start"]), "endText": fmt_ts(s["end"]),
                      "paragraphs": s["paras"]} for s in sections],
    }
    p_path = exclusive_write(outdir / "paragraphs.json",
                             json.dumps(p_payload, ensure_ascii=False, indent=2) + "\n")

    # Markdown 骨架：总结与导图留占位，由 agent 依据正文填写
    L = [f"# {title}", ""]
    L.append(f"> 来源：{a.source or src.get('path') or '（待补）'}  ")
    L.append(f"> 时长：{fmt_ts(duration)}  ")
    if asr:
        L.append(f"> 识别：{asr.get('engine')} / {asr.get('model')}，语言 {asr.get('language')}，"
                 f"{stats.get('segments', len(segs))} 段  ")
    if src.get("sha256"):
        L.append(f"> 素材 SHA-256：`{src['sha256']}`  ")
    L.append("")
    L.append("## 核心总结")
    L.append("")
    L.append("<!-- TODO(agent)：3–6 条，只概括本视频内容，不引入视频外事实 -->")
    L.append("")
    L.append("## 思维导图")
    L.append("")
    L.append("<!-- TODO(agent)：把下方 mindmap 的分支替换为真实内容，删掉本行注释 -->")
    L.append("```mermaid")
    L.append("mindmap")
    L.append(f"  root(({title[:24]}))")
    L.append("    待补充")
    L.append("```")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 完整转写")
    L.append("")
    for i, s in enumerate(sections, 1):
        L.append(f"### [{fmt_ts(s['start'])}–{fmt_ts(s['end'])}] 第 {i} 节（标题待拟）")
        L.append("")
        for p in s["paras"]:
            L.append(p["text"])
            L.append("")
    L.append("<!-- 校验：片段 {0} 条 / 段落 {1} 段 / {2} 字，字符比对通过 -->".format(
        len(segs), len(paras), len(merged_chars)))
    draft = exclusive_write(outdir / f"{title}_完整转写.md", "\n".join(L))

    receipt = {"ok": True, "segments": len(segs), "paragraphs": len(paras),
               "sections": len(sections), "chars": len(merged_chars), "punct": punct_applied,
               "charCheck": "passed", "outputs": {"paragraphs": str(p_path), "draft": str(draft)}}
    if a.json:
        print(json.dumps(receipt, ensure_ascii=False))
    else:
        log(f"片段 {len(segs)} 条 -> 段落 {len(paras)} 段 -> {len(sections)} 节")
        log(f"字符校验：{len(raw_chars)} == {len(merged_chars)} 通过")
        log(f"骨架：{draft}")
        log(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("已中断")
