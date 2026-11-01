"""Self-hosted, event sourced agent memory operating system."""

import os
from pathlib import Path

# A verified public tokenizer asset keeps token counting independent of network.
os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(Path(__file__).parent / "tokenizer_cache"))
