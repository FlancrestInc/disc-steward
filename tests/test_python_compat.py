from __future__ import annotations

import ast
import io
import token
import tokenize
from pathlib import Path


def test_web_fstrings_are_compatible_with_pre_312_python() -> None:
    source = Path("disc_steward/web.py").read_text()
    ast.parse(source)
    fstring_depth = 0
    expression_depth = 0
    offenders: list[tuple[int, str]] = []

    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        token_name = token.tok_name[tok.type]
        if token_name == "FSTRING_START":
            fstring_depth += 1
            continue
        if token_name == "FSTRING_END":
            fstring_depth -= 1
            continue
        if fstring_depth == 0:
            continue

        if tok.type == tokenize.OP and tok.string == "{":
            expression_depth += 1
            continue
        if tok.type == tokenize.OP and tok.string == "}":
            expression_depth -= 1
            continue
        if expression_depth > 0 and "\\" in tok.string:
            offenders.append((tok.start[0], tok.string))

    assert offenders == []


def test_datetime_utc_constant_is_not_imported() -> None:
    for path in Path("disc_steward").glob("*.py"):
        tree = ast.parse(path.read_text())
        offenders = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "datetime"
            for alias in node.names
            if alias.name == "UTC"
        ]

        assert offenders == [], f"{path} imports datetime.UTC, which is unavailable on Python 3.10"
