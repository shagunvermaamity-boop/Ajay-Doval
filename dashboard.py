#!/usr/bin/env python3
"""
Ajay Doval Command Center — Local Dashboard
Run: python3 /Users/shagunverma/ajay-doval/dashboard.py
Open: http://localhost:8080
"""

import http.server
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import uuid
from pathlib import Path


OUTPUTS_DIR = Path("outputs")
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

YTDLP = "yt-dlp"
FFMPEG = "ffmpeg"
WHISPER = "whisper"
PYTHON3 = "python3"

VIDEO_MIME  = {"mp4": "video/mp4", "m4a": "audio/mp4", "webm": "video/webm", "mov": "video/quicktime"}
ORM_API_KEY = "AIzaSyD-m6N82LA2kPJjfQDhsKEQ4Nfxg3ReF28"
GEMINI_KEY  = "AIzaSyD-m6N82LA2kPJjfQDhsKEQ4Nfxg3ReF28"
GEMINI_MODEL = "gemma-3-27b-it"

# In-memory pipeline progress store: {run_id: [events]}
RUNS: dict = {}
RUNS_LOCK = threading.Lock()

# ─── VIDEO CLIP CUTTER ───────────────────────────────────────────────────────

def ts_to_seconds(ts: str) -> float:
    """Convert MM:SS or HH:MM:SS to seconds."""
    parts = ts.strip().split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    return 0.0

def cut_clips(job_dir: Path, source_video: Path, log=None):
    """
    Parse shorts timestamps from CSV or markdown plan, cut clips with ffmpeg,
    save to shorts/ folder, write clips.json index.
    Returns list of clip dicts: [{num, title, start, end, path, url_path}]
    """
    shorts_dir = job_dir / "shorts"
    clips_json = shorts_dir / "clips.json"

    # Already cut — return cached
    if clips_json.exists():
        try:
            return json.loads(clips_json.read_text())
        except Exception:
            pass

    clips = []

    # ── Try CSV first (most reliable) ────────────────────────────────────────
    csv_file = shorts_dir / "shorts_timestamps.csv"
    if csv_file.exists():
        import csv as csv_mod
        with open(csv_file, newline="", errors="replace") as f:
            reader = csv_mod.DictReader(f)
            for row in reader:
                num   = str(row.get("clip_number", len(clips)+1)).strip()
                title = row.get("title", f"Clip {num}").strip()
                start = row.get("start_time", "").strip()
                end   = row.get("end_time",   "").strip()
                if start and end:
                    clips.append({"num": num, "title": title, "start": start, "end": end})

    # ── Fallback: parse markdown plan ─────────────────────────────────────────
    if not clips:
        plan_file = shorts_dir / "shorts_plan.md"
        if plan_file.exists():
            text = plan_file.read_text(errors="replace")
            starts = re.findall(r'\*\*Start(?:\s+Time)?[:\*\s]+([0-9:]+)', text)
            ends   = re.findall(r'\*\*End(?:\s+Time)?[:\*\s]+([0-9:]+)',   text)
            titles = re.findall(r'(?:##\s+Clip\s+\d+[:\s—–-]+|###\s+Clip\s+\d+[:\s]+)"?([^"\n]+)"?', text)
            for i, (s, e) in enumerate(zip(starts, ends)):
                title = titles[i] if i < len(titles) else f"Clip {i+1}"
                clips.append({"num": str(i+1), "title": title.strip(), "start": s, "end": e})

    if not clips:
        if log: log("warn", "⚠️ No timestamp data found — cannot cut clips")
        return []

    # ── Cut each clip ─────────────────────────────────────────────────────────
    results = []
    for clip in clips:
        num   = clip["num"]
        safe  = re.sub(r"[^\w]", "_", clip["title"][:40])
        fname = f"clip_{num.zfill(2)}_{safe}.mp4"
        out   = shorts_dir / fname

        if not out.exists():
            if log: log("info", f"✂️ Cutting clip {num}: {clip['start']} → {clip['end']}")
            cmd = [
                str(FFMPEG), "-y",
                "-i", str(source_video),
                "-ss", clip["start"],
                "-to", clip["end"],
                "-c:v", "libx264", "-crf", "20", "-preset", "fast",
                "-c:a", "aac", "-b:a", "192k",
                "-avoid_negative_ts", "make_zero",
                str(out)
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            if result.returncode != 0:
                if log: log("warn", f"⚠️ Clip {num} cut failed: {result.stderr[-200:]}")
                continue

        results.append({
            "num":   num,
            "title": clip["title"],
            "start": clip["start"],
            "end":   clip["end"],
            "path":  str(out),
            "url":   f"/api/video?file={urllib.parse.quote(str(out))}",
        })

    clips_json.write_text(json.dumps(results, indent=2))
    return results


# ─── PIPELINE ────────────────────────────────────────────────────────────────

def push(run_id, event_type, message, data=None):
    with RUNS_LOCK:
        if run_id not in RUNS:
            RUNS[run_id] = {"events": [], "done": False, "job": None, "error": None}
        RUNS[run_id]["events"].append({
            "type": event_type, "msg": message, "data": data or {}, "ts": time.time()
        })

def run_pipeline(run_id: str, url: str, api_key: str):
    try:
        push(run_id, "start",   "🚀 Pipeline started")
        push(run_id, "agent",   "📋 Ajay Doval: Identifying video and setting up job folder…")

        # ── STEP 1: GET METADATA ──────────────────────────────────────────────
        push(run_id, "agent",  "📥 Raghav Reelkar: Fetching video metadata…")
        meta_result = subprocess.run(
            [str(YTDLP), "--no-playlist", "--skip-download",
             "--print", "title=%(title)s",
             "--print", "duration_string=%(duration_string)s",
             "--print", "uploader=%(uploader)s",
             "--print", "upload_date=%(upload_date)s",
             "--print", "id=%(id)s",
             url],
            capture_output=True, text=True, timeout=60
        )
        meta = {}
        for line in meta_result.stdout.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                meta[k.strip()] = v.strip()

        if not meta.get("title"):
            push(run_id, "error", "❌ Could not fetch video metadata. Check the URL.")
            with RUNS_LOCK: RUNS[run_id]["done"] = True; RUNS[run_id]["error"] = "Metadata fetch failed"
            return

        title     = meta.get("title", "untitled")
        video_id  = meta.get("id", "unknown")
        duration  = meta.get("duration_string", "?")
        channel   = meta.get("uploader", "?")
        upload_dt = meta.get("upload_date", "?")
        push(run_id, "info", f"✅ Video: {title} ({duration}) — {channel}")

        # ── STEP 2: CREATE JOB FOLDER ─────────────────────────────────────────
        safe = re.sub(r"[^\w\-]", "_", title[:50]).strip("_").lower()
        job_name = f"{safe}_{video_id}"
        job_dir  = OUTPUTS_DIR / job_name
        for sub in ["source", "transcript", "shorts", "social", "blog", "qa"]:
            (job_dir / sub).mkdir(parents=True, exist_ok=True)

        with RUNS_LOCK: RUNS[run_id]["job"] = job_name

        # ── STEP 3: DOWNLOAD ─────────────────────────────────────────────────
        push(run_id, "agent",  "📥 Raghav Reelkar: Downloading video…")
        dl_result = subprocess.run(
            [str(YTDLP), "--no-playlist", "--write-info-json", "--write-comments",
             "--extractor-args", "youtube:comment_sort=top;max_comments=200,all,10,5",
             "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
             "-o", str(job_dir / "source" / "%(title)s.%(ext)s"),
             url],
            capture_output=True, text=True, timeout=600
        )
        if dl_result.returncode != 0:
            push(run_id, "error", f"❌ Download failed: {dl_result.stderr[:300]}")
            with RUNS_LOCK: RUNS[run_id]["done"] = True; RUNS[run_id]["error"] = "Download failed"
            return
        push(run_id, "info", "✅ Download complete")

        # ── STEP 4: FIND + MERGE FILES ────────────────────────────────────────
        push(run_id, "agent", "🔧 Raghav Reelkar: Merging video + audio with ffmpeg…")
        src_dir  = job_dir / "source"
        mp4_files = list(src_dir.glob("*.mp4"))
        m4a_files = list(src_dir.glob("*.m4a"))
        merged   = src_dir / "merged.mp4"

        if mp4_files and m4a_files:
            merge_result = subprocess.run(
                [str(FFMPEG), "-y", "-i", str(mp4_files[0]), "-i", str(m4a_files[0]),
                 "-c:v", "copy", "-c:a", "aac", str(merged)],
                capture_output=True, timeout=300
            )
            source_video = merged if merge_result.returncode == 0 else mp4_files[0]
            push(run_id, "info", "✅ Merge complete" if merge_result.returncode == 0 else "⚠️ Merge failed — using video-only file")
        elif mp4_files:
            source_video = mp4_files[0]
            push(run_id, "info", "✅ Using downloaded mp4 directly")
        else:
            push(run_id, "error", "❌ No video file found after download")
            with RUNS_LOCK: RUNS[run_id]["done"] = True; RUNS[run_id]["error"] = "No video file"
            return

        # ── STEP 5: WRITE INTAKE SUMMARY ─────────────────────────────────────
        intake = f"""# Intake Summary
Generated by: Raghav Reelkar
Date: {time.strftime('%Y-%m-%d')}

## Source Details
| Field | Value |
|-------|-------|
| Source Type | YouTube URL |
| URL | {url} |
| Video ID | {video_id} |

## Video Metadata
| Field | Value |
|-------|-------|
| Title | {title} |
| Channel | {channel} |
| Duration | {duration} |
| Upload Date | {upload_dt} |

## Output Path
`{source_video}`

## Status
✅ Intake complete. Ready for transcription.
"""
        (src_dir / "intake_summary.md").write_text(intake)

        # ── STEP 6: TRANSCRIBE ────────────────────────────────────────────────
        push(run_id, "agent", "📝 Naina Verma: Transcribing video with Whisper (this takes a few minutes)…")
        tx_dir = job_dir / "transcript"
        whisper_env = {**os.environ, "PATH": f"/opt/homebrew/bin:{os.environ.get('PATH','')}"}
        whisper_result = subprocess.run(
            [str(WHISPER), str(source_video),
             "--model", "base", "--language", "en",
             "--output_format", "all",
             "--output_dir", str(tx_dir)],
            capture_output=True, text=True, timeout=1200, env=whisper_env
        )
        if whisper_result.returncode != 0:
            push(run_id, "error", f"❌ Transcription failed: {whisper_result.stderr[:300]}")
            with RUNS_LOCK: RUNS[run_id]["done"] = True; RUNS[run_id]["error"] = "Transcription failed"
            return

        # Read the raw transcript
        txt_files = list(tx_dir.glob("*.txt"))
        raw_transcript = txt_files[0].read_text(errors="replace") if txt_files else ""

        srt_files = list(tx_dir.glob("*.srt"))
        srt_text  = srt_files[0].read_text(errors="replace") if srt_files else ""
        push(run_id, "info", f"✅ Transcription complete ({len(raw_transcript.split())} words)")

        if not raw_transcript.strip():
            push(run_id, "error", "❌ Transcript is empty — audio may be silent or unrecognised")
            with RUNS_LOCK: RUNS[run_id]["done"] = True; RUNS[run_id]["error"] = "Empty transcript"
            return

        # ── STEP 7: AI CONTENT GENERATION ────────────────────────────────────
        push(run_id, "agent", "🤖 Ajay Doval: Handing off to content team — Kabir, Tara, Zoya, Mehul running in parallel…")

        def ai(prompt, system):
            import google.generativeai as genai
            genai.configure(api_key=GEMINI_KEY)
            m = genai.GenerativeModel(GEMINI_MODEL)
            for attempt in range(4):
                try:
                    return m.generate_content(system + "\n\n" + prompt).text
                except Exception as e:
                    if "429" in str(e) and attempt < 3:
                        time.sleep(65)
                        continue
                    raise

        transcript_ctx = f"""VIDEO TITLE: {title}
CHANNEL: {channel}
DURATION: {duration}

TRANSCRIPT:
{raw_transcript[:12000]}"""

        # Run all content agents in parallel threads
        results = {}
        errors  = []

        def gen(key, sys_prompt, usr_prompt):
            try:
                results[key] = ai(usr_prompt, sys_prompt)
            except Exception as e:
                errors.append(f"{key}: {e}")
                results[key] = f"_Generation failed: {e}_"

        threads = [
            threading.Thread(target=gen, args=("transcript_clean",
                "You are Naina Verma, a transcript specialist. Create a clean, readable transcript with sections, speaker labels where possible, and key moment highlights.",
                f"Clean this raw transcript into a well-structured markdown document with sections, timestamps preserved where possible, and key moments highlighted.\n\n{transcript_ctx}")),

            threading.Thread(target=gen, args=("key_quotes",
                "You are Naina Verma. Extract the most powerful, quotable lines from this transcript.",
                f"Extract the 10-15 strongest quotes and insight lines from this transcript. Format as a markdown list with timestamps if available.\n\n{transcript_ctx}")),

            threading.Thread(target=gen, args=("chapter_markers",
                "You are Naina Verma. Create chapter markers for this video.",
                f"Create a chapter markers table with timestamp, chapter title, and brief description for this video transcript.\n\n{transcript_ctx}")),

            threading.Thread(target=gen, args=("shorts_plan",
                "You are Kabir Shorts, a short-form video strategist. You identify the 3 best clips for short-form content.",
                f"Identify the 3 best short-form video clips from this transcript. For each: start time, end time, title, hook, why it works, caption idea, and ffmpeg command using source: {source_video}\n\n{transcript_ctx}")),

            threading.Thread(target=gen, args=("linkedin",
                "You are Tara LinkedIn, a professional LinkedIn content writer. Write in a thoughtful, story-driven, insight-first style.",
                f"Write 1 polished LinkedIn post + 2 alternates from this video content. No salesy language. Story-first, insight-driven.\n\n{transcript_ctx}")),

            threading.Thread(target=gen, args=("instagram",
                "You are Zoya Social, an Instagram content writer. Punchy, visual, lowercase, hooks first.",
                f"Write an Instagram caption for this video. Hook first, punchy, platform-native tone, end with a question CTA. Include relevant hashtags.\n\n{transcript_ctx}")),

            threading.Thread(target=gen, args=("x_post",
                "You are Zoya Social. Write for X (Twitter). Sharp, direct, internet-native.",
                f"Write 1 standalone X post (under 280 chars) and 1 thread of 5-7 tweets covering the key arc of this content.\n\n{transcript_ctx}")),

            threading.Thread(target=gen, args=("blog",
                "You are Mehul Blogwala, an SEO blog writer. Organised, structured, 1000-1400 words.",
                f"Write a full SEO blog post from this video transcript. Include: SEO title, meta description, intro, 5-6 sections, conclusion, key takeaways.\n\n{transcript_ctx}")),
        ]

        for i, t in enumerate(threads):
            t.start()
            if i < len(threads) - 1:
                time.sleep(13)  # stagger starts to stay under 5 RPM

        # Stream progress as threads complete
        completed = set()
        agent_map = {
            "transcript_clean": "📝 Naina Verma: Clean transcript",
            "key_quotes":       "📝 Naina Verma: Key quotes",
            "chapter_markers":  "📝 Naina Verma: Chapter markers",
            "shorts_plan":      "🎬 Kabir Shorts: Shorts plan",
            "linkedin":         "💼 Tara LinkedIn: LinkedIn post",
            "instagram":        "📸 Zoya Social: Instagram caption",
            "x_post":           "𝕏 Zoya Social: X thread",
            "blog":             "📰 Mehul Blogwala: Blog draft",
        }
        while any(t.is_alive() for t in threads):
            for key in agent_map:
                if key in results and key not in completed:
                    completed.add(key)
                    push(run_id, "info", f"✅ {agent_map[key]} done")
            time.sleep(1)
        for t in threads: t.join()

        if errors:
            for e in errors:
                push(run_id, "warn", f"⚠️ {e}")

        # ── STEP 8: WRITE ALL FILES ───────────────────────────────────────────
        push(run_id, "agent", "📁 Ajay Doval: Writing all output files…")

        (tx_dir / "transcript_clean.md").write_text(results.get("transcript_clean", ""))
        (tx_dir / "key_quotes.md").write_text(results.get("key_quotes", ""))
        (tx_dir / "chapter_markers.md").write_text(results.get("chapter_markers", ""))

        (job_dir / "shorts" / "shorts_plan.md").write_text(results.get("shorts_plan", ""))
        (job_dir / "social" / "linkedin_post.md").write_text(results.get("linkedin", ""))
        (job_dir / "social" / "instagram_post.md").write_text(results.get("instagram", ""))
        (job_dir / "social" / "x_post.md").write_text(results.get("x_post", ""))
        (job_dir / "blog"   / "blog_draft.md").write_text(results.get("blog", ""))

        # Write raw transcript as md too
        (tx_dir / "transcript_raw.md").write_text(f"# Raw Transcript\n\n```\n{raw_transcript}\n```")

        # ── STEP 9: CUT CLIPS ─────────────────────────────────────────────────
        push(run_id, "agent", "✂️ Kabir Shorts: Cutting video clips with ffmpeg…")
        clip_results = cut_clips(job_dir, source_video, log=lambda t, m: push(run_id, t, m))
        if clip_results:
            push(run_id, "info", f"✅ {len(clip_results)} clips cut and saved")
        else:
            push(run_id, "warn", "⚠️ Clip cutting skipped — timestamps not found in plan")

        # ── STEP 10: QA ───────────────────────────────────────────────────────
        push(run_id, "agent", "✅ Ritu QA: Running quality check…")
        all_content = "\n\n---\n\n".join([
            f"## LinkedIn\n{results.get('linkedin','')}",
            f"## Instagram\n{results.get('instagram','')}",
            f"## X Post\n{results.get('x_post','')}",
            f"## Blog\n{results.get('blog','')}",
            f"## Shorts\n{results.get('shorts_plan','')}",
        ])
        qa_prompt = f"""Review these content outputs for a video titled "{title}".
Check: accuracy to transcript, grammar, platform fit, unsupported claims, tone consistency.
Write a qa_notes.md with per-section notes and a final_approval_summary.md with overall status and top fixes needed.
Return BOTH documents separated by ===SPLIT===

{all_content[:8000]}"""

        qa_output = ai(qa_prompt, "You are Ritu QA, a strict quality assurance editor. You review content pipelines for accuracy, clarity, and consistency.")
        parts = qa_output.split("===SPLIT===")
        (job_dir / "qa" / "qa_notes.md").write_text(parts[0].strip() if len(parts) > 1 else qa_output)
        (job_dir / "qa" / "final_approval_summary.md").write_text(parts[1].strip() if len(parts) > 1 else "# QA Complete\n\nSee qa_notes.md for details.")
        push(run_id, "info", "✅ Ritu QA: Done")

        # ── STEP 11: AUTO ORM REPORT ──────────────────────────────────────────
        push(run_id, "agent", "🔍 Devika Rao: Fetching YouTube comments and generating ORM report…")
        try:
            # Read comments from the info.json yt-dlp wrote during download
            info_jsons = list((job_dir / "source").glob("*.info.json"))
            yt_comments = []
            if info_jsons:
                info = json.loads(info_jsons[0].read_text(errors="replace"))
                yt_comments = info.get("comments", [])

            if yt_comments:
                # Format comments for Devika
                comments_text = "\n".join([
                    f"[YouTube] @{c.get('author','anon')} ({c.get('like_count',0)} likes): {c.get('text','')}"
                    for c in yt_comments[:150]
                ])

                # Build video context
                def r(p): return p.read_text(errors="replace") if p.exists() else ""
                video_ctx = f"""## VIDEO CONTEXT
Title: {title}
Channel: {channel}
Duration: {duration}

### Key Quotes
{r(job_dir / 'transcript' / 'key_quotes.md')[:2000]}

### Chapter Markers
{r(job_dir / 'transcript' / 'chapter_markers.md')[:1500]}
"""
                orm_system = """You are Devika Rao, ORM Intelligence & Reporting Analyst.
You produce structured, actionable ORM reports. Separate facts from interpretation.
Never invent data. If counts are approximate say 'observed trend'. Flag HIGH risk clearly.
Ground all analysis in the specific video content provided."""

                orm_prompt = f"""Generate a full ORM report for these YouTube comments.

{video_ctx}

## COMMENTS ({len(yt_comments)} total):
{comments_text[:7000]}

Produce the full report using this structure:

# ORM Report — {title}

## 1. Report Scope
## 2. Executive Summary
## 3. Sentiment Overview
## 4. Platform Breakdown (YouTube Comments)
## 5. Top Repeating Themes
## 6. Key Risks
## 7. High-Intent User Signals
## 8. Content Insights
## 9. Items That Need Response
## 10. Recommended Actions
## 11. Sample Quotes / Evidence
## 12. Final Verdict

Then output ===SUMMARY=== and write orm_summary.md (3-5 bullet takeaways).
Then output ===RISKS=== and write orm_risk_flags.md listing only HIGH and MEDIUM risk items.
Then output ===ACTIONS=== and write orm_action_items.md with immediate, short-term, and content opportunity actions."""

                orm_resp = ai(orm_prompt, orm_system)
                parts = re.split(r"===SUMMARY===|===RISKS===|===ACTIONS===", orm_resp)

                safe_name = "youtube-auto-" + time.strftime("%Y-%m-%d")
                out_dir = job_dir / "orm-reports" / safe_name
                out_dir.mkdir(parents=True, exist_ok=True)

                (out_dir / "orm_report.md").write_text(parts[0].strip() if parts else orm_resp)
                (out_dir / "orm_summary.md").write_text(parts[1].strip() if len(parts) > 1 else "")
                (out_dir / "orm_risk_flags.md").write_text(parts[2].strip() if len(parts) > 2 else "")
                (out_dir / "orm_action_items.md").write_text(parts[3].strip() if len(parts) > 3 else "")

                push(run_id, "info", f"✅ Devika Rao: ORM report generated from {len(yt_comments)} YouTube comments")
            else:
                push(run_id, "warn", "⚠️ Devika Rao: No YouTube comments found — skipping auto ORM report")
        except Exception as e:
            push(run_id, "warn", f"⚠️ Devika Rao: ORM step failed — {e}")

        # ── DONE ──────────────────────────────────────────────────────────────
        push(run_id, "done", f"🎉 Pipeline complete! Job: {job_name}", {"job": job_name})
        with RUNS_LOCK:
            RUNS[run_id]["done"] = True
            RUNS[run_id]["job"]  = job_name

    except Exception as e:
        push(run_id, "error", f"❌ Pipeline crashed: {e}")
        with RUNS_LOCK:
            RUNS[run_id]["done"]  = True
            RUNS[run_id]["error"] = str(e)


def run_resume(run_id: str, job_name: str):
    """Run transcription + AI content generation on an already-downloaded job."""
    try:
        job_dir = OUTPUTS_DIR / job_name
        push(run_id, "start", "🚀 Resume pipeline started")
        with RUNS_LOCK: RUNS[run_id]["job"] = job_name

        # Read metadata from intake_summary
        intake_path = job_dir / "source" / "intake_summary.md"
        title = job_name; channel = ""; duration = ""; url = ""
        if intake_path.exists():
            for line in intake_path.read_text(errors="replace").splitlines():
                if "| Title"    in line: title    = line.split("|")[2].strip()
                elif "| Channel"  in line: channel  = line.split("|")[2].strip()
                elif "| Duration" in line: duration = line.split("|")[2].strip()
                elif "| URL"      in line: url      = line.split("|")[2].strip()

        # Find source video
        src_dir = job_dir / "source"
        merged  = src_dir / "merged.mp4"
        mp4s    = list(src_dir.glob("*.mp4"))
        source_video = merged if merged.exists() else (mp4s[0] if mp4s else None)
        if not source_video:
            push(run_id, "error", "❌ No source video found"); RUNS[run_id]["done"] = True; return

        push(run_id, "info", f"✅ Found source: {source_video.name}")

        # ── TRANSCRIBE ────────────────────────────────────────────────────────
        tx_dir = job_dir / "transcript"
        tx_dir.mkdir(exist_ok=True)
        existing_txt = list(tx_dir.glob("*.txt"))
        if existing_txt:
            raw_transcript = existing_txt[0].read_text(errors="replace")
            push(run_id, "info", f"✅ Transcript already exists ({len(raw_transcript.split())} words) — skipping Whisper")
        else:
            push(run_id, "agent", "📝 Naina Verma: Transcribing with Whisper (this takes a few minutes)…")
            whisper_env = {**os.environ, "PATH": f"/opt/homebrew/bin:{os.environ.get('PATH','')}"}
            whisper_result = subprocess.run(
                [str(WHISPER), str(source_video),
                 "--model", "base", "--language", "en",
                 "--output_format", "all", "--output_dir", str(tx_dir)],
                capture_output=True, text=True, timeout=1200, env=whisper_env
            )
            if whisper_result.returncode != 0:
                push(run_id, "error", f"❌ Transcription failed: {whisper_result.stderr[:300]}")
                with RUNS_LOCK: RUNS[run_id]["done"] = True; return
            txt_files = list(tx_dir.glob("*.txt"))
            raw_transcript = txt_files[0].read_text(errors="replace") if txt_files else ""
            push(run_id, "info", f"✅ Transcription complete ({len(raw_transcript.split())} words)")

        if not raw_transcript.strip():
            push(run_id, "error", "❌ Transcript is empty"); RUNS[run_id]["done"] = True; return

        # ── AI CONTENT ────────────────────────────────────────────────────────
        push(run_id, "agent", "🤖 Ajay Doval: Handing off to content team — Kabir, Tara, Zoya, Mehul running in parallel…")

        def ai(prompt, system):
            import google.generativeai as genai
            genai.configure(api_key=GEMINI_KEY)
            m = genai.GenerativeModel(GEMINI_MODEL)
            for attempt in range(4):
                try:
                    return m.generate_content(system + "\n\n" + prompt).text
                except Exception as e:
                    if "429" in str(e) and attempt < 3:
                        time.sleep(65)
                        continue
                    raise

        transcript_ctx = f"VIDEO TITLE: {title}\nCHANNEL: {channel}\nDURATION: {duration}\n\nTRANSCRIPT:\n{raw_transcript[:12000]}"

        results = {}; errors = []
        def gen(key, sys_prompt, usr_prompt):
            try: results[key] = ai(usr_prompt, sys_prompt)
            except Exception as e: errors.append(f"{key}: {e}"); results[key] = f"_Generation failed: {e}_"

        threads = [
            threading.Thread(target=gen, args=("transcript_clean",
                "You are Naina Verma, a transcript specialist. Create a clean, readable transcript with sections, speaker labels where possible, and key moment highlights.",
                f"Clean this raw transcript into a well-structured markdown document with sections, timestamps preserved where possible, and key moments highlighted.\n\n{transcript_ctx}")),
            threading.Thread(target=gen, args=("key_quotes",
                "You are Naina Verma. Extract the most powerful, quotable lines from this transcript.",
                f"Extract the 10-15 strongest quotes and insight lines from this transcript. Format as a markdown list with timestamps if available.\n\n{transcript_ctx}")),
            threading.Thread(target=gen, args=("chapter_markers",
                "You are Naina Verma. Create chapter markers for this video.",
                f"Create a chapter markers table with timestamp, chapter title, and brief description for this video transcript.\n\n{transcript_ctx}")),
            threading.Thread(target=gen, args=("shorts_plan",
                "You are Kabir Shorts, a short-form video strategist. Identify the 3 best clips for short-form content.",
                f"Identify the 3 best short-form video clips from this transcript. For each: start time, end time, title, hook, why it works, caption idea, and ffmpeg command using source: {source_video}\n\n{transcript_ctx}")),
            threading.Thread(target=gen, args=("linkedin",
                "You are Tara LinkedIn, a professional LinkedIn content writer. Write in a thoughtful, story-driven, insight-first style.",
                f"Write 1 polished LinkedIn post + 2 alternates from this video content. No salesy language. Story-first, insight-driven.\n\n{transcript_ctx}")),
            threading.Thread(target=gen, args=("instagram",
                "You are Zoya Social, an Instagram content writer. Punchy, visual, lowercase, hooks first.",
                f"Write an Instagram caption for this video. Hook first, punchy, platform-native tone, end with a question CTA. Include relevant hashtags.\n\n{transcript_ctx}")),
            threading.Thread(target=gen, args=("x_post",
                "You are Zoya Social. Write for X (Twitter). Sharp, direct, internet-native.",
                f"Write 1 standalone X post (under 280 chars) and 1 thread of 5-7 tweets covering the key arc of this content.\n\n{transcript_ctx}")),
            threading.Thread(target=gen, args=("blog",
                "You are Mehul Blogwala, an SEO blog writer. Organised, structured, 1000-1400 words.",
                f"Write a full SEO blog post from this video transcript. Include: SEO title, meta description, intro, 5-6 sections, conclusion, key takeaways.\n\n{transcript_ctx}")),
        ]
        for i, t in enumerate(threads):
            t.start()
            if i < len(threads) - 1:
                time.sleep(13)
        completed = set()
        agent_map = {"transcript_clean":"📝 Naina Verma: Clean transcript","key_quotes":"📝 Naina Verma: Key quotes","chapter_markers":"📝 Naina Verma: Chapter markers","shorts_plan":"🎬 Kabir Shorts: Shorts plan","linkedin":"💼 Tara LinkedIn: LinkedIn post","instagram":"📸 Zoya Social: Instagram caption","x_post":"𝕏 Zoya Social: X thread","blog":"📰 Mehul Blogwala: Blog draft"}
        while any(t.is_alive() for t in threads):
            for key in agent_map:
                if key in results and key not in completed:
                    completed.add(key); push(run_id, "info", f"✅ {agent_map[key]} done")
            time.sleep(1)
        for t in threads: t.join()
        for e in errors: push(run_id, "warn", f"⚠️ {e}")

        # ── WRITE FILES ───────────────────────────────────────────────────────
        push(run_id, "agent", "📁 Ajay Doval: Writing all output files…")
        (tx_dir / "transcript_clean.md").write_text(results.get("transcript_clean",""))
        (tx_dir / "key_quotes.md").write_text(results.get("key_quotes",""))
        (tx_dir / "chapter_markers.md").write_text(results.get("chapter_markers",""))
        (tx_dir / "transcript_raw.md").write_text(f"# Raw Transcript\n\n```\n{raw_transcript}\n```")
        (job_dir / "shorts" / "shorts_plan.md").write_text(results.get("shorts_plan",""))
        (job_dir / "social" / "linkedin_post.md").write_text(results.get("linkedin",""))
        (job_dir / "social" / "instagram_post.md").write_text(results.get("instagram",""))
        (job_dir / "social" / "x_post.md").write_text(results.get("x_post",""))
        (job_dir / "blog"   / "blog_draft.md").write_text(results.get("blog",""))

        # ── CUT CLIPS ─────────────────────────────────────────────────────────
        push(run_id, "agent", "✂️ Kabir Shorts: Cutting video clips with ffmpeg…")
        clip_results = cut_clips(job_dir, source_video, log=lambda t, m: push(run_id, t, m))
        push(run_id, "info", f"✅ {len(clip_results)} clips cut" if clip_results else "⚠️ Clip cutting skipped — timestamps not found")

        # ── QA ────────────────────────────────────────────────────────────────
        push(run_id, "agent", "✅ Ritu QA: Running quality check…")
        all_content = "\n\n---\n\n".join([
            f"## LinkedIn\n{results.get('linkedin','')}",
            f"## Instagram\n{results.get('instagram','')}",
            f"## X Post\n{results.get('x_post','')}",
            f"## Blog\n{results.get('blog','')}",
            f"## Shorts\n{results.get('shorts_plan','')}",
        ])
        qa_output = ai(f'Review these content outputs for a video titled "{title}". Check: accuracy to transcript, grammar, platform fit, unsupported claims, tone consistency. Write qa_notes.md and final_approval_summary.md separated by ===SPLIT===\n\n{all_content[:8000]}',
                       "You are Ritu QA, a strict quality assurance editor.")
        parts = qa_output.split("===SPLIT===")
        (job_dir / "qa" / "qa_notes.md").write_text(parts[0].strip() if len(parts) > 1 else qa_output)
        (job_dir / "qa" / "final_approval_summary.md").write_text(parts[1].strip() if len(parts) > 1 else "# QA Complete\n\nSee qa_notes.md.")
        push(run_id, "info", "✅ Ritu QA: Done")

        # ── ORM ───────────────────────────────────────────────────────────────
        push(run_id, "agent", "🔍 Devika Rao: Fetching YouTube comments and generating ORM report…")
        try:
            info_jsons = list(src_dir.glob("*.info.json"))
            yt_comments = []
            for ij in info_jsons:
                info = json.loads(ij.read_text(errors="replace"))
                yt_comments = info.get("comments", [])
                if yt_comments: break
            if not yt_comments and url:
                tmp = src_dir / "_resume_comments.info.json"
                subprocess.run([str(YTDLP), "--skip-download", "--write-comments",
                    "--extractor-args", "youtube:comment_sort=top;max_comments=200,all,10,5",
                    "--write-info-json", "-o", str(src_dir / "_resume_comments"), url],
                    capture_output=True, timeout=120)
                if tmp.exists():
                    yt_comments = json.loads(tmp.read_text(errors="replace")).get("comments", [])
            if yt_comments:
                comments_text = "\n".join([f"[YouTube] @{c.get('author','anon')} ({c.get('like_count',0)} likes): {c.get('text','')}" for c in yt_comments[:150]])
                video_ctx = f"Title: {title}\nChannel: {channel}\n\nKey Quotes:\n{results.get('key_quotes','')[:2000]}\n\nChapter Markers:\n{results.get('chapter_markers','')[:1500]}"
                orm_resp = ai(f"You are Devika Rao, ORM analyst. Generate full ORM report.\n\n{video_ctx}\n\nCOMMENTS ({len(yt_comments)}):\n{comments_text[:7000]}\n\nStructure:\n# ORM Report\n## 1. Report Scope\n## 2. Executive Summary\n## 3. Sentiment Overview\n## 4. Platform Breakdown\n## 5. Top Repeating Themes\n## 6. Key Risks\n## 7. High-Intent User Signals\n## 8. Content Insights\n## 9. Items That Need Response\n## 10. Recommended Actions\n## 11. Sample Quotes\n## 12. Final Verdict\n\nThen ===SUMMARY===\nThen ===RISKS===\nThen ===ACTIONS===", "You are Devika Rao, ORM Intelligence & Reporting Analyst.")
                oparts = re.split(r"===SUMMARY===|===RISKS===|===ACTIONS===", orm_resp)
                odir = job_dir / "orm-reports" / ("youtube-auto-" + time.strftime("%Y-%m-%d"))
                odir.mkdir(parents=True, exist_ok=True)
                (odir / "orm_report.md").write_text(oparts[0].strip() if oparts else orm_resp)
                (odir / "orm_summary.md").write_text(oparts[1].strip() if len(oparts) > 1 else "")
                (odir / "orm_risk_flags.md").write_text(oparts[2].strip() if len(oparts) > 2 else "")
                (odir / "orm_action_items.md").write_text(oparts[3].strip() if len(oparts) > 3 else "")
                push(run_id, "info", f"✅ Devika Rao: ORM report generated from {len(yt_comments)} comments")
            else:
                push(run_id, "warn", "⚠️ Devika Rao: No comments found — skipping ORM")
        except Exception as e:
            push(run_id, "warn", f"⚠️ Devika Rao: ORM failed — {e}")

        push(run_id, "done", f"🎉 Pipeline complete! Job: {job_name}", {"job": job_name})
        with RUNS_LOCK: RUNS[run_id]["done"] = True; RUNS[run_id]["job"] = job_name

    except Exception as e:
        push(run_id, "error", f"❌ Resume crashed: {e}")
        with RUNS_LOCK: RUNS[run_id]["done"] = True; RUNS[run_id]["error"] = str(e)


# ─── HTML ─────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>Ajay Doval — Command Center</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
:root{--bg:#0f1117;--surface:#1a1d27;--surface2:#22263a;--border:#2e3348;--accent:#6c63ff;--accent2:#ff6584;--green:#43e97b;--yellow:#f9ca24;--red:#ff4d4d;--text:#e8eaf0;--muted:#7b82a0;--radius:10px}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);display:flex;height:100vh;overflow:hidden}

/* SIDEBAR */
#sidebar{width:260px;min-width:260px;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
#sidebar-header{padding:20px 18px 14px;border-bottom:1px solid var(--border)}
#sidebar-header .logo{font-size:15px;font-weight:700;letter-spacing:.3px}
#sidebar-header .subtitle{font-size:11px;color:var(--muted);margin-top:3px}
#job-selector{padding:12px 14px;border-bottom:1px solid var(--border)}
#job-select{width:100%;background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:8px 10px;border-radius:6px;font-size:12px;cursor:pointer;outline:none}
#nav{flex:1;overflow-y:auto;padding:8px 0}
.nav-section-label{font-size:10px;font-weight:600;color:var(--muted);letter-spacing:1px;text-transform:uppercase;padding:10px 18px 4px}
.nav-item{display:flex;align-items:center;gap:10px;padding:9px 18px;cursor:pointer;font-size:13px;color:var(--muted);border-left:3px solid transparent;transition:all .15s}
.nav-item:hover{background:var(--surface2);color:var(--text)}
.nav-item.active{background:var(--surface2);color:var(--text);border-left-color:var(--accent)}
.nav-item .icon{font-size:15px;width:18px;text-align:center}
.nav-badge{margin-left:auto;font-size:10px;padding:2px 6px;border-radius:10px;background:var(--surface);color:var(--muted)}
.nav-badge.green{background:rgba(67,233,123,.15);color:var(--green)}

/* NEW JOB BUTTON */
#new-job-btn{margin:12px 14px;background:var(--accent);color:#fff;border:none;padding:9px 0;border-radius:6px;font-size:12px;font-weight:700;cursor:pointer;width:calc(100% - 28px);letter-spacing:.3px;transition:opacity .15s}
#new-job-btn:hover{opacity:.85}

/* MAIN */
#main{flex:1;display:flex;flex-direction:column;overflow:hidden}
#topbar{padding:16px 28px;border-bottom:1px solid var(--border);background:var(--surface);display:flex;align-items:center;justify-content:space-between}
#topbar .page-title{font-size:16px;font-weight:600}
#topbar .page-meta{font-size:12px;color:var(--muted);margin-top:2px}
#copy-btn{background:var(--accent);color:#fff;border:none;padding:8px 16px;border-radius:6px;font-size:12px;cursor:pointer;font-weight:600;display:none}
#copy-btn:hover{opacity:.85}
#content{flex:1;overflow-y:auto;padding:28px}

/* MODAL */
#modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);display:none;align-items:center;justify-content:center;z-index:100}
#modal-overlay.open{display:flex}
#modal{background:var(--surface);border:1px solid var(--border);border-radius:14px;width:560px;max-width:95vw;overflow:hidden}
#modal-header{padding:20px 24px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
#modal-header h2{font-size:15px;font-weight:700}
#modal-close{background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer;line-height:1}
#modal-close:hover{color:var(--text)}
#modal-body{padding:24px}
.field-label{font-size:11px;font-weight:600;color:var(--muted);letter-spacing:.5px;text-transform:uppercase;margin-bottom:8px}
.field-input{width:100%;background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:11px 14px;border-radius:8px;font-size:13px;outline:none;transition:border-color .15s;font-family:inherit}
.field-input:focus{border-color:var(--accent)}
.field-hint{font-size:11px;color:var(--muted);margin-top:6px}
.modal-divider{height:1px;background:var(--border);margin:20px 0}
#modal-run-btn{width:100%;background:var(--accent);color:#fff;border:none;padding:12px;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;transition:opacity .15s;margin-top:16px}
#modal-run-btn:hover:not(:disabled){opacity:.85}
#modal-run-btn:disabled{opacity:.4;cursor:not-allowed}

/* PROGRESS LOG */
#progress-log{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:16px;height:240px;overflow-y:auto;font-family:'SF Mono','Fira Code',monospace;font-size:12px;line-height:1.8;margin-top:16px;display:none}
.log-entry{display:flex;gap:10px;animation:fadeIn .2s}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.log-time{color:var(--muted);min-width:50px}
.log-msg{color:var(--text)}
.log-msg.error{color:var(--red)}
.log-msg.warn{color:var(--yellow)}
.log-msg.done{color:var(--green);font-weight:700}
.log-msg.agent{color:#a78bfa}
#progress-bar-wrap{margin-top:12px;display:none}
#progress-bar{height:4px;background:var(--surface2);border-radius:2px;overflow:hidden}
#progress-fill{height:100%;background:var(--accent);width:0%;transition:width .4s;border-radius:2px}
#progress-label{font-size:11px;color:var(--muted);margin-top:6px;text-align:center}

/* CONTENT STYLES */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:20px;overflow:hidden}
.card-header{padding:14px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.card-header h3{font-size:13px;font-weight:600}
.card-body{padding:20px}
.overview-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-bottom:24px}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:18px;display:flex;flex-direction:column;gap:6px}
.stat-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.stat-value{font-size:20px;font-weight:700}
.file-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.file-chip{background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:10px 14px;cursor:pointer;transition:all .15s;display:flex;align-items:center;gap:8px}
.file-chip:hover{border-color:var(--accent);color:var(--accent)}
.chip-name{font-size:12px;font-weight:500}
.chip-size{font-size:10px;color:var(--muted)}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;padding:3px 9px;border-radius:20px}
.badge.approved{background:rgba(67,233,123,.15);color:var(--green)}
.markdown-body{font-size:13.5px;line-height:1.75;color:var(--text)}
.markdown-body h1{font-size:20px;margin:0 0 16px;padding-bottom:10px;border-bottom:1px solid var(--border)}
.markdown-body h2{font-size:16px;margin:24px 0 10px;color:var(--accent)}
.markdown-body h3{font-size:14px;margin:18px 0 8px}
.markdown-body p{margin-bottom:12px}
.markdown-body ul,.markdown-body ol{margin:8px 0 12px 20px}
.markdown-body li{margin-bottom:5px}
.markdown-body blockquote{border-left:3px solid var(--accent);padding:8px 16px;margin:12px 0;background:var(--surface2);border-radius:0 6px 6px 0;font-style:italic;color:var(--muted)}
.markdown-body code{background:var(--surface2);padding:2px 6px;border-radius:4px;font-size:12px;color:#ff6584}
.markdown-body pre{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:16px;overflow-x:auto;margin:12px 0}
.markdown-body pre code{background:none;padding:0;color:#a8d8a8}
.markdown-body table{width:100%;border-collapse:collapse;margin:12px 0;font-size:13px}
.markdown-body th{background:var(--surface2);padding:10px 14px;text-align:left;font-weight:600;border:1px solid var(--border)}
.markdown-body td{padding:9px 14px;border:1px solid var(--border);color:var(--muted)}
.markdown-body td:first-child{color:var(--text);font-weight:500}
.markdown-body strong{color:var(--text);font-weight:600}
.markdown-body hr{border:none;border-top:1px solid var(--border);margin:20px 0}
.tabs{display:flex;gap:2px;border-bottom:1px solid var(--border);margin-bottom:20px}
.tab{padding:9px 18px;font-size:12px;cursor:pointer;color:var(--muted);border-bottom:2px solid transparent;transition:all .15s}
.tab:hover{color:var(--text)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}
.empty{text-align:center;padding:60px 20px;color:var(--muted)}
.empty .empty-icon{font-size:40px;margin-bottom:12px}
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}

/* VIDEO PLAYER */
.video-wrap{background:#000;border-radius:10px;overflow:hidden;margin-bottom:20px;position:relative}
.video-wrap video{width:100%;display:block;max-height:480px}
.video-label{position:absolute;top:10px;left:12px;background:rgba(0,0,0,.65);color:#fff;font-size:11px;font-weight:600;padding:4px 10px;border-radius:20px;backdrop-filter:blur(4px)}

/* CLIP CARDS */
.clip-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px;margin-bottom:24px}
.clip-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden}
.clip-video-wrap{background:#000;position:relative}
.clip-video-wrap video{width:100%;display:block;max-height:220px}
.clip-num-badge{position:absolute;top:8px;left:10px;background:var(--accent);color:#fff;font-size:11px;font-weight:700;padding:3px 9px;border-radius:12px}
.clip-info{padding:14px 16px}
.clip-title{font-size:13px;font-weight:600;margin-bottom:6px}
.clip-meta-row{display:flex;gap:16px;font-size:11px;color:var(--muted);margin-bottom:8px}
.clip-meta-row span{display:flex;align-items:center;gap:4px}
.cut-btn{width:100%;margin:14px 0 0;padding:9px;background:var(--surface2);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;transition:all .15s}
.cut-btn:hover{border-color:var(--accent);color:var(--accent)}
.cut-btn.cutting{color:var(--yellow);border-color:var(--yellow)}
.cut-btn.done{color:var(--green);border-color:var(--green)}

/* ORM */
.orm-input-area{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:20px;margin-bottom:20px}
.orm-input-area textarea{width:100%;background:var(--surface2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:12px 14px;font-size:12.5px;font-family:'SF Mono','Fira Code',monospace;resize:vertical;outline:none;transition:border-color .15s;line-height:1.6}
.orm-input-area textarea:focus{border-color:var(--accent)}
.orm-run-btn{background:var(--accent);color:#fff;border:none;padding:10px 22px;border-radius:7px;font-size:13px;font-weight:700;cursor:pointer;transition:opacity .15s;margin-top:12px}
.orm-run-btn:hover:not(:disabled){opacity:.85}
.orm-run-btn:disabled{opacity:.4;cursor:not-allowed}
.orm-status{font-size:12px;color:var(--muted);margin-top:10px;min-height:18px}
.orm-status.running{color:#a78bfa}
.orm-status.done{color:var(--green)}
.orm-status.error{color:var(--red)}
.risk-HIGH{color:var(--red);font-weight:700}
.risk-MEDIUM{color:var(--yellow);font-weight:600}
.risk-LOW{color:var(--green)}
</style>
</head>
<body>

<div id="sidebar">
  <div id="sidebar-header">
    <div class="logo">⚡ Ajay Doval</div>
    <div class="subtitle">AI Command Center</div>
  </div>
  <div id="job-selector">
    <select id="job-select" onchange="loadJob(this.value)">
      <option value="">Loading jobs…</option>
    </select>
  </div>
  <button id="new-job-btn" onclick="openModal()">+ Process New Video</button>
  <div id="nav">
    <div class="nav-section-label">Pipeline</div>
    <div class="nav-item active" onclick="showSection('overview')" id="nav-overview"><span class="icon">🏠</span>Overview</div>
    <div class="nav-item" onclick="showSection('intake')" id="nav-intake"><span class="icon">📥</span>Intake<span class="nav-badge green" id="badge-intake">✓</span></div>
    <div class="nav-item" onclick="showSection('transcript')" id="nav-transcript"><span class="icon">📝</span>Transcript<span class="nav-badge green">✓</span></div>
    <div class="nav-item" onclick="showSection('shorts')" id="nav-shorts"><span class="icon">🎬</span>Shorts<span class="nav-badge green">✓</span></div>
    <div class="nav-section-label">Content</div>
    <div class="nav-item" onclick="showSection('linkedin')" id="nav-linkedin"><span class="icon">💼</span>LinkedIn</div>
    <div class="nav-item" onclick="showSection('instagram')" id="nav-instagram"><span class="icon">📸</span>Instagram</div>
    <div class="nav-item" onclick="showSection('x')" id="nav-x"><span class="icon">𝕏</span>X / Twitter</div>
    <div class="nav-item" onclick="showSection('blog')" id="nav-blog"><span class="icon">📰</span>Blog</div>
    <div class="nav-section-label">Quality</div>
    <div class="nav-item" onclick="showSection('qa')" id="nav-qa"><span class="icon">✅</span>QA Report</div>
    <div class="nav-section-label">Intelligence</div>
    <div class="nav-item" onclick="showSection('orm')" id="nav-orm"><span class="icon">🔍</span>ORM Reports</div>
  </div>
</div>

<div id="main">
  <div id="topbar">
    <div>
      <div class="page-title" id="page-title">Overview</div>
      <div class="page-meta" id="page-meta">Select a job or process a new video</div>
    </div>
    <button id="copy-btn" onclick="copyContent()">Copy</button>
    <button id="export-btn" onclick="exportZip()" style="background:var(--surface2);color:var(--text);border:1px solid var(--border);padding:8px 16px;border-radius:6px;font-size:12px;cursor:pointer;font-weight:600;display:none">⬇️ Export ZIP</button>
  </div>
  <div id="content">
    <div class="empty"><div class="empty-icon">⚡</div><p>Select a job from the dropdown or click <strong>+ Process New Video</strong></p></div>
  </div>
</div>

<!-- NEW VIDEO MODAL -->
<div id="modal-overlay" onclick="maybeCloseModal(event)">
  <div id="modal">
    <div id="modal-header">
      <h2>⚡ Process New Video</h2>
      <button id="modal-close" onclick="closeModal()">✕</button>
    </div>
    <div id="modal-body">
      <div class="field-label">YouTube URL</div>
      <input class="field-input" id="yt-url" type="url" placeholder="https://youtu.be/..." />
      <div class="field-hint">Paste any YouTube video URL. Shorts, full videos, and unlisted videos all work.</div>
      <div class="modal-divider"></div>
      <div class="field-hint" style="background:rgba(67,233,123,.08);border:1px solid rgba(67,233,123,.2);border-radius:6px;padding:10px 14px;color:#43e97b;font-size:12px">⚡ AI agents powered by Gemini — no API key needed</div>
      <div id="progress-bar-wrap">
        <div id="progress-bar"><div id="progress-fill"></div></div>
        <div id="progress-label">Starting…</div>
      </div>
      <div id="progress-log"></div>
      <button id="modal-run-btn" onclick="startPipeline()">🚀 Run Full Pipeline</button>
    </div>
  </div>
</div>

<script>
let currentJob = null, currentSection = 'overview', currentCopyText = '', jobData = {}, runSSE = null;

async function fetchJobs() {
  const res = await fetch('/api/jobs');
  const jobs = await res.json();
  const sel = document.getElementById('job-select');
  sel.innerHTML = '<option value="">— Select a job —</option>';
  jobs.forEach(j => { const o = document.createElement('option'); o.value = j; o.textContent = j; sel.appendChild(o); });
  if (jobs.length === 1) { sel.value = jobs[0]; loadJob(jobs[0]); }
}

async function loadJob(name) {
  if (!name) return;
  currentJob = name;
  document.getElementById('page-meta').textContent = 'Loading…';
  const [jobRes, clipsRes, ormRes] = await Promise.all([
    fetch('/api/job/' + encodeURIComponent(name)),
    fetch('/api/clips/' + encodeURIComponent(name)),
    fetch('/api/orm/' + encodeURIComponent(name))
  ]);
  jobData = await jobRes.json();
  const clipsData = await clipsRes.json();
  const ormData = await ormRes.json();
  jobData.clips = clipsData.clips || [];
  jobData.main_video = clipsData.main_video || null;
  ormReports = ormData.reports || [];
  document.getElementById('export-btn').style.display = 'block';
  showSection('overview');
}

async function cutClipsNow() {
  const btn = document.getElementById('cut-clips-btn');
  if (!btn || !currentJob) return;
  btn.textContent = '⏳ Cutting clips…'; btn.disabled = true; btn.className = 'cut-btn cutting';
  const res = await fetch('/api/clips/' + encodeURIComponent(currentJob));
  const data = await res.json();
  jobData.clips = data.clips || [];
  jobData.main_video = data.main_video || null;
  render();
}

function showSection(s) {
  currentSection = s;
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const el = document.getElementById('nav-' + s);
  if (el) el.classList.add('active');
  document.getElementById('copy-btn').style.display = 'none';
  currentCopyText = '';
  render();
}

function render() {
  if (!currentJob || !jobData) return;
  document.getElementById('page-title').textContent = {overview:'Overview',intake:'Intake',transcript:'Transcript',shorts:'Shorts Plan',linkedin:'LinkedIn',instagram:'Instagram',x:'X / Twitter',blog:'Blog Draft',qa:'QA Report',orm:'ORM Reports'}[currentSection] || '';
  document.getElementById('page-meta').textContent = currentJob;
  const fns = { overview: renderOverview, intake: () => renderMarkdown('intake'), transcript: renderTranscript, shorts: renderShorts, linkedin: () => renderSocial('linkedin'), instagram: () => renderSocial('instagram'), x: () => renderSocial('x'), blog: () => renderMarkdown('blog'), qa: () => renderMarkdown('qa'), orm: renderORM };
  document.getElementById('content').innerHTML = (fns[currentSection] || (() => ''))();
}

function renderOverview() {
  const m = jobData.meta || {}, files = jobData.files || [];
  let html = `<div class="overview-grid">
    <div class="stat-card"><div class="stat-label">Video</div><div class="stat-value" style="font-size:13px">${m.title||'—'}</div></div>
    <div class="stat-card"><div class="stat-label">Channel</div><div class="stat-value" style="font-size:15px">${m.channel||'—'}</div></div>
    <div class="stat-card"><div class="stat-label">Duration</div><div class="stat-value">${m.duration||'—'}</div></div>
    <div class="stat-card"><div class="stat-label">Upload Date</div><div class="stat-value" style="font-size:13px">${m.upload_date||'—'}</div></div>
    <div class="stat-card"><div class="stat-label">Output Files</div><div class="stat-value">${files.length}</div></div>
    <div class="stat-card"><div class="stat-label">Status</div><div class="stat-value" style="font-size:13px;color:#43e97b">✓ Complete</div></div>
  </div>`;
  const icons = {source:'📥',transcript:'📝',shorts:'🎬',social:'💬',blog:'📰',qa:'✅'};
  const grouped = {};
  files.forEach(f => { const k = f.folder||'other'; (grouped[k]=grouped[k]||[]).push(f); });
  Object.entries(grouped).forEach(([folder, flist]) => {
    html += `<div class="card"><div class="card-header"><h3>${icons[folder]||'📁'} ${folder}</h3><span class="badge approved">✓</span></div><div class="card-body"><div class="file-grid">`;
    flist.forEach(f => { html += `<div class="file-chip" onclick="showSection('${mapF(f.folder)}')"><span>${fileIcon(f.name)}</span><div><div class="chip-name">${f.name}</div><div class="chip-size">${f.size}</div></div></div>`; });
    html += `</div></div></div>`;
  });
  return html;
}
function mapF(f){return{source:'intake',transcript:'transcript',shorts:'shorts',social:'linkedin',blog:'blog',qa:'qa'}[f]||'overview'}
function fileIcon(n){if(n.endsWith('.mp4'))return'🎥';if(n.endsWith('.m4a'))return'🎵';if(n.endsWith('.sh'))return'⚙️';if(n.endsWith('.csv'))return'📊';if(n.includes('transcript'))return'📝';if(n.includes('blog'))return'📰';if(n.includes('qa')||n.includes('approval'))return'✅';if(n.includes('linkedin'))return'💼';if(n.includes('instagram'))return'📸';if(n.includes('x_post'))return'𝕏';if(n.includes('shorts')||n.includes('clip'))return'🎬';return'📄'}

function renderMarkdown(section) {
  const t = (jobData.content||{})[section]||'';
  if(!t) return `<div class="empty"><div class="empty-icon">📭</div><p>No content found.</p></div>`;
  document.getElementById('copy-btn').style.display='block'; currentCopyText=t;

  // Intake section: prepend main video player
  let prefix = '';
  if (section === 'intake' && jobData.main_video) {
    prefix = `<div class="video-wrap" style="margin-bottom:20px">
      <video controls preload="metadata" style="width:100%;max-height:480px">
        <source src="${jobData.main_video}" type="video/mp4">
      </video>
      <div class="video-label">📹 Source Video</div>
    </div>`;
  }

  return prefix + `<div class="card"><div class="card-body"><div class="markdown-body">${marked.parse(t)}</div></div></div>`;
}
function renderTranscript() {
  const c = jobData.content||{};
  const tabs = [{id:'clean',label:'Clean',key:'transcript_clean'},{id:'quotes',label:'Key Quotes',key:'key_quotes'},{id:'chapters',label:'Chapters',key:'chapter_markers'},{id:'raw',label:'Raw',key:'transcript_raw'}];
  let html = `<div class="tabs">${tabs.map((t,i)=>`<div class="tab ${i===0?'active':''}" onclick="switchTab(this,'ttab-${t.id}')">${t.label}</div>`).join('')}</div>`;
  tabs.forEach((t,i) => { html += `<div id="ttab-${t.id}" style="display:${i===0?'block':'none'}"><div class="card"><div class="card-body"><div class="markdown-body">${marked.parse(c[t.key]||'_No content._')}</div></div></div></div>`; });
  return html;
}
function renderShorts() {
  const c = jobData.content||{};
  const clips = jobData.clips || [];

  // ── Video clip players ──────────────────────────────────────────────────
  let playersHtml = '';
  if (clips.length > 0) {
    playersHtml += `<div class="clip-grid">`;
    clips.forEach(clip => {
      playersHtml += `
        <div class="clip-card">
          <div class="clip-video-wrap">
            <video controls preload="metadata" style="max-height:220px">
              <source src="${clip.url}" type="video/mp4">
            </video>
            <div class="clip-num-badge">Clip ${clip.num}</div>
          </div>
          <div class="clip-info">
            <div class="clip-title">${clip.title}</div>
            <div class="clip-meta-row">
              <span>⏱ ${clip.start} → ${clip.end}</span>
            </div>
          </div>
        </div>`;
    });
    playersHtml += `</div>`;
  } else {
    playersHtml += `<div class="card" style="margin-bottom:20px">
      <div class="card-body" style="display:flex;align-items:center;gap:16px">
        <div style="flex:1;color:var(--muted);font-size:13px">No clips cut yet. Click below to trim and generate all 3 shorts from the timestamps in the plan.</div>
        <button class="cut-btn" id="cut-clips-btn" onclick="cutClipsNow()" style="width:auto;padding:9px 18px;white-space:nowrap">✂️ Cut Clips Now</button>
      </div>
    </div>`;
  }

  // ── Tabs for plan / commands / csv ──────────────────────────────────────
  const tabs = [{id:'plan',label:'Shorts Plan',key:'shorts_plan'},{id:'cmd',label:'ffmpeg Commands',key:'clip_commands'},{id:'csv',label:'Timestamps CSV',key:'shorts_csv'}];
  let tabsHtml = `<div class="tabs">${tabs.map((t,i)=>`<div class="tab ${i===0?'active':''}" onclick="switchTab(this,'stab-${t.id}')">${t.label}</div>`).join('')}</div>`;
  tabs.forEach((t,i) => { tabsHtml += `<div id="stab-${t.id}" style="display:${i===0?'block':'none'}"><div class="card"><div class="card-body"><div class="markdown-body">${marked.parse(c[t.key]||'_No content._')}</div></div></div></div>`; });

  return playersHtml + tabsHtml;
}
function renderSocial(p) {
  const t = (jobData.content||{})[p]||'';
  document.getElementById('copy-btn').style.display='block'; currentCopyText=t;
  return `<div class="card"><div class="card-body"><div class="markdown-body">${marked.parse(t||'_No content._')}</div></div></div>`;
}
let ormReports = [];
async function loadORM() {
  if (!currentJob) return;
  const res = await fetch('/api/orm/' + encodeURIComponent(currentJob));
  const data = await res.json();
  ormReports = data.reports || [];
}

function renderORM() {
  let reportsHtml = '';
  if (ormReports.length > 0) {
    // Top-level: one tab per report name
    const reportTabs = ormReports.map((r,i) => `<div class="tab ${i===0?'active':''}" onclick="switchOrmReport(${i},this)">${r.name}</div>`).join('');
    reportsHtml = `<div class="tabs" id="orm-report-tabs">${reportTabs}</div><div id="orm-report-panels">`;
    ormReports.forEach((r,i) => {
      // Inner tabs per report: ORM Report / Summary / Risk Flags / Action Items
      const innerTabs = (r.tabs||[{name:'ORM Report',content:r.content}]).map((t,j) =>
        `<div class="tab ${j===0?'active':''}" onclick="switchTab(this,'oi-${i}-${j}')">${t.name}</div>`
      ).join('');
      let innerPanels = '';
      (r.tabs||[{name:'ORM Report',content:r.content}]).forEach((t,j) => {
        innerPanels += `<div id="oi-${i}-${j}" style="display:${j===0?'block':'none'}">
          <div class="card"><div class="card-body"><div class="markdown-body">${marked.parse(t.content||'_No content._')}</div></div></div>
        </div>`;
      });
      reportsHtml += `<div id="orm-rp-${i}" style="display:${i===0?'block':'none'}">
        <div class="tabs">${innerTabs}</div>${innerPanels}
      </div>`;
    });
    reportsHtml += `</div>`;
  }

  return `
    <div class="orm-input-area">
      <div class="field-label" style="margin-bottom:10px">🔍 Devika Rao — ORM Analysis</div>
      <div style="font-size:12px;color:var(--muted);margin-bottom:12px">Paste YouTube comments, Reddit threads, or Quora questions. Devika will classify, detect patterns, and generate a structured ORM report grounded in the video content.</div>
      <div class="field-label">Paste Comments / Posts</div>
      <textarea id="orm-comments" rows="8" placeholder="Paste comments here — one per line, or in any format (Reddit threads, YouTube comments, Quora answers)…"></textarea>
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:12px">
        <div style="flex:1">
          <div class="field-label">Report Name (optional)</div>
          <input class="field-input" id="orm-report-name" type="text" placeholder="e.g. youtube-comments-apr-2026" style="margin-top:4px" />
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:14px;margin-top:12px">
        <button class="orm-run-btn" id="orm-run-btn" onclick="runORM()">📊 Generate ORM Report</button>
        <div class="orm-status" id="orm-status"></div>
      </div>
    </div>
    ${reportsHtml || `<div class="card" style="margin-bottom:20px">
      <div class="card-body" style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
        <div style="flex:1;min-width:200px">
          <div style="font-size:13px;font-weight:600;margin-bottom:4px">No ORM reports yet</div>
          <div style="font-size:12px;color:var(--muted)">Fetch YouTube comments automatically and run Devika's full analysis — no manual paste needed.</div>
        </div>
        <div style="display:flex;flex-direction:column;gap:8px;min-width:220px">
          <button class="orm-run-btn" id="orm-fetch-btn" onclick="fetchAndGenerateORM()" style="margin-top:0">📥 Fetch YouTube Comments & Generate</button>
          <div class="orm-status" id="orm-fetch-status"></div>
        </div>
      </div>
    </div>`}
  `;
}

async function fetchAndGenerateORM() {
  const btn = document.getElementById('orm-fetch-btn');
  const status = document.getElementById('orm-fetch-status');
  btn.disabled = true; btn.textContent = '⏳ Fetching comments…';
  status.className = 'orm-status running'; status.textContent = 'Devika Rao is fetching YouTube comments…';
  try {
    const res = await fetch('/api/orm/fetch-and-generate', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({job: currentJob})
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    status.className = 'orm-status done';
    status.textContent = `✅ Report generated from ${data.comments_fetched} comments`;
    const ormRes = await fetch('/api/orm/' + encodeURIComponent(currentJob));
    const ormData = await ormRes.json();
    ormReports = ormData.reports || [];
    render();
  } catch(e) {
    status.className = 'orm-status error'; status.textContent = '✗ ' + e.message;
  } finally {
    btn.disabled = false; btn.textContent = '📥 Fetch YouTube Comments & Generate';
  }
}

function switchOrmReport(idx, el) {
  el.closest('.tabs').querySelectorAll('.tab').forEach(t=>t.classList.remove('active')); el.classList.add('active');
  document.querySelectorAll('[id^="orm-rp-"]').forEach((p,i)=>p.style.display=i===idx?'block':'none');
}

async function runORM() {
  const comments = document.getElementById('orm-comments').value.trim();
  const name = document.getElementById('orm-report-name').value.trim() || ('orm-' + new Date().toISOString().slice(0,10));
  if (!comments) { alert('Paste some comments first.'); return; }
  const btn = document.getElementById('orm-run-btn');
  const status = document.getElementById('orm-status');
  btn.disabled = true; btn.textContent = '⏳ Analysing…';
  status.className = 'orm-status running'; status.textContent = 'Devika Rao is reading the comments and video context…';
  try {
    const res = await fetch('/api/orm/generate', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({job: currentJob, comments, report_name: name})
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    status.className = 'orm-status done'; status.textContent = '✅ Report saved — ' + name;
    await loadORM();
    render();
  } catch(e) {
    status.className = 'orm-status error'; status.textContent = '✗ ' + e.message;
  } finally {
    btn.disabled = false; btn.textContent = '📊 Generate ORM Report';
  }
}

function switchTab(el, id) {
  el.closest('.tabs').querySelectorAll('.tab').forEach(t=>t.classList.remove('active')); el.classList.add('active');
  document.querySelectorAll('[id^="ttab-"],[id^="stab-"]').forEach(t=>{ if(t.id===id)t.style.display='block'; else if(t.id.startsWith(id.split('-')[0]))t.style.display='none'; });
  // more robust: find sibling panels
  const prefix = id.split('-')[0]+'-';
  document.getElementById('content').querySelectorAll(`[id^="${prefix}"]`).forEach(t=>t.style.display=t.id===id?'block':'none');
}
function exportZip() {
  if (!currentJob) return;
  const btn = document.getElementById('export-btn');
  btn.textContent = '⏳ Preparing…'; btn.disabled = true;
  const a = document.createElement('a');
  a.href = '/api/export/' + encodeURIComponent(currentJob);
  a.download = currentJob.slice(0,40) + '.zip';
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  setTimeout(() => { btn.textContent = '⬇️ Export ZIP'; btn.disabled = false; }, 2000);
}

function copyContent() {
  if(!currentCopyText) return;
  navigator.clipboard.writeText(currentCopyText).then(()=>{ const b=document.getElementById('copy-btn'); b.textContent='Copied!'; setTimeout(()=>b.textContent='Copy',1500); });
}

/* ── MODAL ── */
function openModal(){ document.getElementById('modal-overlay').classList.add('open'); document.getElementById('yt-url').focus(); }
function closeModal(){ if(runSSE){runSSE.close();runSSE=null;} document.getElementById('modal-overlay').classList.remove('open'); resetModal(); }
function maybeCloseModal(e){ if(e.target===document.getElementById('modal-overlay')) closeModal(); }
function resetModal(){
  document.getElementById('yt-url').value=''; document.getElementById('modal-run-btn').disabled=false; document.getElementById('modal-run-btn').textContent='🚀 Run Full Pipeline';
  document.getElementById('progress-log').style.display='none'; document.getElementById('progress-log').innerHTML='';
  document.getElementById('progress-bar-wrap').style.display='none'; document.getElementById('progress-fill').style.width='0%';
}

const STEPS = ['Fetching metadata','Downloading','Merging','Transcribing','Generating content','Writing files','QA'];
let stepIdx = 0;

function logLine(type, msg) {
  const log = document.getElementById('progress-log');
  const t = new Date().toLocaleTimeString('en',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
  const div = document.createElement('div'); div.className='log-entry';
  div.innerHTML = `<span class="log-time">${t}</span><span class="log-msg ${type==='error'?'error':type==='warn'?'warn':type==='done'?'done':type==='agent'?'agent':''}">${msg}</span>`;
  log.appendChild(div); log.scrollTop = log.scrollHeight;
}

async function startPipeline() {
  const url = document.getElementById('yt-url').value.trim();
  if (!url) { alert('Please enter a YouTube URL'); return; }
  if (!url.includes('youtube.com') && !url.includes('youtu.be')) { alert('Please enter a valid YouTube URL'); return; }

  document.getElementById('modal-run-btn').disabled = true;
  document.getElementById('modal-run-btn').textContent = '⏳ Running…';
  document.getElementById('progress-log').style.display = 'block';
  document.getElementById('progress-log').innerHTML = '';
  document.getElementById('progress-bar-wrap').style.display = 'block';
  stepIdx = 0;

  const startRes = await fetch('/api/pipeline/start', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({url, api_key: ''})
  });
  const {run_id} = await startRes.json();

  // Stream progress via SSE
  runSSE = new EventSource('/api/pipeline/progress?run_id=' + run_id);
  runSSE.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    logLine(ev.type, ev.msg);

    // Update progress bar heuristically
    const pct = Math.min(95, {'start':5,'info':stepIdx*12+10,'agent':stepIdx*12+5,'warn':stepIdx*12+8,'done':100}[ev.type]||stepIdx*12);
    if(ev.type==='info' || ev.type==='agent') stepIdx = Math.min(stepIdx+1, STEPS.length-1);
    document.getElementById('progress-fill').style.width = (ev.type==='done'?100:pct) + '%';
    document.getElementById('progress-label').textContent = ev.type==='done' ? '✓ Complete!' : STEPS[Math.min(stepIdx, STEPS.length-1)];

    if (ev.type === 'done' || ev.type === 'error') {
      runSSE.close(); runSSE = null;
      document.getElementById('modal-run-btn').textContent = ev.type==='done' ? '✓ Done!' : '✗ Failed';
      if (ev.type === 'done' && ev.data && ev.data.job) {
        setTimeout(async () => {
          await fetchJobs();
          closeModal();
          document.getElementById('job-select').value = ev.data.job;
          loadJob(ev.data.job);
        }, 1500);
      }
    }
  };
  runSSE.onerror = () => { logLine('error','Connection to server lost.'); runSSE.close(); };
}

fetchJobs();
</script>
</body>
</html>"""


# ─── SERVER ───────────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def serve_video(self, file_path: str):
        """Serve a video file with byte-range support so the browser can seek."""
        p = Path(file_path)
        # Security: must be inside OUTPUTS_DIR
        try:
            p.resolve().relative_to(OUTPUTS_DIR.resolve())
        except ValueError:
            self.send_response(403); self.end_headers(); return
        if not p.exists() or not p.is_file():
            self.send_response(404); self.end_headers(); return

        ext      = p.suffix.lstrip(".").lower()
        mime     = VIDEO_MIME.get(ext, "application/octet-stream")
        size     = p.stat().st_size
        rng      = self.headers.get("Range", "")

        try:
            if rng:
                m = re.match(r"bytes=(\d*)-(\d*)", rng)
                start = int(m.group(1)) if m and m.group(1) else 0
                end   = int(m.group(2)) if m and m.group(2) else size - 1
                end   = min(end, size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", length)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with open(p, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining:
                        chunk = f.read(min(65536, remaining))
                        if not chunk: break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            else:
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", size)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with open(p, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk: break
                        self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if self.path == "/api/orm/generate":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            job_name    = body.get("job", "").strip()
            comments    = body.get("comments", "").strip()
            report_name = body.get("report_name", "orm-report").strip()

            if not job_name or not comments:
                self.send_json({"error": "missing job or comments"}, 400); return

            job_dir = OUTPUTS_DIR / job_name
            if not job_dir.exists():
                self.send_json({"error": "job not found"}, 404); return

            try:
                import google.generativeai as genai
                genai.configure(api_key=ORM_API_KEY)
                gemini = genai.GenerativeModel(GEMINI_MODEL)
            except Exception as e:
                self.send_json({"error": str(e)}, 500); return

            def orm_ai(prompt):
                return gemini.generate_content(prompt).text

            # Build video context from existing pipeline outputs
            def r(p): return p.read_text(errors="replace") if p.exists() else ""
            video_ctx = ""
            intake  = r(job_dir / "source" / "intake_summary.md")
            quotes  = r(job_dir / "transcript" / "key_quotes.md")
            chapters = r(job_dir / "transcript" / "chapter_markers.md")
            tx_clean = r(job_dir / "transcript" / "transcript_clean.md")
            if intake or quotes:
                video_ctx = f"""## VIDEO CONTEXT
{intake}

### Key Quotes
{quotes[:3000]}

### Chapter Markers
{chapters[:2000]}

### Transcript Excerpt
{tx_clean[:4000]}
"""

            system_prompt = """You are Devika Rao, ORM Intelligence & Reporting Analyst.
You read public comments and produce structured, actionable ORM reports.
You are analytical, calm, pattern-oriented. You separate facts from interpretation.
You never invent data. If counts are approximate, you say "observed trend."
You connect comments to the specific video content when context is available.
Your reports are for internal brand, content, and community teams."""

            user_prompt = f"""Generate a full ORM report for the following comments.

{video_ctx}

## COMMENTS / ORM INPUT:
{comments[:8000]}

Produce the full ORM report in this exact structure:

# ORM Report

## 1. Report Scope
## 2. Executive Summary
## 3. Sentiment Overview
## 4. Platform Breakdown
## 5. Top Repeating Themes
## 6. Key Risks
## 7. High-Intent User Signals
## 8. Content Insights
## 9. Items That Need Response
## 10. Recommended Actions
## 11. Sample Quotes / Evidence
## 12. Final Verdict

Then output ===SUMMARY=== and write a concise orm_summary.md (3-5 bullet takeaways).
Then output ===RISKS=== and write orm_risk_flags.md listing only HIGH and MEDIUM risk items.
Then output ===ACTIONS=== and write orm_action_items.md with immediate, short-term, and content opportunity actions."""

            try:
                full = orm_ai(system_prompt + "\n\n" + user_prompt)
            except Exception as e:
                self.send_json({"error": f"AI generation failed: {e}"}); return

            # Split sections
            parts = re.split(r"===SUMMARY===|===RISKS===|===ACTIONS===", full)
            orm_report  = parts[0].strip() if len(parts) > 0 else full
            orm_summary = parts[1].strip() if len(parts) > 1 else ""
            orm_risks   = parts[2].strip() if len(parts) > 2 else ""
            orm_actions = parts[3].strip() if len(parts) > 3 else ""

            # Save to folder
            safe_name = re.sub(r"[^\w\-]", "_", report_name)[:50]
            out_dir = job_dir / "orm-reports" / safe_name
            out_dir.mkdir(parents=True, exist_ok=True)

            (out_dir / "orm_report.md").write_text(orm_report)
            (out_dir / "orm_summary.md").write_text(orm_summary)
            (out_dir / "orm_risk_flags.md").write_text(orm_risks)
            (out_dir / "orm_action_items.md").write_text(orm_actions)

            self.send_json({"ok": True, "report_name": safe_name})

        elif self.path == "/api/orm/fetch-and-generate":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            job_name = body.get("job", "").strip()

            if not job_name:
                self.send_json({"error": "missing job"}, 400); return

            job_dir = OUTPUTS_DIR / job_name
            if not job_dir.exists():
                self.send_json({"error": "job not found"}, 404); return

            # Read original URL from intake_summary.md
            intake_path = job_dir / "source" / "intake_summary.md"
            if not intake_path.exists():
                self.send_json({"error": "intake_summary.md not found"}); return

            yt_url = ""
            for line in intake_path.read_text(errors="replace").splitlines():
                if "| URL" in line:
                    parts = line.split("|")
                    if len(parts) >= 3:
                        yt_url = parts[2].strip()
                        break

            if not yt_url:
                self.send_json({"error": "Could not find URL in intake_summary.md"}); return

            # Fetch comments via yt-dlp
            tmp_info = job_dir / "source" / "_comments_fetch.info.json"
            fetch_result = subprocess.run(
                [str(YTDLP), "--skip-download", "--write-comments",
                 "--extractor-args", "youtube:comment_sort=top;max_comments=200,all,10,5",
                 "--write-info-json", "-o", str(job_dir / "source" / "_comments_fetch"),
                 yt_url],
                capture_output=True, text=True, timeout=120
            )

            yt_comments = []
            if tmp_info.exists():
                try:
                    info = json.loads(tmp_info.read_text(errors="replace"))
                    yt_comments = info.get("comments", [])
                except Exception:
                    pass

            if not yt_comments:
                self.send_json({"error": f"No comments found for this video (yt-dlp exit: {fetch_result.returncode})"}); return

            # Run Devika
            try:
                import anthropic as ant
                client = ant.Anthropic(api_key=ORM_API_KEY)
            except Exception as e:
                self.send_json({"error": str(e)}); return

            def r(p): return p.read_text(errors="replace") if p.exists() else ""
            intake_text = r(intake_path)
            title = ""
            for line in intake_text.splitlines():
                if "| Title" in line:
                    title = line.split("|")[2].strip(); break

            video_ctx = f"""## VIDEO CONTEXT
{intake_text}

### Key Quotes
{r(job_dir / 'transcript' / 'key_quotes.md')[:2000]}

### Chapter Markers
{r(job_dir / 'transcript' / 'chapter_markers.md')[:1500]}
"""
            comments_text = "\n".join([
                f"[YouTube] @{c.get('author','anon')} ({c.get('like_count',0)} likes): {c.get('text','')}"
                for c in yt_comments[:150]
            ])

            orm_system = """You are Devika Rao, ORM Intelligence & Reporting Analyst.
You produce structured, actionable ORM reports. Separate facts from interpretation.
Never invent data. If counts are approximate say 'observed trend'. Flag HIGH risk clearly.
Ground all analysis in the specific video content provided."""

            orm_prompt = f"""Generate a full ORM report for these YouTube comments.

{video_ctx}

## COMMENTS ({len(yt_comments)} total):
{comments_text[:7000]}

Produce the full report:

# ORM Report — {title}

## 1. Report Scope
## 2. Executive Summary
## 3. Sentiment Overview
## 4. Platform Breakdown (YouTube Comments)
## 5. Top Repeating Themes
## 6. Key Risks
## 7. High-Intent User Signals
## 8. Content Insights
## 9. Items That Need Response
## 10. Recommended Actions
## 11. Sample Quotes / Evidence
## 12. Final Verdict

Then output ===SUMMARY=== and write orm_summary.md (3-5 bullet takeaways).
Then output ===RISKS=== and write orm_risk_flags.md listing only HIGH and MEDIUM risk items.
Then output ===ACTIONS=== and write orm_action_items.md with immediate, short-term, and content opportunity actions."""

            try:
                import google.generativeai as genai
                genai.configure(api_key=ORM_API_KEY)
                gemini = genai.GenerativeModel(GEMINI_MODEL)
                full = gemini.generate_content(orm_system + "\n\n" + orm_prompt).text
            except Exception as e:
                self.send_json({"error": f"AI generation failed: {e}"}); return

            parts = re.split(r"===SUMMARY===|===RISKS===|===ACTIONS===", full)
            safe_name = "youtube-auto-" + time.strftime("%Y-%m-%d")
            out_dir = job_dir / "orm-reports" / safe_name
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "orm_report.md").write_text(parts[0].strip() if parts else full)
            (out_dir / "orm_summary.md").write_text(parts[1].strip() if len(parts) > 1 else "")
            (out_dir / "orm_risk_flags.md").write_text(parts[2].strip() if len(parts) > 2 else "")
            (out_dir / "orm_action_items.md").write_text(parts[3].strip() if len(parts) > 3 else "")

            self.send_json({"ok": True, "comments_fetched": len(yt_comments), "report_name": safe_name})

        elif self.path == "/api/pipeline/resume":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            job_name = body.get("job", "").strip()
            if not job_name:
                self.send_json({"error": "missing job"}, 400); return
            run_id = str(uuid.uuid4())[:8]
            with RUNS_LOCK:
                RUNS[run_id] = {"events": [], "done": False, "job": job_name, "error": None}
            t = threading.Thread(target=run_resume, args=(run_id, job_name), daemon=True)
            t.start()
            self.send_json({"run_id": run_id})

        elif self.path == "/api/pipeline/start":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            url    = body.get("url", "").strip()
            key    = body.get("api_key", "").strip()

            if not url:
                self.send_json({"error": "missing url"}, 400); return

            run_id = str(uuid.uuid4())[:8]
            with RUNS_LOCK:
                RUNS[run_id] = {"events": [], "done": False, "job": None, "error": None}

            t = threading.Thread(target=run_pipeline, args=(run_id, url, key), daemon=True)
            t.start()

            self.send_json({"run_id": run_id})
        else:
            self.send_response(404); self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self.send_html(HTML)

        elif path == "/api/jobs":
            jobs = sorted([d.name for d in OUTPUTS_DIR.iterdir() if d.is_dir()]) if OUTPUTS_DIR.exists() else []
            self.send_json(jobs)

        elif path.startswith("/api/job/"):
            job_name = urllib.parse.unquote(path[9:])
            job_dir  = OUTPUTS_DIR / job_name
            if not job_dir.exists():
                self.send_json({"error": "not found"}, 404); return

            files = []
            for f in sorted(job_dir.rglob("*")):
                if f.is_file() and not f.name.endswith((".json", ".mp4", ".m4a", ".srt", ".vtt", ".tsv")):
                    rel    = f.relative_to(job_dir)
                    folder = rel.parts[0] if len(rel.parts) > 1 else "root"
                    sz     = f.stat().st_size
                    files.append({"name": f.name, "folder": folder, "size": f"{sz//1024} KB" if sz > 1024 else f"{sz} B"})

            def r(p): return p.read_text(errors="replace") if p.exists() else ""
            s = job_dir
            content = {
                "intake":           r(s/"source"/"intake_summary.md"),
                "transcript_clean": r(s/"transcript"/"transcript_clean.md"),
                "transcript_raw":   r(s/"transcript"/"transcript_raw.md"),
                "key_quotes":       r(s/"transcript"/"key_quotes.md"),
                "chapter_markers":  r(s/"transcript"/"chapter_markers.md"),
                "shorts_plan":      r(s/"shorts"/"shorts_plan.md"),
                "clip_commands":    r(s/"shorts"/"clip_commands.sh"),
                "shorts_csv":       r(s/"shorts"/"shorts_timestamps.csv"),
                "linkedin":         r(s/"social"/"linkedin_post.md"),
                "instagram":        r(s/"social"/"instagram_post.md"),
                "x":                r(s/"social"/"x_post.md"),
                "blog":             r(s/"blog"/"blog_draft.md"),
                "qa":               r(s/"qa"/"final_approval_summary.md"),
            }
            meta = {}
            for line in content["intake"].splitlines():
                if "| Title"  in line: meta["title"]       = line.split("|")[2].strip()
                elif "| Channel" in line: meta["channel"]  = line.split("|")[2].strip()
                elif "| Duration" in line: meta["duration"] = line.split("|")[2].strip()
                elif "| Upload Date" in line: meta["upload_date"] = line.split("|")[2].strip()

            self.send_json({"files": files, "content": content, "meta": meta})

        elif path.startswith("/api/export/"):
            job_name = urllib.parse.unquote(path[12:])
            job_dir  = OUTPUTS_DIR / job_name
            if not job_dir.exists():
                self.send_response(404); self.end_headers(); return

            import zipfile, io
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in sorted(job_dir.rglob("*")):
                    if f.is_file() and not f.name.endswith((".info.json", ".tsv", ".vtt", ".srt", ".json")):
                        zf.write(f, f.relative_to(job_dir))
            buf.seek(0)
            data = buf.read()
            safe = re.sub(r"[^\w\-]", "_", job_name[:40])
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{safe}.zip"')
            self.send_header("Content-Length", len(data))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        elif path == "/api/video":
            file_path = params.get("file", [None])[0]
            if not file_path:
                self.send_response(400); self.end_headers(); return
            self.serve_video(urllib.parse.unquote(file_path))

        elif path.startswith("/api/clips/"):
            job_name = urllib.parse.unquote(path[11:])
            job_dir  = OUTPUTS_DIR / job_name
            if not job_dir.exists():
                self.send_json({"clips": []}, 404); return
            # Find source video
            src = job_dir / "source"
            merged = src / "merged.mp4"
            mp4s   = list(src.glob("*.mp4"))
            source_video = merged if merged.exists() else (mp4s[0] if mp4s else None)
            if not source_video:
                self.send_json({"clips": [], "error": "no source video"}); return
            # Get or cut clips
            clips = cut_clips(job_dir, source_video)
            # Also return main video path
            self.send_json({
                "clips": clips,
                "main_video": f"/api/video?file={urllib.parse.quote(str(source_video))}"
            })

        elif path.startswith("/api/orm/"):
            job_name = urllib.parse.unquote(path[9:])
            job_dir  = OUTPUTS_DIR / job_name
            orm_base = job_dir / "orm-reports"
            if not orm_base.exists():
                self.send_json({"reports": []}); return
            reports = []
            for report_dir in sorted(orm_base.iterdir()):
                if not report_dir.is_dir(): continue
                # Load tabs: report, summary, risks, actions
                tabs = [
                    ("ORM Report",    report_dir / "orm_report.md"),
                    ("Summary",       report_dir / "orm_summary.md"),
                    ("Risk Flags",    report_dir / "orm_risk_flags.md"),
                    ("Action Items",  report_dir / "orm_action_items.md"),
                ]
                tab_data = []
                for tab_name, fpath in tabs:
                    if fpath.exists():
                        tab_data.append({"name": tab_name, "content": fpath.read_text(errors="replace")})
                if tab_data:
                    reports.append({
                        "name":    report_dir.name,
                        "content": tab_data[0]["content"],  # default view
                        "tabs":    tab_data,
                    })
            self.send_json({"reports": reports})

        elif path == "/api/pipeline/progress":
            run_id = params.get("run_id", [None])[0]
            if not run_id or run_id not in RUNS:
                self.send_json({"error": "unknown run_id"}, 404); return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            sent = 0
            try:
                while True:
                    with RUNS_LOCK:
                        run   = RUNS.get(run_id, {})
                        evts  = run.get("events", [])
                        done  = run.get("done", False)

                    while sent < len(evts):
                        ev   = evts[sent]
                        data = f"data: {json.dumps(ev)}\n\n".encode()
                        self.wfile.write(data)
                        self.wfile.flush()
                        sent += 1

                    if done and sent >= len(evts):
                        break
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError):
                pass

        else:
            self.send_response(404); self.end_headers()


if __name__ == "__main__":
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    server = http.server.ThreadingHTTPServer(("localhost", PORT), Handler)
    print(f"""
╔══════════════════════════════════════════╗
║     Ajay Doval — Command Center          ║
║     Dashboard running at:                ║
║     http://localhost:{PORT}                 ║
║                                          ║
║     Press Ctrl+C to stop                 ║
╚══════════════════════════════════════════╝
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
