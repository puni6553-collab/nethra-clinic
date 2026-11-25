"""Build the clinic-documents index (iteration 3, handout R1).

Run from backend/ whenever a document changes — never per request:

    ..\\venv\\Scripts\\python build_index.py

Chunks documents/*.md on ## headings, embeds them with the Gemini model in
.env (EMBEDDING_MODEL) and stores them in .chroma/ next to this folder.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import config, rag  # noqa: E402


def main() -> int:
    if not os.path.isdir(config.DOCS_DIR):
        print(f"Documents not found: {config.DOCS_DIR}")
        return 1

    chunks = rag.load_chunks()
    print(f"Chunked {len(chunks)} pieces from {config.DOCS_DIR}")
    missing = [c.id for c in chunks if not (c.metadata.get("source") and c.metadata.get("section"))]
    if missing:
        print(f"  WARNING: {len(missing)} chunks missing source/section: {missing[:5]}")
    for c in chunks[:3]:
        print(f"  {c.id}  ({len(c.page_content)} chars) {c.page_content[:70]!r}")

    print(f"Embedding with {config.EMBEDDING_MODEL} ...")
    count = rag.build_index()
    print(f"Stored {count} chunks in {config.CHROMA_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
