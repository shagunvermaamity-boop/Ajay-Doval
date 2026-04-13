# Ajay Doval — AI Command Center

A single-URL content pipeline. Paste a YouTube link. Get a full content package in under 10 minutes.

## What it does

One YouTube URL → 12 AI agents run in parallel → complete output:

| Output | Agent |
|--------|-------|
| Clean transcript + chapter markers + key quotes | Naina Verma |
| 3 short-form video clips (trimmed) | Kabir Shorts |
| LinkedIn post (3 versions) | Tara LinkedIn |
| Instagram caption + hashtags | Zoya Social |
| X standalone post + thread | Zoya Social |
| SEO blog draft (1000-1400 words) | Mehul Blogwala |
| QA review of all outputs | Ritu QA |
| ORM report from YouTube comments | Devika Rao |

All outputs visible in a locally-hosted dashboard at `http://localhost:8080`.

---

## Setup

### Requirements

- Python 3.10+
- [ffmpeg](https://ffmpeg.org/) — `brew install ffmpeg`
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — `brew install yt-dlp`
- [Whisper](https://github.com/openai/whisper) — `pip install openai-whisper`
- Google Gemini API key (free) — [aistudio.google.com](https://aistudio.google.com)

### Install Python dependencies

```bash
pip install anthropic google-generativeai
```

### Configure

Open `dashboard.py` and set your Gemini API key:

```python
GEMINI_KEY  = "your-gemini-api-key"
ORM_API_KEY = "your-gemini-api-key"
```

Also verify your binary paths match your system:

```python
YTDLP   = Path("/usr/local/bin/yt-dlp")
FFMPEG  = Path("/usr/local/bin/ffmpeg")
WHISPER = Path("/usr/local/bin/whisper")
```

---

## Run

```bash
python3 dashboard.py
```

Open `http://localhost:8080` in your browser.

---

## Usage

1. Click **+ Process New Video**
2. Paste any YouTube URL
3. Watch the pipeline run live in the modal
4. Browse all outputs in the dashboard — transcript, shorts, social posts, blog, QA, ORM
5. Click **⬇️ Export ZIP** to download everything

---

## Project Structure

```
ajay-doval/
├── dashboard.py        # Single-file server — entire pipeline + dashboard
├── assets/             # Static assets (if any)
├── inbox/              # Drop local video files here for processing
└── outputs/            # Generated per-job outputs (gitignored)
    └── job-name/
        ├── source/     # Downloaded video + intake summary
        ├── transcript/ # Raw + clean transcript, quotes, chapters
        ├── shorts/     # Shorts plan + cut video clips
        ├── social/     # LinkedIn, Instagram, X posts
        ├── blog/       # Blog draft
        ├── qa/         # QA notes + approval summary
        └── orm-reports/# ORM reports from YouTube comments
```

---

## AI Stack

- **Transcription:** OpenAI Whisper (local, base model)
- **Content generation:** Google Gemini / Gemma (via `google-generativeai`)
- **Video download:** yt-dlp
- **Video processing:** ffmpeg
- **Server:** Python `http.server.ThreadingHTTPServer` (no framework)
- **Frontend:** Vanilla JS + marked.js for markdown rendering

---

## Built at a Hackathon

Ajay Doval was built as a full working product — not a prototype — during a hackathon. Three real videos processed, real outputs ready to post.
