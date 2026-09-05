_HEAD = """\
# Mail-Report style guide

You are writing a report that will be rendered to HTML and emailed. Write it in **Markdown**, using only this supported subset (anything else won't render):

- `#` title, `##` section, `###` finding/sub-heading
- `**bold**`, `` `inline code` ``, fenced ```code blocks```
- `| pipe | tables |` (with a `|---|---|` separator row)
- `- ` bullet lists
- `> ` blockquotes, `---` horizontal rules
- `[text](https://link)` links

Write each paragraph on **one line**. The renderer starts a new paragraph at every newline, so hard-wrapping at 80 columns breaks one paragraph into several with gaps between them.

## Every value must already be resolved before you send

`body`, `subject`, `title` and `subtitle` are literal text. This server renders Markdown, it does not run a shell and it does not expand templates. `$(date +%F)`, `` `date` ``, `${USER}`, `%s` and `{{today}}` are copied straight into the email and the recipient reads them exactly as you typed them.

So do the work first, then write the answer down. Need today's date in the subtitle? Run `date +%F` as its own step, read the output, and put `2026-09-04` in the string. The same goes for hostnames, commit SHAs, durations, counts and totals: resolve each one, then paste the resolved value into the text.

The one place a `$(...)` belongs is inside a code span or fenced code block that is showing a command to the reader. There it is the content, not a placeholder, and it renders as written.

`send_report` enforces this: a subject, title, subtitle or body still holding a `$(...)`, `${...}` or `{{...}}` outside code is refused with the offending tokens listed, so nothing goes out half-written. Resolve the value and call again.

## The shape that reads well (security-audit flavour)

1. **Title.** Use `# <Subject> — Round vN` or similar. One line. If you also pass a `title` argument with the same text, the duplicate heading is dropped for you.
2. **Verdict first.** Open with the bottom line in 1–2 sentences, then a compact summary **table** of findings:

   ```
   | ID | Sev | Title |
   |----|-----|-------|
   | EMB-v7-001 | High | <short title> |
   ```

Put the severity word (Critical / High / Medium / Low / Info) in its own cell or set off with ` — High — ` em-dashes, and the renderer turns those into a coloured pill automatically. Order findings by severity, highest first.
3. **Findings.** One `###` per finding: `### EMB-v7-001 — High — <title>`, then prose with **Repro:** (a fenced code block of the exact request/response or commands), **Impact:**, and **Fix:**. Be concrete and specific, and show the evidence rather than asserting it.
4. **Controls that held.** A section listing what you attacked and what correctly resisted, each with the evidence. This matters as much as findings.
5. **Cleanup / Net.** What you created and removed, and a one-paragraph bottom line.

## Tone

- Lead with the conclusion. Every sentence should add information.
- Prefer evidence (a request, a response, a file:line) over adjectives.
- Severity is honest: don't inflate Info to look productive, don't bury a real High. State diminishing returns plainly when that's the truth.

## Reports with images or files: send a bundle

Files never travel inside the tool call. When the report has screenshots, diagrams or attachments, build a zip and upload it in one go:

```
report.md      <- the report, in this markdown subset
login.png      <- referenced from report.md as ![login flow](login.png)
console.png
findings.pdf   <- not referenced, so it rides along as an attachment
```

1. Call `request_upload_ticket` once for the whole batch, not once per file.
2. `POST` the zip to the upload URL with the ticket in the `X-Upload-Ticket` header.
3. Pass the returned `bundle_id` to `send_report`. Leave `body` empty, because report.md is the body.

Every image the markdown references with `![alt](name.png)` is embedded in the message, so the recipient sees it in place without downloading anything. Files the markdown does not reference are attached normally. Reference images by bare filename, exactly as they are named in the zip.

For a report with no files at all, just pass `body` and skip the upload entirely.

## Recipients

"""

_RECIPIENTS_DEFAULT = """\
Leave `to` alone unless the user named an address. The server usually knows where the report goes, either routed per API key or from its own default. If `send_report` has no `to` parameter at all, the recipient is fixed and there is nothing to decide."""

_RECIPIENTS_REQUIRED = """\
This server has no default recipient, so every report needs an address and it has to come from you: pass `to` to `send_report`. If the user has not named one, ask them for it before you write the report. Never guess an address, and never reuse one from an earlier or unrelated task. Sending without `to` comes back as an error telling you to ask, not as a delivered mail.

The one exception is a client or gateway that pins the address by sending the `x-mail-report-to` header, the way it sends the auth token. When that header is set, `to` is optional and the report goes where the header says.
"""

_TAIL = """\

## Sending

Call `send_report` with a `subject` and either a `bundle_id` or a markdown `body`. The subject should front-load the verdict, e.g. `"… v7 (blind) — no Critical/High; boundary held"`. Use `preview_report` first if you want to eyeball the HTML before sending. It returns the rendered HTML rather than sending anything.

What you pass is what the recipient gets. Read the arguments back once before you call: if any of them still contains a placeholder, a `$(...)`, a `${...}` or a value you meant to look up, resolve it now, because there is no substitution step after this one.
"""


def _pills_section() -> str:
    from . import theme

    try:
        colors, _ = theme.pills()
    except theme.ThemeError:
        colors = dict(theme.SEVERITY_PILLS)
    extra = [w for w in colors if w not in theme.SEVERITY_PILLS]
    automatic = (
        "Severity words (Critical, High, Medium, Low, Info) become coloured pills on their own, "
        "but only inside a table body cell or a `###` heading. In ordinary prose they stay text, "
        "which is what you want.\n"
    )
    if extra:
        words = ", ".join(f"`{w}`" for w in extra)
        automatic += (
            f"\nThis server also pills {words} the same way. Spell them exactly as listed.\n"
        )
    return (
        "\n## Pills\n\n"
        + automatic
        + "\nFor anything else, mark it yourself: `[[Blocked|red]]` renders a red BLOCKED pill, and "
        "that form works anywhere, in prose, lists, headings and cells. Colours are `red`, "
        "`orange`, `amber`, `green`, `teal`, `blue`, `purple`, `pink`, `grey`, `black`, `accent`, "
        "or a `#rrggbb` value. Written without a colour, `[[Blocked]]` comes out grey. Inside a "
        "table cell the pipe has to be escaped, `[[Blocked\\|red]]`, because an unescaped one "
        "starts a new column.\n"
        "\nUse pills for a status a reader scans for, not for emphasis. A paragraph with four "
        "pills in it reads worse than one without.\n"
    )


def report_guide() -> str:
    from ..mail import recipients as rcpt

    body = _RECIPIENTS_REQUIRED if rcpt.require_explicit() else _RECIPIENTS_DEFAULT
    return _HEAD + body + _pills_section() + _TAIL
