"""Exercise the real model with deterministic, non-camera fixtures."""
import argparse
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

from PIL import Image, ImageDraw, ImageFont


def fixture(circle=(30, 75, 225), circle_label="BLUE CIRCLE"):
    image = Image.new("RGB", (1280, 720), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 140, 530, 580), fill=(230, 35, 35))
    draw.ellipse((740, 140, 1180, 580), fill=circle)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 62)
    except OSError:
        font = ImageFont.load_default(size=62)
    draw.text((180, 610), "RED BOX", fill="black", font=font)
    draw.text((785, 610), circle_label, fill="black", font=font)
    result = io.BytesIO()
    image.save(result, format="JPEG", quality=95)
    return result.getvalue()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--models-dir", required=True)
    parser.add_argument("--web-dir", required=True)
    parser.add_argument("--port", type=int, default=8901)
    parser.add_argument("--gpu", action="store_true")
    parser.add_argument("--context", type=int, default=4096)
    parser.add_argument("--cross-rows", type=int)
    parser.add_argument("--long-cache", action="store_true")
    parser.add_argument("--visual-cache", action="store_true")
    parser.add_argument("--visual-frames", type=int, default=24)
    parser.add_argument("--runtime-dir", help="Optional compiler/runtime DLL directory for local CPU builds")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not 1 <= args.visual_frames <= 256:
        parser.error("--visual-frames must be 1..256")
    report_path = pathlib.Path(args.output).resolve()
    run_dir = report_path.parent / ("smoke-" + uuid.uuid4().hex[:10])
    run_dir.mkdir(parents=True)
    image = fixture()
    (run_dir / "colors.jpg").write_bytes(image)
    report = {"backend": "gpu" if args.gpu else "cpu", "tests": {}, "passed": False}
    base = "http://127.0.0.1:" + str(args.port)

    def call(path, params=None, body=None, timeout=180, expect=200):
        url = base + path + ("?" + urllib.parse.urlencode(params or {}) if params else "")
        req = urllib.request.Request(url, data=body)
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                content = response.read().decode("utf-8", errors="strict")
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
            content = error.read().decode("utf-8", errors="strict")
        if status != expect:
            raise AssertionError(f"{path}: HTTP {status}, expected {expect}: {content[:500]}")
        return content, (time.perf_counter() - start) * 1000

    def obj(path, params=None, body=None, **kw):
        raw, elapsed = call(path, params, body, **kw)
        return json.loads(raw), elapsed

    command = [str(pathlib.Path(args.binary).resolve()), "--models-dir", str(pathlib.Path(args.models_dir).resolve()),
               "--web-dir", str(pathlib.Path(args.web_dir).resolve()), "--n-ctx", str(args.context), "--threads", "8",
               "--gpu" if args.gpu else "--cpu", "--serve", str(args.port)]
    env = os.environ.copy()
    if args.runtime_dir:
        env["PATH"] = str(pathlib.Path(args.runtime_dir).resolve()) + os.pathsep + env["PATH"]
    env.pop("MOSS_NO_STATIC", None)
    env.pop("MOSS_DUMP_DIR", None)
    if args.cross_rows:
        env["MOSS_XCTX"] = str(args.cross_rows)
    log = (run_dir / "server.log").open("wb")
    process = subprocess.Popen(command, cwd=run_dir, env=env, stdout=log, stderr=subprocess.STDOUT,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    live = "smoke-" + uuid.uuid4().hex[:12]
    api = None
    try:
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None:
                raise RuntimeError("Server exited with " + str(process.returncode))
            try:
                runtime, _ = obj("/api/runtime", timeout=2)
                break
            except urllib.error.URLError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        assert runtime["runtime_api"] == 2 and runtime["n_ctx"] == args.context
        models, _ = obj("/api/models")
        model = next(row for row in models["models"] if row["arch"] == "moss_vl")
        api = "/api/model/" + urllib.parse.quote(model["id"], safe="")
        loaded, _ = obj(api + "/load", body=b"")
        status_latencies = []
        while loaded.get("loading"):
            loaded, elapsed = obj(api + "/status", timeout=30)
            status_latencies.append(elapsed)
            if loaded.get("state") == "error":
                raise RuntimeError(loaded.get("error"))
            if loaded.get("state") == "ready":
                break
            time.sleep(0.2)
        report["load_status_max_ms"] = max(status_latencies, default=0)

        qa_session = "qa-" + uuid.uuid4().hex[:12]

        def ask(question, cap, media=b""):
            raw, elapsed = call(api + "/ask", {"q": question, "n": cap, "stream": 1,
                "session": qa_session, "rt": 1, "max-pixels": 262144 if args.gpu else 65536}, media)
            events = [json.loads(line) for line in raw.splitlines() if line.strip()]
            if any("error" in event for event in events):
                raise AssertionError(events)
            text = "".join(event.get("t", "") for event in events)
            assert "\ufffd" not in text, "Corrupted UTF-8 output"
            final = next(event for event in reversed(events) if event.get("done"))
            return {"text": text, "elapsed_ms": elapsed, "stats": final["stats"]}

        report["tests"]["first_image"] = ask("请用一句中文描述左侧方形和右侧圆形的颜色。", 48, image)
        assert report["tests"]["first_image"]["text"].strip(), "First image response was empty"
        report["tests"]["forced_cap"] = ask("请详细介绍图片里每个形状的颜色、位置和大小。", 2)
        assert report["tests"]["forced_cap"]["stats"]["finish_reason"] == "length"
        report["tests"]["follow_after_cap"] = ask("右侧圆形是什么颜色？只回答颜色名称。", 24)
        assert report["tests"]["follow_after_cap"]["text"].strip(), "Capped response damaged the next turn"
        assert any(word in report["tests"]["follow_after_cap"]["text"].lower() for word in ("蓝", "blue")), "Visual context was lost"
        if args.long_cache:
            # With a 1024-token context these text-only turns force eviction.
            # This exercises physical cache slots, retained vision, and MRoPE
            # continuity together; a stale prefill plan used to overflow KV.
            results = []
            filler = "data " * 220
            for index in range(5):
                response = ask(filler + "\n以上只是占位文字。右侧圆形是什么颜色？请只回答颜色名称。", 16)
                assert any(word in response["text"].lower() for word in ("蓝", "blue")), "Vision/position state was lost after cache eviction"
                results.append(response)
            report["tests"]["text_cache_eviction"] = results

        options = {"session": live, "src": "browser", "interval": 0.2, "frames": 1,
                   "n": 64, "observe-n": 48, "max-pixels": 262144 if args.gpu else 65536,
                   "prompt": "请用中文简短描述画面中的颜色和形状。有变化再回答，没有新变化时输出 <|silence|>。"}
        obj(api + "/rt/start", options, b"")
        obj(api + "/rt/frame", {"session": live, "timestamp": 0}, image)
        seq = 0
        events = []

        def poll_until(predicate, seconds=180):
            nonlocal seq
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                raw, _ = call(api + "/rt/events", {"session": live, "since": seq}, timeout=5)
                for line in raw.splitlines():
                    event = json.loads(line)
                    if "seq" not in event:
                        continue
                    seq = max(seq, event["seq"])
                    events.append(event)
                    if "error" in event:
                        raise AssertionError(event)
                    if predicate(event):
                        return event
                time.sleep(0.05)
            raise TimeoutError("Realtime event did not arrive")

        first_turn = poll_until(lambda event: event.get("event") in ("turn_end", "silence"))
        report["tests"]["realtime_first"] = {"events": events.copy(), "completion": first_turn}
        obj(api + "/rt/start", {"session": "conflicting", "src": "browser"}, b"", expect=409)
        obj(api + "/ask", {"q": "conflicting"}, b"", expect=409)
        obj(api + "/rt/say", {"session": live, "q": "请直接回答：右侧圆形是什么颜色？"}, b"")
        before = len(events)
        question = poll_until(lambda event: event.get("event") == "ask")
        answer = poll_until(lambda event: event.get("event") in ("turn_end", "silence"))
        rt_text = "".join(event.get("t", "") for event in events[before:])
        assert rt_text.strip() and "\ufffd" not in rt_text, "Realtime follow-up failed"
        assert any(word in rt_text.lower() for word in ("蓝", "blue")), "Realtime visual context was lost"
        report["tests"]["realtime_question"] = {"text": rt_text, "stats": answer.get("stats")}

        timestamp_base = 0
        if args.visual_cache:
            # Repeated frames fill and evict cross-KV. Then change the image:
            # the newest frame must win, with no stale GPU feature-cache reuse.
            green = fixture((30, 175, 75), "GREEN CIRCLE")
            observation_stats = []
            for timestamp in range(1, args.visual_frames + 1):
                obj(api + "/rt/frame", {"session": live, "timestamp": timestamp}, green if timestamp == args.visual_frames else image)
                observed = poll_until(lambda event: event.get("event") in ("turn_end", "silence"))
                observation_stats.append(observed.get("stats"))
            obj(api + "/rt/say", {"session": live, "q": "现在右侧圆形是什么颜色？请只回答当前颜色名称。"}, b"")
            before = len(events)
            poll_until(lambda event: event.get("event") == "ask")
            latest = poll_until(lambda event: event.get("event") in ("turn_end", "silence"))
            latest_text = "".join(event.get("t", "") for event in events[before:])
            assert any(word in latest_text.lower() for word in ("绿", "green")), "Latest vision was lost after cross-KV eviction"
            report["tests"]["visual_cache_eviction"] = {"frames": args.visual_frames, "latest_answer": latest_text,
                "stats": latest.get("stats"), "last_observations": observation_stats[-6:]}
            timestamp_base = args.visual_frames
        for timestamp in range(1, 7):
            obj(api + "/rt/frame", {"session": live, "timestamp": timestamp_base + timestamp}, image)
        status, elapsed = obj(api + "/rt/status", {"session": live}, timeout=5)
        assert status["queued_frames"] <= 1 and status["dropped_frames"] > 0, "Frame backlog was not bounded"
        report["tests"]["bounded_queue"] = {"status": status, "status_latency_ms": elapsed}
        obj(api + "/rt/frame", {"session": live, "timestamp": timestamp_base + 5.5}, image, expect=400)
        obj(api + "/rt/interrupt", {"session": live}, b"")
        _, stop_ms = obj(api + "/rt/stop", {"session": live}, b"", timeout=30)
        report["tests"]["stop_ms"] = stop_ms
        assert stop_ms < (3000 if args.gpu else 15000), "Stop did not cooperate with inference"
        report["passed"] = True
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    except Exception as error:
        report["error"] = repr(error)
        print("FAILED:", repr(error), flush=True)
        raise
    finally:
        if api:
            try:
                obj(api + "/rt/stop", {"session": live}, b"", timeout=30)
            except Exception:
                pass
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log.close()
        report["server_log"] = str(run_dir / "server.log")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
