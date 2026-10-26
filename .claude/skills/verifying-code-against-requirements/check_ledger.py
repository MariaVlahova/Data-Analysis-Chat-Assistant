"""Checks a requirements ledger for the verifying-code-against-requirements skill.

The ledger is a markdown table (see requirements-ledger.md). This script does not judge the code. It checks that the review
was complete and that its evidence is real:

  extract stage   every quote is found in its source, every word of the brief and of the client Q&A sits under a quote
                  (or under a CTX row that says why it is not a requirement), and every NFR, risk, assumption, decision,
                  prototype stand-in and answered question of decision.md has a row.
  final stage     adds: every row has a class, a verdict that fits the class, and evidence of the kind the verdict needs.
                  Each cited line exists, each cited test passed in the pytest run given with --junit, and the evidence
                  of a deliverable is tracked by git. It also lists the code of the whole working tree (not the git diff)
                  that no row cites, so unrequested behaviour and unreviewed folders show.

  --merge FILE    writes the verdict, evidence and note of a returned table into the ledger, row by row, by ID.
  --init-decision prints the rows decision.md expects, to start or to complete a ledger.

Exit 0 means the coverage is 100%. It says nothing about whether the code meets the requirements: rows that are not
satisfied are listed as findings. Stdlib only. Run it with the project's Python from the repository root.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

SOURCES = {
    "brief": "docs/requirements/Original_Requirements.md",
    "qa": "docs/requirements/client-QA.md",
    "decision": "docs/decision.md",
}
STRICT = ("brief", "qa")  # every word of these must sit under a quote
STANDARD = ["id", "src", "quote", "class", "look", "verdict", "evidence", "note"]

ALLOWED = {
    "PROTO": {"MET", "PARTIAL", "NOT-MET", "OVERCLAIM", "LIVE-UNPROVEN", "CONFLICT"},
    "DESIGN": {"MET", "DESIGN-OK", "PARTIAL", "NOT-MET", "OVERCLAIM", "CONFLICT"},
    "DELIV": {"MET", "PARTIAL", "NOT-MET", "OVERCLAIM", "CONFLICT"},
    "OOS": {"OOS-OK", "PARTIAL", "OVERCLAIM", "CONFLICT"},
    "CTX": {"EXCLUDED"},
}
SATISFIED = {"MET", "DESIGN-OK", "OOS-OK"}
FINDING_ORDER = ["OVERCLAIM", "NOT-MET", "CONFLICT", "PARTIAL", "LIVE-UNPROVEN"]

WORD = re.compile(r"\w+")
REF = re.compile(r"^([^:\s]+):(\d+)(?:-(\d+))?$")
TEST = re.compile(r"^([^:\s]+\.py)::(\S+)$")
ELLIPSIS = re.compile(r"…|\.\.\.")
RULER = re.compile(r":?-{2,}:?")
SEARCH_SCOPE = re.compile(r"\b(src|eval|evalkit|tests|scenarios|golden|personas|docs|repo|repository|tree)\b|\*\.\w+|/", re.I)


@dataclass
class Row:
    id: str
    src: str
    quote: str
    klass: str
    look: str
    verdict: str
    evidence: str
    note: str
    line: int

    def render(self) -> str:
        cells = [self.id, self.src, self.quote, self.klass, self.look, self.verdict, self.evidence, self.note]
        cells = [" ".join(c.split()).replace("|", "\\|") for c in cells]
        return "|" + "|".join(f" {c} " if c else " " for c in cells) + "|"


def words(text: str) -> list[str]:
    return WORD.findall(text.lower().replace("’", "").replace("'", ""))


def source_words(text: str) -> list[tuple[str, int]]:
    return [(w, n) for n, line in enumerate(text.splitlines(), 1) for w in words(line)]


def split_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", body)]


def parse_ledger(path: Path) -> tuple[list[Row], list[tuple[str, str]]]:
    rows: list[Row] = []
    errors: list[tuple[str, str]] = []
    header_ok = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip().startswith("|"):
            continue
        cells = split_row(line)
        if cells and cells[0].lower() == "id":
            header_ok = [c.lower() for c in cells] == STANDARD
            if not header_ok:
                errors.append((f"line {number}", f"table header must be: {' | '.join(STANDARD)}"))
            continue
        if all(RULER.fullmatch(c) for c in cells if c):
            continue
        if not header_ok:
            continue
        if len(cells) != len(STANDARD):
            errors.append((f"line {number}", f"row has {len(cells)} cells, expected {len(STANDARD)} "
                                             "(a | inside a cell is written \\|)"))
            continue
        cell = dict(zip(STANDARD, cells))
        quote = cell["quote"]
        if len(quote) > 1 and quote[0] in "\"“" and quote[-1] in "\"”":
            quote = quote[1:-1]
        rows.append(Row(cell["id"], cell["src"].lower(), quote, cell["class"].upper(), cell["look"],
                        cell["verdict"].upper(), cell["evidence"], cell["note"], number))
    return rows, errors


def find_run(tokens: list[str], needle: list[str], start: int) -> int:
    size = len(needle)
    for i in range(start, len(tokens) - size + 1):
        if tokens[i:i + size] == needle:
            return i
    return -1


def quote_parts(quote: str) -> list[list[str]]:
    return [p for p in (words(p) for p in ELLIPSIS.split(quote)) if p]


def locate(quote: str, tokens: list[str], covered: list[bool]) -> tuple[int, int] | None:
    """Token span of a quote. `a … b` spans from the first words to the next occurrence of the last words.
    Of several places the quote fits, the first one with a word no row has covered yet."""
    parts = quote_parts(quote)
    if not parts:
        return None
    fallback = None
    start = find_run(tokens, parts[0], 0)
    while start != -1:
        end, ok = start + len(parts[0]), True
        for part in parts[1:]:
            at = find_run(tokens, part, end)
            if at == -1:
                ok = False
                break
            end = at + len(part)
        if ok:
            span = (start, end)
            if not all(covered[start:end]):
                return span
            fallback = fallback or span
        start = find_run(tokens, parts[0], start + 1)
    return fallback


def decision_items(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """The rows decision.md expects the review to cover, and the open questions it does not ask us to verify."""
    items: list[tuple[str, str]] = []
    open_questions: list[str] = []
    in_prototype_table, seen_header = False, False
    for line in text.splitlines():
        if re.match(r"^##\s+8\.", line):
            in_prototype_table, seen_header = True, False
            continue
        if re.match(r"^##\s", line):
            in_prototype_table = False
        if in_prototype_table and line.startswith("|"):
            cells = split_row(line)
            if not seen_header:
                seen_header = True
            elif not all(RULER.fullmatch(c) for c in cells if c):
                items.append((f"P8-{sum(1 for i in items if i[0].startswith('P8-')) + 1}", cells[0]))
            continue
        heading = re.match(r"^###\s+(3\.\d+)\s+(.+?)\s*(\[[A-Z ]+\])?\s*$", line)
        if heading:
            items.append((f"DM-{heading.group(1)}", heading.group(2)))
            continue
        cells = split_row(line) if line.startswith("|") else []
        if len(cells) >= 3 and re.fullmatch(r"NFR\d+|A\d+", cells[0]):
            items.append((cells[0], cells[1]))
        elif len(cells) >= 4 and re.fullmatch(r"S\d+", cells[0]) and cells[1] in ("High", "Medium", "Low"):
            items.append((cells[0], cells[2]))
        elif len(cells) >= 5 and re.fullmatch(r"Q\d+", cells[0]):
            if cells[-1] == "Answered":
                items.append((cells[0], cells[1]))
            else:
                open_questions.append(cells[0])
    return items, open_questions


def load_junit(path: Path) -> dict[tuple[str, str], str]:
    results: dict[tuple[str, str], str] = {}
    for case in ET.parse(path).iter("testcase"):
        key = (case.get("classname", ""), (case.get("name") or "").split("[")[0])
        if case.find("failure") is not None or case.find("error") is not None:
            status = "failed"
        elif case.find("skipped") is not None:
            status = "skipped"
        else:
            status = "passed"
        old = results.get(key)
        results[key] = "failed" if "failed" in (old, status) else "passed" if "passed" in (old, status) else status
    return results


def test_status(junit: dict[tuple[str, str], str], path: str, name: str) -> str:
    module = path[:-3].replace("/", ".")
    found = {s for (cls, n), s in junit.items() if n == name and (cls == module or cls.startswith(module + "."))}
    return "failed" if "failed" in found else "passed" if "passed" in found else "skipped" if found else "missing"


def evidence_tokens(cell: str) -> list[str]:
    tokens = []
    for chunk in re.split(r";|<br\s*/?>|\n", cell):
        chunk = chunk.strip().strip("`").strip()
        if not chunk:
            continue
        tokens.append(chunk if chunk.lower().startswith("search:") else chunk.split()[0].strip("`,"))
    return tokens


def tracked_files(root: Path) -> set[str] | None:
    try:
        done = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return set(done.stdout.splitlines())


class Checker:
    def __init__(self, root: Path, junit: dict[tuple[str, str], str] | None, tracked: set[str] | None):
        self.root, self.junit, self.tracked = root, junit, tracked
        self.lines: dict[str, int] = {}
        self.errors: list[tuple[str, str]] = []
        self.bad_rows: set[str] = set()

    def fail(self, row_id: str, message: str) -> None:
        self.errors.append((row_id, message))
        self.bad_rows.add(row_id)

    def line_count(self, path: Path) -> int:
        key = str(path)
        if key not in self.lines:
            self.lines[key] = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        return self.lines[key]

    def is_tracked(self, path: str) -> bool:
        return self.tracked is None or path in self.tracked or any(t.startswith(path.rstrip("/") + "/") for t in self.tracked)

    def classify(self, row: Row, token: str) -> str | None:
        """The kind of an evidence token (search, test, doc, code, bare), or None after recording why it is invalid."""
        if token.lower().startswith("search:"):
            if len(token) < 15:
                self.fail(row.id, f"search evidence says too little: {token!r}")
                return None
            if not SEARCH_SCOPE.search(token):
                self.fail(row.id, f"search evidence must name where it looked (folders such as src/ eval/ scenarios/, "
                                  f"or 'whole tree'): {token!r}")
                return None
            return "search"
        token = token.replace("\\", "/").removeprefix("./")
        test = TEST.match(token)
        if test:
            path, name = test.group(1), test.group(2).split("::")[-1]
            if not (self.root / path).is_file():
                self.fail(row.id, f"test file not found: {path}")
                return None
            status = test_status(self.junit, path, name) if self.junit is not None else "unchecked"
            if status == "unchecked":
                self.fail(row.id, f"{token}: cites a test but no --junit run was given")
            elif status != "passed":
                self.fail(row.id, f"{token}: test {status} in the pytest run")
            else:
                return "test"
            return None
        ref = REF.match(token)
        path = ref.group(1) if ref else token
        target = self.root / path
        if path.startswith("/") or ":" in path or not target.exists():
            self.fail(row.id, f"evidence is not a path:line, path::test, existing path or search: {token!r}")
            return None
        if ref:
            if not target.is_file():
                self.fail(row.id, f"{token}: path:line needs a file")
                return None
            first, last = int(ref.group(2)), int(ref.group(3) or ref.group(2))
            if not 1 <= first <= last <= self.line_count(target):
                self.fail(row.id, f"{token}: line outside the file ({self.line_count(target)} lines)")
                return None
        if row.klass == "DELIV" and not self.is_tracked(path):
            self.fail(row.id, f"{path} is not tracked by git, so it is not in the repository that is shared "
                              "(--allow-untracked for a run before the commit)")
            return None
        return "doc" if path.endswith(".md") else "code" if ref else "bare"

    def check_final(self, row: Row) -> None:
        if row.klass not in ALLOWED:
            self.fail(row.id, f"class must be one of {', '.join(ALLOWED)} (got {row.klass or 'empty'})")
            return
        if row.verdict not in ALLOWED[row.klass]:
            self.fail(row.id, f"verdict {row.verdict or 'empty'} does not fit class {row.klass} "
                              f"(allowed: {', '.join(sorted(ALLOWED[row.klass]))})")
            return
        if row.verdict == "EXCLUDED":
            return
        before = len(self.errors)
        kinds = [k for k in (self.classify(row, t) for t in evidence_tokens(row.evidence)) if k]
        if len(self.errors) > before:
            return
        have = Counter(kinds)
        if row.verdict not in SATISFIED and len(row.note) < 15:
            self.fail(row.id, f"{row.verdict} needs a note that says what is missing or in conflict")
        if not kinds:
            self.fail(row.id, "no evidence")
        elif row.verdict == "MET":
            if not (have["code"] or have["bare"] or (row.klass == "DELIV" and have["doc"])):
                self.fail(row.id, "MET needs a code or file reference (a document describing the code is not evidence)")
            if row.klass == "PROTO" and not have["test"]:
                self.fail(row.id, "MET on a prototype requirement needs a test that passed in the --junit run")
        elif row.verdict == "DESIGN-OK" and not have["doc"]:
            self.fail(row.id, "DESIGN-OK needs a reference into the design documents (path.md:line)")
        elif row.verdict == "OOS-OK" and not (have["search"] and have["doc"]):
            self.fail(row.id, "OOS-OK needs a search: token (nothing builds it) and the document line that excludes it")
        elif row.verdict == "OVERCLAIM" and not (have["doc"] and (have["code"] or have["test"] or have["search"])):
            self.fail(row.id, "OVERCLAIM needs the document line that claims it and the code, test or search that contradicts it")


def check(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    rows, errors = parse_ledger(Path(args.ledger))
    junit = load_junit(Path(args.junit)) if args.junit else None
    tracked = None if args.allow_untracked else tracked_files(root)
    checker = Checker(root, junit, tracked)
    checker.errors.extend(errors)

    texts = {}
    for name, rel in SOURCES.items():
        path = root / rel
        texts[name] = path.read_text(encoding="utf-8") if path.is_file() else None
    seen: set[str] = set()
    for row in rows:
        if not row.id or row.id in seen:
            checker.fail(row.id or f"line {row.line}", "missing or duplicate ID")
        seen.add(row.id)
    covered = {name: [False] * len(source_words(texts[name])) for name in STRICT if texts[name] is not None}
    tokens = {name: [w for w, _ in source_words(texts[name])] for name in covered}
    decision_tokens = [w for w, _ in source_words(texts["decision"])] if texts["decision"] is not None else []
    for name in STRICT:
        if texts[name] is None:
            checker.fail(name, f"source not found: {SOURCES[name]}")

    for row in rows:
        if row.src not in SOURCES:
            checker.fail(row.id, f"Src must be one of {', '.join(SOURCES)} (got {row.src or 'empty'})")
            continue
        if texts[row.src] is None:
            checker.fail(row.id, f"source not found: {SOURCES[row.src]}")
            continue
        if row.klass == "CTX" and len(row.note) < 10:
            checker.fail(row.id, "a CTX row (not a requirement) needs a note that says why")
        if row.src in covered:
            span = locate(row.quote, tokens[row.src], covered[row.src])
            if span is None:
                checker.fail(row.id, f"quote not found in {SOURCES[row.src]}: {row.quote[:60]!r}")
            else:
                covered[row.src][span[0]:span[1]] = [True] * (span[1] - span[0])
        else:
            parts = quote_parts(row.quote)
            if not parts or any(find_run(decision_tokens, p, 0) == -1 for p in parts):
                checker.fail(row.id, f"quote not found in {SOURCES[row.src]}: {row.quote[:60]!r}")

    gaps = []
    for name in covered:
        numbered = source_words(texts[name])
        run: list[int] = []
        for i, done in enumerate(covered[name] + [True]):
            if not done:
                run.append(i)
            elif run:
                if len(run) >= args.min_run:
                    gaps.append((name, numbered[run[0]][1], " ".join(numbered[j][0] for j in run)))
                run = []
    for name, line, text in gaps:
        checker.errors.append((f"{name}:{line}", f"source text under no row: {text[:90]!r}"))

    open_questions: list[str] = []
    if texts["decision"] is not None and not args.no_decision:
        items, open_questions = decision_items(texts["decision"])
        for item_id, _ in items:
            if item_id not in seen:
                checker.fail(item_id, "decision.md item has no ledger row (--init-decision lists the rows)")

    uncited: tuple[list[str], int] = ([], 0)
    if args.stage == "final":
        for row in rows:
            checker.check_final(row)
        uncited = uncited_code(root, rows, [d for d in args.code_dirs.split(",") if d])
        if args.strict_code and uncited[0]:
            checker.errors.append(("code", f"{len(uncited[0])} code file(s) are cited by no row, so no requirement was traced to them"))

    report(rows, checker, covered, gaps, open_questions, uncited, args)
    return 1 if checker.errors else 0


def uncited_code(root: Path, rows: list[Row], dirs: list[str]) -> tuple[list[str], int]:
    """The .py files of the whole working tree under `dirs` that no row cites as evidence, and how many there are.
    It reads the tree, not the git diff: a file the branch never touched is in scope like any other."""
    cited: set[str] = set()
    for row in rows:
        for token in evidence_tokens(row.evidence):
            token = token.replace("\\", "/").removeprefix("./")
            ref = REF.match(token)
            cited.add((ref.group(1) if ref else token).rstrip("/"))
    files = sorted(p.relative_to(root).as_posix() for d in dirs if (root / d).is_dir() for p in (root / d).rglob("*.py")
                   if p.name != "__init__.py" and "__pycache__" not in p.parts)
    return [f for f in files if f not in cited and not any(f.startswith(c + "/") for c in cited)], len(files)


def report(rows: list[Row], checker: Checker, covered, gaps, open_questions, uncited, args) -> None:
    ok = [r for r in rows if r.id not in checker.bad_rows]
    print(f"LEDGER {args.ledger} - stage {args.stage}")
    print(f"rows {len(rows)}, without a problem {len(ok)}")
    for name, flags in covered.items():
        done = sum(flags)
        print(f"source {name}: {done}/{len(flags)} words under a row ({100 * done / max(len(flags), 1):.1f}%), "
              f"{sum(g[0] == name for g in gaps)} uncovered run(s) of {args.min_run}+ words")
    if args.no_decision:
        print("decision.md items: NOT CHECKED (--no-decision), so this run does not cover decision.md")
    if open_questions:
        print(f"open questions of decision.md, not ledgered (they are not expectations): {', '.join(open_questions)}")
    if args.stage == "final":
        counts = Counter(r.verdict for r in rows)
        print("verdicts: " + ", ".join(f"{v or 'EMPTY'} {n}" for v, n in sorted(counts.items(), key=lambda kv: -kv[1])))
        for src in SOURCES:
            scored = [r for r in rows if r.src == src and r.klass != "CTX"]
            if scored:
                done = sum(r.verdict in SATISFIED and r.id not in checker.bad_rows for r in scored)
                print(f"satisfied (MET, DESIGN-OK, OOS-OK) in {src}: {done}/{len(scored)}"
                      f" requirement rows; {sum(r.src == src and r.klass == 'CTX' for r in rows)} context row(s) excluded")
        for verdict in FINDING_ORDER:
            for r in (r for r in rows if r.verdict == verdict and r.id not in checker.bad_rows):
                print(f"  {verdict:<13} {r.id:<7} {r.quote[:60]!r} - {r.note[:110]}")
        for r in (r for r in rows if r.verdict == "EXCLUDED"):
            print(f"  EXCLUDED      {r.id:<7} {r.quote[:60]!r} - {r.note[:110]}")
        missing, total = uncited
        by_folder: dict[str, list[str]] = {}
        for f in missing:
            folder, _, name = f.rpartition("/")
            by_folder.setdefault(folder, []).append(name)
        print(f"code that no row cites ({args.code_dirs}): {len(missing)} of {total} .py files. "
              "Account for each folder in the report: support code, or behaviour no requirement asks for")
        for folder, names in list(by_folder.items())[:args.limit]:
            print(f"  {folder} ({len(names)}): {', '.join(names)}")
    if checker.errors:
        print(f"\n{len(checker.errors)} PROBLEM(S) - coverage is NOT complete:")
        for row_id, message in checker.errors[:args.limit]:
            print(f"  {row_id}: {message}")
        if len(checker.errors) > args.limit:
            print(f"  ... {len(checker.errors) - args.limit} more (--limit 0 shows all)")
    else:
        print(f"\nCOVERAGE 100% at the {args.stage} stage")


def init_decision(root: Path) -> int:
    path = root / SOURCES["decision"]
    if not path.is_file():
        print(f"not found: {path}", file=sys.stderr)
        return 2
    items, _ = decision_items(path.read_text(encoding="utf-8"))
    print("| " + " | ".join(h.upper() if h == "id" else h.capitalize() for h in STANDARD) + " |")
    print("|" + "---|" * len(STANDARD))
    for item_id, quote in items:
        print(Row(item_id, "decision", quote, "?", "", "", "", "", 0).render())
    return 0


def merge(ledger: Path, update: Path) -> int:
    rows, errors = parse_ledger(ledger)
    by_id = {r.id: r for r in rows}
    lines = ledger.read_text(encoding="utf-8").splitlines()
    merged, header = 0, None
    for number, line in enumerate(update.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip().startswith("|"):
            continue
        cells = split_row(line)
        if cells and cells[0].lower() == "id":
            header = [c.lower() for c in cells]
            if "verdict" not in header:
                errors.append((f"{update.name}:{number}", "the returned table needs a Verdict column"))
                header = None
            continue
        if header is None or all(RULER.fullmatch(c) for c in cells if c):
            continue
        cell = dict(zip(header, cells + [""] * (len(header) - len(cells))))
        row = by_id.get(cell["id"])
        if row is None:
            errors.append((f"{update.name}:{number}", f"unknown ID {cell['id']!r}"))
            continue
        row.verdict = cell["verdict"].upper()
        row.evidence = cell.get("evidence", row.evidence)
        row.note = cell.get("note", row.note)
        if cell.get("class"):
            row.klass = cell["class"].upper()
        lines[row.line - 1] = row.render()
        merged += 1
    if errors:
        for where, message in errors:
            print(f"  {where}: {message}")
        print("nothing written")
        return 1
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    waiting = [r.id for r in by_id.values() if not r.verdict]
    print(f"merged {merged} row(s) into {ledger}; {len(waiting)} row(s) still without a verdict")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("ledger", nargs="?", help="the ledger markdown file")
    parser.add_argument("--stage", choices=("extract", "final"), default="final")
    parser.add_argument("--junit", help="junit XML of the pytest run the evidence cites (pytest --junitxml=FILE)")
    parser.add_argument("--root", default=".", help="repository root (default: the current directory)")
    parser.add_argument("--min-run", type=int, default=1, help="uncovered words in a row that count as a gap (1: every word)")
    parser.add_argument("--limit", type=int, default=60, help="problems to print (0: all)")
    parser.add_argument("--allow-untracked", action="store_true", help="accept deliverable evidence that git does not track")
    parser.add_argument("--code-dirs", default="src,eval,evalkit", help="folders whose .py files the code-not-cited list covers")
    parser.add_argument("--strict-code", action="store_true", help="fail when a .py file under --code-dirs is cited by no row")
    parser.add_argument("--no-decision", action="store_true",
                        help="do not require a row for every item of decision.md (only when the user limits the review)")
    parser.add_argument("--merge", metavar="FILE", help="merge a returned table of ID, Verdict, Evidence, Note into the ledger")
    parser.add_argument("--init-decision", action="store_true", help="print the rows decision.md expects, then stop")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.init_decision:
        return init_decision(Path(args.root).resolve())
    if not args.ledger:
        parser.error("the ledger file is required")
    if args.merge:
        return merge(Path(args.ledger), Path(args.merge))
    args.limit = args.limit or 10 ** 9
    return check(args)


if __name__ == "__main__":
    sys.exit(main())
