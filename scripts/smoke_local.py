"""Optional real-model smoke test using only fictional documents and local APIs."""
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import Application


def main():
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="live-smoke-", dir=runtime) as directory:
        config = Path(directory) / "config.json"
        config.write_text(json.dumps({"auto_import": False}), encoding="utf-8")
        app = Application(data_dir=Path(directory) / "data", config_path=config)
        print("Local models:", app.client.status()["models"], flush=True)
        app.import_folder(str(ROOT / "examples"), {"environment": "DEMO"})
        deadline = time.monotonic() + 300
        while app.job["state"] == "running":
            if time.monotonic() > deadline:
                raise TimeoutError("Document indexing exceeded five minutes")
            time.sleep(1)
        print("Import:", json.dumps(app.job), flush=True)
        print("Semantic index:", json.dumps(app.semantic.stats()), flush=True)
        assert app.job["failed"] == 0 and app.knowledge.stats()["documents"] == 1
        assert app.semantic.stats()["chunks"] > 0, "Local embeddings were not generated"
        start = time.monotonic()
        result = app.chat({"question": "How does DEMO_CUSTOMER_REPLICATION handle duplicate messages?"})
        print("Answer in", round(time.monotonic() - start, 1), "seconds:", flush=True)
        print(result["answer"], flush=True)
        print("Mode:", result["mode"], "Sources:", len(result["sources"]), "Notice:", result.get("notice"), flush=True)
        assert result["mode"] == "generated", "Local answer generation failed"
        assert result["sources"] and "EventId" in result["answer"]
        assert "[1]" in result["answer"] or "[2]" in result["answer"]
        print("PASS: local ingestion, embeddings, retrieval, model generation, and citations", flush=True)


if __name__ == "__main__":
    main()
