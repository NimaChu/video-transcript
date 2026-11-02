---
name: xiaohongshu-video-transcript
description: Download and locally transcribe a public Xiaohongshu (小红书) video from a share link, then create one Markdown document with a summary, Mermaid mind map, and timestamped full transcript. Use only for Xiaohongshu video links, not for already-local audio or video files.
---

# 小红书视频转写

将公开可访问的小红书视频转成一份可交付的 Markdown 文档。文档必须依次包含：来源与时长、核心总结、Mermaid 思维导图、按时间分段的完整转写。整个流程使用浏览器网络记录、系统命令和本地语音识别；不要调用其他技能，也不要要求预置的项目脚本。

## 先决条件与边界

- 仅处理用户提供且有权处理的公开小红书视频链接。若视频要求登录、地区访问受限或无权访问，说明原因并请用户提供可访问链接或原始文件。
- 转写默认在本机进行，不上传音视频。模型下载属于正常准备步骤；如磁盘空间明显不足，先告知用户。
- 输出一份 Markdown；原始视频、音频和机器转写可仅作为复核材料保存在同一输出目录。不要把错误的机器初稿直接当作最终转写。
- 不要使用另一个技能、定制 shell/Python 脚本或第三方“解析站”。可直接使用浏览器自动化、`curl`、`ffmpeg`、`ffprobe`、`uv`、`funasr` 和 `mlx_whisper`。

## 工作流程

### 1. 打开分享链接并定位真实视频地址

1. 在真实浏览器中打开短链接，等待重定向和播放器加载。
2. 从页面确认标题、作者（如可见）和视频描述；不要因登录弹窗而放弃，先查看网络请求。
3. 检查网络请求，筛选 `mp4`、`m3u8`、`video`、`media`。优先选择实际媒体请求（通常是 `.mp4`），而不是页面、播放器 JavaScript 或封面。
4. 若只有分段请求，保留完整带签名 URL；签名 URL 往往会过期，因此应在发现后立即下载。

### 2. 下载并校验视频

小红书 CDN 对长视频的 HTTP/2 下载偶尔会在尾部中断。优先用 HTTP/1.1、断点续传和重试：

```bash
mkdir -p output/transcribe/<job-id>
curl --http1.1 -L --fail --retry 4 --retry-all-errors \
  --silent --show-error '<video-url>' \
  -o output/transcribe/<job-id>/source.mp4
```

如下载中断，使用同一个文件续传；续传后必须校验大小：

```bash
curl --http1.1 -L --fail --retry 4 --retry-all-errors -C - \
  --silent --show-error '<video-url>' \
  -o output/transcribe/<job-id>/source.mp4
stat -f '%z bytes' output/transcribe/<job-id>/source.mp4
```

用 `curl -I` 或小范围 Range 请求获取 `Content-Length` / `Content-Range`，确认实际文件大小等于服务端报告的总字节数。不要对不完整、重复拼接或报解码错误的视频继续转写。

若 `curl -C -` 的服务器响应不是 `206 Partial Content`，说明断点续传不可靠：将当前下载保留为故障副本，再从零下载；不要把完整文件追加到已有部分文件尾部。

### 3. 提取标准音轨

使用单声道、16 kHz WAV，减少识别时的解码差异：

```bash
ffmpeg -y -v error -i output/transcribe/<job-id>/source.mp4 \
  -vn -ac 1 -ar 16000 -c:a pcm_s16le \
  output/transcribe/<job-id>/audio.wav
ffprobe -v error -show_entries format=duration,size -of default=nw=1 \
  output/transcribe/<job-id>/audio.wav
```

记录音频时长。最终字幕或转写的最后一个时间点应接近该时长；明显过早结束通常意味着下载或解码有问题。

### 4. 按实际语音选择识别路线

先试听或试转开头、中段和结尾，按语音而非标题判断语言。不要仅因为出现几个英文专名就把普通话访谈判为英文音频。

| 音频 | 初稿 | 复核 |
| --- | --- | --- |
| 普通话为主，英文术语很少 | 中文 Paraformer (`funasr --model paraformer`) | 抽查专名、数字、短句及异常段；有分歧时用 MLX Whisper 复核。 |
| 英语为主 | MLX Whisper large-v3-turbo，指定 `--language en` | 复核专名、数字及异常段；不要未经样本验证就假定 `paraformer-en` 更准。 |
| 中文为主但频繁夹杂英文术语、代码或产品名，或用户要求尽量逐字保留 | Paraformer 与 MLX Whisper 都跑，逐段比较后整理 | 中文措辞可参考 Paraformer，英文术语可参考 Whisper；两者一致也要核对原音频或保留疑问。 |
| 中英整段交替 | 按语段分别使用上述路线 | 保留统一时间轴；不要强制整段为一种语言。 |

这次中文技术访谈中，Paraformer 的中文口语与标点较顺，Whisper 对部分英文术语较好；两者都出现专名、数字和局部短语错误，Whisper 长段还出现重复幻觉。这是单个样本的经验，不等于通用准确率排名。模型选择有疑问时，先试转有代表性的短段。

若命令不存在，按所需路线安装：

```bash
uv tool install funasr --with torch --with torchaudio
uv tool install mlx-whisper
```

FunASR 首次运行会下载中文模型及语音检测、标点组件；MLX Whisper 首次运行会下载模型。先检查可用磁盘空间。需要提前缓存 Whisper 模型时，用与 `mlx_whisper` 同一工具环境里的 `hf download mlx-community/whisper-large-v3-turbo --local-dir output/transcribe/<job-id>/model`；否则可直接在 `--model` 中使用该模型 ID。

### 5. 生成初稿与时间戳

关键词只取自标题、描述和已确认的上下文。普通话路线先运行：

```bash
funasr output/transcribe/<job-id>/audio.wav \
  --model paraformer --language zh --device cpu \
  --hotwords '<人名,公司,产品名,术语>' \
  --timestamps -f json -o output/transcribe/<job-id>/paraformer
```

英语路线或双模型路线运行 MLX Whisper；把 `<语言>` 设为 `zh` 或 `en`，整段频繁切换时可省略 `--language` 让模型检测，随后检查语言切换处：

```bash
mlx_whisper output/transcribe/<job-id>/audio.wav \
  --model mlx-community/whisper-large-v3-turbo \
  --language <语言> \
  --output-dir output/transcribe/<job-id> \
  --output-name whisper_raw \
  --output-format all --verbose False \
  --initial-prompt '<人名、公司、行业术语、英文缩写>'
```

长音频如出现连续重复，可在重跑时加 `--condition-on-previous-text False`，阻断错误向后传递，并额外检查跨片段术语的一致性。

若长音频一次运行失败或反复卡在同一短语，用 `ffmpeg` 分成约 2–3 分钟的 WAV 片段重跑，并复核切点两侧。可用 `ffmpeg -i audio.wav -f segment -segment_time 180 -reset_timestamps 1 -c:a pcm_s16le clips/clip_%03d.wav` 分段；合并文字时把每段的相对时间加回片段起点，片段时长用 `ffprobe` 核查。`funasr --timestamps` 只保留模型实际返回的时间数据，不能假定每个字都有可靠时间戳。保留原始 JSON/SRT 供复核，不照抄机器初稿。

### 6. 复核异常段落

检查 JSON 中每个片段的时间、长度和文本，并比较两套模型的分歧。以下情况应逐段复核：

- 连续多个片段重复同一句话或同一短语；
- 一段 30 秒音频只出现很少的无意义文字；
- 专有名词、数字、英文缩写明显错误；
- 时间轴出现大段内容缺失或突然跳跃；
- 两模型对人名、数字、英文词或否定词给出不同结果；两模型都可能把同一个术语听错。

对异常时间段单独重跑，关闭前文条件，避免模型把一次幻觉带到后续窗口：

```bash
mlx_whisper output/transcribe/<job-id>/audio.wav \
  --model mlx-community/whisper-large-v3-turbo \
  --language <语言> \
  --output-dir output/transcribe/<job-id>/recheck \
  --output-name segment_<start>_<end> \
  --output-format all \
  --verbose False \
  --clip-timestamps <start>,<end> \
  --condition-on-previous-text False \
  --initial-prompt '<该段的场景、术语和专名>'
```

`--hallucination-silence-threshold` 只有同时启用 `--word-timestamps True` 才生效，不要把未生效的参数当成已完成复核。用音频和复核结果修正异常段；不能仅凭模型之间的多数票改写原话。可规范标点、段落和确认无误的专有名词，但不可把转写改写成仅有摘要；不确定而又会改变含义的词应标注“[听辨不清]”。

### 7. 输出单份 Markdown

保存为 `output/transcribe/<job-id>/<标题>_完整转写.md`。结构固定为：

````markdown
# 标题

> 来源：链接  
> 时长：…  
> 主题：…

## 核心总结

…

## 思维导图

```mermaid
mindmap
  root((主题))
    …
```

---

## 完整转写

### [00:00–…] 小节标题

逐段转写正文。
````

总结应只概括视频内容，不补充视频外的事实。思维导图应覆盖主论点、方法、案例、结论等主要分支。完整转写按 1–3 分钟或明显话题变化分段，并保留每段时间范围。

## 交付前检查

- Markdown 开头先有总结和思维导图，后有完整转写；
- 时间段从开头覆盖到视频结尾，且顺序连续；
- 已替换重复幻觉段；
- 没有把未校验的机器错词当成确定事实；
- 交付转写时，最终回复只给出 Markdown 文件的绝对路径链接，并简要说明已完成。
