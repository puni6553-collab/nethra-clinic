"""Make `ragas` importable in this environment (iteration 5 R6).

ragas 0.4.3 does `from langchain_community.chat_models.vertexai import
ChatVertexAI` at import time. That module was dropped from
langchain-community 0.4 (the version this project pins for other reasons),
so importing ragas fails before it ever runs. We never use Vertex AI — the
wrapper below stands in for the symbol, and ragas only ever touches it when
asked for a Vertex model.

Install the stub, then import ragas. `ensure()` is idempotent.
"""
from __future__ import annotations

import sys
import types

_STUB = "langchain_community.chat_models.vertexai"
_installed = False


def ensure() -> None:
    global _installed
    if _installed:
        return
    if _STUB not in sys.modules:
        try:
            __import__(_STUB)
        except ImportError:
            stub = types.ModuleType(_STUB)

            class _NotVertexAI:  # never instantiated by our code path
                def __init__(self, *a, **k):
                    raise RuntimeError("Vertex AI is not available in this project")

            stub.ChatVertexAI = _NotVertexAI
            sys.modules[_STUB] = stub
    _installed = True
