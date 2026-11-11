"""Run a bounded real-video stream and measure Windows private memory growth.

The video and reports stay local. Only the server started by this test is stopped.
"""
import argparse
import ctypes
import importlib.util
import json
import os
import pathlib
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from ctypes import wintypes


class ProcessMemory(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD),
                ("peak_working", ctypes.c_size_t), ("working", ctypes.c_size_t),
                ("peak_paged", ctypes.c_size_t), ("paged", ctypes.c_size_t),
                ("peak_nonpaged", ctypes.c_size_t), ("nonpaged", ctypes.c_size_t),
                ("pagefile", ctypes.c_size_t), ("peak_pagefile", ctypes.c_size_t),
                ("private", ctypes.c_size_t)]


def private_bytes(process):
    counters = ProcessMemory()
    counters.cb = ctypes.sizeof(counters)
    api = ctypes.WinDLL("psapi", use_last_error=True)
    api.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    if not api.GetProcessMemoryInfo(wintypes.HANDLE(int(process._handle)),
                                    ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return counters.private


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--models-dir", required=True)
    parser.add_argument("--web-dir", required=True)
    parser.add_argument("--video", required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--frames", type=int, default=160)
    parser.add_argument("--cross-rows", type=int, default=8192)
    parser.add_argument("--context", type=int, default=4096)
    parser.add_argument("--port", type=int, default=8901)
    parser.add_argument("--pixel-budget", type=int, default=131072)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--observe-tokens", type=int, default=32)
    parser.add_argument("--repeat-limit-mib", type=int, default=768)
    parser.add_argument("--memory-growth-limit-mib", type=int, default=1800)
    parser.add_argument("--disable-static", action="store_true")
    parser.add_argument("--disable-cuda-graphs", action="store_true")
    parser.add_argument("--prompt", help="Override the initial observation task for diagnosis")
    parser.add_argument("--repeat-penalty", type=float, default=1.05)
    parser.add_argument("--repeat-window", type=int, default=128)
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Memory monitoring requires Windows")
    if not 1 <= args.frames <= 4000:
        parser.error("frames must be 1..4000")
    report_path = pathlib.Path(args.output).resolve()
    if report_path.exists():
        raise FileExistsError(report_path)
    run_dir = report_path.parent / ("stream-regression-" + uuid.uuid4().hex[:10])
    run_dir.mkdir(parents=True)
    # Fast image extraction precedes model loading; no concurrent model processes.
    subprocess.run([args.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-i", str(pathlib.Path(args.video).resolve()), "-an", "-vf",
                    "fps=2,scale=483:272", "-q:v", "5", "-frames:v", "140",
                    str(run_dir / "frame-%04d.jpg")], check=True, timeout=60,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    frames = sorted(run_dir.glob("frame-*.jpg"))
    if not frames:
        raise RuntimeError("No video frames extracted")
    with socket.socket() as probe:
        probe.settimeout(0.4)
        if probe.connect_ex(("127.0.0.1", args.port)) == 0:
            raise RuntimeError("Test port is occupied; no process changed")
    base = "http://127.0.0.1:" + str(args.port)

    def call(path, params=None, body=None, timeout=180):
        query = "?" + urllib.parse.urlencode(params) if params else ""
        request = urllib.request.Request(base + path + query, data=body)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")

    def obj(*a, **kw):
        return json.loads(call(*a, **kw))

    env = os.environ.copy()
    for name in ("MOSS_NO_STATIC", "MOSS_DUMP_DIR", "GGML_CUDA_DISABLE_GRAPHS"):
        env.pop(name, None)
    env["MOSS_XCTX"] = str(args.cross_rows)
    if args.disable_static:
        env["MOSS_NO_STATIC"] = "1"
    if args.disable_cuda_graphs:
        env["GGML_CUDA_DISABLE_GRAPHS"] = "1"
    if args.cpu:
        env["PATH"] = "D:/DEV/C++/MinGW/mingw64/bin" + os.pathsep + env.get("PATH", "")
    command = [str(pathlib.Path(args.binary).resolve()), "--models-dir",
               str(pathlib.Path(args.models_dir).resolve()), "--web-dir",
               str(pathlib.Path(args.web_dir).resolve()), "--n-ctx", str(args.context),
               "--threads", "8", "--cpu" if args.cpu else "--gpu", "--serve", str(args.port),
               "--host", "127.0.0.1"]
    report = {"passed": False, "requested_frames": args.frames, "source_video": str(pathlib.Path(args.video).resolve()),
              "binary": command[0], "cross_rows": args.cross_rows, "context": args.context,
              "static_decode": not args.disable_static, "cuda_graphs": not args.disable_cuda_graphs,
              "cpu": args.cpu, "turns": [], "memory": [], "run_dir": str(run_dir)}
    session = "regression-" + uuid.uuid4().hex[:12]
    process = None
    job = None
    api = None
    log = (run_dir / "server.log").open("xb")
    start = time.monotonic()
    try:
        process = subprocess.Popen(command, cwd=run_dir, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        # Reuse the launcher job wrapper so interruption also cleans up our children.
        launcher = pathlib.Path(args.web_dir).resolve().parent / "scripts" / "start-moss.py"
        spec = importlib.util.spec_from_file_location("moss_launcher_job", launcher)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        job = module.OwnedJob(process)
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None:
                raise RuntimeError("Server exited before startup")
            try:
                runtime = obj("/api/runtime", timeout=2)
                break
            except (TimeoutError, urllib.error.URLError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        entries = obj("/api/models")["models"]
        model = next(row for row in entries if row["arch"] == "moss_vl")
        api = "/api/model/" + urllib.parse.quote(model["id"], safe="")
        status = obj(api + "/load", body=b"")
        while status.get("loading") or status.get("state") == "loading":
            if process.poll() is not None:
                raise RuntimeError("Server exited while loading")
            time.sleep(0.2)
            status = obj(api + "/status", timeout=20)
        if status.get("state") != "ready" and status.get("ok") is not True:
            raise RuntimeError(status)
        baseline = private_bytes(process)
        report["baseline_private_mib"] = round(baseline / 2**20, 2)
        settings = {"session": session, "src": "browser", "interval": args.interval, "frames": 1,
                    "n": 128, "observe-n": args.observe_tokens, "max-pixels": args.pixel_budget,
                    "temperature": 0, "top-k": 0, "top-p": 1, "repeat-penalty": args.repeat_penalty,
                    "repeat-last-n": args.repeat_window, "sys": 1,
                    "prompt": "请关注画面中明显的动作、物体进入离开和位置变化，用一句中文简短报告与用户问题相关的新变化。没有新变化或已经回答完毕时输出 <|silence|>，不要重复描述静止画面。"}
        if args.prompt is not None:
            settings["prompt"] = args.prompt
        report["settings"] = settings
        obj(api + "/rt/start", settings, b"")
        seq = 0
        for index in range(args.frames):
            if process.poll() is not None:
                raise RuntimeError("Server exited with " + str(process.returncode))
            body = frames[index % len(frames)].read_bytes()
            obj(api + "/rt/frame", {"session": session, "timestamp": index * 0.5}, body)
            end = time.monotonic() + 180
            text = ""
            done = None
            while not done:
                if process.poll() is not None:
                    raise RuntimeError("Server exited during turn " + str(index + 1))
                raw = call(api + "/rt/events", {"session": session, "since": seq}, timeout=5)
                for line in raw.splitlines():
                    event = json.loads(line)
                    seq = max(seq, event.get("seq", 0))
                    if "error" in event:
                        raise RuntimeError(event)
                    text += event.get("t", "")
                    if event.get("event") in ("turn_end", "silence"):
                        done = event
                if time.monotonic() > end:
                    raise TimeoutError("Turn " + str(index + 1))
                if not done:
                    time.sleep(0.035)
            report["turns"].append({"index": index + 1, "frame": index % len(frames),
                                    "text": text, "stats": done.get("stats")})
            memory = private_bytes(process)
            growth = (memory - baseline) / 2**20
            report["memory"].append({"turn": index + 1, "private_mib": round(memory / 2**20, 2),
                                      "growth_mib": round(growth, 2)})
            if index % 10 == 0:
                print(json.dumps({"turn": index + 1, "private_mib": round(memory / 2**20, 1),
                                  "growth_mib": round(growth, 1), "text": text}, ensure_ascii=False), flush=True)
            if growth > args.memory_growth_limit_mib:
                raise RuntimeError("Safety stop: private memory growth exceeded " +
                                   str(args.memory_growth_limit_mib) + " MiB")
        # Check meaningful latest-frame answers and responsiveness after many compactions.
        obj(api + "/rt/say", {"session": session,
            "q": "请用一句话描述最新画面中的主要物体或人物，不确定时明确说明。"}, b"")
        deadline = time.monotonic() + 180
        final_text = ""
        final_event = None
        while not final_event:
            raw = call(api + "/rt/events", {"session": session, "since": seq}, timeout=5)
            for line in raw.splitlines():
                event = json.loads(line)
                seq = max(seq, event.get("seq", 0))
                if "error" in event:
                    raise RuntimeError(event)
                final_text += event.get("t", "")
                if event.get("event") in ("turn_end", "silence"):
                    final_event = event
            if time.monotonic() > deadline:
                raise TimeoutError("Final question after long stream")
            if not final_event:
                time.sleep(0.035)
        report["final_question"] = {"text": final_text, "stats": final_event.get("stats")}
        from collections import Counter
        observations = [turn["text"] for turn in report["turns"] if turn["text"]]
        counts = Counter(observations)
        report["observation_counts"] = {"nonempty": len(observations), "unique": len(counts),
            "most_frequent_count": max(counts.values(), default=0)}
        report["passed"] = True
        report["final_private_mib"] = report["memory"][-1]["private_mib"]
        if len(report["memory"]) >= 40:
            report["steady_growth_mib"] = round(report["memory"][-1]["private_mib"] -
                                                 report["memory"][19]["private_mib"], 2)
            if report["steady_growth_mib"] > args.repeat_limit_mib:
                report["passed"] = False
                raise RuntimeError("Steady-state private memory grew beyond the regression limit")
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        if process and process.poll() is None:
            if api:
                try:
                    obj(api + "/rt/stop", {"session": session}, b"", timeout=5)
                except Exception:
                    pass
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if job:
            job.close()
        log.close()
        report["elapsed_seconds"] = round(time.monotonic() - start, 2)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(report_path), "passed": report["passed"],
                          "turns": len(report["turns"]), "error": report.get("error")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
