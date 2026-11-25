"""Score check_red_flags against every row of reason_samples.csv (pack 2).

Run (from backend/, server not required):

    ..\\venv\\Scripts\\python tests\\check_redflag.py [offset] [limit]

Prints one line per sample plus a summary: true positives, false alarms,
misses and accuracy. A miss (red flag predicted as no) is the failure that
matters; a false alarm only costs Staff one click.

The free-tier quota is ~20 requests/day, so with 35 samples pass
`0 20` on day one and `20` on day two; the summary then covers only
the rows you asked for.
"""
from __future__ import annotations

import csv
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402
from app.llm import LlmUnavailable, check_red_flags  # noqa: E402

SAMPLES = os.path.join(config.PACK2_DIR, "reason_samples.csv")

PASS: list[str] = []
FAIL: list[str] = []


def load_samples() -> list[dict]:
    if not os.path.exists(SAMPLES):
        sys.exit(f"reason_samples.csv not found at {SAMPLES}")
    with open(SAMPLES, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    rows = load_samples()
    offset = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else len(rows)
    rows = rows[offset : offset + limit]
    if not rows:
        sys.exit(f"no rows in {SAMPLES} at offset {offset}")
    print(f"Scoring {len(rows)} samples ({offset}..) from {SAMPLES}\n")

    tp = fp = tn = fn = errors = 0
    cat_ok = cat_bad = 0

    for row in rows:
        sid = row["sample_id"]
        reason = row["reason"]
        expected = (row.get("expected_red_flag") or "").strip().lower() == "yes"
        want_cat = (row.get("category") or "").strip() or None

        try:
            result = check_red_flags(reason)
        except LlmUnavailable as exc:
            errors += 1
            label = f"ERROR {sid}: {exc}"
            FAIL.append(label)
            print(f"  ERROR  {sid}  -> {exc}")
            # Free-tier quota is tiny (5/min, 20/day). If we hit it, wait out
            # the window rather than burning the rest of the run on errors.
            text = str(exc)
            if "429" in text or "RESOURCE_EXHAUSTED" in text or "quota" in text.lower():
                print("  ... rate limit hit; sleeping 65s before continuing")
                time.sleep(65)
            continue

        got = bool(result.red_flag)
        got_cat = result.category
        ok = got == expected
        if expected and got:
            tp += 1
            if want_cat and got_cat == want_cat:
                cat_ok += 1
            elif want_cat:
                cat_bad += 1
        elif expected and not got:
            fn += 1
        elif not expected and got:
            fp += 1
        else:
            tn += 1

        detail = f"got={got} category={got_cat} why={result.why!r}"
        if ok and (not expected or not want_cat or got_cat == want_cat):
            PASS.append(sid)
            print(f"  PASS  {sid}  -> {detail}")
        else:
            FAIL.append(sid)
            print(f"  FAIL  {sid}  expected={'yes' if expected else 'no'}/{want_cat}  -> {detail}")

    total = tp + fp + tn + fn
    acc = (tp + tn) / total if total else 0.0
    print("\n--- summary ---")
    print(f"  pass {len(PASS)} / {len(rows)}")
    print(f"  true positives (caught red flags):     {tp}")
    print(f"  false alarms (no -> yes):              {fp}")
    print(f"  MISSED red flags (yes -> no):          {fn}   <-- must be 0")
    print(f"  correct non-flags:                     {tn}")
    print(f"  category correct when yes:             {cat_ok}/{tp + cat_bad}")
    print(f"  model errors:                          {errors}")
    print(f"  accuracy:                              {acc:.1%}")
    if fn:
        print("\n  At least one red flag was missed — tune the prompt in app/llm.py.")
    return 1 if (fn or errors or FAIL) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
