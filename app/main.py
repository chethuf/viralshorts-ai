import os, re, json, uuid, shutil, zipfile, subprocess, textwrap
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl

ROOT=Path(__file__).resolve().parent.parent
DATA=ROOT/"data"
DATA.mkdir(exist_ok=True)
app=FastAPI(title="ViralShorts AI")
app.mount("/static", StaticFiles(directory=str(ROOT/"app"/"static")), name="static")

class AnalyzeRequest(BaseModel):
    url: HttpUrl
    max_shorts: int = 20

def run(cmd):
    p=subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout)[-2500:])
    return p.stdout

def status(job, **kwargs):
    p=DATA/job/"status.json"
    old=json.loads(p.read_text()) if p.exists() else {}
    old.update(kwargs)
    p.write_text(json.dumps(old,ensure_ascii=False,indent=2))

def download_video(url, out):
    # yt-dlp only retrieves media that is accessible under the user's permissions
    run(["yt-dlp","--no-playlist","-f","bv*[height<=1080]+ba/b[height<=1080]/best",
         "--merge-output-format","mp4","-o",str(out),str(url)])
    if not out.exists():
        candidates=list(out.parent.glob("source.*"))
        if candidates: return candidates[0]
        raise RuntimeError("Download did not produce a video file.")

def transcribe(video):
    from faster_whisper import WhisperModel
    model=WhisperModel(os.getenv("WHISPER_MODEL","base"), device="cpu", compute_type="int8")
    segments, info=model.transcribe(str(video), vad_filter=True, word_timestamps=False)
    rows=[]
    for s in segments:
        txt=(s.text or "").strip()
        if txt: rows.append({"start":float(s.start),"end":float(s.end),"text":txt})
    return rows

def score_candidate(text, duration):
    t=text.lower()
    hooks=["but","because","secret","never","why","how","truth","mistake","shocking","finally",
           "impossible","surprising","danger","wrong","best","worst","money","fail","actually"]
    energy=sum(1 for w in hooks if w in t)
    words=len(re.findall(r"\w+",t))
    density=min(words/max(duration,1),3.5)
    punctuation=sum(t.count(x) for x in "?!")
    return min(99, max(48, int(55 + energy*4 + min(punctuation,4)*2 + density*3)))

def make_candidates(segments, max_shorts):
    # Generate overlapping transcript windows, keeping clips in the 15–45 second range.
    candidates=[]
    n=len(segments)
    for i in range(n):
        start=segments[i]["start"]
        for j in range(i,min(n,i+14)):
            end=segments[j]["end"]
            dur=end-start
            if dur>45: break
            if dur<15: continue
            text=" ".join(x["text"] for x in segments[i:j+1])
            if len(text.split())<12: continue
            score=score_candidate(text,dur)
            candidates.append({"start":start,"end":end,"duration":dur,"transcript":text,"score":score})
    candidates.sort(key=lambda x:x["score"],reverse=True)
    # De-duplicate heavily overlapping candidates.
    chosen=[]
    for c in candidates:
        if all(abs(c["start"]-x["start"])>8 for x in chosen):
            chosen.append(c)
        if len(chosen)>=max_shorts: break
    chosen.sort(key=lambda x:x["start"])
    return chosen

def ass_time(seconds):
    h=int(seconds//3600); m=int((seconds%3600)//60); s=seconds%60
    return f"{h}:{m:02d}:{s:05.2f}"

def safe_ass(s):
    return s.replace("\\","").replace("{","").replace("}","").replace("\n"," ")

def render_short(video, jobdir, idx, c):
    out=jobdir/f"short_{idx:02d}.mp4"
    ass=jobdir/f"short_{idx:02d}.ass"
    # Split transcript into readable caption chunks, displayed across the clip.
    words=c["transcript"].split()
    chunks=[" ".join(words[k:k+6]) for k in range(0,len(words),6)] or ["WATCH THIS"]
    dur=c["duration"]
    events=[]
    for k,chunk in enumerate(chunks):
        st=dur*k/len(chunks); en=dur*(k+1)/len(chunks)
        events.append(f"Dialogue: 0,{ass_time(st)},{ass_time(en)},Default,,0,0,0,,{safe_ass(chunk)}")
    hook="WAIT FOR IT..."
    script=f"""[Script Info]
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
    ass.write_text(script,encoding="utf-8")
    vf=f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,subtitles='{ass.as_posix()}'"
    run(["ffmpeg","-y","-ss",str(c["start"]),"-i",str(video),"-t",str(c["duration"]),
         "-vf",vf,"-r","30","-c:v","libx264","-preset","veryfast","-crf","23",
         "-c:a","aac","-b:a","128k","-movflags","+faststart",str(out)])
    return out

def make_thumb(video, jobdir, idx, c):
    from PIL import Image, ImageDraw, ImageFont
    jpg=jobdir/f"thumbnail_{idx:02d}.jpg"
    tmp=jobdir/f"frame_{idx:02d}.jpg"
    run(["ffmpeg","-y","-ss",str(c["start"]+min(1.5,c["duration"]/4)),"-i",str(video),
         "-frames:v","1","-vf","scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280",str(tmp)])
    im=Image.open(tmp).convert("RGB")
    overlay=Image.new("RGBA",im.size,(0,0,0,0)); d=ImageDraw.Draw(overlay)
    d.rectangle((0,820,720,1280),fill=(0,0,0,170))
    try: font=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",58)
    except: font=ImageFont.load_default()
    words=c["transcript"].split()
    title=" ".join(words[:7]).upper() or "YOU WON'T BELIEVE THIS"
    lines=textwrap.wrap(title,18)[:3]
    y=900
    for line in lines:
        d.text((42,y),line,font=font,fill=(255,255,255,255),stroke_width=2,stroke_fill=(0,0,0,255)); y+=76
    Image.alpha_composite(im.convert("RGBA"),overlay).convert("RGB").save(jpg,quality=90)
    tmp.unlink(missing_ok=True)
    return jpg

def process_job(job,url,max_shorts):
    jobdir=DATA/job
    try:
        status(job,state="processing",progress=5,message="Retrieving source video")
        video=download_video(url,jobdir/"source.mp4")
        status(job,progress=20,message="Transcribing full video")
        segments=transcribe(video)
        if not segments: raise RuntimeError("No speech detected. This prototype currently relies on speech transcription.")
        status(job,progress=40,message="Finding and ranking candidate moments")
        candidates=make_candidates(segments,max_shorts)
        results=[]
        for i,c in enumerate(candidates,1):
            status(job,progress=40+int(50*i/max(len(candidates),1)),message=f"Editing Short {i} of {len(candidates)}")
            mp4=render_short(video,jobdir,i,c)
            thumb=make_thumb(video,jobdir,i,c)
            excerpt=" ".join(c["transcript"].split()[:12])
            title=(excerpt[:75].strip().rstrip(".,!?") or f"Interesting moment #{i}")
            results.append({
                "id":i,"score":c["score"],"duration":round(c["duration"],1),
                "start":round(c["start"],1),"end":round(c["end"],1),
                "video":f"/files/{job}/short_{i:02d}.mp4",
                "thumbnail":f"/files/{job}/thumbnail_{i:02d}.jpg",
                "title":title+" #shorts",
                "description":excerpt+". Watch the full video for more. #shorts",
                "tags":", ".join(["shorts","viral moments","highlights","interesting clips"]),
                "hook":"WAIT FOR IT...",
                "transcript":c["transcript"]
            })
        (jobdir/"results.json").write_text(json.dumps(results,ensure_ascii=False,indent=2))
        with zipfile.ZipFile(jobdir/"all_shorts.zip","w",zipfile.ZIP_DEFLATED) as z:
            for p in jobdir.glob("short_*.*"): z.write(p,p.name)
            for p in jobdir.glob("thumbnail_*.*"): z.write(p,p.name)
            z.write(jobdir/"results.json","shorts_metadata.json")
        status(job,state="complete",progress=100,message=f"Finished {len(results)} Shorts",count=len(results))
    except Exception as e:
        status(job,state="error",progress=100,message=str(e))

@app.get("/",response_class=HTMLResponse)
def home():
    return (ROOT/"app"/"static"/"index.html").read_text(encoding="utf-8")

@app.post("/api/analyze")
def analyze(req:AnalyzeRequest, bg:BackgroundTasks):
    job=uuid.uuid4().hex[:10]
    jobdir=DATA/job; jobdir.mkdir(parents=True)
    status(job,state="queued",progress=0,message="Job queued")
    bg.add_task(process_job,job,str(req.url),max(1,min(req.max_shorts,40)))
    return {"job":job}

@app.get("/api/status/{job}")
def get_status(job:str):
    p=DATA/job/"status.json"
    if not p.exists(): raise HTTPException(404,"Job not found")
    return json.loads(p.read_text())

@app.get("/api/results/{job}")
def get_results(job:str):
    p=DATA/job/"results.json"
    if not p.exists(): raise HTTPException(404,"Results not ready")
    return json.loads(p.read_text())

@app.get("/files/{job}/{name}")
def get_file(job:str,name:str):
    p=(DATA/job/name).resolve()
    if not str(p).startswith(str((DATA/job).resolve())) or not p.exists(): raise HTTPException(404,"File not found")
    return FileResponse(p)
