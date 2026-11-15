
import os
import re
import json
import uuid
import zipfile
import subprocess
import textwrap
from pathlib import Path

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

INDEX_FILE = ROOT / "index.html"

app = FastAPI(title="ViralShorts AI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AnalyzeRequest(BaseModel):
    url: HttpUrl
    max_shorts: int = 20


def run(cmd):
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout)[-2500:])
    return p.stdout


def status(job, **kwargs):
    path = DATA / job / "status.json"
    old = json.loads(path.read_text()) if path.exists() else {}
    old.update(kwargs)
    path.write_text(json.dumps(old, ensure_ascii=False))


def download_video(url, output):
    run([
        "yt-dlp",
        "--no-playlist",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/best",
        "--merge-output-format", "mp4",
        "-o", str(output),
        str(url)
    ])

    if output.exists():
        return output

    candidates = list(output.parent.glob("source.*"))
    if candidates:
        return candidates[0]

    raise RuntimeError("Video download failed.")


def transcribe(video):
    from faster_whisper import WhisperModel

    model = WhisperModel(
        os.getenv("WHISPER_MODEL", "base"),
        device="cpu",
        compute_type="int8"
    )

    segments, info = model.transcribe(
        str(video),
        vad_filter=True
    )

    rows = []
    for segment in segments:
        text = (segment.text or "").strip()
        if text:
            rows.append({
                "start": float(segment.start),
                "end": float(segment.end),
                "text": text
            })

    return rows


def score_candidate(text, duration):
    t = text.lower()

    hooks = [
        "but", "because", "secret", "never", "why",
        "how", "truth", "mistake", "shocking",
        "finally", "impossible", "surprising",
        "danger", "wrong", "best", "worst",
        "money", "fail", "actually"
    ]

    energy = sum(1 for word in hooks if word in t)
    words = len(re.findall(r"\w+", t))
    density = min(words / max(duration, 1), 3.5)
    punctuation = sum(t.count(x) for x in "?!")

    return min(
        99,
        max(48, int(55 + energy * 4 + min(punctuation, 4) * 2 + density * 3))
    )


def make_candidates(segments, max_shorts):
    candidates = []

    for i in range(len(segments)):
        start = segments[i]["start"]

        for j in range(i, min(len(segments), i + 14)):
            end = segments[j]["end"]
            duration = end - start

            if duration > 45:
                break

            if duration < 15:
                continue

            transcript = " ".join(
                item["text"] for item in segments[i:j + 1]
            )

            if len(transcript.split()) < 12:
                continue

            candidates.append({
                "start": start,
                "end": end,
                "duration": duration,
                "transcript": transcript,
                "score": score_candidate(transcript, duration)
            })

    candidates.sort(key=lambda x: x["score"], reverse=True)

    chosen = []
    for candidate in candidates:
        if all(abs(candidate["start"] - item["start"]) > 8 for item in chosen):
            chosen.append(candidate)

        if len(chosen) >= max_shorts:
            break

    chosen.sort(key=lambda x: x["start"])
    return chosen


def ass_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def safe_ass(text):
    return (
        text.replace("\\", "")
        .replace("{", "")
        .replace("}", "")
        .replace("\n", " ")
    )


def render_short(video, jobdir, idx, candidate):
    output = jobdir / f"short_{idx:02d}.mp4"
    ass = jobdir / f"short_{idx:02d}.ass"

    words = candidate["transcript"].split()
    chunks = [
        " ".join(words[k:k + 6])
        for k in range(0, len(words), 6)
    ] or ["WATCH THIS"]

    duration = candidate["duration"]
    events = []

    for k, chunk in enumerate(chunks):
        start = duration * k / len(chunks)
        end = duration * (k + 1) / len(chunks)

        events.append(
            f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{safe_ass(chunk)}"
        )

    script = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,64,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,1,0,1,4,2,2,80,80,250,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 1,0:00:00.00,0:00:03.50,Default,,0,0,0,,WAIT FOR IT...
{chr(10).join(events)}
"""

    ass.write_text(script, encoding="utf-8")

    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        f"subtitles='{ass.as_posix()}'"
    )

    run([
        "ffmpeg", "-y",
        "-ss", str(candidate["start"]),
        "-i", str(video),
        "-t", str(candidate["duration"]),
        "-vf", vf,
        "-r", "30",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output)
    ])

    return output


def make_thumb(video, jobdir, idx, candidate):
    from PIL import Image, ImageDraw, ImageFont

    jpg = jobdir / f"thumbnail_{idx:02d}.jpg"
    tmp = jobdir / f"frame_{idx:02d}.jpg"

    run([
        "ffmpeg", "-y",
        "-ss", str(candidate["start"] + min(1.5, candidate["duration"] / 4)),
        "-i", str(video),
        "-frames:v", "1",
        "-vf", "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280",
        str(tmp)
    ])

    im = Image.open(tmp).convert("RGB")
    overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle((0, 820, 720, 1280), fill=(0, 0, 0, 170))

    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            58
        )
    except Exception:
        font = ImageFont.load_default()

    title = " ".join(candidate["transcript"].split()[:7]).upper()
    lines = textwrap.wrap(title or "WATCH THIS", 18)[:3]

    y = 900
    for line in lines:
        draw.text(
            (42, y),
            line,
            font=font,
            fill=(255, 255, 255, 255),
            stroke_width=2,
            stroke_fill=(0, 0, 0, 255)
        )
        y += 76

    Image.alpha_composite(
        im.convert("RGBA"), overlay
    ).convert("RGB").save(jpg, quality=90)

    tmp.unlink(missing_ok=True)
    return jpg


def process_job(job, url, max_shorts):
    jobdir = DATA / job

    try:
        status(job, state="processing", progress=5, message="Downloading video")

        video = download_video(url, jobdir / "source.mp4")

        status(job, progress=20, message="Transcribing video")
        segments = transcribe(video)

        if not segments:
            raise RuntimeError("No speech detected in this video.")

        status(job, progress=40, message="Finding interesting moments")

        candidates = make_candidates(segments, max_shorts)
        results = []

        for i, candidate in enumerate(candidates, 1):
            status(
                job,
                progress=40 + int(50 * i / max(len(candidates), 1)),
                message=f"Creating Short {i} of {len(candidates)}"
            )

            render_short(video, jobdir, i, candidate)
            make_thumb(video, jobdir, i, candidate)

            excerpt = " ".join(candidate["transcript"].split()[:12])
            title = excerpt[:75].strip().rstrip(".,!?")

            results.append({
                "id": i,
                "score": candidate["score"],
                "duration": round(candidate["duration"], 1),
                "start": round(candidate["start"], 1),
                "end": round(candidate["end"], 1),
                "video": f"/files/{job}/short_{i:02d}.mp4",
                "thumbnail": f"/files/{job}/thumbnail_{i:02d}.jpg",
                "title": (title or f"Interesting moment {i}") + " #shorts",
                "description": excerpt + ". Watch the full video for more. #shorts",
                "tags": "shorts, viral moments, highlights, interesting clips",
                "hook": "WAIT FOR IT..."
            })

        (jobdir / "results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2)
        )

        with zipfile.ZipFile(
            jobdir / "all_shorts.zip",
            "w",
            zipfile.ZIP_DEFLATED
        ) as archive:
            for path in jobdir.glob("short_*.mp4"):
                archive.write(path, path.name)

            for path in jobdir.glob("thumbnail_*.jpg"):
                archive.write(path, path.name)

            archive.write(jobdir / "results.json", "shorts_metadata.json")

        status(
            job,
            state="complete",
            progress=100,
            message=f"Finished {len(results)} Shorts"
        )

    except Exception as error:
        status(
            job,
            state="error",
            progress=100,
            message=str(error)
        )


@app.get("/", response_class=HTMLResponse)
def home():
    if not INDEX_FILE.exists():
        raise HTTPException(500, "index.html was not found in the repository root.")
    return INDEX_FILE.read_text(encoding="utf-8")


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest, bg: BackgroundTasks):
    job = uuid.uuid4().hex[:10]
    jobdir = DATA / job
    jobdir.mkdir(parents=True)

    status(job, state="queued", progress=0, message="Job queued")

    bg.add_task(
        process_job,
        job,
        str(req.url),
        max(1, min(req.max_shorts, 40))
    )

    return {"job": job}


@app.get("/api/status/{job}")
def get_status(job: str):
    path = DATA / job / "status.json"

    if not path.exists():
        raise HTTPException(404, "Job not found")

    return json.loads(path.read_text())


@app.get("/api/results/{job}")
def get_results(job: str):
    path = DATA / job / "results.json"

    if not path.exists():
        raise HTTPException(404, "Results not ready")

    return json.loads(path.read_text())


@app.get("/files/{job}/{name}")
def get_file(job: str, name: str):
    folder = (DATA / job).resolve()
    path = (folder / name).resolve()

    if not str(path).startswith(str(folder)) or not path.exists():
        raise HTTPException(404, "File not found")

    return FileResponse(path)
