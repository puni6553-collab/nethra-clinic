"""Suite 3 — policy answers (brief target: 100% of answered rows cite a
document) plus RAGAS on the same answers (handout R6).

The handout says to call the iteration 3 answer function directly — the one
`search_clinic_docs` uses — so each row yields the answer, its sources and
the retrieved chunks. Retrieval + answering happen in-process on the eval
database; no server is needed.

Two kinds of row:

* `expected_source` set  -> must come back with at least one source, and
  the bar asks whether it cites the *expected* section
  (`file > section`, alternatives separated by `|`).
* `expected_source = none` -> must come back with the fixed "I don't know"
  message and no sources.
"""
from __future__ import annotations

import time

from .. import harness
from .base import Measure, Row, SuiteResult, frac, pct


def _expected_sections(value: str) -> list[tuple[str, str]]:
    out = []
    for part in str(value or "").split("|"):
        part = part.strip()
        if not part or part.lower() == "none":
            continue
        file_name, _, section = part.partition(">")
        out.append((file_name.strip().lower(), section.strip().lower()))
    return out


def _cite_matches(cites: list[str], expected: list[tuple[str, str]]) -> bool:
    have = []
    for cite in cites:
        file_name, _, section = cite.partition(">")
        have.append((file_name.strip().lower(), section.strip().lower()))
    return any(pair in have for pair in expected)


def _ragas(answered: list[dict]) -> dict:
    """Four RAGAS metrics over the answered rows. Raises on failure."""
    import math

    from .. import ragas_compat

    ragas_compat.ensure()
    from datasets import Dataset
    from ragas import evaluate as ragas_evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import AnswerRelevancy, ContextPrecision, ContextRecall, Faithfulness
    from ragas.run_config import RunConfig

    from app import llm as app_llm
    from app import rag as app_rag

    ds = Dataset.from_list(answered)
    result = ragas_evaluate(
        ds,
        metrics=[Faithfulness(), AnswerRelevancy(), ContextPrecision(), ContextRecall()],
        llm=LangchainLLMWrapper(app_llm._chat_model()),
        embeddings=LangchainEmbeddingsWrapper(app_rag._embeddings()),
        show_progress=False,
        # 16 workers at once trips the free tier's rate limit and the jobs
        # time out into NaN; two gentle workers finish the small set.
        run_config=RunConfig(max_workers=2, timeout=600, max_retries=5),
    )
    per_metric: dict[str, list[float]] = {}
    kept_rows: list[dict] = []
    for row in result.scores:
        clean: dict = {}
        for key, value in row.items():
            if key.startswith("__") or value is None:
                continue
            try:
                f = float(value)
            except (TypeError, ValueError):
                continue
            if math.isnan(f) or math.isinf(f):
                continue
            clean[key] = f
            per_metric.setdefault(key, []).append(f)
        kept_rows.append(clean)
    averages = {
        key: round(sum(vals) / len(vals), 4) for key, vals in per_metric.items() if vals
    }
    return {"averages": averages, "per_row": kept_rows,
            "rows_scored": len(kept_rows), "rows_total": len(answered)}


def run(ctx) -> SuiteResult:
    from app import config
    from app import rag as app_rag

    rows_in = harness.load_rows("policy_questions", ctx.large)
    result = SuiteResult(suite="policy_questions", size=harness.size_label(ctx.large))
    answered_payload: list[dict] = []
    payload_rows: list[Row] = []   # same order as answered_payload

    for spec in rows_in:
        qid = spec.get("question_id", "")
        question = spec.get("question", "")
        want_idk = str(spec.get("expected_source", "")).strip().lower() in ("none", "")
        row = Row(id=qid, input=question, extra={
            "expected_answer": spec.get("expected_answer", ""),
            "expected_source": spec.get("expected_source", ""),
            "kind": "idk" if want_idk else "answered",
        })
        started = time.perf_counter()
        try:
            chunks = app_rag.retrieve(question)
            answer = app_rag.answer_question(question, chunks)
            sources = app_rag.build_sources(answer, chunks)
        except Exception as exc:  # noqa: BLE001 — quota/timeout = row error
            row.error = f"answer function failed: {exc}"
            sources = None
            chunks = []
        row.latency_ms = (time.perf_counter() - started) * 1000.0

        if sources is None:
            reply = config.IDK_MESSAGE
            row.extra["sources"] = ""
        else:
            reply = answer.answer
            row.extra["sources"] = "; ".join(sources)
        row.output = reply

        if want_idk:
            row.scores["idk_ok"] = (not sources) and not row.error
        else:
            row.scores["has_source"] = bool(sources) and not row.error
            expected = _expected_sections(spec.get("expected_source", ""))
            row.scores["section_ok"] = bool(sources) and _cite_matches(sources or [], expected)
            if not row.error and sources is not None:
                answered_payload.append({
                    "question": question,
                    "answer": reply,
                    "contexts": [c.page_content for c in chunks],
                    "reference": spec.get("expected_answer", ""),
                })
                payload_rows.append(row)
        result.rows.append(row)

    # --- RAGAS (handout R6) -------------------------------------------------
    ragas_block: dict = {"enabled": False}
    if answered_payload and not ctx.no_ragas:
        try:
            ragas_out = _ragas(answered_payload)
            ragas_block = {"enabled": True, **ragas_out}
            averages = ragas_out["averages"]
            # per_row lines up with answered_payload, which lines up with
            # payload_rows (both appended under the same guard).
            for i, row in enumerate(payload_rows):
                per = ragas_out["per_row"][i] if i < len(ragas_out["per_row"]) else {}
                for key, value in per.items():
                    if key.startswith("__"):
                        continue
                    row.scores[f"ragas_{key}"] = round(float(value), 4)
            result.extra["ragas"] = {
                "averages": averages,
                "lowest": _lowest_rows(payload_rows),
                "rows_scored": ragas_out.get("rows_scored", len(payload_rows)),
                "rows_total": ragas_out.get("rows_total", len(payload_rows)),
            }
            if ragas_out.get("rows_scored", 0) < ragas_out.get("rows_total", 0):
                result.notes.append(
                    f"RAGAS scored {ragas_out['rows_scored']} of "
                    f"{ragas_out['rows_total']} rows (the rest timed out)")
            faith = averages.get("faithfulness")
            recall = averages.get("context_recall")
            result.measures.extend([
                Measure("RAGAS faithfulness (working bar)", "bar",
                        "n/a" if faith is None else f"{faith:.3f}", ">= 0.85",
                        None if faith is None else faith >= 0.85),
                Measure("RAGAS context recall (working bar)", "bar",
                        "n/a" if recall is None else f"{recall:.3f}", ">= 0.80",
                        None if recall is None else recall >= 0.80),
                Measure("RAGAS response relevancy", "report",
                        _avg_text(averages, "answer_relevancy"), "report only", None),
                Measure("RAGAS context precision", "report",
                        _avg_text(averages, "context_precision"), "report only", None),
            ])
        except Exception as exc:  # noqa: BLE001 — reported, never fatal
            ragas_block = {"enabled": False, "error": str(exc)}
            result.notes.append(f"RAGAS did not run: {exc}")
    elif not answered_payload:
        result.notes.append("RAGAS skipped: no answered rows this run")
    elif ctx.no_ragas:
        result.notes.append("RAGAS skipped (--no-ragas)")
    result.extra["ragas_block"] = ragas_block

    # --- measures -----------------------------------------------------------
    answered_rows = [r for r in result.rows if r.extra.get("kind") == "answered"]
    idk_rows = [r for r in result.rows if r.extra.get("kind") == "idk"]
    cited = sum(1 for r in answered_rows if r.scores.get("has_source"))
    right_section = sum(1 for r in answered_rows if r.scores.get("section_ok"))
    idk_ok = sum(1 for r in idk_rows if r.scores.get("idk_ok"))

    target = Measure("Policy answers cite a document", "target",
                     frac(cited, len(answered_rows)), "100%",
                     bool(answered_rows) and cited == len(answered_rows))
    result.measures.insert(0, target)
    result.measures.insert(1, Measure(
        "Policy answers citing the expected section", "bar",
        f"{frac(right_section, len(answered_rows))} ({pct(right_section, len(answered_rows))})",
        ">= 90%",
        bool(answered_rows) and right_section / len(answered_rows) >= 0.90))
    if idk_rows:
        result.measures.insert(2, Measure(
            "\"I don't know\" rows answered with the fixed message", "bar",
            frac(idk_ok, len(idk_rows)), f"{len(idk_rows)} of {len(idk_rows)}",
            idk_ok == len(idk_rows)))

    if result.error_rows:
        result.notes.append(
            f"rows that errored (counted as failures): {', '.join(result.error_rows)}")
    return result


def _avg_text(averages: dict, key: str) -> str:
    value = averages.get(key)
    return "n/a" if value is None else f"{value:.3f}"


def _lowest_rows(rows: list[Row]) -> list[dict]:
    """The three lowest-scoring answered rows (handout R6 check)."""
    import math

    scored = []
    for row in rows:
        vals = [v for k, v in row.scores.items()
                if k.startswith("ragas_") and isinstance(v, (int, float))
                and not math.isnan(v)]
        if vals:
            scored.append({"id": row.id, "question": row.input,
                           "mean": round(sum(vals) / len(vals), 4),
                           "scores": {k: v for k, v in row.scores.items()
                                      if k.startswith("ragas_")}})
    scored.sort(key=lambda r: r["mean"])
    return scored[:3]
