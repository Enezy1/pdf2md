"""تجميع ملف Markdown النهائي من أجزاء الصفحات.

كل صفحة تبدأ بعلامتين:
  <!-- page:12 -->      يقرأها البرنامج لاحقاً (إسناد كل واقعة لملف وصفحة)
  **[صفحة 12]**        يقرأها الإنسان
وحالة الصفحة غير المكتملة تُكتب كتعليق <!-- status:... --> مع سطر توضيحي.
"""

from __future__ import annotations

from pathlib import Path

from . import checkpoint as cp
from .fsutil import write_markdown_file
from .tables import MergeStats, merge_tables

PLACEHOLDERS = {
    cp.PENDING_ENGINE: "[غير مقروء: صفحة مصوّرة بانتظار المحرّك المحلي]",
    cp.ERROR: "[غير مقروء: تعذّرت معالجة الصفحة]",
    cp.NEEDS_REVIEW: "[غير مقروء: بانتظار المراجعة]",
    cp.ILLEGIBLE: "[غير مقروء]",
    cp.UNRELIABLE: "[غير مقروء: ناتج المحرّك غير موثوق]",
    None: "[لم تُعالج بعد]",
}


def merged_fragments(state: cp.FileState) -> tuple[dict[int, str], MergeStats]:
    """نصوص الصفحات المكتملة بعد دمج الجداول الممتدة. أجزاء الصفحات على القرص لا تُعدَّل،
    فالدمج يُعاد حسابه عند كل تجميع (ويتغير تلقائياً إذا أُعيدت معالجة صفحة)."""
    pages = []
    for n in range(1, state.page_count + 1):
        p = state.page(n) or {}
        if p.get("status") == cp.DONE:
            pages.append((n, state.read_fragment(n).strip(), p.get("method", "")))
        else:
            pages.append((n, "", ""))
    return merge_tables(pages)


def build_markdown(state: cp.FileState, source_name: str, clean: bool = False, pages: list[int] | None = None,
                   part: int = 0) -> str:
    """pages/part: جزء من الكتاب (ص a–b) بدل الملف كله؛ الدمج بين الأجزاء يعيد بناءه الملف النهائي."""
    n_pages = state.page_count
    merged, _stats = merged_fragments(state)
    if clean:  # ذيل الصفحة المتكرر وأرقام الصفحات وبقايا التوقيع
        from .cleanup import strip_page_furniture
        merged = strip_page_furniture(merged)
    counts = state.counts()
    done = counts.get(cp.DONE, 0) + counts.get(cp.BLANK, 0) + counts.get(cp.ILLEGIBLE, 0)
    title = f"# {Path(source_name).stem}"
    if pages:
        title += f" — الجزء {part} (ص{pages[0]}–{pages[-1]})"
    out = [
        title,
        "",
        f"<!-- source: {source_name} | pages: {n_pages} | sha256: {state.file_hash} -->",
    ]
    providers = state.providers_summary()
    if providers:
        desc = " | ".join(f"{pid}: {_ranges(pages)}" for pid, pages in providers.items())
        out.append(f"<!-- providers: {desc} -->")
        out.append(f"> صفحات عولجت بمزوّد خارجي — {desc}")
    engines = state.engines_summary()
    if engines:
        out.append("<!-- engines: " + " | ".join(f"{e}: {_ranges(p)}" for e, p in engines.items()) + " -->")
    want = len(state.data.get("only_pages") or ()) or n_pages
    if done < want and not pages:
        out.append(f"<!-- incomplete: {done}/{want} -->")
    out.append("")

    only = state.data.get("only_pages")
    if only:
        out.insert(3, f"<!-- selected pages: {_ranges(only)} of {n_pages} -->")
    for n in (pages or only or range(1, n_pages + 1)):
        status = state.status(n)
        out.append(f"<!-- page:{n} -->")
        out.append(f"**[صفحة {n}]**")
        out.append("")
        if status == cp.DONE:
            check = (state.page(n) or {}).get("ocr_check")
            if check and not check.get("ok"):
                out.append(f"<!-- check:weak_match score={check.get('score')} -->")
            if (state.page(n) or {}).get("review_reason") == "corrector_failed":
                out.append("<!-- check:corrector_failed -->")
            warn = [w for w in (state.page(n) or {}).get("local_warning") or [] if w not in ("numbers",
                                                                                          "implausible_numbers")]
            if warn:
                from .ocr_router import LABELS
                out.append(f"<!-- check:local_unverified {','.join(warn)} -->")
                out.append(f"> ⚠ قراءة داخلية لم يؤكدها مزوّد خارجي ({'، '.join(LABELS.get(w, w) for w in warn)})"
                           " — قارنها بالأصل")
            if (state.page(n) or {}).get("unverified_numbers"):
                nums = ", ".join((state.page(n) or {})["unverified_numbers"])
                out.append(f"<!-- check:numbers_unverified {nums} -->")
                out.append(f"> ⚠ أرقام تحتاج مراجعة في هذه الصفحة: {nums}")
            method = (state.page(n) or {}).get("method", "")
            if method.startswith("api:"):
                out.append(f"<!-- provider:{method[4:]} -->")
            out.append(merged.get(n, "").strip())
            out.extend(_photo_lines(state.page(n)))
        elif status == cp.BLANK:
            if (state.page(n) or {}).get("photos"):
                out.extend(_photo_lines(state.page(n)))
            else:
                out.append("<!-- status:blank -->")
                out.append("_(صفحة فارغة)_")
        else:
            out.append(f"<!-- status:{status or 'todo'} -->")
            out.append(PLACEHOLDERS.get(status, PLACEHOLDERS[None]))
            page = state.page(n) or {}
            if page.get("review_reason"):
                out.append(f"<!-- review:{page['review_reason']} -->")
            if page.get("converted_reason"):
                out.append(f"<!-- reason:{page['converted_reason']} -->")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _photo_lines(page: dict | None) -> list[str]:
    from .images import photo_block
    out = []
    for ph in (page or {}).get("photos") or []:
        out += ["", photo_block(ph)]
    return out


def _ranges(pages: list[int]) -> str:
    parts, start, prev = [], None, None
    for p in pages:
        if start is None:
            start = prev = p
        elif p == prev + 1:
            prev = p
        else:
            parts.append(f"{start}-{prev}" if prev != start else str(start))
            start = prev = p
    if start is not None:
        parts.append(f"{start}-{prev}" if prev != start else str(start))
    return ",".join(parts)


def _page_list(state: cp.FileState) -> list[int]:
    return list(state.data.get("only_pages") or range(1, state.page_count + 1))


def part_path(docs_dir: Path, source_name: str, part: int) -> Path:
    return Path(docs_dir) / f"{Path(source_name).stem}.part{part:02d}.md"


def write_ready_parts(state: cp.FileState, source_name: str, docs_dir: Path, size: int, normalize: bool = True,
                      clean: bool = False) -> list[Path]:
    """يكتب كل جزء من `size` صفحة اكتملت صفحاته كلها ولم يُكتب بعد (الجزء المكتوب لا يُلمس ثانية)."""
    pages = _page_list(state)
    written = []
    for i in range(0, len(pages), size):
        block = pages[i:i + size]
        target = part_path(docs_dir, source_name, i // size + 1)
        if target.exists() or any(state.status(n) not in cp.FINAL_STATUSES for n in block):
            continue
        write_markdown_file(target, build_markdown(state, source_name, clean, block, i // size + 1), normalize)
        written.append(target)
    return written


def remove_parts(docs_dir: Path, source_name: str) -> int:
    """يحذف أجزاء الملف بعد دمجها في الملف النهائي."""
    n = 0
    for f in Path(docs_dir).glob(glob_escape(Path(source_name).stem) + ".part[0-9][0-9].md"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n


def glob_escape(name: str) -> str:
    import glob
    return glob.escape(name)


def write_markdown(state: cp.FileState, source_name: str, docs_dir: Path, normalize: bool = True,
                   clean: bool = False) -> Path:
    target = Path(docs_dir) / (Path(source_name).stem + ".md")
    write_markdown_file(target, build_markdown(state, source_name, clean), normalize)
    return target
