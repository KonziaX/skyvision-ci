"""Verify Windows FFmpeg paths, upload cleanup, EOF, and request cancellation."""
import argparse
import ctypes
import io
import json
import os
import pathlib
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from ctypes import wintypes

from PIL import Image


def ffmpeg_children(parent_pid):
    if os.name != "nt":
        return []
    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    entry = Entry()
    entry.dwSize = ctypes.sizeof(entry)
    result = []
    try:
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32ParentProcessID == parent_pid and entry.szExeFile.lower() == "ffmpeg.exe":
                result.append(entry.th32ProcessID)
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--models-dir", required=True)
    parser.add_argument("--web-dir", required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--runtime-dir")
    parser.add_argument("--port", type=int, default=8903)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = pathlib.Path(args.output).resolve()
    work = output.parent / ("media-smoke-" + uuid.uuid4().hex[:10])
    work.mkdir(parents=True)
    fixture = work / "回归样例 & source.mp4"
    ffmpeg = args.ffmpeg
    result = {"backend": "cpu" if args.cpu else "gpu", "tests": {}, "passed": False}
    # No camera access: the source is a two-second generated video.
    subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc2=size=640x360:rate=30:duration=2", "-an", "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(fixture)], check=True, timeout=30,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    # Validate a time-based two-second trim against BOTH input rates.
    warmup = []
    for fps in (30, 60):
        filter_text = ("drawbox=x=0:y=0:w=iw:h=ih:color=red:t=fill:enable='gte(t,2)',"
                       "setpts=PTS-STARTPTS,trim=start=2,setpts=PTS-STARTPTS,fps=5")
        captured = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=lime:s=64x64:r={fps}:d=3", "-vf", filter_text,
            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
            check=True, capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        pixel = Image.open(io.BytesIO(captured)).convert("RGB").getpixel((32, 32))
        assert pixel[0] > 200 and pixel[1] < 80, (fps, pixel)
        warmup.append({"input_fps": fps, "trim_seconds": 2, "first_pixel": pixel})
    result["tests"]["time_warmup"] = warmup
    root = "http://127.0.0.1:" + str(args.port)
    log_path = work / "server.log"
    log = log_path.open("wb")
    env = os.environ.copy()
    if args.runtime_dir:
        env["PATH"] = str(pathlib.Path(args.runtime_dir).resolve()) + os.pathsep + env["PATH"]
    env["FFMPEG"] = ffmpeg
    env["MOSS_XCTX"] = "4096"
    process = subprocess.Popen([str(pathlib.Path(args.binary).resolve()), "--models-dir",
        str(pathlib.Path(args.models_dir).resolve()), "--web-dir", str(pathlib.Path(args.web_dir).resolve()),
        "--cpu" if args.cpu else "--gpu", "--n-ctx", "4096", "--threads", "8", "--serve", str(args.port)],
        cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    active = ""
    api = ""

    def call(path, params=None, body=None, timeout=30):
        url = root + path + ("?" + urllib.parse.urlencode(params) if params else "")
        with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=timeout) as response:
            return response.read()

    def obj(path, params=None, body=None, timeout=30):
        return json.loads(call(path, params, body, timeout))

    try:
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None:
                raise RuntimeError("Server exited")
            try:
                models = obj("/api/models", timeout=2)
                break
            except urllib.error.URLError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        model = next(row for row in models["models"] if row["arch"] == "moss_vl")
        api = "/api/model/" + urllib.parse.quote(model["id"], safe="")
        state = obj(api + "/load", body=b"")
        while state.get("loading"):
            state = obj(api + "/status")
            if state.get("state") == "error":
                raise RuntimeError(state.get("error"))
            if state.get("state") == "ready":
                break
            time.sleep(0.2)
        for mode in ("unicode_path", "upload"):
            active = "media-" + uuid.uuid4().hex[:12]
            params = {"session": active, "interval": 0.2, "frames": 1, "n": 32,
                      "observe-n": 16, "max-pixels": 65536}
            if mode == "unicode_path":
                params["src"] = str(fixture)
            obj(api + "/rt/start", params, b"" if mode == "unicode_path" else fixture.read_bytes())
            deadline = time.monotonic() + 30
            detail = ""
            while time.monotonic() < deadline:
                status = obj(api + "/rt/status", {"session": active})
                if status["source_ended"]:
                    break
                time.sleep(0.1)
            else:
                raise TimeoutError("Generated video did not end")
            events = [json.loads(line) for line in call(api + "/rt/events", {"session": active}).splitlines()]
            source_end = next(event for event in events if event.get("event") == "source_end")
            detail = source_end.get("detail", "")
            assert not detail, detail
            assert status["captured_frames"] >= 5, status
            preview = call(api + "/rt/preview", {"session": active})
            assert Image.open(io.BytesIO(preview)).size == (640, 360)
            upload_path = work / "work" / ("rt-upload-" + active + ".tmp")
            assert upload_path.exists() == (mode == "upload")
            started = time.perf_counter()
            obj(api + "/rt/stop", {"session": active}, b"")
            stop_ms = (time.perf_counter() - started) * 1000
            assert not upload_path.exists(), "Owned upload was not removed"
            for _ in range(30):
                if not ffmpeg_children(process.pid):
                    break
                time.sleep(0.1)
            assert not ffmpeg_children(process.pid), "Owned FFmpeg survived stop"
            result["tests"][mode] = {"captured_frames": status["captured_frames"],
                "stop_ms": stop_ms, "preview_size": [640, 360], "upload_cleaned": True,
                "owned_ffmpeg_exited": True}
            active = ""
        # A bad source must carry a useful FFmpeg error rather than silently hanging.
        active = "missing-" + uuid.uuid4().hex[:12]
        obj(api + "/rt/start", {"session": active, "src": str(work / "不存在 source.mp4")}, b"")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            events = [json.loads(line) for line in call(api + "/rt/events", {"session": active}).splitlines()]
            ended = next((event for event in events if event.get("event") == "source_end"), None)
            if ended:
                break
            time.sleep(0.1)
        assert ended and ended.get("detail"), "Missing source had no diagnostic"
        result["tests"]["invalid_source"] = {"reported": True, "detail": ended["detail"][:500]}
        obj(api + "/rt/stop", {"session": active}, b"")
        active = ""
        # Close a response during generation, then require the model lock promptly.
        params = {"q": "请从1数到300，每个数字后换行，完整列出全部数字。", "n": 1024,
                  "stream": 1, "session": "disconnect-smoke", "rt": 1}
        response = urllib.request.urlopen(urllib.request.Request(root + api + "/ask?" +
            urllib.parse.urlencode(params), data=b""), timeout=30)
        try:
            for line in response:
                if json.loads(line).get("t"):
                    break
        finally:
            response.close()
        started = time.perf_counter()
        raw = call(api + "/ask", {"q": "只回答OK。", "n": 8, "stream": 1}, b"", timeout=30 if args.cpu else 10)
        elapsed = (time.perf_counter() - started) * 1000
        events = [json.loads(line) for line in raw.splitlines()]
        assert any(event.get("done") for event in events) and not any(event.get("error") for event in events)
        assert elapsed < (20000 if args.cpu else 5000), "Disconnected answer continued generating"
        result["tests"]["disconnect_cancellation"] = {"next_request_ms": elapsed}
        result["passed"] = True
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    finally:
        if active and api:
            try:
                obj(api + "/rt/stop", {"session": active}, b"")
            except Exception:
                pass
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log.close()
        result["server_log"] = str(log_path)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
