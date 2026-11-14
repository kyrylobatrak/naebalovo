"""Answer window that draws LaTeX formulas as pictures.

Claude writes formulas in LaTeX ($...$ in a line, $$...$$ on their own).
QTextBrowser shows Markdown but not LaTeX, so each formula is drawn with
ziamath (pure Python, no TeX installation) into an image, and the Markdown gets
an image link "math:<key>" in its place; loadResource() hands out the image.
A formula ziamath cannot parse becomes Unicode text (mathtext.py).
"""
from __future__ import annotations

import hashlib
import logging
import math
import re

from PySide6 import QtCore, QtGui, QtSvg, QtWidgets

import mathtext

try:
    import ziamath
    ziamath.config.svg2 = False  # Qt's SVG renderer needs SVG 1.1 (xlink:href)
except Exception:  # noqa: BLE001 - without it, formulas stay Unicode text
    ziamath = None

log = logging.getLogger("overlay")
_VIEWBOX = re.compile(r'viewBox="([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)"')


class AnswerView(QtWidgets.QTextBrowser):
    def __init__(self, color: str = "#e6e9ef"):
        super().__init__()
        self._color = color
        self._images: dict[str, QtGui.QImage] = {}  # key -> picture of a formula
        self._tex: dict[str, str] = {}               # key -> its LaTeX, for copying
        self._display: set[str] = set()              # keys of formulas on their own line
        self._failed: set[str] = set()               # formulas ziamath could not parse

    def set_answer_markdown(self, markdown: str, streaming: bool = False):
        if streaming:
            markdown = mathtext.hide_unfinished_math(markdown)
        self.setMarkdown(mathtext.replace_math(markdown, self._formula))
        self._align_formulas()

    # ---- formulas -> images

    def _formula(self, tex: str, display: bool) -> str:
        key = self._render(tex.strip(), display)
        if key is None:
            return mathtext.unicode_formula(tex, display)
        link = f"![f](math:{key})"  # Qt drops an image with an empty alt text
        return f"\n\n{link}\n\n" if display else link

    def _render(self, tex: str, display: bool) -> str | None:
        """Draw a formula once; returns the key of its image or None."""
        font_px = QtGui.QFontInfo(self.font()).pixelSize()
        size = round(font_px * (1.2 if display else 1.05), 1)
        max_width = max(100, self.viewport().width() - 24)
        dpr = self.devicePixelRatioF()
        key = hashlib.sha1(f"{display}|{size}|{max_width}|{dpr}|{tex}".encode()).hexdigest()[:20]
        if key in self._images:
            return key
        if ziamath is None or key in self._failed or not tex:
            return None
        if len(self._images) > 2000:  # a very long session: start the cache anew
            self._images.clear()
            self._tex.clear()
            self._display.clear()
        try:
            svg = ziamath.Latex(tex, size=size, color=self._color).svg()
            renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(svg.encode("utf-8")))
            box = _VIEWBOX.search(svg)
            if not renderer.isValid() or not box:
                raise ValueError("bad svg")
        except Exception as exc:  # noqa: BLE001 - unsupported LaTeX: Unicode text instead
            log.info("formula not drawn (%s): %s", type(exc).__name__, tex[:80])
            self._failed.add(key)
            return None
        width, height = float(box.group(3)), float(box.group(4))
        baseline = -float(box.group(2))  # y = 0 of the SVG is the baseline of the formula
        top = bottom = 0.0
        if not display:
            # Qt puts the middle of an inline image (AlignMiddle) half an x-height above
            # the text baseline; pad the picture so that point of the formula is its middle.
            axis = baseline - QtGui.QFontMetricsF(self.font()).xHeight() / 2
            top, bottom = max(0.0, height - 2 * axis), max(0.0, 2 * axis - height)
        scale = min(1.0, max_width / width) if width else 1.0
        w, h = width * scale, (height + top + bottom) * scale
        image = QtGui.QImage(max(1, math.ceil(w * dpr)), max(1, math.ceil(h * dpr)),
                             QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QtCore.Qt.GlobalColor.transparent)
        image.setDevicePixelRatio(dpr)
        painter = QtGui.QPainter(image)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        renderer.render(painter, QtCore.QRectF(0, top * scale, w, height * scale))
        painter.end()
        self._images[key] = image
        self._tex[key] = tex
        if display:
            self._display.add(key)
        return key

    def loadResource(self, kind, url):  # noqa: N802 - Qt name
        if url.scheme() == "math":
            image = self._images.get(url.path())
            if image is not None:
                return image
        return super().loadResource(kind, url)

    def createMimeDataFromSelection(self):  # noqa: N802 - Qt name
        """Ctrl+C: plain text where every formula picture becomes its LaTeX ($...$).
        Only text is copied: the pictures would not load in another program."""
        cursor = self.textCursor()
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        parts = []
        block = self.document().findBlock(start)
        while block.isValid() and block.position() < end:
            if parts:
                parts.append("\n")
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                lo = max(frag.position(), start)
                hi = min(frag.position() + frag.length(), end)
                if lo < hi:
                    fmt = frag.charFormat()
                    name = fmt.toImageFormat().name() if fmt.isImageFormat() else ""
                    if name.startswith("math:"):
                        key = name[5:]
                        tex = self._tex.get(key, "")
                        one = f"$${tex}$$" if key in self._display else f"${tex}$"
                        parts.append(one * (hi - lo))  # equal neighbours share a fragment
                    else:
                        parts.append(frag.text()[lo - frag.position(): hi - frag.position()])
                it += 1
            block = block.next()
        mime = QtCore.QMimeData()
        mime.setText("".join(parts).replace(" ", "\n").replace("￼", ""))
        return mime

    def _align_formulas(self):
        """Inline formulas sit on the text line; formulas on their own line are centred."""
        doc = self.document()
        found = []
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                fmt = frag.charFormat()
                if fmt.isImageFormat() and fmt.toImageFormat().name().startswith("math:"):
                    found.append((frag.position(), frag.length(),
                                  fmt.toImageFormat().name()[5:] in self._display))
                it += 1
            block = block.next()
        for position, length, display in found:
            cursor = QtGui.QTextCursor(doc)
            cursor.setPosition(position)
            cursor.setPosition(position + length, QtGui.QTextCursor.MoveMode.KeepAnchor)
            if display:
                block_format = QtGui.QTextBlockFormat()
                block_format.setAlignment(QtCore.Qt.AlignmentFlag.AlignHCenter)
                cursor.mergeBlockFormat(block_format)
            else:
                char_format = QtGui.QTextCharFormat()
                char_format.setVerticalAlignment(QtGui.QTextCharFormat.VerticalAlignment.AlignMiddle)
                cursor.mergeCharFormat(char_format)
