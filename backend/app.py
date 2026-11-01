"""mediascrape API + worker, a single Lambda function.

Two ways in:

* HTTP (Lambda function URL, reached through CloudFront at /api/*)
    POST /api/download   {"url": "...", "quality": "1080|720|480|audio", "code": "..."}
                         -> {"jobId": "..."}; the work runs in an async self-invoke
    GET  /api/status?id= -> the job's status document

* Async worker invocation: {"worker": {...job...}} runs yt-dlp, uploads the
  result to the downloads bucket under files/<jobId>/ (served by CloudFront at
  /files/*) and records progress in jobs/<jobId>.json.

Both prefixes expire after a day through an S3 lifecycle rule.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import time
import urllib.parse
import uuid

import boto3

BUCKET = os.environ.get("DOWNLOADS_BUCKET", "")
ACCESS_CODE = os.environ.get("ACCESS_CODE", "")
MAX_DURATION_SECONDS = int(os.environ.get("MAX_DURATION_MINUTES", "120")) * 60
MAX_FILESIZE_MB = int(os.environ.get("MAX_FILESIZE_MB", "1500"))
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "")
COOKIES_KEY = "config/cookies.txt"

QUALITIES = {
    "1080": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b[height<=1080]/b",
    "720": "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/bv*[height<=720]+ba/b[height<=720]/b",
    "480": "bv*[height<=480][ext=mp4]+ba[ext=m4a]/b[height<=480][ext=mp4]/bv*[height<=480]+ba/b[height<=480]/b",
    "audio": "ba[ext=m4a]/ba",
}

_YT_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
    "youtu.be",
}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_JOB_RE = re.compile(r"^[0-9a-f]{32}$")

_s3 = None
_lambda = None


def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def lambda_client():
    global _lambda
    if _lambda is None:
        _lambda = boto3.client("lambda")
    return _lambda


# --------------------------------------------------------------------------- #
# URL handling
# --------------------------------------------------------------------------- #


def extract_video_id(url: str) -> str | None:
    """Return the 11-character YouTube video id for a link, or None.

    Only single-video links on YouTube hosts are accepted; the worker then
    downloads a canonical watch URL rebuilt from the id, so playlists and
    other sites can never be reached through this endpoint.
    """
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or len(url) > 2048:
        return None
    if "://" not in url:
        url = "https://" + url
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if host not in _YT_HOSTS:
        return None

    path_parts = [p for p in parsed.path.split("/") if p]
    candidate = None
    if host == "youtu.be":
        candidate = path_parts[0] if path_parts else None
    elif path_parts[:1] == ["watch"]:
        candidate = urllib.parse.parse_qs(parsed.query).get("v", [None])[0]
    elif len(path_parts) >= 2 and path_parts[0] in ("shorts", "embed", "live", "v", "e"):
        candidate = path_parts[1]

    if candidate and _ID_RE.match(candidate):
        return candidate
    return None


def canonical_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def safe_filename(title: str, ext: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", title or "video")
    name = re.sub(r"\s+", " ", name).strip(" .") or "video"
    return f"{name[:120]}.{ext}"


def content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode() or "download"
    ascii_name = ascii_name.replace('"', "")
    quoted = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


# --------------------------------------------------------------------------- #
# Job status
# --------------------------------------------------------------------------- #


def _status_key(job_id: str) -> str:
    return f"jobs/{job_id}.json"


def write_status(job_id: str, **fields) -> None:
    doc = {"jobId": job_id, "updatedAt": int(time.time()), **fields}
    s3().put_object(
        Bucket=BUCKET,
        Key=_status_key(job_id),
        Body=json.dumps(doc).encode(),
        ContentType="application/json",
    )


def read_status(job_id: str) -> dict | None:
    try:
        obj = s3().get_object(Bucket=BUCKET, Key=_status_key(job_id))
    except s3().exceptions.NoSuchKey:
        return None
    return json.loads(obj["Body"].read())


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body),
    }


def _parse_body(event: dict) -> dict:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8", "replace")
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _code_ok(supplied) -> bool:
    if not ACCESS_CODE:
        return True
    return isinstance(supplied, str) and hmac.compare_digest(supplied, ACCESS_CODE)


def handle_http(event: dict, context) -> dict:
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "GET").upper()
    path = event.get("rawPath", "/")

    if path.endswith("/config") and method == "GET":
        return _response(200, {"codeRequired": bool(ACCESS_CODE), "qualities": list(QUALITIES)})

    if path.endswith("/download") and method == "POST":
        body = _parse_body(event)
        if not _code_ok(body.get("code")):
            return _response(401, {"error": "Wrong access code."})
        video_id = extract_video_id(body.get("url", ""))
        if not video_id:
            return _response(400, {"error": "That doesn't look like a YouTube video link."})
        quality = str(body.get("quality") or "1080")
        if quality not in QUALITIES:
            return _response(400, {"error": "Unknown quality."})

        job_id = uuid.uuid4().hex
        write_status(job_id, state="queued", videoId=video_id, quality=quality)
        lambda_client().invoke(
            FunctionName=context.invoked_function_arn,
            InvocationType="Event",
            Payload=json.dumps(
                {"worker": {"jobId": job_id, "videoId": video_id, "quality": quality}}
            ).encode(),
        )
        return _response(202, {"jobId": job_id})

    if path.endswith("/status") and method == "GET":
        params = event.get("queryStringParameters") or {}
        job_id = params.get("id", "")
        if not _JOB_RE.match(job_id):
            return _response(400, {"error": "Bad job id."})
        status = read_status(job_id)
        if status is None:
            return _response(404, {"error": "Job not found (downloads expire after a day)."})
        return _response(200, status)

    return _response(404, {"error": "Not found."})


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #


def _friendly_error(message: str) -> str:
    lowered = message.lower()
    if "sign in to confirm" in lowered or "not a bot" in lowered:
        return (
            "YouTube refused the request from AWS (bot check). Add a cookies file "
            "or a proxy, see the README."
        )
    if "private video" in lowered:
        return "This video is private."
    if ("age" in lowered and "restricted" in lowered) or "confirm your age" in lowered:
        return "This video is age-restricted; it needs a cookies file, see the README."
    if "too long" in lowered:
        return message
    if "larger than max-filesize" in lowered or "file is larger" in lowered:
        return f"The file is over the {MAX_FILESIZE_MB} MB limit."
    if "video unavailable" in lowered:
        return "This video is unavailable."
    return message.replace("ERROR: ", "")[:400]


def _load_cookies(workdir: str) -> str | None:
    path = os.path.join(workdir, "cookies.txt")
    try:
        s3().download_file(BUCKET, COOKIES_KEY, path)
    except Exception:  # noqa: BLE001 - no cookies configured is normal
        return None
    return path


def run_worker(job: dict) -> dict:
    import yt_dlp  # imported lazily: the HTTP path stays fast on cold start
    from yt_dlp.utils import DownloadError

    job_id = job["jobId"]
    video_id = job["videoId"]
    quality = job.get("quality", "1080")
    workdir = f"/tmp/job-{job_id}"
    os.makedirs(workdir, exist_ok=True)

    last_write = [0.0]
    skip_reason = []

    def progress(d):
        if d.get("status") != "downloading":
            return
        now = time.time()
        if now - last_write[0] < 3:
            return
        last_write[0] = now
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or 0
        pct = round(done * 100 / total, 1) if total else None
        write_status(job_id, state="downloading", videoId=video_id, quality=quality, percent=pct)

    def duration_filter(info, *, incomplete):
        duration = info.get("duration")
        if duration and duration > MAX_DURATION_SECONDS:
            reason = f"Video is too long (limit is {MAX_DURATION_SECONDS // 60} minutes)."
            skip_reason.append(reason)
            return reason
        return None

    opts = {
        "format": QUALITIES[quality],
        "outtmpl": os.path.join(workdir, "media.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "cachedir": "/tmp/yt-dlp-cache",
        "ffmpeg_location": os.environ.get("FFMPEG_LOCATION", "/opt/bin"),
        "max_filesize": MAX_FILESIZE_MB * 1024 * 1024,
        "match_filter": duration_filter,
        "progress_hooks": [progress],
        "retries": 3,
    }
    if quality != "audio":
        opts["merge_output_format"] = "mp4"
    if YTDLP_PROXY:
        opts["proxy"] = YTDLP_PROXY
    cookies = _load_cookies(workdir)
    if cookies:
        opts["cookiefile"] = cookies

    write_status(job_id, state="starting", videoId=video_id, quality=quality)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(canonical_url(video_id), download=True)
        files = [f for f in os.listdir(workdir) if f.startswith("media.") and not f.endswith(".part")]
        if not files:
            raise DownloadError(
                skip_reason[0]
                if skip_reason
                else f"Nothing was downloaded; the file may be over the {MAX_FILESIZE_MB} MB limit."
            )
        local = os.path.join(workdir, files[0])
        ext = files[0].rsplit(".", 1)[-1]
        title = (info or {}).get("title") or video_id
        filename = safe_filename(title, ext)

        write_status(job_id, state="uploading", videoId=video_id, quality=quality, title=title)
        # The key stays plain ASCII; the browser gets the real title from
        # Content-Disposition.
        key = f"files/{job_id}/{video_id}.{ext}"
        s3().upload_file(
            local,
            BUCKET,
            key,
            ExtraArgs={
                "ContentType": "audio/mp4" if ext == "m4a" else f"video/{ext}",
                "ContentDisposition": content_disposition(filename),
            },
        )
        size = os.path.getsize(local)
        result = dict(
            state="done",
            videoId=video_id,
            quality=quality,
            title=title,
            filename=filename,
            size=size,
            file=f"/{key}",
        )
        write_status(job_id, **result)
        return result
    except Exception as exc:  # noqa: BLE001 - every failure must reach the user
        message = _friendly_error(str(exc))
        write_status(job_id, state="error", videoId=video_id, quality=quality, error=message)
        return {"state": "error", "error": message}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def handler(event, context):
    if isinstance(event, dict) and "worker" in event:
        return run_worker(event["worker"])
    return handle_http(event, context)
