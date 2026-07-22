"""Parser for OP.GG MCP's compact class-repr response format.

Every OP.GG MCP tool that requires `desired_output_fields` (which is every stats
tool draftiq needs -- champion analysis, synergies, lane meta, leaderboard) does NOT
return JSON. It returns a bespoke text format that declares field names for a
handful of classes once, then serializes the actual data as bare positional
constructor calls, e.g.:

    class WeakCounter: champion_name,play,win,win_rate

    LolGetChampionAnalysis(Data([WeakCounter("Singed",927,408,0.56)]))

This is presumably a token-efficiency measure for LLM consumption (declare field
names once instead of repeating JSON keys per object). This module turns that text
into plain Python data (nested dicts/lists/scalars) so the rest of the OP.GG provider
never has to think about the wire format again.

Confirmed live against the real MCP server (see CLAUDE.md for how and when). If
OP.GG changes this format, `parse()` raises `OpggFormatError` loudly rather than
silently returning wrong data -- there is no fallback to guessing field order.
"""

from __future__ import annotations

from typing import Any


class OpggFormatError(ValueError):
    """Raised when an OP.GG response doesn't match the expected compact-repr grammar."""


def parse(text: str) -> Any:
    """Parses a full OP.GG MCP tool response body (optional class declarations
    followed by one trailing data expression) into nested dicts/lists/scalars.
    Class instances become dicts keyed by their declared field names."""
    header, sep, data_expr = text.strip().partition("\n\n")
    if sep:
        classes = _parse_class_declarations(header)
    else:
        classes = {}
        data_expr = header
    parser = _Parser(data_expr.strip(), classes)
    value = parser.parse_value()
    parser.expect_end()
    return value


def _parse_class_declarations(header: str) -> dict[str, list[str]]:
    classes: dict[str, list[str]] = {}
    for line in header.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith("class "):
            raise OpggFormatError(f"expected a 'class Name: fields' line, got: {line!r}")
        name, _, fields = line[len("class ") :].partition(":")
        name = name.strip()
        field_names = [f.strip() for f in fields.split(",") if f.strip()]
        classes[name] = field_names
    return classes


_LITERALS = {"true": True, "false": False, "null": None, "None": None}


class _Parser:
    def __init__(self, text: str, classes: dict[str, list[str]]) -> None:
        self._text = text
        self._classes = classes
        self._pos = 0

    def _peek(self) -> str:
        if self._pos >= len(self._text):
            raise OpggFormatError("unexpected end of input")
        return self._text[self._pos]

    def _skip_ws(self) -> None:
        while self._pos < len(self._text) and self._text[self._pos] in " \t\n\r":
            self._pos += 1

    def expect_end(self) -> None:
        self._skip_ws()
        if self._pos != len(self._text):
            raise OpggFormatError(
                f"trailing data at position {self._pos}: {self._text[self._pos : self._pos + 40]!r}"
            )

    def parse_value(self) -> Any:
        self._skip_ws()
        ch = self._peek()
        if ch == '"':
            return self._parse_string()
        if ch == "[":
            return self._parse_list()
        if ch == "-" or ch.isdigit():
            return self._parse_number()
        for literal, value in _LITERALS.items():
            if self._text[self._pos : self._pos + len(literal)] == literal:
                self._pos += len(literal)
                return value
        if ch.isalpha() or ch == "_":
            return self._parse_class_instance()
        raise OpggFormatError(f"unexpected character {ch!r} at position {self._pos}")

    def _parse_string(self) -> str:
        self._pos += 1  # opening quote
        chars: list[str] = []
        escapes = {"\\": "\\", '"': '"', "n": "\n", "t": "\t"}
        while True:
            if self._pos >= len(self._text):
                raise OpggFormatError("unterminated string literal")
            ch = self._text[self._pos]
            if ch == '"':
                self._pos += 1
                return "".join(chars)
            if ch == "\\":
                self._pos += 1
                if self._pos >= len(self._text):
                    raise OpggFormatError("unterminated escape sequence")
                escaped = self._text[self._pos]
                chars.append(escapes.get(escaped, escaped))
                self._pos += 1
            else:
                chars.append(ch)
                self._pos += 1

    def _parse_number(self) -> int | float:
        start = self._pos
        if self._text[self._pos] == "-":
            self._pos += 1
        is_float = False
        while self._pos < len(self._text) and (
            self._text[self._pos].isdigit() or self._text[self._pos] in ".eE+-"
        ):
            if self._text[self._pos] in ".eE":
                is_float = True
            self._pos += 1
        raw = self._text[start : self._pos]
        return float(raw) if is_float else int(raw)

    def _parse_list(self) -> list[Any]:
        self._pos += 1  # opening bracket
        items: list[Any] = []
        self._skip_ws()
        if self._pos < len(self._text) and self._text[self._pos] == "]":
            self._pos += 1
            return items
        while True:
            items.append(self.parse_value())
            self._skip_ws()
            ch = self._peek()
            if ch == ",":
                self._pos += 1
                continue
            if ch == "]":
                self._pos += 1
                return items
            raise OpggFormatError(f"expected ',' or ']' at position {self._pos}, got {ch!r}")

    def _parse_identifier(self) -> str:
        start = self._pos
        while self._pos < len(self._text) and (
            self._text[self._pos].isalnum() or self._text[self._pos] == "_"
        ):
            self._pos += 1
        if self._pos == start:
            raise OpggFormatError(f"expected an identifier at position {self._pos}")
        return self._text[start : self._pos]

    def _parse_class_instance(self) -> dict[str, Any]:
        name = self._parse_identifier()
        self._skip_ws()
        if self._pos >= len(self._text) or self._text[self._pos] != "(":
            raise OpggFormatError(f"expected '(' after class name {name!r} at position {self._pos}")
        self._pos += 1
        fields = self._classes.get(name)
        if fields is None:
            raise OpggFormatError(f"undeclared class {name!r} referenced at position {self._pos}")
        values: list[Any] = []
        self._skip_ws()
        if self._pos < len(self._text) and self._text[self._pos] == ")":
            self._pos += 1
        else:
            while True:
                values.append(self.parse_value())
                self._skip_ws()
                ch = self._peek()
                if ch == ",":
                    self._pos += 1
                    self._skip_ws()
                    continue
                if ch == ")":
                    self._pos += 1
                    break
                raise OpggFormatError(f"expected ',' or ')' at position {self._pos}, got {ch!r}")
        if len(values) != len(fields):
            raise OpggFormatError(
                f"class {name!r} declared {len(fields)} fields {fields} but got "
                f"{len(values)} values"
            )
        return dict(zip(fields, values, strict=True))
