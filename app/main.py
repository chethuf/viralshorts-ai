
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
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl


# --------------------------------------------------
# PATHS AND APPLICATION
# --------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATIC_DIR = ROOT / "static"
INDEX_FILE = ROOT / "index.html"

DATA.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

app = FastAPI(title="ViralShorts AI")

# Allow GitHub Pages frontend to communicate with Render.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://chethuf.github.io"
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR)),
    name="static"
)


# --------------------------------------------------
# REQUEST MODEL
# --------------------------------------------------

class AnalyzeRequest(BaseModel):
    url: HttpUrl
    max_shorts: int = 20


# --------------------------------------------------
# COMMAND RUNNER
# --------------------------------------------------

def run(cmd):
    process = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if process.returncode != 0:
        raise RuntimeError(
            (process.stderr or process.stdout)[-2500:]
        )

    return process.stdout


# --------------------------------------------------
# JOB STATUS
# --------------------------------------------------

def status(job, **kwargs):
    path = DATA / job / "status.json"

    old = json.loads(path.read_text()) if path.exists() else {}
    old.update(kwargs)

    path.write_text(
        json.dumps(old, ensure_ascii=False, indent=2)
    )


# --------------------------------------------------
# DOWNLOAD SOURCE VIDEO
# --------------------------------------------------

def download_video(url, output):
    run([
        "yt-dlp",
        "--no-playlist",
        "-f",
        "bv*[height<=1080]+ba/b[height<=1080]/best",
        "--merge-output-format",
        "mp4",
        "-o",
        str(output),
        str(url)
    ])

    if output.exists():
        return output

    candidates = list(output.parent.glob("source.*"))

    if candidates:
        return candidates[0]

    raise RuntimeError("Video download did not produce a file.")


# --------------------------------------------------
# TRANSCRIBE VIDEO
# --------------------------------------------------

def transcribe(video):
    from faster_whisper import WhisperModel

    model = WhisperModel(
        os.getenv("WHISPER_MODEL", "base"),
        device="cpu",
        compute_type="int8"
    )

    segments, info = model.transcribe(
        str(video),
        vad_filter=True,
        word_timestamps=False
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


# --------------------------------------------------
# SCORE CLIPS
# --------------------------------------------------

def score_candidate(text, duration):
    text = text.lower()

    hooks = [
        "but", "because", "secret", "never",
        "why", "how", "truth", "mistake",
        "shocking", "finally", "impossible",
        "surprising", "danger", "wrong",
        "best", "worst", "money", "fail",
        "actually"
    ]

    energy = sum(1 for word in hooks if word in text)

    words = len(re.findall(r"\w+", text))

    density = min(words / max(duration, 1), 3.5)

    punctuation = sum(text.count(x) for x in "?!")

    score = 55 + energy * 4
    score += min(punctuation, 4) * 2
    score += density * 3

    return min(99, max(48, int(score)))


# --------------------------------------------------
# FIND SHORT CLIP CANDIDATES
# --------------------------------------------------

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
                item["text"]
                for item in segments[i:j + 1]
            )

            if len(transcript.split()) < 12:
                continue

            score = score_candidate(transcript, duration)

            candidates.append({
                "start": start,
                "end": end,
                "duration": duration,
                "transcript": transcript,
                "score": score
            })

    candidates.sort(
        key=lambda item: item["score"],
        reverse=True
    )

    chosen = []

    for candidate in candidates:
        if all(
            abs(candidate["start"] - item["start"]) > 8
            for item in chosen
        ):
            chosen.append(candidate)

        if len(chosen) >= max_shorts:
            break

    chosen.sort(key=lambda item: item["start"])

    return chosen


# --------------------------------------------------
# SUBTITLE HELPERS
# --------------------------------------------------

def ass_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    remaining = seconds % 60

    return f"{hours}:{minutes:02d}:{remaining:05.2f}"


def safe_ass(text):
    return (
        text.replace("\\", "")
        .replace("{", "")
        .replace("}", "")
        .replace("\n", " ")
    )


# --------------------------------------------------
# RENDER VERTICAL SHORT
# --------------------------------------------------

def render_short(video, jobdir, index, candidate):
    output = jobdir / f"short_{index:02d}.mp4"
    subtitle_file = jobdir / f"short_{index:02d}.ass"

    words = candidate["transcript"].split()

    chunks = [
        " ".join(words[i:i + 6])
        for i in range(0, len(words), 6)
    ]

    if not chunks:
        chunks = ["WATCH THIS"]

    duration = candidate["duration"]

    events = []

    for i, chunk in enumerate(chunks):
        start = duration * i / len(chunks)
        end = duration * (i + 1) / len(chunks)

        events.append(
            f"Dialogue: 0,{ass_time(start)},"
            f"{ass_time(end)},Default,,0,0,0,,"
            f"{safe_ass(chunk)}"
        )

    hook = "WAIT FOR IT..."

    script = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,64,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,1,0,1,4,2,2,80,80,250,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 1,0:00:00.00,0:00:03.50,Default,,0,0,0,,{hook}
{chr(10).join(events)}
"""

    subtitle_file.write_text(
        script,
        encoding="utf-8"
    )

    subtitle_path = str(subtitle_file.resolve()).replace(
        "'", r"\'"
    )

    video_filter = (
        "scale=1080:1920:"
        "force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        f"subtitles='{subtitle_path}'"
    )

    run([
        "ffmpeg",
        "-y",
        "-ss",
        str(candidate["start"]),
        "-i",
        str(video),
        "-t",
        str(duration),
        "-vf",
        video_filter,
        "-r",
        "30",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(output)
    ])

    return output


# --------------------------------------------------
# GENERATE THUMBNAIL
# --------------------------------------------------

def make_thumb(video, jobdir, index, candidate):
    from PIL import Image, ImageDraw, ImageFont

    thumbnail = jobdir / f"thumbnail_{index:02d}.jpg"
    temporary = jobdir / f"frame_{index:02d}.jpg"

    run([
        "ffmpeg",
        "-y",
        "-ss",
        str(candidate["start"] + min(1.5, candidate["duration"] / 4)),
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280",
        str(temporary)
    ])

    image = Image.open(temporary).convert("RGB")

    overlay = Image.new(
        "RGBA",
        image.size,
        (0, 0, 0, 0)
    )

    draw = ImageDraw.Draw(overlay)

    draw.rectangle(
        (0, 820, 720, 1280),
        fill=(0, 0, 0, 170)
    )

    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            58
        )
    except:
        font = ImageFont.load_default()

    words = candidate["transcript"].split()

    title = " ".join(words[:7]).upper()

    if not title:
        title = "YOU WON'T BELIEVE THIS"

    lines = textwrap.wrap(title, 18)[:3]

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

    result = Image.alpha_composite(
        image.convert("RGBA"),
        overlay
    ).convert("RGB")

    result.save(thumbnail, quality=90)

    temporary.unlink(missing_ok=True)

    return thumbnail


# --------------------------------------------------
# PROCESS JOB
# --------------------------------------------------

def process_job(job, url, max_shorts):
    jobdir = DATA / job

    try:
        status(
            job,
            state="processing",
            progress=5,
            message="Retrieving source video"
        )

        video = download_video(
            url,
            jobdir / "source.mp4"
        )

        status(
            job,
            progress=20,
            message="Transcribing full video"
        )

        segments = transcribe(video)

        if not segments:
            raise RuntimeError(
                "No speech detected. This prototype relies on speech transcription."
            )

        status(
            job,
            progress=40,
            message="Finding and ranking candidate moments"
        )

        candidates = make_candidates(
            segments,
            max_shorts
        )

        results = []

        for index, candidate in enumerate(candidates, 1):
            status(
                job,
                progress=40 + int(
                    50 * index / max(len(candidates), 1)
                ),
                message=f"Editing Short {index} of {len(candidates)}"
            )

            render_short(
                video,
                jobdir,
                index,
                candidate
            )

            make_thumb(
                video,
                jobdir,
                index,
                candidate
            )

            excerpt = " ".join(
                candidate["transcript"].split()[:12]
            )

            title = (
                excerpt[:75].strip().rstrip(".,!?")
                or f"Interesting moment #{index}"
            )

            results.append({
                "id": index,
                "score": candidate["score"],
                "duration": round(candidate["duration"], 1),
                "start": round(candidate["start"], 1),
                "end": round(candidate["end"], 1),
                "video": f"/files/{job}/short_{index:02d}.mp4",
                "thumbnail": f"/files/{job}/thumbnail_{index:02d}.jpg",
                "title": title + " #shorts",
                "description": excerpt + ". Watch the full video for more. #shorts",
                "tags": "shorts, viral moments, highlights, interesting clips",
                "hook": "WAIT FOR IT...",
                "transcript": candidate["transcript"]
            })

        results_file = jobdir / "results.json"

        results_file.write_text(
            json.dumps(results, ensure_ascii=False, indent=2)
        )

        zip_file = jobdir / "all_shorts.zip"

        with zipfile.ZipFile(
            zip_file,
            "w",
            zipfile.ZIP_DEFLATED
        ) as archive:

            for path in jobdir.glob("short_*.*"):
                archive.write(path, path.name)

            for path in jobdir.glob("thumbnail_*.*"):
                archive.write(path, path.name)

            archive.write(
                results_file,
                "shorts_metadata.json"
            )

        status(
            job,
            state="complete",
            progress=100,
            message=f"Finished {len(results)} Shorts",
            count=len(results)
        )

    except Exception as error:
        status(
            job,
            state="error",
            progress=100,
            message=str(error)
        )


# --------------------------------------------------
# HOMEPAGE
# --------------------------------------------------

@app.api_route(
    "/",
    methods=["GET", "HEAD"],
    response_class=HTMLResponse
)
def home():
    if not INDEX_FILE.exists():
        raise HTTPException(
            status_code=500,
            detail="Frontend index.html is missing from the repository root."
        )

    return HTMLResponse(
        INDEX_FILE.read_text(encoding="utf-8")
    )


# --------------------------------------------------
# START ANALYSIS
# --------------------------------------------------

@app.post("/api/analyze")
def analyze(
    request: AnalyzeRequest,
    background_tasks: BackgroundTasks
):
    job = uuid.uuid4().hex[:10]

    jobdir = DATA / job
    jobdir.mkdir(parents=True)

    status(
        job,
        state="queued",
        progress=0,
        message="Job queued"
    )

    background_tasks.add_task(
        process_job,
        job,
        str(request.url),
        max(1, min(request.max_shorts, 40))
    )

    return {"job": job}


# --------------------------------------------------
# JOB STATUS
# --------------------------------------------------

@app.get("/api/status/{job}")
def get_status(job: str):
    path = DATA / job / "status.json"

    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="Job not found"
        )

    return json.loads(path.read_text())


# --------------------------------------------------
# RESULTS
# --------------------------------------------------

@app.get("/api/results/{job}")
def get_results(job: str):
    path = DATA / job / "results.json"

    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="Results not ready"
        )

    return json.loads(path.read_text())


# --------------------------------------------------
# DOWNLOAD FILES
# --------------------------------------------------

@app.get("/files/{job}/{name}")
def get_file(job: str, name: str):
    jobdir = (DATA / job).resolve()
    path = (jobdir / name).resolve()

    if (
        not str(path).startswith(str(jobdir) + os.sep)
        or not path.is_file()
    ):
        raise HTTPException(
            status_code=404,
            detail="File not found"
        )

    return FileResponse(path)
