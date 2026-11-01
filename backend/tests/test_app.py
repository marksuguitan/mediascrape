import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import app  # noqa: E402

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VID}",
        f"https://youtube.com/watch?v={VID}&t=42s",
        f"https://m.youtube.com/watch?v={VID}",
        f"https://music.youtube.com/watch?v={VID}&list=RDAMVM",
        f"https://youtu.be/{VID}?si=abc",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://www.youtube.com/embed/{VID}",
        f"https://www.youtube.com/live/{VID}",
        f"www.youtube.com/watch?v={VID}",
        f"  https://www.youtube.com/watch?v={VID}  ",
    ],
)
def test_accepts_youtube_links(url):
    assert app.extract_video_id(url) == VID


@pytest.mark.parametrize(
    "url",
    [
        "",
        None,
        "https://vimeo.com/123",
        f"https://evil.com/watch?v={VID}",
        f"https://youtube.com.evil.com/watch?v={VID}",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/watch?v=short",
        f"ftp://youtube.com/watch?v={VID}",
        "https://www.youtube.com/@channel",
    ],
)
def test_rejects_other_links(url):
    assert app.extract_video_id(url) is None


def test_canonical_url():
    assert app.canonical_url(VID) == f"https://www.youtube.com/watch?v={VID}"


def test_safe_filename_and_disposition():
    name = app.safe_filename('A/B: "C" ✨ video', "mp4")
    assert name == "A B C ✨ video.mp4"
    disp = app.content_disposition(name)
    assert disp.startswith('attachment; filename="A B C  video.mp4"')
    assert "filename*=UTF-8''A%20B%20C%20%E2%9C%A8%20video.mp4" in disp


class Ctx:
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:mediascrape-api"


def _event(method, path, body=None, query=None):
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": method}},
        "body": json.dumps(body) if body is not None else None,
        "queryStringParameters": query,
    }


def test_download_requires_code(monkeypatch):
    monkeypatch.setattr(app, "ACCESS_CODE", "secret")
    res = app.handler(_event("POST", "/api/download", {"url": f"https://youtu.be/{VID}"}), Ctx())
    assert res["statusCode"] == 401


def test_download_rejects_bad_url(monkeypatch):
    monkeypatch.setattr(app, "ACCESS_CODE", "")
    res = app.handler(_event("POST", "/api/download", {"url": "https://example.com"}), Ctx())
    assert res["statusCode"] == 400


def test_download_queues_job(monkeypatch):
    monkeypatch.setattr(app, "ACCESS_CODE", "secret")
    statuses, invokes = [], []
    monkeypatch.setattr(app, "write_status", lambda job_id, **f: statuses.append((job_id, f)))

    class FakeLambda:
        def invoke(self, **kw):
            invokes.append(kw)

    monkeypatch.setattr(app, "lambda_client", lambda: FakeLambda())
    res = app.handler(
        _event("POST", "/api/download", {"url": f"https://youtu.be/{VID}", "quality": "720", "code": "secret"}),
        Ctx(),
    )
    assert res["statusCode"] == 202
    job_id = json.loads(res["body"])["jobId"]
    assert statuses == [(job_id, {"state": "queued", "videoId": VID, "quality": "720"})]
    payload = json.loads(invokes[0]["Payload"])
    assert invokes[0]["InvocationType"] == "Event"
    assert payload == {"worker": {"jobId": job_id, "videoId": VID, "quality": "720"}}


def test_status_validates_id():
    res = app.handler(_event("GET", "/api/status", query={"id": "../../etc"}), Ctx())
    assert res["statusCode"] == 400


def test_config(monkeypatch):
    monkeypatch.setattr(app, "ACCESS_CODE", "x")
    res = app.handler(_event("GET", "/api/config"), Ctx())
    assert json.loads(res["body"])["codeRequired"] is True
