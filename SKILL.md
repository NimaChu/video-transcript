---
name: video-transcript
description: 把视频/音频转成带时间戳的文字稿。输入可以是本地音视频文件（mp4/mkv/mov/webm/m4a/mp3/wav），也可以是公开的小红书视频分享链接。产出原始识别稿与一份可交付 Markdown（来源与时长、核心总结、Mermaid 思维导图、按时间分段的完整转写）。全程本机识别，音视频不外发；Windows / macOS / Linux 通用，引擎支持 faster-whisper（首选）、funasr、mlx。触发词：视频转写、音频转文字、视频转文字稿、要带时间戳的稿子、小红书视频转写、字幕初稿、听写、transcribe video。
---

# 视频/音频转写（带时间戳）

把一段视频或音频，做成一份能直接交付的 Markdown 文字稿：

```
来源与时长 → 核心总结 → Mermaid 思维导图 → 按时间分段的完整转写
```

两种入口，后面流程完全一样：

| 入口 | 起点 |
| --- | --- |
| **本地文件** | 直接进第三步（体检）→ 第四步（识别） |
| **小红书链接** | 先做第一、二步（取真实地址、下载校验） |

分工原则：**agent 负责判断内容、复核错词、写总结**；**脚本负责解码、识别、分段、字符校验、写盘**这些机械且容易出错的活。脚本只依赖 Python 标准库，识别引擎按需安装。

## 一、边界（先看这条，能省掉一半返工）

- 本地文件：用户给的路径，不拿旧素材顶替；路径不明就问。
- 小红书链接：只处理公开可访问的视频。要登录、地区受限、需要会员，直接说明并请用户换链接或提供原始文件。
- 音视频只在本机处理，不上传任何平台。下载模型属于正常准备步骤，磁盘不足要先告知。
- 不做「解析站」。真实播放地址从浏览器网络请求里拿，不猜、不调第三方解析接口。
- **机器初稿不是终稿**。人名、数字、品牌、专业术语必须按第六步复核；不确定的标 `[听辨不清]`，不要凭想象补全。

## 二、快速路径

```bash
# 1) 体检（新机器先跑一次）
python scripts/env_check.py

# 2) 抽音轨 + 识别，产出 segments.json
python scripts/transcribe.py --input "素材.mp4" --outdir "out" --engine auto

# 3) 合并分段 + 字符校验，产出 Markdown 骨架
python scripts/merge_segments.py --segments "out/segments.json" --outdir "out" \
  --title "标题" --source "<链接或文件路径>"
```

第 2、3 步最后一行都是 JSON 回执（`ok` / `segments` / `coverage` / `flags` / `outputs`）。下一步的输入路径**必须来自回执**，不要手挑历史文件。然后按第七步填总结与导图。

## 三、小红书链接：取地址与下载

**1. 定位真实视频地址**

1. 用真实浏览器打开分享短链，等重定向和播放器加载完。登录弹窗不用管，先看网络请求。
2. 开发者工具 → Network，筛 `mp4`、`m3u8`、`media`、`video`。挑真正的媒体请求（`Content-Type: video/mp4` 或分片 range 请求），不要拿页面 HTML、播放器 JS 或封面图。
3. 记下标题、作者、发布时间、视频描述——后面写总结和关键词要用。
4. **签名 URL 会过期**，看到就立刻下载。

**2. 下载与完整性校验**

CDN 对长视频的 HTTP/2 下载常在尾部中断，首选 HTTP/1.1 + 断点续传：

```bash
# macOS / Linux / Windows 10+ 自带的 curl
curl.exe --http1.1 -L --fail --retry 4 --retry-all-errors \
  --silent --show-error "<视频URL>" -o "note.mp4"
```

PowerShell 兜底（老旧 curl 不支持 `--retry-all-errors` 时）：

```powershell
Invoke-WebRequest -Uri "<视频URL>" -OutFile "note.mp4" -Headers @{ "Referer" = "https://www.xiaohongshu.com/" }
```

中断续传用同一文件加 `-C -`。**必须确认拿全了**：`curl -I` 看 `Content-Length` 与本地字节数比对，或用 Range 请求看 `Content-Range` 总长度。第四步的脚本还会用 `ffprobe` 交叉核对容器时长与音轨时长，并对素材做 SHA-256 快照。

若续传响应不是 `206 Partial Content`，说明服务端不支持断点：把当前文件留作故障副本，从零重下。**绝不要把新下载追加到不完整文件尾部**——这种文件能解码，但时间轴全错。

## 四、体检与识别

```bash
python scripts/env_check.py            # 只报告
python scripts/env_check.py --json     # 机器可读
python scripts/env_check.py --fix      # 按推荐引擎装缺失依赖
```

体检看：Python 版本、ffmpeg/ffprobe、三个引擎可用性、numpy/scipy（降噪用）、已缓存的 whisper 模型（只把权重已落盘的算作可用）。**Windows 上最常见的是「没有 ffmpeg」**：脚本会用 PyAV 兜底解码，但那样没有 `ffprobe` 交叉核时长，会提示装 `static-ffmpeg` 或 `av`。

识别由 `transcribe.py` 一次完成，中间产物（16k WAV）只放临时目录，结束即清理：

```bash
python scripts/transcribe.py --input "note.mp4" --outdir "out" \
  --engine faster-whisper --model medium --language zh \
  --prompt "人名、品牌、术语"
```

引擎选择（`--engine auto` 按 faster-whisper → funasr → mlx 取第一个可用的）：

| 引擎 | 时间戳 | 平台 | 何时用 |
| --- | --- | --- | --- |
| `faster-whisper` | 原生片段级 | 全平台，CPU int8 | **首选**，Windows 就用它 |
| `funasr` | 原生（毫秒） | 全平台，需模型下载 | 纯中文口语、术语少；`--hotwords` 喂专名 |
| `mlx` | 原生片段级 | 仅 Apple 芯片 | Mac 上想更快且已有缓存模型 |

参数取舍：

- `--model`：`small` 快但中文同音字错得多；`medium` 是精度/速度平衡点；`large-v3` 更准但慢约 2 倍。交付级稿件用 `medium` 起步。
- `--language`：已知中文就写 `zh`，别用 `auto` 徒增误判。
- `--prompt`：放人名、品牌、行业术语，显著降低专名错字。**只写你确认的字**，写错等于引导模型写错。
- `--denoise auto`（默认）：用「最安静 10% 帧 / 最响 10% 帧」估底噪，底噪重才启用谱减法。BGM 很响的视频受益；安静人声不要强开 `on`，会伤气声和细节。

脚本自动做的自检会写进回执的 `flags`：空稿、时间戳乱序、覆盖不足（末尾丢内容）、连续重复片段（whisper 幻觉特征）、大量 ≤2 字碎片。**`flags` 非空就要处理，不能直接往下写稿。**

## 五、分段合并

```bash
python scripts/merge_segments.py --segments "out/segments.json" --outdir "out" \
  --title "标题" --source "<链接或文件>" --gap 0.8 --maxlen 60 --section 150
```

- 段落只按「停顿 ≥ gap 秒 / 句末标点 / 超过 maxlen 字」切，**逐字保留识别内容**，不摘要、不改写。
- 合并前后做**字符总量比对**，不一致立即停止（防漏字）。
- `--punct zh`（默认）只把中文语境里的半角 `,;?!.` 换成全角、去掉汉字间多余空格，不改字。
- `--section 150` 决定 `### [mm:ss–mm:ss]` 的时间节长度；口播类 2–3 分钟一节最易读。

## 六、复核异常段

以下情况必须回到音频逐段核对，不能只看文本顺不顺：

- 连续多个片段重复同一句话（幻觉）；
- 30 秒音频只出几个字；
- 人名、数字、英文缩写、否定词明显可疑；
- 时间轴大段缺失或跳跃；
- 两个引擎结论不一致——**两个都可能错**，一致也不代表对。

局部重跑（自动把时间轴平移回全局，不影响已确认的部分）：

```bash
python scripts/transcribe.py --input "note.mp4" --outdir "out/recheck" \
  --clip 62,78 --engine faster-whisper --model medium --language zh \
  --prompt "该段出现的专名和场景"
```

`--clip` 先切出该段音频再识别，天然切断前文对后文的污染。需要第二意见时换引擎再跑同一段，仍不确定的标 `[听辨不清]` 并保留原位置。

可以做的：规范标点、合并碎句、订正确认过的专名。
不可以做的：把转写改写成摘要、补出音频里没有的信息、凭语义「顺」掉一个你听不懂的词。

## 七、写成交付文档

在骨架上补齐两处占位，其余不动：

```markdown
# 标题

> 来源：<链接或文件>
> 时长：mm:ss
> 主题：一句话

## 核心总结

3–6 条，只概括这段素材说了什么，不引入素材外的事实。

## 思维导图

```mermaid
mindmap
  root((主题))
    分支一
      细节
```

---

## 完整转写

### [00:00–02:31] 小节标题

正文（逐字转写，可分段）
```

- 思维导图覆盖主论点、方法/步骤、案例、结论，层级控制在三层内。
- 小节标题写实质内容（「三条下载避坑」），不要留「第 1 节（标题待拟）」。
- 交付前删掉所有 `TODO(agent)` 注释与 `待补充` 节点。

## 八、交付前检查

- [ ] 开头是总结与导图，后面才是完整转写，顺序没错。
- [ ] 时间段从 `00:00` 连续覆盖到片尾，没有空洞或倒序。
- [ ] 回执里 `coverage` ≥ 0.95；`flags` 里的重复幻觉段已重跑替换。
- [ ] 没人名、数字、品牌被当成确定事实却没核对过。
- [ ] 没有 `TODO`、`待补充`、占位标题残留。
- [ ] 最终回复只给 Markdown 的绝对路径，外加一句「已完成 + 哪里存疑需要人工确认」。

## 九、常见故障

| 现象 | 原因与处理 |
| --- | --- |
| 加载模型直接崩，exit 139 / 0xC0000005 | 老 CPU 与新版 ctranslate2 不兼容。降级 `ctranslate2==4.6.0`、`setuptools<81`、`onnxruntime==1.20.1` |
| 模型下载 502 / 卡住 | 脚本默认 `HF_ENDPOINT=https://hf-mirror.com` 且禁用 xet。pip 装包也建议加 `-i https://pypi.tuna.tsinghua.edu.cn/simple` |
| 离线模式报「缓存不可用」 | 权重没下完（目录里只有 config）。去掉 `--offline` 让它续传，`env_check` 会显示「权重未下载完」 |
| `mlx_whisper` 报 ProxyError 但模型在本地 | mlx 默认联网校验版本，脚本检测到缓存会自动设 `HF_HUB_OFFLINE=1` |
| 识别成英文或乱码 | `--language` 判断错。中文口播写死 `zh` |
| 音轨明显短于视频 | 下载不完整或解码失败。回第三步重校字节数；看 ffmpeg 报的具体流信息 |
| 时长对但中间大段空白 | 该段确实没人声（BGM/字幕卡），或 VAD 误切。用 `--clip` 重跑确认 |
| Windows 路径含中文/空格 | 脚本用 pathlib 处理没问题；仍失败就换短英文路径输出 |
| 控制台中文乱码 | 脚本已把输出设为 UTF-8；`cmd` 可用 `chcp 65001` |

## 十、资源

- `scripts/transcribe.py` — 快照校验、双路解码（ffmpeg/PyAV）、可选降噪、三引擎识别、自检、排他写盘。
- `scripts/merge_segments.py` — 段落合并、字符校验、标点规范化、Markdown 骨架。
- `scripts/env_check.py` — 环境体检与修复命令。
- `references/pitfalls.md` — PyAV 三个坑、HF 镜像、降噪边界、幻觉识别、Windows 专项。
