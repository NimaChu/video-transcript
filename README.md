# 视频/音频转写（video-transcript）

把视频或音频转成带时间戳的文字稿，并产出一份可交付 Markdown：来源与时长、核心总结、Mermaid 思维导图、按时间分段的完整转写。**识别全部在本机完成，音视频不上传。**

两种入口：本地音视频文件（mp4/mkv/mov/webm/m4a/mp3/wav），或公开的小红书视频分享链接。

支持 Windows / macOS / Linux。识别引擎三选一：`faster-whisper`（全平台首选）、`funasr`（中文口语）、`mlx`（Apple 芯片加速）。

## 安装

1. 把整个目录放到技能目录下：
   - WorkBuddy：`%USERPROFILE%\.workbuddy\skills\video-transcript\`（Windows）或 `~/.workbuddy/skills/video-transcript/`（macOS/Linux）
   - Codex：`%USERPROFILE%\.codex\skills\video-transcript\`
2. 体检环境：

```bash
python scripts/env_check.py
```

3. 按提示装依赖。Windows 推荐（用国内镜像，否则 `pythonhosted.org` 常超时）：

```bash
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple faster-whisper onnxruntime
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple static-ffmpeg numpy scipy
```

## 用法

```bash
# 1) 抽音轨 + 识别（本地文件直接这样用；小红书链接先按 SKILL.md 第三、四步下载）
python scripts/transcribe.py --input "素材.mp4" --outdir "out" \
  --engine faster-whisper --model medium --language zh \
  --prompt "人名、品牌、术语"

# 2) 合并分段 + 生成 Markdown 骨架
python scripts/merge_segments.py --segments "out/segments.json" --outdir "out" \
  --title "标题" --source "<链接或文件>"

# 3) 按 SKILL.md 第七步补齐总结与思维导图
```

异常段复核（只重跑某几秒，时间轴自动平移回全局）：

```bash
python scripts/transcribe.py --input "素材.mp4" --outdir "out/recheck" --clip 62,78
```

## 产出

| 文件 | 内容 |
| --- | --- |
| `segments.json` | 结构化分段 + 素材 SHA-256 + 引擎信息 + 自检 flags |
| `transcript_raw.txt` | 带 `[mm:ss - mm:ss]` 时间戳的原始识别稿 |
| `paragraphs.json` | 合并后的段落与分节结构 |
| `<标题>_完整转写.md` | 交付稿骨架（总结与导图待补） |

所有输出**排他创建**：同名文件已存在时自动加 `__r2`、`__r3`，不覆盖历史稿。

## 设计取舍

- **脚本只做确定的事**：解码、识别、分段、字符比对、写盘。总结、导图、错词判断交给 agent —— 这些需要理解内容，脚本做只会更差。
- **合并前后强制字符比对**：合并只调段落边界，字数必须严格相等，不等就停，宁可失败也不交一份漏字的稿。
- **不用「解析站」**：真实播放地址从浏览器网络请求里取，避免第三方接口带来的失效与合规问题。
- **降噪默认自动**：只在高底噪（BGM 明显）时启用谱减法，安静人声强开反而掉精度。
- **双路解码**：ffmpeg 优先，缺了就 PyAV 兜底，Windows 上不必先装外部二进制。

## 已知限制

- 不做说话人分离、翻译、双语字幕、ASS 排版。多人对话需要人工判断说话人。
- 语音识别不保证逐字准确。人名、数字、品牌必须按 SKILL.md 第六步复核。
- 首次使用需下载模型（`medium` 约 1.5 GB）；之后可 `--offline` 复用缓存。

## 目录

```
SKILL.md                     工作流与纪律（agent 读这个）
README.md                    安装与用法
scripts/transcribe.py        解码 + 识别 + 自检
scripts/merge_segments.py    合并 + 字符校验 + 骨架
scripts/env_check.py         环境体检
references/pitfalls.md       坑点与取舍记录
```
