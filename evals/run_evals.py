"""Iteration 5's runner: six suites, one command (handout R3).

    python run_evals.py                      # every suite, small sets
    python run_evals.py --suite red_flags    # just one
    python run_evals.py --large              # the large sets
    python run_evals.py --no-ragas           # while building (quota)
    python run_evals.py --recalibrate        # re-check the judge
    python run_evals.py --upload-datasets    # golden sets into Opik first

Every suite writes `evals/results/<suite>.json` + `<suite>.csv`, is recorded
as an experiment in Opik when an `OPIK_API_KEY` is present, and the run ends
by rebuilding `evals/results/summary.md`. The exit code is non-zero when a
brief target is missed or a suite crashes, so CI can gate on it.
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
import time
from dataclasses import dataclass

from . import harness, opik_rec

ALIASES = {
    "red_flags": "red_flags", "red-flags": "red_flags",
    "routing": "routing",
    "policy": "policy_questions", "policy_questions": "policy_questions",
    "safety": "safety",
    "summary": "summary_cases", "summary_cases": "summary_cases",
    "booking": "booking",
}
ORDER = ["red_flags", "routing", "policy_questions", "safety",
         "summary_cases", "booking"]
MODULES = {
    "red_flags": "red_flags",
    "routing": "routing",
    "policy_questions": "policy",
    "safety": "safety",
    "summary_cases": "summary",
    "booking": "booking",
}
CHAT_SUITES = {"red_flags", "routing", "safety"}
P20 = "P20"   # Aditya Bose — no appointments, the pack's chat patient


@dataclass
class Ctx:
    """What every scorer reads instead of reaching for globals."""
    large: bool = False
    no_ragas: bool = False
    recalibrate: bool = False
    base: str = ""
    patient_token: str = ""
    patient_id: str = P20

    def size_label(self) -> str:
        return harness.size_label(self.large)


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_evals.py",
        description="Run NetraCare's iteration 5 eval suites.")
    parser.add_argument("--suite", action="append", default=[],
                        metavar="NAME",
                        help="red_flags | routing | policy | safety | "
                             "summary | booking (repeatable; default: all)")
    parser.add_argument("--large", action="store_true",
                        help="run the -large golden sets")
    parser.add_argument("--no-ragas", action="store_true",
                        help="skip RAGAS in the policy suite (saves quota)")
    parser.add_argument("--recalibrate", action="store_true",
                        help="re-run the safety judge on judge_calibration.csv")
    parser.add_argument("--upload-datasets", action="store_true",
                        help="refresh the golden sets in Opik before running")
    return parser.parse_args(argv)


def _resolve(names: list[str]) -> list[str]:
    if not names:
        return list(ORDER)
    out: list[str] = []
    for raw in names:
        key = ALIASES.get(raw.strip().lower())
        if key is None:
            raise SystemExit(
                f"unknown suite {raw!r} — choose from: "
                + ", ".join(sorted(ALIASES)))
        if key not in out:
            out.append(key)
    # keep the handout's order regardless of how they were typed
    return [s for s in ORDER if s in out]


def _start_chat_server(ctx: Ctx) -> harness.EvalServer | None:
    """One shared server for the chat sets, logged in as P20."""
    server = harness.EvalServer(harness.CHAT_DB)
    try:
        server.start()
    except Exception as exc:  # noqa: BLE001
        print(f"  !! chat server failed to start: {exc}")
        return None
    ctx.base = server.base
    staff = harness.login(ctx.base, "staff", name="Reception")
    person = harness.patient_by_id(ctx.base, staff or "", P20)
    if person is None:
        print(f"  !! {P20} is not in the seeded patient directory")
        return None
    token = harness.login(ctx.base, "patient",
                          mobile=person["mobile"], name=person["name"])
    if not token:
        print(f"  !! could not log in as {P20}")
        return None
    ctx.patient_token = token
    ctx.patient_id = P20
    return server


def _print_measures(result) -> None:
    for m in result.measures:
        print(f"  [{m.label:<6}] {m.name}: {m.value} (want {m.threshold})")
    for note in result.notes:
        print(f"  note: {note}")


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    suites = _resolve(args.suite)
    harness.configure()          # before any `app` import — clock, DB, path

    from app import config       # noqa: E402 — loads .env now

    if config.LLM_MODEL == "gemini-3.5-flash":
        print("  !! LLM_MODEL is gemini-3.5-flash — its daily quota runs out "
              "fast; prefix with $env:LLM_MODEL='gemini-3.5-flash-lite'")

    ctx = Ctx(large=args.large, no_ragas=args.no_ragas,
              recalibrate=args.recalibrate)

    if "policy_questions" in suites and not os.path.isdir(config.CHROMA_DIR):
        print("  !! clinic-documents index is missing — run "
              "`python backend/build_index.py` before the policy suite")

    if args.upload_datasets:
        print("Opik datasets: " + opik_rec.upload_datasets())
    ok, why = opik_rec.available()
    print("Opik: " + why)

    git = harness.git_commit()
    model = config.LLM_MODEL
    server: harness.EvalServer | None = None
    if set(suites) & CHAT_SUITES:
        print(f"chat server on :{harness.PORT} as {P20} ...")
        server = _start_chat_server(ctx)
        if server is None:
            return 2

    targets_missed: list[str] = []
    crashed: list[str] = []
    try:
        for suite in suites:
            started = time.time()
            print(f"\n=== {suite} ({ctx.size_label()}) ===")
            module = importlib.import_module(
                f".scorers.{MODULES[suite]}", package=__package__)
            try:
                result = module.run(ctx)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 — one suite never stops the run
                crashed.append(suite)
                print(f"  !! suite crashed: {exc}")
                continue
            result.stamp(git, model)
            status = opik_rec.record_experiment(result, f"netracare-{suite}")
            result.extra["opik"] = status
            result.save()
            _print_measures(result)
            print(f"  saved {result.path.name} + {result.csv_path.name} "
                  f"in {time.time() - started:.0f}s · {status}")
            for m in result.measures:
                if m.kind == "target" and m.met is False:
                    targets_missed.append(
                        f"{suite}: {m.name} = {m.value} (want {m.threshold})")
    finally:
        if server is not None:
            server.stop()

    from . import report
    report.build()
    print(f"\nreport: {report.PATH}")

    if targets_missed:
        print("\nMISSED targets:")
        for line in targets_missed:
            print(f"  - {line}")
    if crashed:
        print("\nCRASHED suites:")
        for line in crashed:
            print(f"  - {line}")
    if targets_missed or crashed:
        return 1
    print("\nall targets met this run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
