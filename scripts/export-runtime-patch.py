"""Export only the audited C++ changes from the ignored build checkout."""
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "skyvision"
BASE = "a4822e5065a08f1198b9d6b1cffc274626172bd1"
EXPECTED = {
    "src/cli/realtime_options.hpp", "src/cli/vqa_cli.cpp", "src/cli/vqa_server.cpp",
    "src/core/runtime/http_server.hpp", "src/core/vqa.hpp",
    "src/models/moss_vl/moss_vl_model.cpp",
}


def git(*args):
    return subprocess.run(["git", *args], cwd=SOURCE, capture_output=True, check=True).stdout


if git("rev-parse", "HEAD").decode().strip() != BASE:
    raise RuntimeError("Build source is not at the pinned upstream revision")
git("add", "-N", "src/cli/realtime_options.hpp")
changed = set(git("diff", "--name-only").decode().splitlines())
if changed != EXPECTED:
    raise RuntimeError("Unexpected patch scope: " + repr(changed))
git("diff", "--check")
patch = git("diff", "--binary", "--", *sorted(EXPECTED))
output = ROOT / "patches" / "moss-runtime.patch"
output.parent.mkdir(exist_ok=True)
output.write_bytes(patch)
print("Exported", len(patch), "bytes to", output)
print(git("diff", "--stat").decode())
