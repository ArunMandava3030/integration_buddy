"""Setup-only fallback: verified model downloads from their authors' repositories.

Run after starting the local Ollama server. This script downloads public model
weights; the application itself never downloads models or sends documents here.
"""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MODELS = [
    {"name": "qwen3:4b", "repo": "Qwen/Qwen3-4B-GGUF", "revision": "bc640142c66e1fdd12af0bd68f40445458f3869b",
     "file": "Qwen3-4B-Q4_K_M.gguf", "sha256": "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5", "size": 2497280256},
    {"name": "nomic-embed-text", "repo": "nomic-ai/nomic-embed-text-v1.5-GGUF", "revision": "0188c9bf409793f810680a5a431e7b899c46104c",
     "file": "nomic-embed-text-v1.5.f16.gguf", "sha256": "f7af6f66802f4df86eda10fe9bbcfc75c39562bed48ef6ace719a251cf1c2fdb", "size": 274290560},
]


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(spec, destination):
    if destination.exists() and checksum(destination) == spec["sha256"]:
        return
    partial = destination.with_suffix(".partial")
    url = f"https://huggingface.co/{spec['repo']}/resolve/{spec['revision']}/{spec['file']}"
    for attempt in range(3):
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": "CPI-Local-Setup/1.0"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            request = urllib.request.Request(url + f"?download=true&offset={offset}", headers=headers)
            with urllib.request.urlopen(request, timeout=90) as response:
                if offset and response.status == 200:
                    offset = 0
                elif response.status == 206 and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                    raise RuntimeError("Download server returned an unexpected byte range")
                with partial.open("ab" if offset else "wb") as output:
                    last_report = offset // (100 * 1024 * 1024)
                    while block := response.read(1024 * 1024):
                        output.write(block)
                        offset += len(block)
                        if offset // (100 * 1024 * 1024) != last_report:
                            last_report = offset // (100 * 1024 * 1024)
                            print(f"{spec['name']}: {offset // (1024*1024)} / {spec['size'] // (1024*1024)} MB", flush=True)
            if offset != spec["size"]:
                raise RuntimeError("Incomplete model download")
            if checksum(partial) != spec["sha256"]:
                raise RuntimeError("Model checksum verification failed; file will not be used")
            partial.replace(destination)
            return
        except (OSError, RuntimeError) as exc:
            print(f"Download attempt {attempt + 1}: {exc}", flush=True)
            if attempt == 2:
                raise
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ollama", type=Path, default=ROOT / ".runtime" / "ollama" / "ollama.exe")
    args = parser.parse_args()
    destination = ROOT / ".runtime" / "downloads"
    destination.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(OLLAMA_HOST="127.0.0.1:11434", OLLAMA_NO_CLOUD="1", OLLAMA_MODELS=str(ROOT / ".runtime" / "models"))
    for spec in MODELS:
        path = destination / spec["file"]
        print(f"Fetching {spec['name']} from {spec['repo']}...", flush=True)
        download(spec, path)
        modelfile = destination / (spec["file"] + ".Modelfile")
        modelfile.write_text(f'FROM "{path.as_posix()}"\nPARAMETER num_ctx {8192 if spec["name"].startswith("qwen") else 2048}\n', encoding="utf-8")
        print(f"Verified SHA-256. Importing {spec['name']} into local Ollama...", flush=True)
        result = subprocess.run([str(args.ollama), "create", spec["name"], "-f", str(modelfile)], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            raise RuntimeError(result.stderr[-2000:])
        print(f"{spec['name']} is installed locally.", flush=True)


if __name__ == "__main__":
    main()
