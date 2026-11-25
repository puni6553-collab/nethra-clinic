"""Iteration 3 RAG — knowledge base, hybrid search, rewrite and answer chains.

Flow for one patient message (handout §04):
  red-flag check (llm.check_red_flags, called by the chat route)
  -> rewrite(history, message)      — LLM, standalone question
  -> retrieve(question)             — BM25 + vector, top 4 (code, no LLM)
  -> answer_question(question, chunks) — LLM, {answer, source_ids, answered}
  -> the ROUTE validates source_ids and builds sources from chunk metadata

Rules that live here (not in the prompts):
- chunking: split on ## headings first, then only sections > CHUNK_SIZE
- every chunk carries source, section and chunk_id — citations are built
  from these by code, never by the model
- hybrid = BM25 + vector, equal weight (EnsembleRetriever, rank fusion)
- the index is built by build_index.py and loaded lazily; never rebuilt
  on a request
- any failure raises LlmUnavailable so the route can show the fixed
  "Sorry, I can't answer just now..." message and stay silent
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from langchain_core.documents import Document
from pydantic import BaseModel, Field

from . import config, observability
from .llm import LlmUnavailable, _chat_model, _invoke_with_retry

COLLECTION = "netracare_docs"


# --- R1: chunking -------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    """Lowercase + strip punctuation, so 'Menon?' matches 'Menon' (R2)."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "section"


def load_chunks() -> list[Document]:
    """Split documents/*.md on ## headings; split a section again only if
    it is longer than CHUNK_SIZE (overlap CHUNK_OVERLAP). Metadata on every
    chunk: source (file name), section (heading), chunk_id (stable)."""
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    if not os.path.isdir(config.DOCS_DIR):
        raise FileNotFoundError(f"documents folder not found: {config.DOCS_DIR}")

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.CHUNK_SIZE,
        chunk_overlap=config.CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    chunks: list[Document] = []
    for name in sorted(os.listdir(config.DOCS_DIR)):
        if not name.endswith(".md"):
            continue
        with open(os.path.join(config.DOCS_DIR, name), encoding="utf-8") as fh:
            raw = fh.read()

        parts = re.split(r"(?m)^(## .*)$", raw)
        pieces: list[tuple[str, str]] = []  # (section, body)
        preamble = parts[0]
        title_m = re.search(r"(?m)^# (.*)$", preamble)
        title = title_m.group(1).strip() if title_m else name[:-3]
        if preamble.strip():
            pieces.append((title, preamble))
        for i in range(1, len(parts), 2):
            pieces.append((parts[i][2:].strip(), parts[i + 1]))

        for section, body in pieces:
            text = f"## {section}\n{body.strip()}" if section != title else body.strip()
            base_id = f"{name}#{_slug(section)}"
            if len(text) <= config.CHUNK_SIZE:
                parts_out = [text]
            else:
                parts_out = splitter.split_text(text)
            for n, piece in enumerate(parts_out, start=1):
                chunk_id = base_id if n == 1 else f"{base_id}-{n}"
                chunks.append(
                    Document(
                        page_content=piece,
                        metadata={
                            "source": name,
                            "section": section,
                            "chunk_id": chunk_id,
                        },
                        id=chunk_id,
                    )
                )
    return chunks


# --- index build (R1) ---------------------------------------------------------

def _embeddings():
    from langchain_google_genai import GoogleGenerativeAIEmbeddings

    if not config.GEMINI_API_KEY:
        raise LlmUnavailable("GEMINI_API_KEY is not set")
    return GoogleGenerativeAIEmbeddings(
        model=config.EMBEDDING_MODEL,
        google_api_key=config.GEMINI_API_KEY,
    )


def build_index() -> int:
    """Chunk -> embed -> store in Chroma at config.CHROMA_DIR. Returns the
    chunk count. Run by build_index.py, never on a request."""
    import chromadb
    from langchain_chroma import Chroma

    chunks = load_chunks()
    client = chromadb.PersistentClient(path=config.CHROMA_DIR)
    try:
        client.delete_collection(COLLECTION)
    except Exception:  # noqa: BLE001 — first build has nothing to delete
        pass
    Chroma.from_documents(
        documents=chunks,
        embedding=_embeddings(),
        client=client,
        collection_name=COLLECTION,
    )
    return len(chunks)


# --- lazy index load + retrievers (R2) ----------------------------------------

_STATE: dict[str, Any] | None = None


def _ensure_state() -> dict[str, Any]:
    """Load the built index once: BM25 + vector + hybrid retrievers."""
    global _STATE
    if _STATE is not None:
        return _STATE

    import chromadb
    from langchain_chroma import Chroma
    from langchain_classic.retrievers import EnsembleRetriever
    from langchain_community.retrievers.bm25 import BM25Retriever

    if not os.path.isdir(config.CHROMA_DIR):
        raise LlmUnavailable("index not built — run build_index.py first")
    client = chromadb.PersistentClient(path=config.CHROMA_DIR)
    try:
        collection = client.get_collection(COLLECTION)
    except Exception as exc:  # noqa: BLE001
        raise LlmUnavailable("index not built — run build_index.py first") from exc
    data = collection.get(include=["documents", "metadatas"])
    if not data["ids"]:
        raise LlmUnavailable("index is empty — run build_index.py")

    docs = [
        Document(page_content=text, metadata=meta or {}, id=cid)
        for cid, text, meta in zip(data["ids"], data["documents"], data["metadatas"])
    ]
    texts = [d.page_content for d in docs]
    metas = [d.metadata for d in docs]
    ids = [d.id for d in docs]

    bm25 = BM25Retriever.from_texts(
        texts, metadatas=metas, ids=ids, preprocess_func=_tokenize
    )
    bm25.k = config.TOP_K

    store = Chroma(
        client=client,
        collection_name=COLLECTION,
        embedding_function=_embeddings(),
    )
    vector = store.as_retriever(search_kwargs={"k": config.TOP_K})
    ensemble = EnsembleRetriever(
        retrievers=[bm25, vector],
        weights=[0.5, 0.5],
        id_key="chunk_id",
    )

    _STATE = {
        "docs": {d.id: d for d in docs},
        "bm25": bm25,
        "store": store,
        "vector": vector,
        "ensemble": ensemble,
    }
    return _STATE


@observability.tracked("chat", role="patient", name="clinic_retrieve")
def retrieve(question: str) -> list[Document]:
    """Hybrid top-4: BM25 keyword + vector search, equal weight (R2)."""
    state = _ensure_state()
    found = state["ensemble"].invoke(question)
    return found[: config.TOP_K]


def search_debug(query: str) -> dict:
    """Top-4 from keyword only, vector only and hybrid, with scores —
    the Staff search-debug view (R2)."""
    state = _ensure_state()
    bm25 = state["bm25"]

    # Keyword: scores straight from BM25Okapi, aligned with retriever docs.
    kw_scores = bm25.vectorizer.get_scores(_tokenize(query))
    id_score = {d.id: float(s) for d, s in zip(bm25.docs, kw_scores)}
    keyword_docs = sorted(
        bm25.docs, key=lambda d: id_score.get(d.id, 0.0), reverse=True
    )[: config.TOP_K]
    keyword = [
        _hit(d, id_score.get(d.id, 0.0)) for d in keyword_docs
    ]

    # Vector: Chroma returns a distance (lower is better); show as a
    # similarity so every column reads "higher = better".
    vec_pairs = state["store"].similarity_search_with_score(
        query, k=config.TOP_K
    )
    vector = [
        _hit(d, 1.0 / (1.0 + float(dist))) for d, dist in vec_pairs
    ]

    # Hybrid: the same ensemble the chat uses, scored with rank fusion
    # (weights 0.5/0.5, c=60 — display only; the order is the ensemble's).
    hybrid_docs = retrieve(query)
    kw_rank = {d.id: i + 1 for i, d in enumerate(keyword_docs)}
    vec_rank = {d.id: i + 1 for i, (d, _) in enumerate(vec_pairs)}

    def _rrf(cid: str) -> float:
        score = 0.0
        if cid in kw_rank:
            score += 0.5 / (60 + kw_rank[cid])
        if cid in vec_rank:
            score += 0.5 / (60 + vec_rank[cid])
        return score

    hybrid = [_hit(d, _rrf(d.id)) for d in hybrid_docs]
    return {"query": query, "keyword": keyword, "vector": vector, "hybrid": hybrid}


def _hit(doc: Document, score: float) -> dict:
    return {
        "chunk_id": doc.id,
        "source": doc.metadata.get("source", ""),
        "section": doc.metadata.get("section", ""),
        "score": round(float(score), 4),
        "text": doc.page_content[:400],
    }


# --- R3: rewrite the latest message using the conversation --------------------

def rewrite(history: list[dict], message: str) -> str:
    """Standalone question from the last messages. A message that is already
    complete stays as it is. Raises LlmUnavailable on failure."""
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import ChatPromptTemplate

    if not history:
        return message
    lines = []
    for turn in history[-config.CHAT_HISTORY_MAX:]:
        who = "Patient" if turn.get("role") == "user" else "Assistant"
        lines.append(f"{who}: {turn.get('content', '')}")
    transcript = "\n".join(lines)

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                (
                    "Rewrite the patient's latest message as one standalone "
                    "question that can be understood without the conversation. "
                    "Use the earlier turns only to fill in what 'it', 'they' or "
                    "a missing subject refers to. If the message is already "
                    "standalone, return it unchanged. Return only the rewritten "
                    "question, no preamble."
                ),
            ),
            (
                "human",
                "Conversation so far:\n{transcript}\n\n"
                "Latest patient message: {message}\n\n"
                "Standalone question:",
            ),
        ]
    )
    chain = prompt | _chat_model() | StrOutputParser()

    def _call() -> str:
        out = chain.invoke({"transcript": transcript, "message": message}).strip()
        return out or message

    try:
        return _invoke_with_retry(_call)
    except Exception as exc:  # noqa: BLE001
        raise LlmUnavailable(str(exc)) from exc


# --- R5/R6: answer chain ------------------------------------------------------

class ChatAnswer(BaseModel):
    """What the answer chain returns — data, not free text."""

    answer: str = Field(default="", description="The reply, at most 120 words")
    source_ids: list[str] = Field(
        default_factory=list,
        description="chunk_ids of the chunks actually used — only ids from the list",
    )
    answered: bool = Field(
        description="True only if the given chunks really answer the question"
    )


_ANSWER_SYSTEM = (
    "You are the NetraCare eye-clinic chat assistant. Answer ONLY from the "
    "numbered chunks below. Each chunk starts with its chunk_id in [brackets].\n\n"
    "Hard rules:\n"
    "- At most 120 words. Plain wording.\n"
    "- Never name a condition the patient might have, and never say what is "
    "causing a problem. Never suggest a treatment, medicine, drops or products.\n"
    "- A doctor always assesses the patient: suggest which speciality to book "
    "and which doctor(s) at NetraCare cover it (from the doctors.md chunks), "
    "then say the doctor will assess you at your visit.\n"
    "- Children under 16 are seen by Paediatric. A patient who is not sure "
    "which speciality goes to General.\n"
    "- If the problem is not about the eyes, reply exactly that NetraCare only "
    "looks after eyes, with no speciality, and set answered to true citing "
    "the chunk that shows NetraCare is an eye clinic.\n"
    "- Costs are indicative only, shared as information.\n"
    "- Care instructions the documents state (fasting times, activity "
    "limits, drop schedules) are shared as what the clinic's own sheet "
    "says: attribute them to the sheet and ask the patient to confirm the "
    "exact timings with their care team. Keep every number exactly as the "
    "document states it.\n"
    "- Politely decline anything that is not about NetraCare.\n"
    "- You cannot see free slots or book appointments: if asked to book, say "
    "so and point the patient to the Book page. You may share a doctor's OPD "
    "days when doctors.md says them.\n"
    "- If the chunks do not contain the answer — including any rate, "
    "percentage, outcome or other number they do not state — set answered "
    "to false, leave answer and source_ids empty, and do not answer, "
    "decline or paraphrase instead. Do not answer from your own knowledge.\n"
    "- source_ids: only chunk_ids that appear in the list above and that you "
    "actually used. Empty when answered is false."
)


@observability.tracked("chat", role="patient", name="clinic_answer")
def answer_question(question: str, chunks: list[Document]) -> ChatAnswer:
    """prompt | model | PydanticOutputParser over the retrieved chunks (R5).
    Raises LlmUnavailable on failure — the route shows the fixed message."""
    from langchain_core.output_parsers import PydanticOutputParser
    from langchain_core.prompts import ChatPromptTemplate

    if not chunks:
        return ChatAnswer(answer="", source_ids=[], answered=False)

    block = "\n\n".join(
        f"[{c.id}]\n{c.page_content}" for c in chunks
    )
    parser = PydanticOutputParser(pydantic_object=ChatAnswer)
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", _ANSWER_SYSTEM + "\n\n{format_instructions}"),
            ("human", "Chunks:\n{chunks}\n\nPatient question: {question}"),
        ]
    )
    chain = prompt | _chat_model() | parser

    def _call() -> ChatAnswer:
        result = chain.invoke(
            {
                "chunks": block,
                "question": question,
                "format_instructions": parser.get_format_instructions(),
            }
        )
        if not isinstance(result, ChatAnswer):
            result = ChatAnswer.model_validate(result)
        return result

    try:
        return _invoke_with_retry(_call)
    except Exception as exc:  # noqa: BLE001
        raise LlmUnavailable(str(exc)) from exc


def build_sources(answer: ChatAnswer, chunks: list[Document]) -> Optional[list[str]]:
    """Validate source_ids against what was retrieved and build the citation
    list from chunk metadata. Returns None when the answer must be replaced
    by the fixed 'I don't know' message (handout R5)."""
    if not answer.answered or not answer.source_ids:
        return None
    retrieved = {c.id: c for c in chunks}
    if any(sid not in retrieved for sid in answer.source_ids):
        return None  # names a chunk that wasn't retrieved
    sources: list[str] = []
    for sid in answer.source_ids:
        meta = retrieved[sid].metadata
        cite = f"{meta.get('source', '?')} > {meta.get('section', '?')}"
        if cite not in sources:
            sources.append(cite)
    return sources
