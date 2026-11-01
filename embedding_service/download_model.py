"""Download real BGE-M3 files; ModelScope downloads verify repository SHA256.

Set BGE_SOURCE=modelscope to use the explicitly selected mirror snapshot.
No model-provided Python files are downloaded or executed.
"""
import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

FILES = {
    "config.json", "config_sentence_transformers.json", "modules.json",
    "sentence_bert_config.json", "special_tokens_map.json", "tokenizer.json",
    "tokenizer_config.json", "sentencepiece.bpe.model", "pytorch_model.bin",
    "1_Pooling/config.json",
}


def prepare_model():
    explicit = os.getenv("BGE_MODEL_PATH")
    if explicit:
        return explicit
    source = os.getenv("BGE_SOURCE", "huggingface")
    if source == "huggingface":
        from huggingface_hub import snapshot_download
        return snapshot_download("BAAI/bge-m3", revision=os.getenv("BGE_REVISION", "5617a9f61b028005a4858fdac845db406aefb181"),
                                 allow_patterns=sorted(FILES), max_workers=2)
    if source != "modelscope":
        raise RuntimeError("BGE_SOURCE must be huggingface or modelscope")
    # ModelScope provides commit identifiers distinct from Hugging Face.
    revision = os.getenv("BGE_MODELSCOPE_REVISION", "e44369c5623cc146f016da906583db4ee0e3488d")
    api = "https://modelscope.cn/api/v1/models/BAAI/bge-m3/repo/files?" + urllib.parse.urlencode({"Revision": revision, "Recursive": "true"})
    with urllib.request.urlopen(api, timeout=60) as response:
        entries = json.load(response)["Data"]["Files"]
    selected = {f["Path"]: f for f in entries if f["Path"] in FILES and f["Type"] == "blob"}
    if set(selected) != FILES:
        raise RuntimeError("ModelScope snapshot is missing required BGE-M3 files")
    manifest = {path: entry["Sha256"] for path, entry in sorted(selected.items())}
    fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    target = Path(os.getenv("HF_HOME", "/models/huggingface")) / "modelscope" / fingerprint
    target.mkdir(parents=True, exist_ok=True)
    for name, entry in selected.items():
        destination = target / name
        if destination.exists() and digest(destination) == entry["Sha256"]:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".partial")
        # Pin each file to the commit reported by the listing and verify content.
        url = "https://modelscope.cn/models/BAAI/bge-m3/resolve/" + urllib.parse.quote(entry["Revision"], safe="") + "/" + name
        for attempt in range(3):
            try:
                print(f"Downloading BGE-M3 {name} ({entry['Size']} bytes) from ModelScope", flush=True)
                with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as stream:
                    while block := response.read(1024 * 1024):
                        stream.write(block)
                if temporary.stat().st_size != entry["Size"] or digest(temporary) != entry["Sha256"]:
                    raise RuntimeError("Model weight checksum/size mismatch")
                temporary.replace(destination)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
    (target / "verified-manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    return str(target)


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            hasher.update(block)
    return hasher.hexdigest()


if __name__ == "__main__":
    print(prepare_model())
