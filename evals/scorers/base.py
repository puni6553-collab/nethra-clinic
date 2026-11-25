"""Row / suite result types shared by the six suites (handout R3).

Every suite produces a `SuiteResult`: the rows with their outputs and
scores, and the measures (five targets + working bars) the report prints.
Results persist as JSON (the report merges the six) and as one CSV per
suite with every row's output and scores.
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

RESULTS = Path(__file__).resolve().parents[1] / "results"


@dataclass
class Row:
    id: str
    input: str = ""
    output: str = ""
    scores: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    latency_ms: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        """A row fails when it errors, or when any boolean score is False."""
        if self.error:
            return True
        return any(v is False for v in self.scores.values())


@dataclass
class Measure:
    name: str
    kind: str              # "target" | "bar" | "report"
    value: str             # e.g. "10/10"
    threshold: str         # e.g. "100%", "0", "report only"
    met: bool | None       # None -> report only (or not run)
    note: str = ""

    @property
    def label(self) -> str:
        if self.met is None:
            return "REPORT" if self.kind == "report" else "n/a"
        return "MET" if self.met else "MISSED"


@dataclass
class SuiteResult:
    suite: str
    size: str                       # small | large
    rows: list[Row] = field(default_factory=list)
    measures: list[Measure] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    started: str = ""
    finished: str = ""
    git_commit: str = ""
    model: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    # --- roll-ups -----------------------------------------------------------
    @property
    def failed_rows(self) -> list[str]:
        return [r.id for r in self.rows if r.failed]

    @property
    def error_rows(self) -> list[str]:
        return [r.id for r in self.rows if r.error]

    def tally(self, score: str) -> tuple[int, int]:
        """(passed, total) over the rows that carry this boolean score."""
        vals = [r.scores[score] for r in self.rows if score in r.scores]
        return sum(1 for v in vals if v is True), len(vals)

    def timed_latencies(self) -> list[float]:
        return [r.latency_ms for r in self.rows if r.latency_ms]

    # --- persistence --------------------------------------------------------
    @property
    def path(self) -> Path:
        return RESULTS / f"{self.suite}.json"

    @property
    def csv_path(self) -> Path:
        return RESULTS / f"{self.suite}.csv"

    def save(self) -> None:
        RESULTS.mkdir(parents=True, exist_ok=True)
        payload = {
            "suite": self.suite,
            "size": self.size,
            "started": self.started,
            "finished": self.finished,
            "git_commit": self.git_commit,
            "model": self.model,
            "measures": [asdict(m) for m in self.measures],
            "notes": self.notes,
            "extra": self.extra,
            "rows": [asdict(r) for r in self.rows],
        }
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                             encoding="utf-8")
        self._write_csv()

    def _write_csv(self) -> None:
        """One CSV per suite: every row's output and scores (R3)."""
        cols: list[str] = []
        for r in self.rows:
            for key in list(r.scores) + list(r.extra):
                if key not in cols:
                    cols.append(key)
        header = ["id", "input", "output", "error", "latency_ms"] + cols
        with open(self.csv_path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            for r in self.rows:
                writer.writerow(
                    [r.id, r.input, r.output, r.error,
                     "" if r.latency_ms is None else round(r.latency_ms, 1)]
                    + [r.scores.get(c, r.extra.get(c, "")) for c in cols]
                )

    @classmethod
    def load(cls, suite: str) -> "SuiteResult | None":
        path = RESULTS / f"{suite}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return cls(
            suite=data.get("suite", suite),
            size=data.get("size", "small"),
            rows=[Row(**r) for r in data.get("rows", [])],
            measures=[Measure(**m) for m in data.get("measures", [])],
            notes=list(data.get("notes", [])),
            started=data.get("started", ""),
            finished=data.get("finished", ""),
            git_commit=data.get("git_commit", ""),
            model=data.get("model", ""),
            extra=data.get("extra", {}),
        )

    def stamp(self, git_commit: str, model: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        if not self.started:
            self.started = now
        self.finished = now
        self.git_commit = git_commit
        self.model = model


def frac(n: int, d: int) -> str:
    return f"{n}/{d}"


def pct(n: int, d: int) -> str:
    return "n/a" if not d else f"{100.0 * n / d:.0f}%"
