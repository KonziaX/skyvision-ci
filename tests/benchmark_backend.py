"""Measure the retained old binary and the rebuilt binary with the same fixture."""
import argparse
import json
import os
import pathlib
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def gpu_memory():
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
            "--format=csv,noheader,nounits"], capture_output=True, text=True, check=True, timeout=5)
        return [{"used_mib": int(row.split(",")[0]), "total_mib": int(row.split(",")[1])}
                for row in result.stdout.strip().splitlines()]
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--binary", required=True)
    p.add_argument("--models-dir", required=True)
    p.add_argument("--image", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--port", type=int, default=8902)
    p.add_argument("--pixels", type=int, default=0)
    p.add_argument("--label", required=True)
    args = p.parse_args()
    output = pathlib.Path(args.output).resolve()
    work = output.parent / ("benchmark-" + uuid.uuid4().hex[:10])
    work.mkdir(parents=True)
    root = "http://127.0.0.1:" + str(args.port)
    binary = pathlib.Path(args.binary).resolve()
    log_path = work / "server.log"
    log = log_path.open("wb")
    command = [str(binary), "--models-dir", str(pathlib.Path(args.models_dir).resolve()),
               "--serve", str(args.port), "--n-ctx", "4096"]
    baseline_memory = gpu_memory()
    process = subprocess.Popen(command, cwd=work, stdout=log, stderr=subprocess.STDOUT,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    result = {"label": args.label, "binary": str(binary), "requested_n_ctx": 4096,
              "max_pixels": args.pixels, "trials": [], "server_log": str(log_path),
              "requested_cross_rows": os.getenv("MOSS_XCTX"), "gpu_baseline": baseline_memory}

    def request(path, body=None):
        return urllib.request.urlopen(urllib.request.Request(root + path, data=body), timeout=180)

    try:
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None:
                raise RuntimeError("Server exited with " + str(process.returncode))
            try:
                with request("/api/models") as response:
                    models = json.load(response)
                break
            except urllib.error.URLError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        model = next(item for item in models["models"] if item["arch"] == "moss_vl")
        api = "/api/model/" + urllib.parse.quote(model["id"], safe="")
        with request(api + "/load", b"") as response:
            state = json.load(response)
        latency = []
        while state.get("loading"):
            start = time.perf_counter()
            with request(api + "/status") as response:
                state = json.load(response)
            latency.append((time.perf_counter() - start) * 1000)
            if state.get("state") == "ready":
                break
            if state.get("state") == "error":
                raise RuntimeError(state.get("error"))
            time.sleep(0.2)
        result["load_status_max_ms"] = max(latency, default=0)
        result["gpu_loaded"] = gpu_memory()
        media = pathlib.Path(args.image).read_bytes()
        for trial in range(3):
            # n precedes session, so the old substring-based parser receives
            # the same intended cap too. Ask() gives identical cold chat state.
            params = {"q": "请用一句中文描述左侧方形和右侧圆形的颜色。", "n": 48, "stream": 1}
            if args.pixels:
                params["max-pixels"] = args.pixels
            start = time.perf_counter()
            first = 0
            text = ""
            stats = None
            with request(api + "/ask?" + urllib.parse.urlencode(params), media) as response:
                for line in response:
                    event = json.loads(line.decode("utf-8", errors="strict"))
                    if event.get("error"):
                        raise RuntimeError(event["error"])
                    if event.get("t"):
                        if not first:
                            first = (time.perf_counter() - start) * 1000
                        text += event["t"]
                    if event.get("done"):
                        stats = event.get("stats")
            elapsed = (time.perf_counter() - start) * 1000
            assert text and "\ufffd" not in text
            result["trials"].append({"trial": trial + 1, "first_token_ms": first,
                                     "total_ms": elapsed, "text": text, "stats": stats})
        result["gpu_after_inference"] = gpu_memory()
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log.close()
        source = log_path.read_text(encoding="utf-8", errors="replace")
        result["actual_context"] = next(iter(re.findall(r"ready .*?ctx (\d+)", source)), None)
        result["log_perf"] = [line for line in source.splitlines() if "[perf]" in line]
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
