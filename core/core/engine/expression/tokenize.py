"""Lexer for the closed-grammar ``$filter`` / ``$having`` parser.

Hand-rolled instead of via a generic tokenizer library — the grammar is
small and the lexer needs to handle a few specifics directly: doubled
single quotes inside string literals, leading-minus on numbers, and
keyword vs identifier distinction (the same lowercase characters can
mean either depending on whether they're in :data:`KEYWORDS`).
"""

import re
from typing import Any

from .ast import KEYWORDS, ExpressionError, Token

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER_RE = re.compile(r"-?\d+(\.\d+)?")


def tokenize(text: str) -> list[Token]:
    """Walk ``text`` character-by-character, emitting :class:`Token`.

    Whitespace is skipped; punctuation (``(``, ``)``, ``,``) becomes
    its own token; single-quoted strings honor the doubled-quote escape
    (``'O''Brien'`` → ``O'Brien``); numbers parse to ``int`` or
    ``float``; everything else is matched against the identifier regex
    and labelled ``keyword`` if the lowercased form is in :data:`KEYWORDS`,
    ``ident`` otherwise. Identifier case is normalized to lowercase so
    the parser doesn't need to compare case-insensitively downstream.
    """
    tokens: list[Token] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == "(":
            tokens.append(Token("lparen", "(", i))
            i += 1
            continue
        if c == ")":
            tokens.append(Token("rparen", ")", i))
            i += 1
            continue
        if c == ",":
            tokens.append(Token("comma", ",", i))
            i += 1
            continue
        if c == "'":
            start = i
            i += 1
            buf: list[str] = []
            closed = False
            while i < n:
                if text[i] == "'":
                    if i + 1 < n and text[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    closed = True
                    i += 1
                    break
                buf.append(text[i])
                i += 1
            if not closed:
                raise ExpressionError(f"unterminated string literal at position {start}")
            tokens.append(Token("string", "".join(buf), start))
            continue
        if c == "-" or c.isdigit():
            m = _NUMBER_RE.match(text, i)
            if m:
                raw = m.group(0)
                value: Any = float(raw) if "." in raw else int(raw)
                tokens.append(Token("number", value, i))
                i += len(raw)
                continue
            raise ExpressionError(f"unexpected '{c}' at position {i}")
        m = _IDENT_RE.match(text, i)
        if m:
            word = m.group(0)
            lower = word.lower()
            kind = "keyword" if lower in KEYWORDS else "ident"
            tokens.append(Token(kind, lower, i))
            i += len(word)
            continue
        raise ExpressionError(f"unexpected '{c}' at position {i}")
    return tokens
