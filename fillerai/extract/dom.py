"""A minimal DOM built on ``html.parser``.

Label association, fieldset scoping and ``aria-describedby`` lookups all need
to see structure, so streaming events are not enough. This builds just enough
of a tree to answer "what encloses this control" and "what text is inside this
element", with no third-party dependency.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Iterator

# Elements that never have a closing tag; anything else nests.
VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# Elements that auto-close a previous sibling of the same kind.
IMPLIED_CLOSE = {
    "li": {"li"},
    "option": {"option"},
    "p": {"p"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "tr": {"tr"},
}


class Node:
    """An element or a chunk of text."""

    __slots__ = ("tag", "attrs", "children", "parent", "text")

    def __init__(self, tag: str, attrs: dict[str, str] | None = None,
                 parent: "Node | None" = None, text: str = "") -> None:
        self.tag = tag  # "" for a text node
        self.attrs = attrs or {}
        self.children: list[Node] = []
        self.parent = parent
        self.text = text

    # -- traversal ----------------------------------------------------------

    def walk(self) -> Iterator["Node"]:
        yield self
        for child in self.children:
            yield from child.walk()

    def elements(self, *tags: str) -> Iterator["Node"]:
        wanted = set(tags)
        for node in self.walk():
            if node.tag and (not wanted or node.tag in wanted):
                yield node

    def ancestors(self) -> Iterator["Node"]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def closest(self, *tags: str) -> "Node | None":
        wanted = set(tags)
        for node in self.ancestors():
            if node.tag in wanted:
                return node
        return None

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.attrs.get(name, default)

    def has(self, name: str) -> bool:
        return name in self.attrs

    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    # -- content ------------------------------------------------------------

    def inner_text(self, skip: tuple[str, ...] = ("script", "style")) -> str:
        """Visible text of this subtree, whitespace-collapsed.

        Nested form controls are skipped so a wrapping ``<label>`` that
        contains its own ``<input>`` still yields only the caption.
        """
        parts: list[str] = []
        stack = list(reversed(self.children))
        while stack:
            node = stack.pop()
            if node.tag in skip or node.tag in ("input", "select", "textarea"):
                continue
            if not node.tag:
                parts.append(node.text)
            else:
                stack.extend(reversed(node.children))
        return " ".join(" ".join(parts).split())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        if not self.tag:
            return f"Text({self.text[:24]!r})"
        return f"<{self.tag} {self.attrs}>"


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self._stack: list[Node] = [self.root]

    @property
    def _current(self) -> Node:
        return self._stack[-1]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        implied = IMPLIED_CLOSE.get(tag)
        if implied and self._current.tag in implied:
            self._stack.pop()
        # Attribute names are lowercased by HTMLParser; a valueless attribute
        # such as ``required`` arrives as None and is normalised to "".
        attributes = {k.lower(): (v if v is not None else "") for k, v in attrs}
        node = Node(tag, attributes, parent=self._current)
        self._current.children.append(node)
        if tag not in VOID_ELEMENTS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {k.lower(): (v if v is not None else "") for k, v in attrs}
        self._current.children.append(Node(tag, attributes, parent=self._current))

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_ELEMENTS:
            return
        # Pop to the nearest matching open element; ignore strays so that
        # malformed real-world markup still parses.
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._current.children.append(Node("", parent=self._current, text=data))


def parse(html: str) -> Node:
    """Parse an HTML document into a :class:`Node` tree."""
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return builder.root
