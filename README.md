# Clipwise

Paste a YouTube link (normal video, finished stream, or **live stream in progress**) or upload a video file. Clipwise finds the most engaging moments and exports them as captioned clips ready for YouTube Shorts or your channel, with transcripts.

Everything heavy (download, transcription, scoring, rendering, enhancement) runs on your PC. Free Gemini / Groq API keys (Settings) are optional and only used to pick the best moments from transcripts.

## Run it (Windows)

**Option A: single .exe (no Python needed).** Copy `Clipwise.exe` anywhere and double-click it. The browser opens at `http://127.0.0.1:8765`. Clips are saved in a `data` folder next to the .exe.

**Option B: from source.** Double-click **`start.bat`**. The first run installs Python (if missing) and the packages, which takes a few minutes.

Keep the black window open while using the app; close it to stop. If YouTube downloads stop working one day (YouTube changes often), run **`update.bat`** (source) or rebuild the .exe.

### Build the .exe (on a Windows PC)

1. Run `start.bat` once (sets up `.venv`).
2. Run **`build_exe.bat`**. It downloads ffmpeg, bundles everything with PyInstaller and writes `dist\Clipwise.exe` (~400–600 MB).

PyInstaller can't cross-compile, so it has to be built on Windows. Windows SmartScreen may warn about an unsigned .exe the first time ("More info → Run anyway"). Code-signing removes that.

### Manual setup (macOS / Linux / dev)

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m app
```

## Updating to a new version

Your clips and settings live in the **`data`** folder and your API keys in the **`.env`** file (see `.env.example`); updates never touch either, and both are git-ignored.

- **With Git (recommended):** install the app with `git clone <repo-url> Clipwise` once. To update, double-click **`update.bat`**: it downloads the latest version and refreshes packages.
- **With a zip:** close Clipwise, extract the new zip **over** the existing folder and choose *Replace the files*. Keep `data` and `.venv`. `start.bat` notices new requirements and installs them by itself.
- **.exe:** replace `Clipwise.exe`; keep the `data` folder and `.env` next to it.

The current version is in the `VERSION` file and in the black window's first line.

## Features

- **YouTube videos, finished streams, live streams and local files** (MP4, MKV, MOV, WEBM…)
- **Live streams:** analyzes from the start of the stream up to *now*. Clipwise fetches the stream's DVR fragments directly, so it stops exactly at "now" and can fetch any part of a 120-hour DVR quickly.
- **Watch mode** (live only): re-checks the stream every N minutes, analyzes only the new part (including **live chat** recorded while watching), and adds new clips automatically until the stream ends or you click *Stop watching*.
- **Transcription on/off:** local English Whisper. Off is the fastest mode: no captions or transcripts, and moments are picked from audience and audio signals only.
- **Captions:** bold word-highlight (Shorts style), **pop** (each word bounces in; numbers turn green and strong words red), clean subtitle, or none. Also SRT and TXT per clip, plus a full transcript.
- **Formats:** Shorts 9:16 (fit with blurred background, or **face-tracking crop** that follows the speaker like a camera operator) or 16:9.
- **Learns your taste:** 👍/👎 on clips. After 3 votes, the ranking leans toward the qualities (hook, emotion, payoff…) of clips you liked.
- **Clean cuts:** clips end at the end of a sentence, not mid-thought.
- **Clip editor:** drag the start/end handles over the source (±30 s around each clip for YouTube, ±90 s for local files), change title, format and captions, then re-render.
- **NVIDIA GPU:** used automatically for rendering (NVENC) and transcription (CUDA) when available, with automatic fallback to the CPU.
- **Optional AI ranking:** Gemini (keys 1→2→3), then Groq (keys 1→2→3), optionally Claude. Picks the strongest candidates, trims them, writes title, hook and hashtags.
- **Tools:** Transcribe (works through background music), Enhance video (choose 1080p, 2K or 4K and strength; YouTube links download the real 2K/4K source), Copyright check (YouTube link, or your own edited video: finds background music, **names the song** via Shazam, spots TikTok/Instagram watermarks, @creator tags, TV/sports logos, scoreboards and movie framing, optionally searches the web for your frames with a Google Cloud Vision key, and can **remove the music while keeping the voice** in one click).
- **Automatic copyright check:** every exported clip is checked in the background and gets a 🟢/🟡/🔴 badge.
- **yt-dlp stays current:** checked once a day at startup (from source), since outdated yt-dlp is the usual reason YouTube downloads break.

## How it finds "hot" moments

| Signal | Source | Available for |
|---|---|---|
| **Chat activity**: message rate, laughter (`lol`, `😂`, `KEKW`…), hype (`W`, `POG`, `🔥`, `clip it`…), Super Chats | Chat replay (finished streams) or chat recorded live in watch mode; shifted ~8 s back because chat reacts late | Streams with chat enabled |
| **Most replayed** | YouTube's replay heatmap | Popular uploads |
| **Audio energy**: shouting, crowd noise, laughter | Loudness above the local baseline | Everything |
| **Speech**: jokes, hype phrases, strong hooks, pace | Local Whisper transcript | When transcription is on |
| **AI ranking** *(optional)* | Claude reads candidate transcripts | With an API key + transcription |

The top peaks become candidate windows (lead-in before the peak, payoff after). They're snapped to sentence boundaries, re-scored, de-duplicated, and the best N are rendered.

**Speed:** only audio is downloaded for analysis, and only the chosen sections in video. Long videos transcribe only the candidate regions unless *Full transcript* is on. *Advanced → From/To* also limits what gets downloaded.

## Output (per job, in `data/jobs/<id>/`)

- `clip_01.mp4`: final clip with burned-in captions (`clip_01_v2.mp4` after an edit)
- `clip_01.srt` / `clip_01.txt`: subtitles / title + link + transcript
- `clip_01_src.mp4`: the padded source section the editor uses
- `transcript.txt` / `transcript.json`
- **Download all** in the UI zips the clips, subtitles and transcripts

## Settings (gear icon)

- **API keys** (Gemini, Groq, Anthropic, optional Google Cloud Vision): stored in the git-ignored `.env` file, never in `data/settings.json`.
- **Transcription quality**: `small` (default, English-only model). Use `base` on slow PCs, or `medium` / `large-v3-turbo` with an NVIDIA GPU.
- **Processor**: Auto uses an NVIDIA GPU for transcription when CUDA is available. The settings dialog shows what was detected.

## Project layout

```
hotclip.py       entry point for the .exe
app/
  main.py        FastAPI routes (jobs, upload, editor, settings) + static UI
  pipeline.py    analysis job, watch mode, clip re-render
  youtube.py     yt-dlp: info, audio, chat, sections + live DVR fragment downloader
  scoring.py     signal processing, peak picking, speech scoring
  transcribe.py  faster-whisper (English, word timestamps, GPU if available)
  captions.py    ASS (styled, burned-in), SRT, TXT
  media.py       ffmpeg: probing, loudness, rendering (NVENC or x264), thumbnails
  llm.py         optional Claude re-ranking (structured output)
  jobs.py        job queue, edit queue, watch threads, JSON persistence
  copyright.py   copyright checks (YouTube link + local file) and the remove-music fix
  songid.py      song identification (Shazam) on music-only audio
  visual_check.py watermarks, logos, on-screen text (local OCR), movie framing, web search
  reframe.py     face-tracking 9:16 crop
  feedback.py    👍/👎 learning for the ranking
  updater.py     daily yt-dlp update check
web/             vanilla HTML/CSS/JS UI (no build step)
tests/           unit tests: python -m unittest discover -s tests
build_exe.py     PyInstaller build (run via build_exe.bat on Windows)
```

Use this tool only on content you have the rights to clip, and respect YouTube's Terms of Service.
