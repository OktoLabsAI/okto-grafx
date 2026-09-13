"""Turning query text into tokens, safely (CONTRACT.md section 8.9, section 11 item 5).

The lexer is the first door hostile input reaches, so every rule here is written against input
that is trying to break something rather than input that is trying to be a query. Four shapes get
named defences:

* **Unterminated literals.** A string or a block comment that never closes is refused at the
  character where it began, with the position of the opening quote, rather than consuming the
  rest of the text and failing somewhere unrelated.
* **Enormous numbers.** A digit run is bounded before it is converted, because building an
  integer from a very long digit string is refused by CPython with a ``ValueError`` -- a
  non-``Grafx*`` escape from a public door -- and is expensive on the way there.
* **Runaway size.** The text and the token count are both bounded, so the memory a caller can
  make this component allocate is a function of the limits and not of the input.
* **Pathological backtracking.** There is none to have: the scanner is a single forward pass with
  no regular expression engine behind it, so every character is examined once and the cost is
  linear in the length of the text by construction. That is also why ``re`` appears nowhere in
  this component -- a catastrophic pattern is a hang, and a hang is a blocking defect.

The scanner is deliberately strict about what may appear OUTSIDE a literal: only ASCII. A
non-ASCII character in a query is either inside a string, inside back quotes, or a mistake, and
the third case is much more common than a caller expects. Inside a string or back quotes any
character is accepted, because the text a caller stores is not this component's business.
"""

from __future__ import annotations

from math import isfinite

from okto_grafx.domain.errors import GrafxParseError
from okto_grafx.domain.query.limits import (
    MAX_NAME_CHARACTERS,
    MAX_NUMBER_CHARACTERS,
    MAX_QUERY_CHARACTERS,
    MAX_STRING_CHARACTERS,
    MAX_TOKENS,
)
from okto_grafx.domain.query.tokens import SYMBOLS, Token, TokenKind

__all__ = [
    "INTEGER_MAGNITUDE_LIMIT",
    "STRING_ESCAPES",
    "tokenize",
]

INTEGER_MAGNITUDE_LIMIT: int = 1 << 63
"""The largest magnitude a numeric literal may carry before a sign is applied.

It is one above ``INT64_MAX`` on purpose. The most negative 64-bit integer is written as a minus
sign in front of a magnitude that is itself out of range, so refusing the magnitude here would
make ``-9223372036854775808`` unwritable. The parser applies the sign and then refuses a value
that does not fit, which is a different input and carries its own test.
"""

STRING_ESCAPES: dict[str, str] = {
    "'": "'",
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
"""The single-character escapes a string literal accepts, beside the numeric ``u`` and ``U``."""

_QUOTES: frozenset[str] = frozenset({"'", '"'})
_BACK_QUOTE: str = "`"
_HEX_DIGITS: frozenset[str] = frozenset("0123456789abcdefABCDEF")
_DIGITS: frozenset[str] = frozenset("0123456789")
_SURROGATE_FIRST: int = 0xD800
_SURROGATE_LAST: int = 0xDFFF
_MAX_CODE_POINT: int = 0x10FFFF


def _refuse(
    message: str, *, line: int, column: int, offset: int, **details: object
) -> GrafxParseError:
    """Return the parse failure for a lexical problem, located at a character."""
    return GrafxParseError(
        f"{message} (line {line}, column {column})",
        line=line,
        column=column,
        offset=offset,
        **details,
    )


class _Scanner:
    """A forward cursor over the query text that knows which line and column it is on."""

    __slots__ = ("_text", "_length", "_index", "_line", "_line_start")

    def __init__(self, text: str) -> None:
        self._text = text
        self._length = len(text)
        self._index = 0
        self._line = 1
        self._line_start = 0

    @property
    def index(self) -> int:
        """Return the offset of the next character."""
        return self._index

    @property
    def line(self) -> int:
        """Return the one-based line the cursor is on."""
        return self._line

    @property
    def column(self) -> int:
        """Return the one-based column the cursor is on."""
        return self._index - self._line_start + 1

    @property
    def done(self) -> bool:
        """Return True when there is nothing left to read."""
        return self._index >= self._length

    def peek(self, ahead: int = 0) -> str:
        """Return the character that far ahead, or the empty string past the end."""
        position = self._index + ahead
        if position >= self._length:
            return ""
        return self._text[position]

    def advance(self, count: int = 1) -> str:
        """Consume that many characters and return them, stopping at the end of the text."""
        stop = min(self._index + count, self._length)
        taken = self._text[self._index : stop]
        for character in taken:
            if character == "\n":
                self._line += 1
                self._line_start = self._index + 1
            self._index += 1
        return taken

    def starts_with(self, literal: str) -> bool:
        """Return True when the text at the cursor begins with that literal."""
        return self._text.startswith(literal, self._index)


def tokenize(text: str) -> tuple[Token, ...]:
    """Return the tokens of one query, refusing anything the dialect cannot read.

    The returned tuple always ends with a single :attr:`TokenKind.END` token, so a parser never
    has to check whether there is a next token before looking at it -- which is the ordinary way
    a hand-written parser grows an ``IndexError`` on truncated input.
    """
    source = _require_text(text)
    scanner = _Scanner(source)
    tokens: list[Token] = []
    while True:
        _skip_ignorable(scanner)
        if scanner.done:
            break
        if len(tokens) >= MAX_TOKENS:
            raise _refuse(
                f"A query may carry at most {MAX_TOKENS} tokens",
                line=scanner.line,
                column=scanner.column,
                offset=scanner.index,
                field="tokens",
                value=MAX_TOKENS,
            )
        tokens.append(_next_token(scanner))
    tokens.append(
        Token(
            kind=TokenKind.END,
            text="",
            end_offset=scanner.index,
            offset=scanner.index,
            line=scanner.line,
            column=scanner.column,
        )
    )
    return tuple(tokens)


def _require_text(text: object) -> str:
    """Return the query text after refusing a non-string and an oversized one."""
    if not isinstance(text, str):
        raise GrafxParseError(
            f"A query is written as text; got {type(text).__name__}.",
            field="text",
            value=type(text).__name__,
        )
    if len(text) > MAX_QUERY_CHARACTERS:
        raise GrafxParseError(
            f"A query may carry at most {MAX_QUERY_CHARACTERS} characters; got {len(text)}.",
            field="text",
            value=len(text),
        )
    return text


def _skip_ignorable(scanner: _Scanner) -> None:
    """Consume whitespace and comments until the next thing that means something."""
    while not scanner.done:
        character = scanner.peek()
        if character.isspace():
            scanner.advance()
            continue
        if scanner.starts_with("//"):
            while not scanner.done and scanner.peek() != "\n":
                scanner.advance()
            continue
        if scanner.starts_with("/*"):
            _skip_block_comment(scanner)
            continue
        return


def _skip_block_comment(scanner: _Scanner) -> None:
    """Consume a block comment, refusing one that never closes."""
    line = scanner.line
    column = scanner.column
    offset = scanner.index
    scanner.advance(2)
    while not scanner.done:
        if scanner.starts_with("*/"):
            scanner.advance(2)
            return
        scanner.advance()
    raise _refuse(
        "A block comment opened here and was never closed",
        line=line,
        column=column,
        offset=offset,
        field="comment",
    )


def _next_token(scanner: _Scanner) -> Token:
    """Return the token that starts at the cursor."""
    character = scanner.peek()
    if character == "$":
        return _read_parameter(scanner)
    if character == _BACK_QUOTE:
        return _read_quoted_name(scanner)
    if character in _QUOTES:
        return _read_string(scanner)
    if character in _DIGITS:
        return _read_number(scanner)
    if character == "." and scanner.peek(1) in _DIGITS:
        return _read_number(scanner)
    if character.isalpha() or character == "_":
        return _read_name(scanner)
    return _read_symbol(scanner)


def _read_name(scanner: _Scanner) -> Token:
    """Read an unquoted word, which the parser may later read as a keyword."""
    line, column, offset = scanner.line, scanner.column, scanner.index
    characters: list[str] = []
    while not scanner.done:
        character = scanner.peek()
        if character.isascii() and (character.isalnum() or character == "_"):
            characters.append(scanner.advance())
            continue
        if not character.isascii() and (character.isalnum() or character == "_"):
            raise _refuse(
                "An unquoted name may hold ASCII letters, digits and underscores only; write "
                "it between back quotes to use other characters",
                line=scanner.line,
                column=scanner.column,
                offset=scanner.index,
                field="name",
            )
        break
    name = "".join(characters)
    _require_name_length(name, line=line, column=column, offset=offset)
    return Token(
        kind=TokenKind.NAME,
        text=name,
        end_offset=scanner.index,
        offset=offset,
        line=line,
        column=column,
        value=name,
    )


def _read_quoted_name(scanner: _Scanner) -> Token:
    """Read a back-quoted name, which is never read as a keyword."""
    line, column, offset = scanner.line, scanner.column, scanner.index
    scanner.advance()
    characters: list[str] = []
    while True:
        if scanner.done:
            raise _refuse(
                "A back-quoted name opened here and was never closed",
                line=line,
                column=column,
                offset=offset,
                field="name",
            )
        character = scanner.advance()
        if character == _BACK_QUOTE:
            if scanner.peek() == _BACK_QUOTE:
                characters.append(scanner.advance())
                continue
            break
        if len(characters) >= MAX_NAME_CHARACTERS:
            raise _refuse(
                f"A name may carry at most {MAX_NAME_CHARACTERS} characters",
                line=line,
                column=column,
                offset=offset,
                field="name",
                value=MAX_NAME_CHARACTERS,
            )
        characters.append(character)
    name = "".join(characters)
    # Empty quoted text is a legitimate map key. Whether this token names a
    # variable, schema object or map key belongs to parsing/semantic admission.
    return Token(
        kind=TokenKind.NAME,
        text=name,
        offset=offset,
        line=line,
        column=column,
        value=name,
        quoted=True,
        end_offset=scanner.index,
    )


def _require_name_length(name: str, *, line: int, column: int, offset: int) -> None:
    """Refuse a name longer than the schema identifier rule allows."""
    if len(name) > MAX_NAME_CHARACTERS:
        raise _refuse(
            f"A name may carry at most {MAX_NAME_CHARACTERS} characters; got {len(name)}",
            line=line,
            column=column,
            offset=offset,
            field="name",
            value=len(name),
        )


def _read_parameter(scanner: _Scanner) -> Token:
    """Read a named, quoted or decimal-integer parameter reference without renaming it."""
    line, column, offset = scanner.line, scanner.column, scanner.index
    scanner.advance()
    if scanner.peek() == _BACK_QUOTE:
        inner = _read_quoted_name(scanner)
        return Token(
            kind=TokenKind.PARAMETER,
            text=inner.text,
            end_offset=scanner.index,
            offset=offset,
            line=line,
            column=column,
            value=inner.text,
        )
    characters: list[str] = []
    while not scanner.done:
        character = scanner.peek()
        if character.isascii() and (character.isalnum() or character == "_"):
            characters.append(scanner.advance())
            continue
        break
    name = "".join(characters)
    if not name or (name[0] in _DIGITS and not name.isdecimal()):
        raise _refuse(
            "A parameter requires a name or decimal integer after the dollar sign, as in $limit or $1",
            line=line,
            column=column,
            offset=offset,
            field="parameter",
        )
    _require_name_length(name, line=line, column=column, offset=offset)
    return Token(
        kind=TokenKind.PARAMETER,
        text=name,
        end_offset=scanner.index,
        offset=offset,
        line=line,
        column=column,
        value=name,
    )


def _read_number(scanner: _Scanner) -> Token:
    """Read an integer or a double, refusing one too long to convert or not finite."""
    if scanner.peek() == "0" and scanner.peek(1) in ("x", "X", "o"):
        return _read_based_integer(scanner)
    line, column, offset = scanner.line, scanner.column, scanner.index
    characters: list[str] = []
    is_double = False

    def take() -> None:
        """Consume one character of the literal, refusing one that has grown too long."""
        if len(characters) >= MAX_NUMBER_CHARACTERS:
            raise _refuse(
                f"A numeric literal may carry at most {MAX_NUMBER_CHARACTERS} characters",
                line=line,
                column=column,
                offset=offset,
                field="number",
                value=MAX_NUMBER_CHARACTERS,
            )
        characters.append(scanner.advance())

    while not scanner.done and scanner.peek() in _DIGITS:
        take()
    if scanner.peek() == "." and scanner.peek(1) != ".":
        is_double = True
        take()
        while not scanner.done and scanner.peek() in _DIGITS:
            take()
    if scanner.peek() in ("e", "E"):
        following = scanner.peek(1)
        exponent_digit = following in _DIGITS or (
            following in ("+", "-") and scanner.peek(2) in _DIGITS
        )
        if exponent_digit:
            is_double = True
            take()
            if scanner.peek() in ("+", "-"):
                take()
            while not scanner.done and scanner.peek() in _DIGITS:
                take()
    literal = "".join(characters)
    if is_double:
        return _double_token(literal, line=line, column=column, offset=offset)
    return _integer_token(literal, line=line, column=column, offset=offset)


def _double_token(literal: str, *, line: int, column: int, offset: int) -> Token:
    """Return the token of a floating-point literal, refusing one that is not finite."""
    number = float(literal)
    if not isfinite(number):
        raise _refuse(
            f"The literal {literal} is outside the range a double can hold",
            line=line,
            column=column,
            offset=offset,
            field="number",
            value=literal,
        )
    return Token(
        kind=TokenKind.DOUBLE,
        text=literal,
        end_offset=offset + len(literal),
        offset=offset,
        line=line,
        column=column,
        value=number,
    )


def _read_based_integer(scanner: _Scanner) -> Token:
    """Read reference hexadecimal/octal spellings without rounding through DOUBLE.

    The lexer admits magnitude 2**63 so a following parser sign can form INT64_MIN.
    All other overflow and invalid digits are rejected before any statement effects.
    """
    line, column, offset = scanner.line, scanner.column, scanner.index
    prefix = scanner.advance(2)
    base = 16 if prefix[1] in ("x", "X") else 8
    allowed = _HEX_DIGITS if base == 16 else frozenset("01234567")
    characters: list[str] = []
    while not scanner.done and (scanner.peek().isalnum() or scanner.peek() == "_"):
        if scanner.index - offset >= MAX_NUMBER_CHARACTERS:
            raise _refuse(f"A numeric literal may carry at most {MAX_NUMBER_CHARACTERS} characters",
                          line=line, column=column, offset=offset, field="number",
                          value=MAX_NUMBER_CHARACTERS)
        characters.append(scanner.advance())
    digits = "".join(characters)
    valid = bool(digits) and digits[-1] != "_" and "__" not in digits
    valid = valid and all(character in allowed or character == "_" for character in digits)
    if not valid or not digits.replace("_", ""):
        raise _refuse("Invalid based integer literal", line=line, column=column, offset=offset,
                      field="number_literal", value=prefix + digits)
    return _integer_token(prefix + digits, line=line, column=column, offset=offset, base=base)


def _integer_token(literal: str, *, line: int, column: int, offset: int, base: int = 10) -> Token:
    """Return the token of an integer literal, refusing a magnitude no signed word can hold."""
    if base == 10:
        # Convert at most 19 significant digits. A longer finite DOUBLE spelling
        # is legal, but increasing its lexical budget must not permit expensive
        # decimal integer conversion or leak CPython's host-configured ValueError.
        significant = literal.lstrip("0") or "0"
        bound = str(INTEGER_MAGNITUDE_LIMIT)
        oversized = len(significant) > len(bound) or (len(significant) == len(bound) and significant > bound)
        number = INTEGER_MAGNITUDE_LIMIT + 1 if oversized else int(significant)
    else:
        number = int(literal, base)
    if number > INTEGER_MAGNITUDE_LIMIT:
        raise _refuse(
            f"The literal {literal} is outside the range a 64-bit integer can hold",
            line=line,
            column=column,
            offset=offset,
            field="number",
            value=literal,
        )
    return Token(
        kind=TokenKind.INTEGER,
        text=literal,
        end_offset=offset + len(literal),
        offset=offset,
        line=line,
        column=column,
        value=number,
    )


def _read_string(scanner: _Scanner) -> Token:
    """Read a quoted string, resolving its escapes and refusing an unterminated one."""
    line, column, offset = scanner.line, scanner.column, scanner.index
    quote = scanner.advance()
    characters: list[str] = []
    while True:
        if scanner.done:
            raise _refuse(
                "A string literal opened here and was never closed",
                line=line,
                column=column,
                offset=offset,
                field="string",
            )
        character = scanner.advance()
        if character == quote:
            break
        if character == "\\":
            characters.append(_read_escape(scanner, line=line, column=column, offset=offset))
        else:
            characters.append(character)
        if len(characters) > MAX_STRING_CHARACTERS:
            raise _refuse(
                f"A string literal may carry at most {MAX_STRING_CHARACTERS} characters",
                line=line,
                column=column,
                offset=offset,
                field="string",
                value=MAX_STRING_CHARACTERS,
            )
    body = "".join(characters)
    _require_storable_string(body, line=line, column=column, offset=offset)
    return Token(
        kind=TokenKind.STRING,
        text=body,
        end_offset=scanner.index,
        offset=offset,
        line=line,
        column=column,
        value=body,
    )


def _require_storable_string(body: str, *, line: int, column: int, offset: int) -> None:
    """Refuse a string literal carrying a lone surrogate, however it was written.

    One check covers both ways a surrogate can arrive -- pasted into the query as a raw character
    and written as a numeric escape -- because two checks for one rule would each hide the
    other's failure (amendment A67). The rule is not this component's taste: the domain model
    stores a string as UTF-8 and no UTF-8 encoding of a lone surrogate exists, so accepting one
    here would push the refusal into the write path with the offending position long gone.
    """
    for position, character in enumerate(body):
        if _SURROGATE_FIRST <= ord(character) <= _SURROGATE_LAST:
            raise _refuse(
                f"The string literal carries the surrogate {ord(character):#06x} at position "
                f"{position}, which no stored string can hold",
                line=line,
                column=column,
                offset=offset,
                field="string",
                value=ord(character),
            )


def _read_escape(scanner: _Scanner, *, line: int, column: int, offset: int) -> str:
    """Return the character one backslash escape stands for."""
    if scanner.done:
        raise _refuse(
            "A string literal opened here and was never closed",
            line=line,
            column=column,
            offset=offset,
            field="string",
        )
    marker = scanner.advance()
    simple = STRING_ESCAPES.get(marker)
    if simple is not None:
        return simple
    if marker == "u":
        return _read_code_point(scanner, digits=4, line=line, column=column, offset=offset)
    if marker == "U":
        return _read_code_point(scanner, digits=8, line=line, column=column, offset=offset)
    raise _refuse(
        f"A string literal has no escape \\{marker}",
        line=scanner.line,
        column=scanner.column,
        offset=scanner.index,
        field="escape",
        value=marker,
    )


def _read_code_point(
    scanner: _Scanner, *, digits: int, line: int, column: int, offset: int
) -> str:
    """Return the character a numeric escape names, refusing one beyond the last code point.

    Surrogates are NOT judged here. They are judged once, over the assembled literal, by
    :func:`_require_storable_string`, so that a pasted surrogate and an escaped one meet the same
    rule and neither check can mask the other.
    """
    taken: list[str] = []
    for _ in range(digits):
        character = scanner.peek()
        if character not in _HEX_DIGITS:
            raise _refuse(
                f"A numeric string escape needs {digits} hexadecimal digits",
                line=scanner.line,
                column=scanner.column,
                offset=scanner.index,
                field="escape",
                value="".join(taken),
            )
        taken.append(scanner.advance())
    code_point = int("".join(taken), 16)
    if code_point > _MAX_CODE_POINT:
        raise _refuse(
            f"The escape names {code_point:#x}, which is beyond the last character",
            line=line,
            column=column,
            offset=offset,
            field="escape",
            value=code_point,
        )
    return chr(code_point)


def _read_symbol(scanner: _Scanner) -> Token:
    """Read one punctuation symbol, refusing a character the dialect has no meaning for."""
    line, column, offset = scanner.line, scanner.column, scanner.index
    for symbol in SYMBOLS:
        if scanner.starts_with(symbol):
            scanner.advance(len(symbol))
            return Token(
                kind=TokenKind.SYMBOL,
                text=symbol,
                end_offset=scanner.index,
                offset=offset,
                line=line,
                column=column,
                value=symbol,
            )
    character = scanner.advance()
    raise _refuse(
        f"The character {character!r} has no meaning in a query",
        line=line,
        column=column,
        offset=offset,
        field="character",
        value=character,
        reason="unsupported_query_character", query_phase="planning",
    )
