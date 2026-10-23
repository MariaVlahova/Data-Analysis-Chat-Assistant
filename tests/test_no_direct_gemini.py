"""Plan 04: agent code never calls Gemini directly. Every model call goes through `LLMGateway.generate()`.

A direct SDK call skips the turn budget, the rate limiter, the breaker, the retry rules and the spans (brief R5, S3).
Only the `llm` package may import the Google SDK.
"""
import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
ALLOWED = SRC / "llm"


def files_outside_llm():
    return [path for path in SRC.rglob("*.py") if ALLOWED not in path.parents]


def genai_routes(source: str) -> list[str]:
    """Every way the source reaches the Google GenAI SDK, found in the syntax tree (a string that merely names it is no route)."""
    found: list[str] = []

    def is_route(name: str) -> bool:
        return name == "google.genai" or name.startswith("google.genai.")

    def dotted(node: ast.AST) -> str:
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        return ".".join(reversed(parts))

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if is_route(alias.name)]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            found += [f"{base}.{alias.name}" for alias in node.names if is_route(base) or is_route(f"{base}.{alias.name}")]
        elif isinstance(node, ast.Call):
            name = dotted(node.func)
            if name.endswith("import_module") or name == "__import__":
                literal = node.args[0] if node.args else None
                if isinstance(literal, ast.Constant) and isinstance(literal.value, str) and is_route(literal.value):
                    found.append(literal.value)
        elif isinstance(node, ast.Attribute) and is_route(dotted(node)):
            found.append(dotted(node))
    return found


def test_the_scan_sees_the_source_tree():
    files = files_outside_llm()
    assert len(files) > 10 and any(path.name == "main.py" for path in files)


@pytest.mark.parametrize("source", [
    "import google.genai",
    "import google.genai as g",
    "from google import genai",
    "from google import auth, genai",
    "from google.genai import types",
    "from google.genai.types import GenerateContentConfig",
    "import importlib\nimportlib.import_module('google.genai')",
    "import google\ngoogle.genai.Client()",
])
def test_the_scanner_finds_every_spelling_of_a_route_to_the_sdk(source):
    assert genai_routes(source), source


@pytest.mark.parametrize("source", [
    "import google.auth",
    "from google import auth",
    "from google.cloud import bigquery",
    "REQUIRED = [('google.genai', 'google-genai')]\nimportlib.import_module(REQUIRED[0][0])",
    "from src.llm import LLMGateway",
])
def test_the_scanner_leaves_alone_code_that_does_not_reach_the_sdk(source):
    assert genai_routes(source) == [], source


def test_no_file_outside_llm_reaches_the_google_sdk():
    offenders = {
        str(path.relative_to(SRC)): routes for path in files_outside_llm()
        if (routes := genai_routes(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}


def test_no_file_outside_llm_calls_generate_content():
    offenders = [str(path.relative_to(SRC)) for path in files_outside_llm() if "generate_content" in path.read_text(encoding="utf-8")]
    assert offenders == []
