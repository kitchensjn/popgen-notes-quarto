# Population and Quantitative Genetics — Quarto edition

Automatic conversion of https://github.com/cooplab/popgen-notes (Graham Coop, CC BY 3.0) from Tufte-LaTeX to a Quarto book.

    quarto render --to html        # output in _book/

Regenerate the .qmd files from a fresh clone of the LaTeX source (needs pandoc, pdftoppm, ImageMagick):

    python3 tools/convert.py /path/to/popgen-notes .

`tools/convert.py` + `tools/popgen.lua` do the work; `tools/conversion-log.txt` lists warnings.
`TODO-from-source.md` keeps the author's hidden todonotes and visible draft notes.

Conventions: Tufte margin/side notes -> Quarto margin; `\ref`/`\eqref` -> `@fig-`/`@eq-`/`@tbl-`/`@sec-`;
`question` boxes -> numbered "Question" exercises; PDF figures converted to 200-dpi PNG in `images/`.
Math needs internet access (MathJax CDN) when viewing.
