---
name: convert-doc
description: Convert between Markdown and PDF formats
---

# /convert-doc - Document Conversion

Convert Markdown files to PDF or extract text from PDF to Markdown.

## Usage

```
/convert-doc report.md                    # MD to PDF (same directory)
/convert-doc report.md output.pdf         # MD to PDF (specify output)
/convert-doc document.pdf                 # PDF to MD (same directory)
/convert-doc document.pdf notes.md        # PDF to MD (specify output)
```

## Instructions

When the user invokes `/convert-doc <input> [output]`:

1. **Determine conversion direction** based on input file extension:
   - `.md` or `.markdown` → Convert to PDF
   - `.pdf` → Convert to Markdown

2. **For Markdown to PDF**:
   ```bash
   pandoc "<input>" -o "<output>.pdf" --pdf-engine=pdflatex -V geometry:margin=1in
   ```

   If pdflatex is not available, try:
   ```bash
   pandoc "<input>" -o "<output>.pdf" --pdf-engine=wkhtmltopdf
   ```

   Or use HTML intermediate:
   ```bash
   pandoc "<input>" -s -o "<output>.html" && wkhtmltopdf "<output>.html" "<output>.pdf"
   ```

3. **For PDF to Markdown**:
   ```bash
   pandoc "<input>" -o "<output>.md" --wrap=none
   ```

   If pandoc PDF extraction fails, use pdftotext as fallback:
   ```bash
   pdftotext -layout "<input>" - | pandoc -f plain -t markdown -o "<output>.md"
   ```

4. **Output file naming**:
   - If output not specified, use input filename with new extension
   - Example: `report.md` → `report.pdf`
   - Example: `document.pdf` → `document.md`

5. **After conversion**:
   - Confirm the output file was created
   - Report the file size
   - For PDF→MD, note if any formatting was lost

## Requirements

- `pandoc` - Document converter (install: `brew install pandoc`)
- For PDF output: `pdflatex` (via MacTeX) or `wkhtmltopdf`
- For PDF input: `pdftotext` (via `brew install poppler`) as fallback

## Examples

```bash
# Convert investigation notes to PDF
/convert-doc STELLA_MMINT_INVESTIGATION.md

# Convert a PDF spec to markdown for editing
/convert-doc "MMint Mocked Endpoints.pdf" mmint-endpoints.md

# Batch convert (use shell loop)
for f in *.md; do /convert-doc "$f"; done
```
