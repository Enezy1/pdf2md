# Product

<!-- impeccable:product-schema 1 -->

## Platform

desktop

Windows desktop app (Tkinter + sv-ttk Fluent theme, follows system light/dark via darkdetect). Not web, iOS, or Android; native Windows 11 conventions apply.

## Users

The author is the only user: a personal tool for converting their own PDFs (mostly Arabic) into Markdown.

## Product Purpose

Turn PDFs into clean Markdown that serves as raw material for drafting reports. Success means text and tables are reliable enough to write from without going back to the PDF. A future phase (reserved in `style/`) will draft reports in the user's own writing style.

## Positioning

Privacy comes first, and the code enforces it, not just the UI: "local" mode is the default and blocks any outside connection that carries document content. Cloud OCR runs only in "cloud" mode, goes through the single `cloud_gate`, checks the file's fingerprint before each send, and logs each attempt to `sent_log.csv` without the document text.

## Operating Context

- Select files on the main window, choose the mode (local or cloud), start, and review the result.
- Only the Markdown file is written next to each PDF (or to a folder the user picks); work files (state, raw OCR, correction and review reports, index) live in `%TEMP%\pdf2md` and are cleaned after a week. There is a review window for flagged pages, an index builder, and summaries. See `DEVLOG.md` for the OCR.space + corrector chain.
- OCR engines: a local llama model, or cloud providers (OCR.space followed by an LLM text corrector, Gemini, OpenAI-compatible). API keys are stored with keyring or DPAPI.

## Capabilities and Constraints

- Arabic-first pipeline: an Arabic dictionary, table extraction, page quality scores, and cross-checks.
- Heavy work runs in background threads; the UI polls a queue every 100ms and must never freeze.
- Undecided: the report-drafting style feature (planned, not built).

## Brand Commitments

- The interface is Arabic only and right-to-left. All Arabic strings go through `theme.R()` (RLE embedding) and are wrapped at about 80 characters to work around Tk's bidi limits.
- Two mode names are kept: "داخلي" (local) and "عبر المواقع" (cloud).

## Evidence on Hand

- Sample PDFs: `samples/sample_mixed.pdf`, `samples/sample_tables.pdf`.
- UI screenshots in light and dark: `screenshots/` (main, done, settings tabs).
- No users, testimonials, or benchmarks beyond the user's own; do not invent any.

## Product Principles

1. Nothing leaves the device unless the user switches to cloud mode on purpose, and each send can be audited.
2. Output quality is judged by whether you can write a report from it.
3. A personal tool: short workflow, all settings kept in the settings window, a simple main window.
4. Respect modest hardware: models and features must degrade gracefully without a GPU.

## Accessibility & Inclusion

- Must stay legible on high-DPI and scaled displays, and with large text (sizes go through `theme.px`).
- Must run on modest PCs with no GPU.
