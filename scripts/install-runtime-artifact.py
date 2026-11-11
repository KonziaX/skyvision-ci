"""Validate a build artifact and extract it without replacing existing files."""
import argparse
import hashlib
import json
import pathlib
import re
import shutil
import stat
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--expected-commit", required=True)
    args = parser.parse_args()
    repo = pathlib.Path(__file__).resolve().parents[1]
    dest = pathlib.Path(args.dest).resolve()
    if not dest.is_relative_to(repo) or dest == repo:
        raise ValueError("Destination must be a new folder inside this workflow repository")
    if dest.exists():
        raise FileExistsError("Refusing to overwrite " + str(dest))
    patch_sha = hashlib.sha256((repo / "patches/moss-runtime.patch").read_bytes()).hexdigest()
    with zipfile.ZipFile(args.archive) as archive:
        entries = archive.infolist()
        if not entries or len(entries) > 1000 or sum(item.file_size for item in entries) > 3 * 2**30:
            raise ValueError("Artifact size or file count is unexpected")
        names = set()
        for item in entries:
            name = item.filename.replace("\\", "/")
            path = pathlib.PurePosixPath(name)
            if path.is_absolute() or not path.parts or any(part in (".", "..") for part in path.parts):
                raise ValueError("Unsafe ZIP path: " + name)
            if any(":" in part or part.endswith((".", " ")) or
                   re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", part, re.I)
                   for part in path.parts):
                raise ValueError("Unsafe Windows ZIP path: " + name)
            if stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError("ZIP links are not allowed")
            target = dest.joinpath(*path.parts).resolve()
            if not target.is_relative_to(dest) or target == dest:
                raise ValueError("ZIP path escaped the destination")
            normalized = str(path).lower()
            if normalized in names:
                raise ValueError("Duplicate ZIP path: " + name)
            names.add(normalized)
        required = {"vqa-cli.exe", "build-info.json", "web/moss.html", "cublas64_13.dll",
                    "cublaslt64_13.dll", "cudart64_13.dll", "nvjitlink_130_0.dll"}
        if not required.issubset(names):
            raise ValueError("Missing packaged files: " + str(sorted(required - names)))
        info = json.loads(archive.read("build-info.json").decode("utf-8-sig"))
        expected = {"ci_ref": args.expected_commit,
                    "skyvision_ref": "a4822e5065a08f1198b9d6b1cffc274626172bd1",
                    "ggml_ref": "cd6d2b75f5d3e4b2f3fe8e8fbc6c8b84ac80e74a",
                    "patch_sha256": patch_sha, "cuda": "13.0.0", "cuda_arch": "120",
                    "cuda_graphs": True, "runtime_api": 2}
        for key, value in expected.items():
            if info.get(key) != value:
                raise ValueError(f"Build metadata mismatch: {key}")
        digest = hashlib.sha256()
        with archive.open("vqa-cli.exe") as source:
            for block in iter(lambda: source.read(2**20), b""):
                digest.update(block)
        if digest.hexdigest() != info.get("executable_sha256"):
            raise ValueError("Executable SHA-256 does not match the build metadata")
        # Validation is complete before the first destination file is created.
        dest.mkdir()
        for item in entries:
            target = dest.joinpath(*pathlib.PurePosixPath(item.filename.replace("\\", "/")).parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=2**20)
        print(json.dumps({"installed": str(dest), "files": len(entries), "build": info}, indent=2))


if __name__ == "__main__":
    main()
