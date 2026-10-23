"""The HTML part of every email, derived from its plain text (2026-10-23).

Every email the site sends is written as plain text, and its cards ("Course:", "Date:",
"Tee time:") align their values with spaces. That only lines up in a monospace font; Gmail
shows plain text in a proportional one, where "Date:" is narrower than "Tee time:", so the
values drifted by a space or two. ``html_from_text`` lays the SAME words out as HTML for the
``html`` part ACS sends beside ``plainText``: a card block becomes a two-column table, the
run summary's attempt lines a three-column one, everything else a paragraph with its line
breaks and indentation kept. The words never change (``tests/tenant/test_email_html.py`` pins
the HTML's text against every renderer's output), and the markup sets no colour or font: the
client themes it, dark mode included.

What becomes a table, and nothing else:

* A CARD block: a blank-line-delimited block whose first line is ``  Label:  value`` (two
  leading spaces, a label ending in a colon, two or more spaces, the value) and whose other
  lines are card rows or continuations (three or more leading spaces, as the operator
  notice's second window). A colon followed by ONE space (the summary's ``Brad: Mangrove
  Bay, …``) is not a card row.
* COLUMN lines inside any other block: six or more leading spaces and cells separated by two
  or more spaces (the summary's attempts). A run of them is one table.
"""

from __future__ import annotations

import html
import re

_CARD_ROW = re.compile(r"^ {2}(?P<label>\S[^:]*:) {2,}(?P<value>\S.*)$")
_CONTINUATION = re.compile(r"^ {3,}(?P<value>\S.*)$")
_COLUMN_INDENT = 6
_MIN_COLUMNS = 2
_CELL_GAP = re.compile(r" {2,}")
_URL = re.compile(r'https?://[^\s<>"]+')
_TRAILING_PUNCTUATION = ".,;:!?)"

_DOCUMENT = '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>{body}</body></html>'
_P = '<p style="margin:0 0 {bottom}">{text}</p>'
_TABLE = (
    '<table cellpadding="0" cellspacing="0" role="presentation" '
    'style="border-collapse:collapse;margin:0 0 {bottom} {left}">{rows}</table>'
)
_CELL = '<td style="padding:0 12px 2px 0;vertical-align:top;white-space:nowrap">{text}</td>'
_LAST_CELL = '<td style="padding:0 0 2px;vertical-align:top">{text}</td>'


def html_from_text(text: str) -> str:
    """The HTML document for a plain-text email body (see the module doc)."""
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    pieces: list[str] = []
    for block in blocks:
        lines = block.split("\n")
        rows = _card_rows(lines)
        if rows is not None:
            pieces.append(_table(rows, left="1em", last=True))
        else:
            pieces.extend(_text_block(lines))
    return _DOCUMENT.format(body="".join(pieces))


def _card_rows(lines: list[str]) -> list[list[str]] | None:
    first = _CARD_ROW.match(lines[0])
    if first is None:
        return None
    rows: list[list[str]] = []
    for line in lines:
        if m := _CARD_ROW.match(line):
            rows.append([m["label"], m["value"]])
        elif m := _CONTINUATION.match(line):
            rows.append(["", m["value"]])
        else:
            return None
    return rows


def _column_cells(line: str) -> list[str] | None:
    indent = len(line) - len(line.lstrip(" "))
    if indent < _COLUMN_INDENT:
        return None
    cells = _CELL_GAP.split(line.strip())
    return cells if len(cells) >= _MIN_COLUMNS else None


def _text_block(lines: list[str]) -> list[str]:
    """Paragraph(s) with ``<br>`` line breaks, each run of column lines as its own table."""
    groups: list[tuple[bool, list[str]]] = []  # (is a run of column lines, its lines)
    for line in lines:
        is_columns = _column_cells(line) is not None
        if groups and groups[-1][0] == is_columns:
            groups[-1][1].append(line)
        else:
            groups.append((is_columns, [line]))
    out: list[str] = []
    for i, (is_columns, items) in enumerate(groups):
        last = i == len(groups) - 1
        if is_columns:
            rows = [cells for line in items if (cells := _column_cells(line)) is not None]
            out.append(_table(rows, left="2em", last=last))
        else:
            text = "<br>".join(_line(line) for line in items)
            out.append(_P.format(bottom="1em" if last else "0", text=text))
    return out


def _table(rows: list[list[str]], *, left: str, last: bool) -> str:
    markup = []
    for cells in rows:
        tds = [_CELL.format(text=_inline(c)) for c in cells[:-1]]
        tds.append(_LAST_CELL.format(text=_inline(cells[-1])))
        markup.append(f"<tr>{''.join(tds)}</tr>")
    return _TABLE.format(bottom="1em" if last else "0", left=left, rows="".join(markup))


def _line(line: str) -> str:
    """One text line: its leading spaces kept as non-breaking ones (a browser collapses the
    plain kind, and the summary's hierarchy is its indentation), the rest escaped + linked."""
    stripped = line.lstrip(" ")
    return "&nbsp;" * (len(line) - len(stripped)) + _inline(stripped)


def _inline(text: str) -> str:
    """Escaped text with every URL a link (trailing punctuation stays outside the link)."""
    out: list[str] = []
    pos = 0
    for m in _URL.finditer(text):
        url = m.group(0).rstrip(_TRAILING_PUNCTUATION)
        out.append(html.escape(text[pos : m.start()], quote=False))
        out.append(f'<a href="{html.escape(url)}">{html.escape(url, quote=False)}</a>')
        pos = m.start() + len(url)
    out.append(html.escape(text[pos:], quote=False))
    return "".join(out)
