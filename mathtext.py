"""LaTeX formulas in Claude's answers: finding them, and plain Unicode text.

The answer window draws formulas as pictures (mathview.py); this module finds
them outside code and converts to Unicode text a formula that cannot be drawn,
e.g. "$\\hat\\theta_n \\to \\theta$" -> "θ̂ₙ → θ". Code is left untouched.
"""
from __future__ import annotations

import re

GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "varpi": "ϖ", "rho": "ρ",
    "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "φ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
    "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}

SYMBOLS = {
    "to": "→", "rightarrow": "→", "longrightarrow": "⟶", "Rightarrow": "⇒", "implies": "⇒",
    "leftarrow": "←", "Leftarrow": "⇐", "leftrightarrow": "↔", "Leftrightarrow": "⇔",
    "iff": "⇔", "mapsto": "↦", "uparrow": "↑", "downarrow": "↓",
    "infty": "∞", "forall": "∀", "exists": "∃", "nexists": "∄", "in": "∈", "notin": "∉",
    "ni": "∋", "subset": "⊂", "subseteq": "⊆", "supset": "⊃", "supseteq": "⊇",
    "cup": "∪", "cap": "∩", "setminus": "∖", "emptyset": "∅", "varnothing": "∅",
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠", "approx": "≈",
    "equiv": "≡", "sim": "~", "simeq": "≃", "cong": "≅", "propto": "∝", "ll": "≪", "gg": "≫",
    "pm": "±", "mp": "∓", "times": "×", "cdot": "·", "div": "÷", "ast": "∗", "star": "⋆",
    "circ": "∘", "bullet": "•", "oplus": "⊕", "otimes": "⊗",
    "sum": "∑", "prod": "∏", "int": "∫", "iint": "∬", "iiint": "∭", "oint": "∮",
    "partial": "∂", "nabla": "∇", "ldots": "…", "dots": "…", "cdots": "⋯", "vdots": "⋮",
    "ddots": "⋱", "land": "∧", "wedge": "∧", "lor": "∨", "vee": "∨", "neg": "¬", "lnot": "¬",
    "perp": "⊥", "parallel": "∥", "angle": "∠", "triangle": "△", "degree": "°",
    "prime": "′", "ell": "ℓ", "hbar": "ħ", "Re": "ℜ", "Im": "ℑ", "aleph": "ℵ",
    "mid": "|", "vert": "|", "Vert": "‖", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈",
    "rceil": "⌉", "langle": "⟨", "rangle": "⟩", "quad": "  ", "qquad": "    ",
    "colon": ":", "therefore": "∴", "because": "∵", "square": "□", "checkmark": "✓",
}

# Function names stay as words: \lim -> lim, \sin -> sin.
WORDS = {"lim", "liminf", "limsup", "max", "min", "sup", "inf", "log", "ln", "lg", "exp",
         "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan", "sinh",
         "cosh", "tanh", "det", "dim", "ker", "deg", "arg", "gcd", "Pr", "mod", "bmod"}

DROP = {"left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr",
        "biggl", "biggr", "displaystyle", "textstyle", "scriptstyle", "limits", "nolimits",
        "middle", "nonumber", "notag"}

ACCENTS = {"hat": "̂", "widehat": "̂", "bar": "̄", "overline": "̅",
           "tilde": "̃", "widetilde": "̃", "vec": "⃗", "dot": "̇",
           "ddot": "̈", "check": "̌", "acute": "́", "grave": "̀",
           "underline": "̲"}

TEXT_ARG = {"text", "textrm", "textit", "textbf", "mathrm", "mathit", "mathbf", "mathsf",
            "mathtt", "boldsymbol", "bm", "operatorname", "mathcal", "mathscr", "mathfrak",
            "textnormal", "emph", "boxed"}

BLACKBOARD = {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "E": "𝔼", "P": "ℙ"}

SUPER = dict(zip("0123456789+-−=()niaeoxyhkmprstuvwbcdfgjlzABDEGHIJKLMNOPRTUVW′'*",
                 "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾ⁿⁱᵃᵉᵒˣʸʰᵏᵐᵖʳˢᵗᵘᵛʷᵇᶜᵈᶠᵍʲˡᶻᴬᴮᴰᴱᴳᴴᴵᴶᴷᴸᴹᴺᴼᴾᴿᵀᵁⱽᵂ′′*"))
SUB = dict(zip("0123456789+-−=()aehijklmnoprstuvxβγρφχ",
               "₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓᵦᵧᵨᵩᵪ"))


class _Reader:
    """Converts one formula; a tiny recursive reader of LaTeX math."""

    def __init__(self, src: str):
        self.s = src
        self.i = 0

    def convert(self) -> str:
        out = []
        while self.i < len(self.s):
            out.append(self._item())
        return "".join(out)

    def _item(self) -> str:
        ch = self.s[self.i]
        if ch == "\\":
            return self._command()
        if ch in "^_":
            after_operator = re.search(r"\\(lim|liminf|limsup|max|min|sup|inf|sum|prod|int)\s*$",
                                       self.s[:self.i])
            self.i += 1
            arg = _Reader(self._raw_arg()).convert().strip()
            if ch == "_" and after_operator and not all(c in SUB for c in arg):
                return f" ({arg})"  # lim_{n→∞} -> "lim (n→∞)"
            return _script(arg, SUPER if ch == "^" else SUB, ch)
        if ch == "{":
            return _Reader(self._group()).convert()
        self.i += 1
        if ch in "}&":
            return ""
        if ch == "~":
            return " "
        if ch == "'":
            return "′"
        return ch

    def _command(self) -> str:
        m = re.match(r"\\([A-Za-z]+)", self.s[self.i:])
        if not m:  # \, \; \! \{ \} \\ \%  ...
            sym = self.s[self.i + 1: self.i + 2]
            self.i += 2
            return {",": " ", ";": " ", ":": " ", " ": " ", "!": "", "\\": "; ",
                    "|": "‖"}.get(sym, sym)
        name = m.group(1)
        self.i += len(m.group(0))
        if name in GREEK:
            return GREEK[name]
        if name in SYMBOLS:
            return SYMBOLS[name]
        if name in WORDS:
            return name
        if name in DROP:
            return ""
        if name in ACCENTS:
            arg = _Reader(self._raw_arg()).convert()
            mark = ACCENTS[name]
            if name in ("bar", "overline", "underline", "widetilde") or len(arg) == 1:
                return "".join(c + mark for c in arg) if len(arg) > 1 else arg + mark
            return arg + mark
        if name in ("frac", "dfrac", "tfrac", "cfrac"):
            num = _Reader(self._raw_arg()).convert().strip()
            den = _Reader(self._raw_arg()).convert().strip()
            return f"{_wrap(num)}/{_wrap(den)}"
        if name == "sqrt":
            degree = ""
            if self.s[self.i: self.i + 1] == "[":
                end = self.s.find("]", self.i)
                degree = _script(_Reader(self.s[self.i + 1: end]).convert(), SUPER, "^")
                self.i = end + 1
            arg = _Reader(self._raw_arg()).convert().strip()
            return f"{degree}√{_wrap(arg)}"
        if name in ("binom", "dbinom", "tbinom"):
            n = _Reader(self._raw_arg()).convert().strip()
            k = _Reader(self._raw_arg()).convert().strip()
            return f"C({n}, {k})"
        if name == "mathbb":
            arg = _Reader(self._raw_arg()).convert()
            return "".join(BLACKBOARD.get(c, c) for c in arg)
        if name in TEXT_ARG:
            return _Reader(self._raw_arg()).convert()
        if name in ("begin", "end"):
            self._raw_arg()  # environment name
            return ""
        return name  # unknown command: keep its name rather than lose it

    def _skip_spaces(self):
        while self.i < len(self.s) and self.s[self.i] == " ":
            self.i += 1

    def _group(self) -> str:
        """Content of a {...} group starting at self.i."""
        depth, start = 0, self.i
        while self.i < len(self.s):
            ch = self.s[self.i]
            if ch == "\\":
                self.i += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    self.i += 1
                    return self.s[start + 1: self.i - 1]
            self.i += 1
        return self.s[start + 1:]

    def _raw_arg(self) -> str:
        """Next argument: a {group}, a \\command or one character."""
        self._skip_spaces()
        if self.i >= len(self.s):
            return ""
        ch = self.s[self.i]
        if ch == "{":
            return self._group()
        if ch == "\\":
            m = re.match(r"\\([A-Za-z]+|.)", self.s[self.i:])
            self.i += len(m.group(0))
            return m.group(0)
        self.i += 1
        return ch


def _script(text: str, table: dict, mark: str) -> str:
    if text and all(c in table for c in text):
        return "".join(table[c] for c in text)
    if mark == "^" and text in ("′", "′′"):
        return text
    return f"{mark}{text}" if len(text) == 1 else f"{mark}({text})"


def _wrap(text: str) -> str:
    return text if re.fullmatch(r"[\ẁ-ͯ⃗.′]+", text) else f"({text})"


def latex_to_unicode(formula: str) -> str:
    text = _Reader(formula.strip()).convert()
    return re.sub(r" {2,}", " ", text).strip()


# Math delimiters: $$...$$, \[...\], \(...\), $...$ (pandoc rules for single $:
# no space right after the opening $ or before the closing one, no digit after it,
# so "costs $5 and $10" stays as it is).
_MATH = re.compile(
    r"\$\$(?P<d1>.+?)\$\$"
    r"|\\\[(?P<d2>.+?)\\\]"
    r"|\\\((?P<i1>.+?)\\\)"
    r"|(?<![\\$\w])\$(?=[^\s$])(?P<i2>[^$\n]+?)(?<=[^\s\\])\$(?![\d$])",
    re.S,
)
_CODE = re.compile(r"(```.*?(?:```|$)|`[^`\n]*`)", re.S)


def unicode_formula(tex: str, display: bool) -> str:
    text = latex_to_unicode(tex)
    return f"\n\n{text}\n\n" if display else text


def replace_math(markdown: str, convert=unicode_formula) -> str:
    """Replace every LaTeX formula outside code with convert(tex, display)."""
    if "$" not in markdown and "\\(" not in markdown and "\\[" not in markdown:
        return markdown

    def one(match: re.Match) -> str:
        display = match.group("d1") or match.group("d2")
        if display is not None:
            return convert(display, True)
        return convert(match.group("i1") or match.group("i2"), False)

    parts = _CODE.split(markdown)
    for k in range(0, len(parts), 2):  # even parts are outside code
        parts[k] = _MATH.sub(one, parts[k])
    text = "".join(parts)
    return re.sub(r"\n{3,}", "\n\n", text)


def render_math(markdown: str) -> str:
    """Replace LaTeX formulas outside code with Unicode text."""
    return replace_math(markdown, unicode_formula)


def hide_unfinished_math(markdown: str) -> str:
    """While an answer streams in, cut off a display formula that is not closed yet,
    so raw "$$\\frac{1}{" does not flash before it turns into a picture."""
    parts = _CODE.split(markdown)
    if len(parts) % 2 == 0:  # the answer ends inside an open code block
        return markdown
    tail = parts[-1]
    for opener, closer in (("$$", "$$"), ("\\[", "\\]")):
        if opener == closer:
            if tail.count(opener) % 2:
                tail = tail[:tail.rfind(opener)] + " …"
        else:
            start = tail.rfind(opener)
            if start > tail.rfind(closer):
                tail = tail[:start] + " …"
    parts[-1] = tail
    return "".join(parts)
