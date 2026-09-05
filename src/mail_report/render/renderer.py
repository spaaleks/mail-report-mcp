from __future__ import annotations

import html
import re
from collections.abc import Mapping

from . import theme

RULE = "#ececec"
CODE_BG = "#f3f3f3"
PRE_BG = "#1e1e1e"
PRE_FG = "#e6e6e6"

_EXPLICIT_PILL = re.compile(r"\[\[\s*([^\[\]\n|]{1,40}?)\s*(?:\|\s*([^\[\]\n|]{1,20}?)\s*)?\]\]")
_MARK = re.compile("\x00(\\d+)\x00")


def _pill(word: str, color: str) -> str:
    """`word` is already HTML-escaped, because every caller works on escaped text."""
    return (
        f'<span style="display:inline-block;background:{color};color:#fff;'
        f'font-size:11px;font-weight:700;letter-spacing:.3px;text-transform:uppercase;'
        f'padding:2px 8px;border-radius:10px;vertical-align:middle">{word}</span>'
    )


def _image_tag(alt: str, src: str) -> str:
    return (
        f'<img src="{src}" alt="{alt}" '
        f'style="display:block;max-width:100%;width:auto;height:auto;border:0;outline:none;'
        f'text-decoration:none;margin:12px 0;border-radius:4px">'
    )


def _missing_image(alt: str, name: str) -> str:
    label = alt or name
    return (
        f'<span style="display:inline-block;background:{CODE_BG};color:#8a4b00;border:1px dashed #d0a000;'
        f'padding:2px 8px;border-radius:4px;font-size:12px">missing image: {label}</span>'
    )


def _inline(
    s: str,
    accent: str,
    *,
    pills: bool = False,
    images: Mapping[str, str] | None = None,
) -> str:
    s = html.escape(s.replace("\x00", ""))
    held: list[str] = []

    def _hold(fragment: str) -> str:
        held.append(fragment)
        return f"\x00{len(held) - 1}\x00"

    # Code spans and explicit pills are lifted out before the remaining inline rules run, so
    # markup inside a code span stays literal and the auto-pill pass cannot wrap a rendered pill.
    code_style = (
        f"background:{CODE_BG};padding:1px 5px;border-radius:4px;"
        "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px"
    )
    s = re.sub(r"`([^`]+)`", lambda m: _hold(f'<code style="{code_style}">{m.group(1)}</code>'), s)

    def _explicit(match: re.Match) -> str:
        word = match.group(1)
        color = theme.pill_color(match.group(2) or "", html.unescape(word), accent)
        return _hold(_pill(word, color))

    s = _EXPLICIT_PILL.sub(_explicit, s)

    if images is not None:
        def _img(match: re.Match) -> str:
            alt, name = match.group(1), match.group(2)
            key = name.rsplit("/", 1)[-1]
            src = images.get(name) or images.get(key)
            return _image_tag(alt, src) if src else _missing_image(alt, key)

        s = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", _img, s)
    s = re.sub(
        r"\[([^\]]+)\]\((https?:[^)\s]+)\)",
        rf'<a href="\2" style="color:{accent};text-decoration:underline">\1</a>',
        s,
    )
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    if pills:
        colors, token = theme.pills()
        s = token.sub(lambda m: _pill(m.group(1), colors[m.group(1)]), s)
    if held:
        s = _MARK.sub(lambda m: held[int(m.group(1))], s)
    return s


def _table(rows: list[list[str]], accent: str) -> str:
    if not rows:
        return ""
    header = rows[0]
    body = rows[2:] if len(rows) >= 2 else []
    th_style = (
        "text-align:left;padding:7px 11px;border-bottom:2px solid #ddd;font-size:13px;color:#333"
    )
    th = "".join(f'<th style="{th_style}">{_inline(c.strip(), accent)}</th>' for c in header)
    trs = []
    for r in body:
        td_style = "padding:6px 11px;border-bottom:1px solid #eee;font-size:13px;vertical-align:top"
        tds = "".join(
            f'<td style="{td_style}">{_inline(c.strip(), accent, pills=True)}</td>'
            for c in r
        )
        trs.append(f"<tr>{tds}</tr>")
    return (
        '<table style="border-collapse:collapse;width:100%;margin:14px 0">'
        f"<thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table>"
    )


def _normalize_title(text: str) -> str:
    return " ".join(text.replace("*", "").replace("`", "").split()).casefold()


def _drop_duplicate_title(md: str, title: str | None) -> str:
    if not title:
        return md
    lines = md.split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if line.startswith("# ") and _normalize_title(line[2:]) == _normalize_title(title):
            del lines[i]
            while i < len(lines) and not lines[i].strip():
                del lines[i]
            return "\n".join(lines)
        return md
    return md


def render_markdown(
    md: str,
    *,
    title: str | None = None,
    subtitle: str | None = None,
    logo_src: str | None = None,
    logo_width: int = 100,
    images: Mapping[str, str] | None = None,
    accent: str | None = None,
) -> str:
    accent = accent or theme.accent()
    tint = theme.tint(accent)
    md = _drop_duplicate_title(md, title)
    out: list[str] = []
    in_code = False
    code_buf: list[str] = []
    tbl: list[list[str]] = []
    list_buf: list[str] = []

    def flush_table():
        nonlocal tbl
        if tbl:
            out.append(_table(tbl, accent))
            tbl = []

    def flush_list():
        nonlocal list_buf
        if list_buf:
            items = "".join(
                f'<li style="margin:3px 0;font-size:14px;line-height:1.55">{li}</li>'
                for li in list_buf
            )
            out.append(f'<ul style="margin:8px 0;padding-left:22px">{items}</ul>')
            list_buf = []

    for raw in md.split("\n"):
        line = raw.rstrip("\n")

        if line.strip().startswith("```"):
            if not in_code:
                flush_table()
                flush_list()
                in_code = True
                code_buf = []
            else:
                in_code = False
                body = html.escape("\n".join(code_buf))
                out.append(
                    f'<pre style="background:{PRE_BG};color:{PRE_FG};padding:13px 15px;'
                    f'border-radius:6px;overflow-x:auto;font-family:ui-monospace,SFMono-Regular,'
                    f'Menlo,monospace;font-size:12.5px;line-height:1.5;margin:12px 0">{body}</pre>'
                )
            continue
        if in_code:
            code_buf.append(line)
            continue

        if line.strip().startswith("|") and line.strip().endswith("|"):
            flush_list()
            inner = line.strip().strip("|")
            tbl.append([c.replace("\\|", "|") for c in re.split(r"(?<!\\)\|", inner)])
            continue
        flush_table()

        if re.match(r"^\s*[-*]\s+", line):
            list_buf.append(_inline(re.sub(r"^\s*[-*]\s+", "", line), accent, images=images))
            continue
        flush_list()

        if line.startswith("### "):
            out.append(
                f'<h3 style="font-size:15px;margin:20px 0 6px;color:#1a1a1a;'
                f'font-weight:700">{_inline(line[4:], accent, pills=True)}</h3>'
            )
        elif line.startswith("## "):
            out.append(
                f'<h2 style="font-size:18px;margin:24px 0 8px;color:{accent};'
                f'border-bottom:1px solid {RULE};padding-bottom:5px">{_inline(line[3:], accent)}</h2>'
            )
        elif line.startswith("# "):
            out.append(
                f'<h1 style="font-size:24px;margin:4px 0 12px;color:#111">'
                f"{_inline(line[2:], accent)}</h1>"
            )
        elif line.strip() == "---":
            out.append(f'<hr style="border:none;border-top:1px solid {RULE};margin:20px 0">')
        elif line.strip().startswith(">"):
            out.append(
                '<blockquote style="margin:10px 0;padding:8px 14px;border-left:3px solid '
                f'{accent};background:{tint};color:#444;font-size:14px">'
                f'{_inline(line.strip()[1:].strip(), accent)}</blockquote>'
            )
        elif line.strip() == "":
            out.append('<div style="height:8px"></div>')
        else:
            out.append(
                f'<p style="margin:8px 0;font-size:14px;line-height:1.62">'
                f"{_inline(line, accent, images=images)}</p>"
            )

    flush_table()
    flush_list()

    header_html = ""
    if logo_src:
        header_html += (
            f'<img src="{html.escape(logo_src, quote=True)}" width="{logo_width}" alt="" '
            f'style="display:block;width:{logo_width}px;max-width:100%;height:auto;'
            f'border:0;outline:none;text-decoration:none;margin:0 0 20px">'
        )
    if title:
        header_html += (
            f'<h1 style="font-size:25px;line-height:1.25;margin:0 0 6px;color:#111">'
            f"{html.escape(title)}</h1>"
        )
    if subtitle:
        header_html += (
            f'<p style="margin:0 0 18px;color:#777;font-size:13px">{html.escape(subtitle)}</p>'
        )
    if title or subtitle:
        header_html += f'<hr style="border:none;border-top:2px solid {accent};margin:0 0 30px">'

    body = header_html + "\n".join(out)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'></head>"
        '<body style="margin:0;background:#f6f6f6">'
        '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,'
        'sans-serif;color:#1a1a1a;max-width:840px;margin:0 auto;padding:26px 30px;background:#fff">'
        f"{body}"
        "</div></body></html>"
    )
