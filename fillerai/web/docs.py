"""The documentation site: Markdown in ``docs/`` read as HTML pages.

``/docs`` serves the same files a contributor reads on GitHub, rendered here
so that somebody who has only ever seen the product page can read them in a
browser. Nothing is converted ahead of time and nothing new has to be kept in
step: the Markdown is the source, and this module is a small renderer for the
part of Markdown those files actually use - headings, paragraphs, lists,
tables, fenced code, quotes, links and emphasis.

Every byte of the source is escaped. The documents contain no raw HTML, and a
renderer that passed some through would be a renderer somebody could put a
script in. Mermaid diagrams are shown as their source in a labelled box,
because drawing them would mean loading a script from the network into a page
that sits behind a passcode.

The passcode itself is the server's business (:mod:`fillerai.web.server`);
this module only answers "what is there" and "what does it look like".
"""

from __future__ import annotations

import html
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

#: The repository's ``docs/`` directory. Installed away from the repository
#: there is none, and the docs site says so rather than serving a 404 page.
DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"

#: The repository's README, shown as the product tour.
README = Path(__file__).resolve().parents[2] / "README.md"

#: The user guide ships inside the package, because the in-app help panel is
#: made from it and has to work wherever AIrForms is installed.
GUIDE = Path(__file__).resolve().parent / "guide.md"

#: How the documents are grouped on the docs home page, by slug. Anything in
#: ``docs/`` that is not named here still appears, under "More".
GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Help", "How to use each screen, step by step.",
     ("user-guide", "tour", "glossary")),
    ("Product", "What AIrForms does, how a form becomes a model, and how other "
                "applications use it.",
     ("overview", "process", "bot-builder", "integration", "nlp-and-chatbot",
      "algorithms")),
    ("Architecture", "How it is built, the decisions behind it, and what is "
                     "still open.",
     ("architecture", "training-and-scale", "weight-based-training",
      "llm-modelling", "llm-implementation-plan", "assumptions", "pending")),
    ("Operations", "Running it for other people, and keeping it safe.",
     ("operations", "security")),
    ("Reference", "Looked up rather than read through.",
     ("reference/cli", "reference/http-api", "reference/data-formats",
      "reference/modules", "readme")),
)

#: One-line descriptions for the home page cards, where the first paragraph
#: of the document would not make a good one.
BLURBS = {
    "user-guide": "Every screen in the app: what it is for, how to use it, and where to go next.",
    "tour": "The README: what AIrForms is and what each stage looks like.",
    "readme": "The reading guide to these documents, by who you are.",
}

SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)?$")


@dataclass(frozen=True)
class Doc:
    slug: str
    title: str
    path: Path
    blurb: str = ""

    @property
    def url(self) -> str:
        return f"/docs/{self.slug}"


# ----------------------------------------------------------------------
# what is there
# ----------------------------------------------------------------------


def _title_of(path: Path) -> str:
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("# "):
                    return line[2:].strip()
    except OSError:
        pass
    return path.stem.replace("-", " ").capitalize()


def _blurb_of(path: Path) -> str:
    """The first ordinary paragraph, as plain text, shortened."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    in_code = False
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        stripped = line.strip()
        if not stripped:
            if lines:
                break
            continue
        if stripped[0] in "#|>-*!<" or re.match(r"\d+\.", stripped):
            if lines:
                break
            continue
        lines.append(stripped)
    plain = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", " ".join(lines))
    plain = re.sub(r"[*_`]", "", plain)
    if len(plain) > 180:
        plain = plain[:177].rsplit(" ", 1)[0] + "..."
    return plain


def catalog() -> dict[str, Doc]:
    """Every document the site can show, keyed by slug."""
    found: dict[str, Doc] = {}
    if GUIDE.is_file():
        found["user-guide"] = Doc("user-guide", "User guide", GUIDE,
                                  BLURBS["user-guide"])
    if README.is_file():
        found["tour"] = Doc("tour", "Product tour", README, BLURBS["tour"])
    if DOCS_DIR.is_dir():
        for path in sorted(DOCS_DIR.rglob("*.md")):
            relative = path.relative_to(DOCS_DIR).with_suffix("").as_posix()
            slug = relative.lower()
            if not SLUG.match(slug):
                continue
            title = _title_of(path)
            if slug == "readme":
                title = "Reading guide"
            found[slug] = Doc(slug, title, path,
                              BLURBS.get(slug) or _blurb_of(path))
    return found


def grouped(docs: dict[str, Doc]) -> list[tuple[str, str, list[Doc]]]:
    placed: set[str] = set()
    groups = []
    for name, about, slugs in GROUPS:
        members = [docs[s] for s in slugs if s in docs]
        placed.update(d.slug for d in members)
        if members:
            groups.append((name, about, members))
    rest = [doc for slug, doc in sorted(docs.items()) if slug not in placed]
    if rest:
        groups.append(("More", "Everything else in the docs folder.", rest))
    return groups


def _link_target(href: str, doc: Doc | None) -> str | None:
    """Where a relative link in a document should go on this site.

    A link to another document becomes that document's page; a link into the
    source tree has nowhere to go here and becomes ``None``, which renders as
    text. Absolute and in-page links are left alone.
    """
    scheme = re.match(r"^([a-z][a-z0-9+.-]*):", href, re.I)
    if scheme:
        # Only the schemes a document has a reason to use; javascript: and
        # data: would be a way to put a script behind a link.
        return href if scheme.group(1).lower() in ("http", "https", "mailto") else None
    if href.startswith("#"):
        return href
    if href.startswith("/"):
        return href
    target, _, fragment = href.partition("#")
    fragment = f"#{fragment}" if fragment else ""
    if doc is None:
        return None
    if doc.path == README or doc.path == GUIDE:
        base = "docs" if doc.path == GUIDE else ""
        joined = posixpath.normpath(posixpath.join(base, target))
        if joined.startswith("docs/"):
            joined = joined[len("docs/"):]
        elif joined == "README.md":
            return "/docs/tour" + fragment
        else:
            return None
    else:
        here = posixpath.dirname(doc.path.relative_to(DOCS_DIR).as_posix())
        joined = posixpath.normpath(posixpath.join(here, target))
        if joined == "../README.md":
            return "/docs/tour" + fragment
        if joined.startswith(".."):
            return None
    if not joined.endswith(".md"):
        return None
    return "/docs/" + joined[:-3].lower() + fragment


# ----------------------------------------------------------------------
# Markdown, the part of it these documents use
# ----------------------------------------------------------------------


def heading_id(text: str) -> str:
    """The anchor GitHub gives a heading, so links written for it still work."""
    plain = re.sub(r"<[^>]+>", "", text).lower()
    plain = re.sub(r"[^\w\- ]", "", plain)
    return plain.replace(" ", "-")


_INLINE_CODE = re.compile(r"(`+)(.+?)\1", re.S)
_LINK = re.compile(r"!?\[((?:[^\[\]]|\[[^\]]*\])*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_AUTOLINK = re.compile(r"&lt;(https?://[^\s&]+)&gt;")


class Renderer:
    def __init__(self, doc: Doc | None = None) -> None:
        self.doc = doc
        self.ids: dict[str, int] = {}
        self.toc: list[tuple[int, str, str]] = []

    # -- inline -------------------------------------------------------------

    def inline(self, text: str) -> str:
        stash: list[str] = []

        def keep(fragment: str) -> str:
            stash.append(fragment)
            return f"\x00{len(stash) - 1}\x00"

        text = _INLINE_CODE.sub(
            lambda m: keep(f"<code>{html.escape(m.group(2).strip())}</code>"), text)

        def link(match: re.Match[str]) -> str:
            label, href = match.group(1), match.group(2)
            if match.group(0).startswith("!"):
                return keep(f"<em>[{html.escape(label)}]</em>")
            target = _link_target(href, self.doc)
            inner = self._emphasis(html.escape(label, quote=False))
            if target is None:
                return keep(f'<span class="src-ref">{inner}</span>')
            external = re.match(r"^https?:", target) is not None
            extra = ' target="_blank" rel="noopener"' if external else ""
            return keep(f'<a href="{html.escape(target)}"{extra}>{inner}</a>')

        text = _LINK.sub(link, text)
        text = html.escape(text, quote=False)
        text = _AUTOLINK.sub(
            lambda m: keep(f'<a href="{m.group(1)}" target="_blank" rel="noopener">'
                           f"{m.group(1)}</a>"), text)
        text = self._emphasis(text)
        return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)

    @staticmethod
    def _emphasis(text: str) -> str:
        text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"(?<![\w*])__(?=\S)(.+?)(?<=\S)__(?![\w*])", r"<strong>\1</strong>", text)
        text = re.sub(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])", r"<em>\1</em>", text)
        text = re.sub(r"(?<![\w])_(?=\S)(.+?)(?<=\S)_(?![\w])", r"<em>\1</em>", text)
        text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<del>\1</del>", text)
        return text

    # -- blocks -------------------------------------------------------------

    def render(self, text: str) -> str:
        lines = text.replace("\r\n", "\n").replace("\t", "    ").split("\n")
        return self.blocks(lines)

    def blocks(self, lines: list[str]) -> str:
        out: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if not stripped:
                i += 1
                continue

            fence = re.match(r"^(\s*)(```+|~~~+)\s*([\w+-]*)", line)
            if fence:
                marker, lang = fence.group(2), fence.group(3).lower()
                body: list[str] = []
                i += 1
                indent = len(fence.group(1))
                while i < len(lines) and not lines[i].strip().startswith(marker):
                    body.append(lines[i][indent:] if lines[i][:indent].strip() == ""
                                else lines[i])
                    i += 1
                i += 1
                code = html.escape("\n".join(body))
                if lang == "mermaid":
                    out.append('<figure class="diagram"><figcaption>Diagram '
                               '(Mermaid source)</figcaption>'
                               f"<pre><code>{code}</code></pre></figure>")
                else:
                    cls = f' class="lang-{html.escape(lang)}"' if lang else ""
                    out.append(f"<pre><code{cls}>{code}</code></pre>")
                continue

            heading = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
            if heading:
                level = len(heading.group(1))
                inner = self.inline(heading.group(2))
                anchor = heading_id(inner) or "section"
                seen = self.ids.get(anchor, 0)
                self.ids[anchor] = seen + 1
                if seen:
                    anchor = f"{anchor}-{seen}"
                if level in (2, 3):
                    self.toc.append((level, anchor, re.sub(r"<[^>]+>", "", inner)))
                out.append(f'<h{level} id="{anchor}">{inner}'
                           f'<a class="anchor" href="#{anchor}" aria-hidden="true">#</a>'
                           f"</h{level}>")
                i += 1
                continue

            if re.match(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$", line):
                out.append("<hr>")
                i += 1
                continue

            if stripped.startswith(">"):
                quoted: list[str] = []
                while i < len(lines) and lines[i].strip().startswith(">"):
                    quoted.append(re.sub(r"^\s*>\s?", "", lines[i]))
                    i += 1
                out.append(f"<blockquote>{self.blocks(quoted)}</blockquote>")
                continue

            if stripped.startswith("|") and i + 1 < len(lines) \
                    and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
                rows: list[str] = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    rows.append(lines[i])
                    i += 1
                out.append(self.table(rows))
                continue

            item = _list_item(line)
            if item:
                block, i = self.list_block(lines, i)
                out.append(block)
                continue

            para: list[str] = []
            while i < len(lines):
                current = lines[i]
                if not current.strip() or (para and _starts_block(current)):
                    break
                para.append(current.strip())
                i += 1
            out.append(f"<p>{self.inline(' '.join(para))}</p>")
        return "\n".join(out)

    def table(self, rows: list[str]) -> str:
        def cells(row: str) -> list[str]:
            row = row.strip()
            if row.startswith("|"):
                row = row[1:]
            if row.endswith("|") and not row.endswith("\\|"):
                row = row[:-1]
            parts = re.split(r"(?<!\\)\|", row)
            return [part.strip().replace("\\|", "|") for part in parts]

        head = cells(rows[0])
        aligns = []
        for spec in cells(rows[1]):
            if spec.startswith(":") and spec.endswith(":"):
                aligns.append("center")
            elif spec.endswith(":"):
                aligns.append("right")
            else:
                aligns.append("")

        def cell(tag: str, text: str, n: int) -> str:
            align = aligns[n] if n < len(aligns) else ""
            style = f' style="text-align:{align}"' if align else ""
            return f"<{tag}{style}>{self.inline(text)}</{tag}>"

        thead = "".join(cell("th", text, n) for n, text in enumerate(head))
        body = "".join(
            "<tr>" + "".join(cell("td", text, n) for n, text in enumerate(cells(row)))
            + "</tr>" for row in rows[2:])
        return (f'<div class="table-wrap"><table><thead><tr>{thead}</tr></thead>'
                f"<tbody>{body}</tbody></table></div>")

    def list_block(self, lines: list[str], i: int) -> tuple[str, int]:
        first = _list_item(lines[i])
        assert first is not None
        indent, ordered = first[0], first[1]
        start = first[3]
        items: list[list[str]] = []
        loose = False
        while i < len(lines):
            found = _list_item(lines[i])
            if not found or found[0] != indent or found[1] != ordered:
                break
            content_indent = found[2]
            body = [found[4]]
            i += 1
            while i < len(lines):
                current = lines[i]
                if not current.strip():
                    # A blank line ends the item unless what follows is still
                    # indented under it.
                    nxt = next((l for l in lines[i + 1:] if l.strip()), None)
                    if nxt is not None and _indent(nxt) >= content_indent:
                        body.append("")
                        loose = True
                        i += 1
                        continue
                    if nxt is not None and _list_item(nxt) and _list_item(nxt)[0] == indent:
                        loose = True
                    break
                deeper = _indent(current) >= content_indent
                sibling = _list_item(current)
                if sibling and _indent(current) <= indent:
                    break
                if not deeper and _starts_block(current) and not sibling:
                    break
                body.append(current[content_indent:] if deeper else current.strip())
                i += 1
            items.append(body)
            # Skip blank lines between items of the same list.
            j = i
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                found = _list_item(lines[j])
                if found and found[0] == indent and found[1] == ordered:
                    i = j
                    continue
            break

        rendered = []
        for body in items:
            inner = self.blocks(body)
            if not loose and inner.startswith("<p>"):
                inner = re.sub(r"^<p>(.*?)</p>", r"\1", inner, count=1, flags=re.S)
            rendered.append(f"<li>{inner}</li>")
        tag = "ol" if ordered else "ul"
        attr = f' start="{start}"' if ordered and start not in (None, 1) else ""
        return f"<{tag}{attr}>{''.join(rendered)}</{tag}>", i


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _list_item(line: str) -> tuple[int, bool, int, int | None, str] | None:
    """(indent, ordered, content indent, start number, text) or None."""
    match = re.match(r"^(\s*)([-*+]|\d{1,9}[.)])(\s+)(.*)$", line)
    if not match:
        return None
    marker = match.group(2)
    if marker in "-*" and re.match(r"^\s*([-*])(\s*\1){2,}\s*$", line):
        return None  # a horizontal rule, not an item
    ordered = marker[0].isdigit()
    start = int(marker[:-1]) if ordered else None
    content = len(match.group(1)) + len(marker) + len(match.group(3))
    return len(match.group(1)), ordered, content, start, match.group(4)


def _starts_block(line: str) -> bool:
    stripped = line.strip()
    return bool(
        stripped.startswith(("#", "```", "~~~", ">", "|"))
        or _list_item(line)
        or re.match(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$", line))


def render(text: str, doc: Doc | None = None) -> tuple[str, list[tuple[int, str, str]]]:
    """HTML for a Markdown text, and its table of contents (h2 and h3)."""
    renderer = Renderer(doc)
    return renderer.render(text), renderer.toc


# ----------------------------------------------------------------------
# the user guide, which is also the in-app help panel
# ----------------------------------------------------------------------

_PANEL = re.compile(r"^<!--\s*panel:\s*([a-z]+)\s*-->\s*$", re.M)


def help_sections() -> dict[str, dict[str, str]]:
    """The user guide cut into one section per screen, for the help panel.

    A section starts at a ``<!-- panel: name -->`` line and runs to the next
    one. Its ``##`` heading is the title; the rest is the panel's body.
    """
    try:
        text = GUIDE.read_text(encoding="utf-8")
    except OSError:
        return {}
    found: dict[str, dict[str, str]] = {}
    marks = list(_PANEL.finditer(text))
    for n, mark in enumerate(marks):
        end = marks[n + 1].start() if n + 1 < len(marks) else len(text)
        chunk = text[mark.end():end].strip("\n")
        title = ""
        heading = re.match(r"^##\s+(.*)\n", chunk)
        if heading:
            title = heading.group(1).strip()
            chunk = chunk[heading.end():]
        body, _toc = render(chunk, Doc("user-guide", "User guide", GUIDE))
        # Inside the panel the sub-headings are small and need no anchors.
        body = re.sub(r'<a class="anchor"[^>]*>#</a>', "", body)
        found[mark.group(1)] = {"title": title, "html": body,
                                "anchor": heading_id(html.escape(title))}
    return found


# ----------------------------------------------------------------------
# pages
# ----------------------------------------------------------------------

_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} &middot; AIrForms docs</title>
<link rel="icon" href="{icon}">
<link rel="stylesheet" href="/static/docs.css">
</head>
<body class="docs">
<header class="docs-top">
  <a class="docs-brand" href="/docs">{logo}<span class="wordmark"><b>AIr</b>Forms</span><span class="docs-tag">Docs</span></a>
  <nav class="docs-links">
    <a href="/">Product</a>
    <a href="/app">Open the app</a>
  </nav>
</header>
"""

ICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
        "%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' "
        "stop-color='%2322d3ee'/%3E%3Cstop offset='.55' stop-color='%236366f1'/%3E%3Cstop "
        "offset='1' stop-color='%238b5cf6'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect "
        "width='32' height='32' rx='8' fill='url%28%23g%29'/%3E%3Crect x='7' y='8.7' "
        "width='13' height='2.6' rx='1.3' fill='white'/%3E%3Crect x='7' y='14.7' width='18' "
        "height='2.6' rx='1.3' fill='white' opacity='.85'/%3E%3Crect x='7' y='20.7' "
        "width='10' height='2.6' rx='1.3' fill='white' opacity='.55'/%3E%3C/svg%3E")

LOGO = ('<svg class="logo" viewBox="0 0 32 32" aria-hidden="true"><defs>'
        '<linearGradient id="afDocs" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#22d3ee"/><stop offset=".55" stop-color="#6366f1"/>'
        '<stop offset="1" stop-color="#8b5cf6"/></linearGradient></defs>'
        '<rect width="32" height="32" rx="8" fill="url(#afDocs)"/>'
        '<rect x="7" y="8.7" width="13" height="2.6" rx="1.3" fill="#fff"/>'
        '<rect x="7" y="14.7" width="18" height="2.6" rx="1.3" fill="#fff" opacity=".85"/>'
        '<rect x="7" y="20.7" width="10" height="2.6" rx="1.3" fill="#fff" opacity=".55"/>'
        '<path d="M25 5.6l1.1 2.8 2.8 1.1-2.8 1.1L25 13.4l-1.1-2.8-2.8-1.1 2.8-1.1z" fill="#fff"/>'
        "</svg>")


def _head(title: str) -> str:
    return _HEAD.format(title=html.escape(title), icon=ICON, logo=LOGO)


def _sidebar(docs: dict[str, Doc], current: str = "") -> str:
    parts = ['<nav class="docs-side" aria-label="Documents">']
    for name, _about, members in grouped(docs):
        parts.append(f"<h4>{html.escape(name)}</h4><ul>")
        for doc in members:
            on = ' class="is-current" aria-current="page"' if doc.slug == current else ""
            parts.append(f'<li><a href="{doc.url}"{on}>{html.escape(doc.title)}</a></li>')
        parts.append("</ul>")
    parts.append("</nav>")
    return "".join(parts)


def home_page(docs: dict[str, Doc]) -> str:
    if not docs:
        body = ('<main class="docs-home"><h1>Documentation</h1><p class="lead">This '
                "install has no documentation with it. The documents live in the "
                "<code>docs/</code> folder of the AIrForms repository.</p></main>")
        return _head("Documentation") + body + "</body></html>"
    sections = []
    for name, about, members in grouped(docs):
        cards = "".join(
            f'<a class="doc-card" href="{doc.url}"><strong>{html.escape(doc.title)}</strong>'
            f"<span>{html.escape(doc.blurb)}</span></a>" for doc in members)
        sections.append(f'<section class="doc-group"><h2>{html.escape(name)}</h2>'
                        f'<p class="muted">{html.escape(about)}</p>'
                        f'<div class="doc-grid">{cards}</div></section>')
    body = ('<main class="docs-home"><h1>Documentation</h1>'
            '<p class="lead">How to use AIrForms, how it works, and how it is built. '
            "New here? Start with the <a href=\"/docs/user-guide\">user guide</a>, "
            "then the <a href=\"/docs/overview\">technical overview</a>.</p>"
            + "".join(sections) + "</main>")
    return _head("Documentation") + body + "</body></html>"


def doc_page(docs: dict[str, Doc], doc: Doc) -> str:
    text = doc.path.read_text(encoding="utf-8")
    if doc.path == GUIDE:
        text = _PANEL.sub("", text)
    content, toc = render(text, doc)
    toc_html = ""
    if len(toc) > 2:
        entries = "".join(
            f'<li class="l{level}"><a href="#{anchor}">{html.escape(label)}</a></li>'
            for level, anchor, label in toc)
        toc_html = (f'<aside class="docs-toc"><h4>On this page</h4>'
                    f"<ul>{entries}</ul></aside>")
    body = (f'<div class="docs-layout">{_sidebar(docs, doc.slug)}'
            f'<main class="docs-body"><article class="prose">{content}</article></main>'
            f"{toc_html}</div>")
    return _head(doc.title) + body + "</body></html>"


def gate_page(configured: bool) -> str:
    if configured:
        form = """
      <p class="sub">The documentation is for people who have been given its access code.
         Ask your AIrForms administrator for it.</p>
      <form id="gate" autocomplete="off">
        <label class="stack"><span>Access code</span>
          <input type="password" id="code" autocomplete="off" required autofocus></label>
        <button class="btn btn-primary wide" type="submit">Open the docs</button>
        <p class="gate-error" id="gateError" role="alert" hidden></p>
      </form>"""
    else:
        form = """
      <p class="sub">The documentation has no access code yet, so it is closed to everybody.
         An administrator sets one in the app, under <strong>Settings &rarr;
         Documentation access</strong>.</p>
      <a class="btn btn-primary wide" href="/app">Go to the app</a>"""
    return (_head("Access code") + f"""
<main class="gate">
  <div class="gate-card">
    <div class="gate-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor"
           stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>
    </div>
    <h1>AIrForms documentation</h1>{form}
  </div>
</main>
<script src="/static/docs-gate.js"></script>
</body></html>""")
