#!/usr/bin/env python3
"""
convert.py -- convert the Tufte-LaTeX source of "Population and Quantitative
Genetics" (https://github.com/cooplab/popgen-notes) into a Quarto book.

    python3 tools/convert.py  <path-to-popgen-notes-clone>  <output-quarto-dir>

Pipeline for every chapter
    1. pre-process the LaTeX (strip comments, expand custom macros, turn Tufte
       margin/side notes into things pandoc understands, rename every \\label to
       a Quarto-style id, rewrite \\ref/\\eqref as sentinels, copy/convert images,
       expand the csv-driven tables)
    2. pandoc  -f latex -t markdown  with tools/popgen.lua
    3. post-process the markdown into Quarto syntax (@fig-x, {#eq-x}, ...)

Idempotent: rerun whenever the LaTeX source changes.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

if len(sys.argv) != 3:
    sys.exit(__doc__)
SRC = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
TOOLS = Path(__file__).resolve().parent
LUA = TOOLS / "popgen.lua"
IMG_DPI = 200

CHAPTERS = [
    ("Chapters/intro.tex", "introduction"),
    ("Chapters/eugenics_frontpiece.tex", "_eugenics"),
    ("Chapters/chapter-01.tex", "allele-and-genotype-frequencies"),
    ("Chapters/Population_structure.tex", "population-structure"),
    ("Chapters/genetic_drift.tex", "genetic-drift"),
    ("Chapters/Molecular_evolution.tex", "molecular-evolution"),
    ("Chapters/genetic_drift_pop_structure.tex", "neutral-diversity-and-structure"),
    ("Chapters/Pheno_var.tex", "phenotypic-variation"),
    ("Chapters/Response_to_sel.tex", "response-to-selection"),
    ("Chapters/Multi_trait_selection.tex", "correlated-traits"),
    ("Chapters/One_locus_selection.tex", "one-locus-selection"),
    ("Chapters/Interaction_selection_mut_mig.tex", "selection-mutation-migration"),
    ("Chapters/Genetic_drift_selection.tex", "drift-and-selection"),
    ("Chapters/Linked_selection.tex", "linked-selection"),
    ("Chapters/multi_locus_sel.tex", "multiple-selected-loci"),
    ("math_background/Math-background.tex", "math-background"),
]
EQUATION_SHEET = ("math_background/Equation_sheet.txt", "equation-sheet")

LOG = []


def warn(msg):
    LOG.append(msg)
    print("  !", msg)


# --------------------------------------------------------------------------
# LaTeX parsing helpers
# --------------------------------------------------------------------------
def strip_comments(s):
    out = []
    for line in s.replace("\r", "").split("\n"):
        m = re.search(r"(?<!\\)%", line)
        if m:
            if line[: m.start()].strip() == "":
                continue
            line = line[: m.start()]
        out.append(line)
    return "\n".join(out)


def match_brace(s, i):
    assert s[i] == "{"
    depth = 0
    j = i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    raise ValueError("unbalanced brace")


def get_args(s, i, n, allow_opt=True):
    args = []
    j = i
    while len(args) < n:
        while j < len(s) and s[j] in " \t\n":
            j += 1
        while allow_opt and j < len(s) and s[j] == "[":
            j = s.index("]", j) + 1
            while j < len(s) and s[j] in " \t\n":
                j += 1
        if j >= len(s) or s[j] != "{":
            raise ValueError("missing argument")
        k = match_brace(s, j)
        args.append(s[j + 1 : k])
        j = k + 1
    return args, j


def replace_cmd(s, name, nargs, fn, allow_opt=True):
    pat = re.compile(r"\\" + name + r"(?![A-Za-z@])")
    res, pos = [], 0
    while True:
        m = pat.search(s, pos)
        if not m:
            res.append(s[pos:])
            break
        res.append(s[pos : m.start()])
        try:
            args, end = get_args(s, m.end(), nargs, allow_opt)
        except ValueError:
            warn(f"could not parse arguments of \\{name} near: {s[m.start():m.start()+60]!r}")
            res.append(s[m.start() : m.end()])
            pos = m.end()
            continue
        res.append(fn(*args))
        pos = end
    return "".join(res)


def slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")


# --------------------------------------------------------------------------
# labels
# --------------------------------------------------------------------------
ENV_TYPE = {
    "figure": "fig", "figure*": "fig", "marginfigure": "fig",
    "table": "tbl", "table*": "tbl", "margintable": "tbl",
    "equation": "eq", "equation*": "eq", "align": "eq", "align*": "eq", "eqnarray": "eq",
    "question": "exr",
}
TOKEN = re.compile(
    r"\\begin\{([A-Za-z]+\*?)\}|\\end\{([A-Za-z]+\*?)\}"
    r"|\\(chapter|section|subsection|subsubsection)(\*?)(?![A-Za-z])"
    r"|\\label\{([^}]*)\}"
)
PREFIX_STRIP = re.compile(
    r"^(fig|Fig|figure|eqn|eq|table|Table|tab|section|sec|Section|Chapter|chapter|ch|Appendix|question|Q)[:_.\-]+"
)

LABELS = {}
USED_IDS = {}


def make_id(typ, label):
    base = slug(PREFIX_STRIP.sub("", label) or label) or "x"
    cand = f"{typ}-{base}"
    n = 2
    while cand in USED_IDS and USED_IDS[cand] != label:
        cand = f"{typ}-{base}-{n}"
        n += 1
    USED_IDS[cand] = label
    return cand


def collect_labels(text, stem):
    stack = []
    last_heading_end = None
    for m in TOKEN.finditer(text):
        if m.group(1):
            stack.append(m.group(1))
        elif m.group(2):
            if m.group(2) in stack:
                while stack and stack.pop() != m.group(2):
                    pass
        elif m.group(3):
            try:
                brace = text.index("{", m.end())
                last_heading_end = match_brace(text, brace) + 1
            except ValueError:
                last_heading_end = None
        elif m.group(5):
            lab = m.group(5)
            typ = None
            for env in reversed(stack):
                if env in ENV_TYPE:
                    typ = ENV_TYPE[env]
                    break
            if typ is None:
                if last_heading_end is not None and text[last_heading_end : m.start()].strip() == "":
                    typ = "sec"
                else:
                    warn(f"[{stem}] label {lab!r} is not attached to a heading/figure/table/equation; treated as section")
                    typ = "sec"
            if lab in LABELS and LABELS[lab][1] != typ:
                warn(f"label {lab!r} defined twice")
            LABELS.setdefault(lab, (make_id(typ, lab), typ))


# --------------------------------------------------------------------------
# images
# --------------------------------------------------------------------------
IMG_OUT = OUT / "images"
IMG_CACHE = {}


def process_image(path):
    path = path.strip()
    if path in IMG_CACHE:
        return IMG_CACHE[path]
    base = SRC / path
    cands = [base] if base.suffix.lower() in (".png", ".jpg", ".jpeg", ".pdf", ".eps") and base.exists() else []
    if not cands:
        for ext in (".png", ".jpg", ".jpeg", ".JPG", ".pdf"):
            if (SRC / (path + ext)).exists():
                cands = [SRC / (path + ext)]
                break
    f = cands[0] if cands else None
    if f is not None and f.suffix.lower() == ".eps":
        for ext in (".png", ".jpg", ".pdf"):
            if f.with_suffix(ext).exists():
                f = f.with_suffix(ext)
                break
    if f is None or not f.exists():
        stem = Path(path).name
        alts = sorted((SRC / Path(path).parent).glob(re.sub(r"_new$", "", stem) + ".*"))
        alts = [a for a in alts if a.suffix.lower() in (".png", ".pdf", ".jpg")]
        if alts:
            warn(f"image {path!r} not found, using {alts[0].relative_to(SRC)}")
            f = alts[0]
        else:
            warn(f"image not found: {path}")
            IMG_CACHE[path] = "images/MISSING.png"
            return IMG_CACHE[path]
    rel = f.relative_to(SRC)
    out = IMG_OUT / str(rel).replace(" ", "_")
    out.parent.mkdir(parents=True, exist_ok=True)
    if f.suffix.lower() == ".pdf":
        out = out.with_suffix(".png")
        if not out.exists():
            subprocess.run(
                ["pdftoppm", "-png", "-r", str(IMG_DPI), "-singlefile", str(f), str(out.with_suffix(""))],
                check=True,
            )
    elif f.suffix.lower() == ".eps":
        out = out.with_suffix(".png")
        if not out.exists():
            subprocess.run(["convert", "-density", "200", str(f), "-flatten", str(out)], check=True)
    else:
        if not out.exists():
            shutil.copy2(f, out)
    res = "images/" + str(out.relative_to(IMG_OUT))
    IMG_CACHE[path] = res
    return res


def fix_graphics(s):
    def sub(m):
        opts, path = m.group(1) or "", m.group(2)
        w = re.search(r"width\s*=\s*([0-9.]*)\s*\\textwidth", opts)
        frac = (w.group(1) if w and w.group(1) else "1") if w else "1"
        return "\\includegraphics[width=%s\\textwidth]{%s}" % (frac, process_image(path))
    return re.sub(r"\\includegraphics\s*(?:\[([^\]]*)\])?\s*\{([^}]*)\}", sub, s)


# --------------------------------------------------------------------------
# macro expansion
# --------------------------------------------------------------------------
MATH_MACROS = {
    "E": r"\mathbb{E}", "P": r"\mathbb{P}", "half": r"\tfrac{1}{2}",
    "wbar": r"\overline{w}", "Wbar": r"\overline{W}",
    "fis": r"F_{\mathrm{IS}}", "fit": r"F_{\mathrm{IT}}", "fst": r"F_{\mathrm{ST}}",
    "dNdS": r"\nicefrac{d_N}{d_S}",
    "mars": r"\text{♂}", "venus": r"\text{♀}",
}
DROP_BARE = [
    "noindent", "newline", "newpage", "clearpage", "medskip", "bigskip", "smallskip", "centering",
    "tiny", "small", "footnotesize", "scriptsize", "normalsize", "large", "Large", "hfill", "vfill",
    "raggedright", "raggedleft", "pagebreak", "nohyphenation",
]
EDITORIAL_TODOS = []
VISIBLE_DRAFT_NOTES = []


MATH_SPAN = re.compile(
    r"(?<!\\)\$\$.*?(?<!\\)\$\$|(?<!\\)\$.*?(?<!\\)\$"
    r"|\\begin\{(equation|align|eqnarray)\*?\}.*?\\end\{\1\*?\}", re.S)


def nicefrac_everywhere(s):
    """\\nicefrac works in text and math in LaTeX; in Markdown it must become math either way."""
    inside = lambda a, b: "{}^{%s}\\!/\\!{}_{%s}" % (a, b)
    outside = lambda a, b: "$" + inside(a, b) + "$"
    out, pos = [], 0
    for m in MATH_SPAN.finditer(s):
        out.append(replace_cmd(s[pos : m.start()], "nicefrac", 2, outside))
        out.append(replace_cmd(m.group(0), "nicefrac", 2, inside))
        pos = m.end()
    out.append(replace_cmd(s[pos:], "nicefrac", 2, outside))
    res = "".join(out)
    # nested fractions: the arguments of the outer one were copied unexpanded
    return nicefrac_everywhere(res) if "\\nicefrac" in res else res


def expand_macros(s, stem):
    def todo(who):
        def f(txt):
            EDITORIAL_TODOS.append((stem, who, re.sub(r"\s+", " ", txt).strip()))
            return ""
        return f
    s = replace_cmd(s, "graham", 1, todo("graham"))
    s = replace_cmd(s, "erin", 1, todo("erin"))

    def keep(who):
        def f(txt):
            VISIBLE_DRAFT_NOTES.append((stem, who, re.sub(r"\s+", " ", txt).strip()[:140]))
            return txt
        return f
    s = replace_cmd(s, "gc", 1, keep("gc"))
    s = replace_cmd(s, "ec", 1, keep("ec"))
    s = replace_cmd(s, "sa", 1, keep("sa"))

    A0, A1 = "⟦ATTR⟧", "⟦/ATTR⟧"

    def bhl(kind):
        tail = {"CC": "Licensed under CC BY-{4}.", "NKC": "No known copyright restrictions.", "NC": "Not in copyright."}[kind]

        def f(*a):
            t = tail.replace("{4}", a[3]) if kind == "CC" else tail
            return f"{A0}{a[0]} Image from the \\href{{{a[1]}}}{{Biodiversity Heritage Library}}. Contributed by {a[2]}. {t}{A1}"
        return f
    s = replace_cmd(s, "BHLCC", 4, bhl("CC"))
    s = replace_cmd(s, "BHLNKC", 3, bhl("NKC"))
    s = replace_cmd(s, "BHLNC", 3, bhl("NC"))
    s = replace_cmd(s, "IANC", 3, lambda a, b, c: f"{A0}{a} Image from the \\href{{{b}}}{{Internet Archive}}. Contributed by {c}. No known copyright restrictions.{A1}")
    s = replace_cmd(s, "wikimedia", 4, lambda a, b, c, d: f"{A0}{a} Image from \\href{{{b}}}{{wikimedia}} contributed by {c}. {d}{A1}")
    s = replace_cmd(s, "gitcode", 1, lambda u: f"Code \\href{{{u}}}{{here}}.")
    s = re.sub(r"\\PLOSccBY(?![A-Za-z])", "licensed under CC BY 4.0", s)

    s = replace_cmd(s, "newthought", 1, lambda t: f"\\textsc{{{t}}}")

    def para(t):
        t = t.strip()
        if not re.search(r"[.?!:]\s*(\$\))?$", t):
            t += "."
        return f"\\textbf{{{t}}}"
    s = replace_cmd(s, "paragraph", 1, para)
    s = replace_cmd(s, "subparagraph", 1, para)

    s = re.sub(r"\{(\\sidenote)", r"\1", s)
    def sidenote(t):
        # Quarto cannot cross-reference an equation that sits inside a footnote (PDF output),
        # so a sidenote that carries a labelled equation becomes an ordinary paragraph
        if re.search(r"\\begin\{(equation|align|eqnarray)\}.*?\\label", t, re.S):
            warn(f"[{stem}] sidenote containing a labelled equation was moved into the main text")
            return "\n\n" + t.strip() + "\n\n"
        return f"\\footnote{{{t}}}"
    s = replace_cmd(s, "sidenote", 1, sidenote)
    s = replace_cmd(s, "marginnote", 1, lambda t: f"\\footnote{{ZZMARGINZZ {re.sub(r'\\\\label\{[^}]*\}', '', t)}}}")

    def caption(t):
        labs = re.findall(r"\\label\{[^}]*\}", t)
        t = re.sub(r"\\label\{[^}]*\}", "", t)
        return "\\caption{" + t.strip() + "}" + "".join(labs)
    s = replace_cmd(s, "caption", 1, caption)

    for c in DROP_BARE:
        s = re.sub(r"\\" + c + r"(?![A-Za-z])", "", s)
    s = re.sub(r"\\(vspace|hspace)\*?\{[^}]*\}", "", s)
    s = re.sub(r"\\setlength\{[^}]*\}\{[^}]*\}", "", s)
    s = re.sub(r"\\renewcommand\{?\\arraystretch\}?\{[^}]*\}", "", s)
    s = re.sub(r"\\setcounter\{[^}]*\}\{[^}]*\}", "", s)
    s = re.sub(r"\\begin\{center\}|\\end\{center\}", "", s)
    s = replace_cmd(s, "multirow", 3, lambda n, w, t: t)
    s = replace_cmd(s, "rotatebox", 2, lambda a, t: t)

    def href(u, t):
        u = u.replace("\\#", "#").replace("\\%", "%").replace("\\_", "_").replace("\\&", "&")
        return f"\\href{{{u}}}{{{t}}}"
    s = replace_cmd(s, "href", 2, href)

    for name, rep in MATH_MACROS.items():
        s = re.sub(r"\\" + name + r"(?![A-Za-z])", lambda m, r=rep: r, s)
    s = nicefrac_everywhere(s)
    s = re.sub(r"\\eqn(?![A-Za-z])", "eqn", s)
    return s


# --------------------------------------------------------------------------
# environments
# --------------------------------------------------------------------------
def fix_environments(s, stem):
    s = re.sub(r"\\begin\{marginfigure\}(\[[^\]]*\])?", r"\\begin{marginfigure}\\begin{figure}", s)
    s = s.replace("\\end{marginfigure}", "\\end{figure}\\end{marginfigure}")
    s = re.sub(r"\\begin\{figure\*\}(\[[^\]]*\])?", r"\\begin{widefigure}\\begin{figure}", s)
    s = s.replace("\\end{figure*}", "\\end{figure}\\end{widefigure}")
    s = re.sub(r"\\begin\{table\*\}(\[[^\]]*\])?", r"\\begin{widetbl}\\begin{table}", s)
    s = s.replace("\\end{table*}", "\\end{table}\\end{widetbl}")
    s = re.sub(r"\\begin\{margintable\}(\[[^\]]*\])?", r"\\begin{margintbl}\\begin{table}", s)
    s = s.replace("\\end{margintable}", "\\end{table}\\end{margintbl}")
    s = re.sub(r"\\begin\{(figure|table)\}\s*\[[^\]]*\]", r"\\begin{\1}", s)

    s = s.replace("\\begin{ChapterSummary}", "\\begin{ChapterSummary}\\begin{itemize}")
    s = s.replace("\\end{ChapterSummary}", "\\end{itemize}\\end{ChapterSummary}")

    qn = [0]

    def question(m):
        qn[0] += 1
        title = (m.group(1) or "").strip()
        title = re.sub(r"^\((.*)\)$", r"\1", re.sub(r"\.$", "", title)).strip()
        rest = m.group(2)
        lab = re.match(r"\s*\\label\{([^}]*)\}", rest)
        if lab:
            qid = LABELS[lab.group(1)][0]
            rest = rest[lab.end():]
        else:
            qid = f"exr-{stem}-{qn[0]}"
        t = f"⟦QTITLE:{title}⟧ " if title else ""
        return f"\\begin{{question}}\\label{{{qid}}}{t}{rest}"
    s = re.sub(r"\\begin\{question\}(?:\{([^}]*)\})?(.*?)(?=\\end\{question\})", question, s, flags=re.S)
    return s


def fix_math_envs(s, stem):
    auto = [0]

    def sub(m):
        env, body = m.group(1), m.group(2)
        labs = re.findall(r"\\label\{([^}]*)\}", body)
        # LaTeX numbers every equation unless starred / \\nonumber; Quarto only numbers those with an id
        unnumbered = env.endswith("*") or re.search(r"\\(nonumber|notag)(?![A-Za-z])", body)
        body = re.sub(r"\\label\{[^}]*\}", "", body)
        body = re.sub(r"\\(nonumber|notag)(?![A-Za-z])", "", body)
        body = re.sub(r"\n[ \t]*(?=\n)", "", body)   # a blank line would end the $$ block in Markdown
        if env == "eqnarray":
            rows = body.split("\\\\")
            rows = [re.sub(r"&([^&]*)&", r"&\1 ", r) for r in rows]
            body = "\\\\".join(rows)
        name = "equation" if env.rstrip("*") == "equation" else "align"
        tag = ""
        if labs:
            ids = [LABELS[l][0] for l in labs if l in LABELS]
            if len(labs) > 1:
                warn(f"[{stem}] equation block has several labels {labs}; all now point to {ids[0]}")
            tag = f"⟦EQID:{ids[0]}⟧"
        elif not unnumbered:
            auto[0] += 1
            tag = f"⟦EQID:eq-{stem}-{auto[0]}⟧"
        return f"\n\n\\begin{{{name}}}{body}\\end{{{name}}}{tag}\n\n"
    return re.sub(r"\\begin\{(equation\*?|align\*?|eqnarray)\}(.*?)\\end\{\1\}", sub, s, flags=re.S)


# Typos in the LaTeX source that TeX tolerates but pandoc does not: (stem, old, new)
SOURCE_TYPOS = [
    ("allele-and-genotype-frequencies",
     "{\\textit{Ciona savignyi}, Sea Squirts", "\\textit{Ciona savignyi}, Sea Squirts"),
    # "{%\bf B)}" in the source: the closing brace sits inside a comment
    ("one-locus-selection",
     "\n{\n\\marginnote[-2cm]{This example is loosely", "\n\\marginnote[-2cm]{This example is loosely"),
    ("linked-selection",
     "\\wikimedia{\\href{Centers for\n    Disease Control and Prevention (caption modified).}}",
     "\\wikimedia{Centers for Disease Control and Prevention (caption modified).}"),
    ("linked-selection", "\\ref{fig:HIV_sweep}}A)", "\\ref{fig:HIV_sweep}A)"),
    ("drift-and-selection", "and {then use a Taylor series", "and then use a Taylor series"),
    # label inside a tabular, attached to nothing and never cited
    ("phenotypic-variation", "\\label{add_values}", ""),
]


def apply_source_typos(s, stem):
    for st, old, new in SOURCE_TYPOS:
        if st == stem:
            if old not in s:
                warn(f"[{stem}] source-typo patch not found: {old[:40]!r}")
            s = s.replace(old, new)
    return s


def unwrap_size_groups(s):
    """`{\\small ...}` -> `...`: pandoc cannot cope with such groups spanning blank lines."""
    pat = re.compile(r"\{\s*\\(?:tiny|small|footnotesize|scriptsize|large|Large|normalsize|centering)(?![A-Za-z])")
    while True:
        m = pat.search(s)
        if not m:
            return s
        try:
            end = match_brace(s, m.start())
        except ValueError:
            warn("unbalanced group after size switch")
            return s
        inner = s[m.start() + 1 : end]
        inner = re.sub(r"^\s*\\[A-Za-z]+", "", inner)
        s = s[: m.start()] + inner + s[end + 1 :]


def check_braces(s, stem):
    stack, j, line = [], 0, 1
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "\n":
            line += 1
        if c == "{":
            stack.append(line)
        elif c == "}":
            if stack:
                stack.pop()
            else:
                warn(f"[{stem}] stray }} at preprocessed line {line}")
        j += 1
    if stack:
        warn(f"[{stem}] unclosed '{{' opened at preprocessed lines {stack[:5]}")


def manual_patches(s, stem):
    if stem == "allele-and-genotype-frequencies":
        # one align block carrying two equation numbers that are both cited from the text
        m = re.search(r"\\begin\{align\}\s*F_\{ij\} = & \\P\(.*?\\label\{eqn:coeffkinship\}\s*\\end\{align\}", s, re.S)
        if m:
            new = (
                "\\begin{equation}\\begin{aligned}"
                "F_{ij} &= \\P(\\text{I\\&J IBD}) \\\\"
                " &= \\P(\\text{I\\&J IBD} |~ \\text{i\\&j 0 IBD})\\, \\P(\\text{i\\&j 0 IBD}) \\\\"
                " &\\quad + \\P(\\text{I\\&J IBD} |~ \\text{i\\&j 1 IBD})\\, \\P(\\text{i\\&j 1 IBD}) \\\\"
                " &\\quad + \\P(\\text{I\\&J IBD} |~ \\text{i\\&j 2 IBD})\\, \\P(\\text{i\\&j 2 IBD})"
                "\\end{aligned}\\label{eqn:coeffkinship_step}\\end{equation}\n\n"
                "\\begin{equation}F_{ij} = 0 \\times r_0 + \\frac{1}{4} r_1 + \\frac{1}{2} r_2.\\label{eqn:coeffkinship}\\end{equation}"
            )
            s = s[: m.start()] + new + s[m.end():]
        else:
            warn("manual patch for coeffkinship align block did not match")
    return s


# --------------------------------------------------------------------------
# \ref / \eqref  ->  sentinels
# --------------------------------------------------------------------------
WORD_TYPE = [
    (r"Figures?|Figs?\.?|figures?", "fig"),
    (r"Tables?|tables?", "tbl"),
    (r"Sections?|sections?|Chapters?|chapters?|Appendix|appendix|Appendices", "sec"),
    (r"Questions?|questions?", "exr"),
    (r"Equations?|equations?|[Ee]qns?\.?|[Ee]qs?\.?", "eq"),
]
REF = re.compile(
    r"(?:(?<![A-Za-z])(?P<word>" + "|".join(w for w, _ in WORD_TYPE) + r")(?P<sep>(?:\s|~|\\ |\\,)*))?"
    r"\\(?P<cmd>eqref|ref)\{(?P<lab>[^}]*)\}"
)


def fix_refs(s, stem):
    def sub(m):
        lab = m.group("lab").strip()
        if lab not in LABELS:
            warn(f"[{stem}] unresolved reference to {lab!r}")
            return (m.group("word") or "") + (m.group("sep") or "") + "??"
        rid, typ = LABELS[lab]
        word, sep = m.group("word"), m.group("sep") or ""
        if word:
            wtyp = next(t for pat, t in WORD_TYPE if re.fullmatch(pat, word))
            singular = not word.rstrip(".").lower().endswith("s")
            if wtyp == typ and singular and (word[0].isupper() or typ == "eq"):
                out = f"⟦REF:{rid}⟧"
            else:
                out = word + (sep or " ") + f"⟦NREF:{rid}⟧"
        else:
            out = f"⟦NREF:{rid}⟧"
        if typ == "eq" and "NREF" in out and not out.startswith("⟦REF"):
            out = re.sub(r"(⟦NREF:[^⟧]*⟧)", r"(\1)", out)
        return out
    s = REF.sub(sub, s)
    s = re.sub(r"\(\((⟦NREF:[^⟧]*⟧)\)\)", r"(\1)", s)
    return s


# --------------------------------------------------------------------------
# csv driven tables
# --------------------------------------------------------------------------
def csv_to_tabular(path, sep=","):
    rows = [r for r in (SRC / path).read_text(encoding="utf-8", errors="replace").replace("\r", "").split("\n") if r.strip()]
    cells = [r.split(sep) for r in rows]
    n = max(len(r) for r in cells)
    lines = ["\\begin{tabular}{" + "c" * n + "}", "\\hline"]
    for i, r in enumerate(cells):
        r = r + [""] * (n - len(r))
        lines.append(" & ".join(c.strip() for c in r) + " \\\\")
        if i == 0:
            lines.append("\\hline")
    lines += ["\\hline", "\\end{tabular}"]
    return "\n".join(lines)


def expand_csv_tables(s):
    return replace_cmd(s, "csvautobooktabular", 1, lambda p: csv_to_tabular(p.strip()), allow_opt=True)


def equation_sheet_tex():
    raw = (SRC / EQUATION_SHEET[0]).read_text(encoding="utf-8", errors="replace").replace("\r", "")
    lines = raw.split("\n")[1:]
    rows = []

    def split_half(h):
        h = h.strip().strip("~").strip()
        if "\t," in h:
            eq, ref = h.split("\t,", 1)
            return ("eq", eq.strip(), ref.replace("~", " ").strip())
        return ("title", h.replace("\t", " ").strip().strip("~").strip(), "")

    items = []
    for l in lines:
        if ";" not in l:
            continue
        left, right = l.split(";", 1)
        items.append((split_half(left), split_half(right)))
    cur_titles = None
    for L, R in items:
        if L[0] == "title" and R[0] == "title" and (L[1] or R[1]):
            cur_titles = (L[1], R[1])
        elif L[0] == "eq" or R[0] == "eq":
            for (side, k) in ((L, 0), (R, 1)):
                if side[0] == "eq" and side[1]:
                    rows.append((cur_titles[k] if cur_titles else "", side[1], side[2]))
    out = [
        "\\chapter*{Equation Sheet}",
        "All results below, unless otherwise stated, refer to diploid individuals and autosomal loci. "
        "More general results are often given in the relevant sections.",
        "",
        "\\begin{widetbl}\\begin{table}",
        "\\begin{tabular}{lll}",
        "Quantity & Equation & Ref. \\\\ \\hline",
    ]
    for t, e, r in rows:
        e = e.replace("|", "\\mid ")
        out.append(f"{t} & {e} & {r} \\\\")
    out += ["\\end{tabular}", "\\end{table}\\end{widetbl}"]
    return "\n".join(out)


# --------------------------------------------------------------------------
# pandoc + post-processing
# --------------------------------------------------------------------------
def run_pandoc(tex, stem):
    p = subprocess.run(
        [
            "pandoc", "-f", "latex", "-t", "markdown-smart-simple_tables-multiline_tables",
            "--wrap=none", "--top-level-division=chapter", "--markdown-headings=atx",
            "--reference-location=block", f"--lua-filter={LUA}",
        ],
        input=tex, capture_output=True, text=True,
    )
    if p.returncode != 0:
        sys.exit(f"pandoc failed on {stem}:\n{p.stderr}")
    for line in p.stderr.strip().split("\n"):
        if line.strip():
            warn(f"[{stem}] pandoc: {line}")
    return p.stdout


def normalize_headings(md, stem):
    """A heading may be at most one level deeper than the previous one (WCAG 1.3.1 / 2.4.6)."""
    out, prev, fixed = [], 0, 0
    for line in md.split("\n"):
        m = re.match(r"^(#{1,6}) (.*)$", line)
        if m:
            lvl = len(m.group(1))
            if prev and lvl > prev + 1:
                lvl = prev + 1
                fixed += 1
            prev = lvl
            line = "#" * lvl + " " + m.group(2)
        out.append(line)
    if fixed:
        warn(f"[{stem}] {fixed} heading(s) promoted to avoid skipped levels")
    return "\n".join(out)


def postprocess(md, stem):
    def ref_sub(m):
        kind, rid = m.group(1), m.group(2)
        nxt = m.string[m.end() : m.end() + 1]
        prev = m.string[m.start() - 1 : m.start()] if m.start() else ""
        if kind == "NREF":
            return f"[-@{rid}]"
        following = m.string[m.end() : m.end() + 2]
        if (nxt and re.match(r"[A-Za-z0-9_\-]", nxt)) or (prev and prev.isalnum()) \
                or re.match(r"[.:/+][A-Za-z0-9]", following):
            return f"[@{rid}]"
        return f"@{rid}"
    md = re.sub(r"⟦(N?REF):([^⟧]*)⟧", ref_sub, md)
    # pandoc leaves a blank line inside aligned blocks, which ends the $$ block in Markdown
    md = re.sub(r"\$\$(?!\s*\{#)(.*?)\$\$", lambda m: "$$" + re.sub(r"\n[ \t]*(?=\n)", "", m.group(1)) + "$$", md, flags=re.S)
    md = re.sub(r"\$\$\s*⟦EQID:([^⟧]*)⟧", r"$$ {#\1}", md)
    md = re.sub(r"(::: \{#exr-[^}]*)\}\s*\n\s*⟦QTITLE:(.*?)⟧\s*", r'\1 name="\2"}\n', md)
    # headings that are cited by \\ref must be numbered for Quarto to resolve them
    md = re.sub(r"^(#{2,} .*\{#sec-[^ }]*) \.unnumbered\}", r"\1}", md, flags=re.M)
    md = re.sub(r"⟦ATTR⟧\s*(.*?)\s*⟦/ATTR⟧", r"[\1]{.attribution}", md)
    md = re.sub(r"^(# .*?)\.( \{.*\})?$", lambda m: m.group(1) + (m.group(2) or ""), md, flags=re.M)
    md = re.sub(r"@([A-Za-z0-9_:\-]+) ?\[@\1\]", r"@\1", md)   # "\citet{k} \cite{k}" typed twice in the source
    md = normalize_headings(md, stem)
    # footnote ids must be unique across the whole book (the PDF is one merged document)
    md = re.sub(r"\[\^(\d+)\]", lambda m: f"[^{stem}-{m.group(1)}]", md)
    md = re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"
    leftovers = re.findall(r"⟦[^⟧]*⟧", md)
    if leftovers:
        warn(f"[{stem}] unprocessed sentinels: {leftovers[:5]}")
    return md


def raw_tex_leftovers(md, stem):
    no_math = md.replace("\\$", "")
    no_math = re.sub(r"\$\$.*?\$\$", "", no_math, flags=re.S)
    no_math = re.sub(r"\$[^$]+?\$", "", no_math, flags=re.S)
    for m in list(re.finditer(r"\\[A-Za-z]+", no_math))[:8]:
        ctx = no_math[max(0, m.start() - 30) : m.end() + 30].replace("\n", " ")
        warn(f"[{stem}] raw LaTeX left in text: {m.group(0)}  …{ctx}…")


CITE_OPT = re.compile(r"\\(cite[tp])\[([^\]]*)\](?:\[([^\]]*)\])?\{([^}]*)\}")


def lift_refs_out_of_cites(s):
    """pandoc turns \\citep[pre][post]{k} into a citation with a note; a \\ref inside such a note
    would then be treated as part of the citation, so move it outside."""
    def sub(m):
        cmd, a, b, key = m.group(1), m.group(2), m.group(3), m.group(4)
        if "\\ref{" not in (a or "") + (b or ""):
            return m.group(0)
        pre, post = (a, b) if b is not None else ("", a)
        pre, post = (pre or "").strip(), (post or "").strip()
        out = f"\\{cmd}{{{key}}}"
        if pre:
            out = pre + " " + out
        if post:
            out += f" ({post})"
        return out
    return CITE_OPT.sub(sub, s)


def preprocess(text, stem):
    s = strip_comments(text)
    s = manual_patches(s, stem)
    if "\\citep{}" in s:
        warn(f"[{stem}] empty \\citep{{}} in the source (missing citation) was removed")
        s = s.replace("\\citep{}", "")
    s = expand_csv_tables(s)
    s = unwrap_size_groups(s)
    s = expand_macros(s, stem)
    s = fix_environments(s, stem)
    s = fix_math_envs(s, stem)
    s = lift_refs_out_of_cites(s)
    s = fix_refs(s, stem)
    s = fix_graphics(s)
    s = re.sub(r"\\label\{([^}]*)\}", lambda m: "\\label{%s}" % LABELS.get(m.group(1), (m.group(1),))[0], s)
    s = re.sub(r"\\\\\s*\n\s*\n", "\n\n", s)
    s = re.sub(r"\\\\\s*(?=\\end\{(question|quote|quotation|itemize|enumerate)\})", "", s)
    s = re.sub(r"\\\\\s*(?=\\begin\{(equation|align|itemize|enumerate|question|quote|figure|table|widetbl|margintbl|marginfigure|widefigure)\})", "", s)
    s = re.sub(r"\\\\\s*$", "", s)
    check_braces(s, stem)
    return s


def strip_ids_in_asides(md):
    """Quarto cannot number an equation inside a margin note ([...]{.aside}); the id would print
    literally. Drop such ids (the equation stays, unnumbered)."""
    out, i = [], 0
    pat = re.compile(r" ?\{#eq-[^}]*\}")
    for m in pat.finditer(md):
        # find the enclosing '[' by scanning back with bracket depth
        depth, j = 0, m.start() - 1
        while j >= 0 and m.start() - j < 3000 and md[j-1:j+1] != "\n\n":
            c = md[j]
            if c == "]": depth += 1
            elif c == "[":
                if depth == 0: break
                depth -= 1
            j -= 1
        if j < 0 or md[j] != "[": continue
        # matching close
        depth, k = 0, j + 1
        while k < len(md):
            c = md[k]
            if c == "[": depth += 1
            elif c == "]":
                if depth == 0: break
                depth -= 1
            k += 1
        if md[k:k+9] == "]{.aside}":
            out.append((m.start(), m.end()))
    for a, b in reversed(out):
        md = md[:a] + md[b:]
    return md


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    texts = {}
    for path, stem in CHAPTERS:
        raw = (SRC / path).read_text(encoding="utf-8", errors="replace")
        texts[stem] = apply_source_typos(strip_comments(raw), stem)
    for path, stem in CHAPTERS:
        collect_labels(texts[stem], stem)
    sheet_tex = equation_sheet_tex()
    print(f"{len(LABELS)} labels collected")

    work = TOOLS / "_work"
    work.mkdir(exist_ok=True)
    jobs = CHAPTERS + [(EQUATION_SHEET[0], EQUATION_SHEET[1])]
    for path, stem in jobs:
        raw = sheet_tex if stem == EQUATION_SHEET[1] else texts[stem]
        (work / f"{stem}.tex").write_text(preprocess(raw, stem))
    for path, stem in jobs:
        print("converting", stem)
        md = run_pandoc((work / f"{stem}.tex").read_text(), stem)
        md = postprocess(md, stem)
        md = strip_ids_in_asides(md)
        raw_tex_leftovers(md, stem)
        if stem == "introduction":   # the eugenics section is part of the introduction in the book
            md += "\n{{< include _eugenics.qmd >}}\n"
        (OUT / f"{stem}.qmd").write_text(md)

    notes = ["# Editorial notes found in the LaTeX source\n",
             "These notes were written with the `\\graham{}` and `\\erin{}` todonotes commands. "
             "`todonotes` was loaded with the `disable` option, so none of them appeared in the released PDF "
             "and they have been removed from the Quarto text. They are preserved here.\n"]
    for stem, who, txt in EDITORIAL_TODOS:
        notes.append(f"- **{who}** in `{stem}`: {txt}")
    notes.append("\n# Draft notes that ARE visible in the released PDF\n")
    notes.append("These passages were wrapped in colour commands (`\\gc{}` green, `\\ec{}` blue, `\\sa{}` black); "
                 "the text was kept unchanged. Some are genuine content, some are reminders to the authors.\n")
    for stem, who, txt in VISIBLE_DRAFT_NOTES:
        notes.append(f"- `{who}` in `{stem}`: {txt}")
    (OUT / "TODO-from-source.md").write_text("\n".join(notes) + "\n")
    (TOOLS / "conversion-log.txt").write_text("\n".join(LOG) + "\n")
    print(f"done; {len(LOG)} warnings (see tools/conversion-log.txt)")


if __name__ == "__main__":
    main()
