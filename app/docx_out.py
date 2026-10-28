"""تحويل Markdown البرنامج إلى ملف Word (.docx) بلا مكتبات إضافية (zipfile + XML).

العربية: كل فقرة عربية تُكتب من اليمين لليسار (w:bidi)، والجدول بترتيب أعمدة معكوس (w:bidiVisual)،
والمحاذاة بداية السطر (اليمين) أو ضبط الطرفين، والخط المركّب (cs) لا يتغير حجمه.
الإنجليزية تبقى من اليسار لليمين. الفقرة المختلطة يقرّر اتجاهها الحرف الأغلب.
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from .fsutil import atomic_write_bytes

FONT = "Arial"
BODY_PT = 12
_AR = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
_LAT = re.compile(r"[A-Za-z]")
_BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_SEP_ROW = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_PAGE_MARK = re.compile(r"^\*\*\[(?:صفحة|page)\s*\d+\]\*\*$")
_INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`|(?<![\w*])\*[^*\s][^*]*?\*(?![\w*])|(?<!\w)_[^_\s][^_]*?_(?!\w))")


def _x(s: str) -> str:
    return escape(_BAD_XML.sub("", s))


def _is_rtl(text: str, default: bool) -> bool:
    ar, lat = len(_AR.findall(text)), len(_LAT.findall(text))
    if ar == 0 and lat == 0:
        return default
    return ar >= lat


def _html_to_text(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</?(div|p|span|b|i|u|strong|em|font|center)[^>]*>", "", s, flags=re.I)
    return s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


# ---------------------------------------------------------------- الأسطر المضمّنة
def _runs(text: str, rtl: bool, bold=False, italic=False, size: int | None = None, color: str = "") -> str:
    """نص Markdown مضمّن (غامق/مائل/كود) ⇒ w:r متتالية."""
    out = []
    for part in _INLINE.split(text.replace("\\|", "|")):
        if not part:
            continue
        b, i, code = bold, italic, False
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            part, b = part[2:-2], True
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            part, code = part[1:-1], True
        elif len(part) > 2 and part[0] in "*_" and part[-1] == part[0]:
            part, i = part[1:-1], True
        part = re.sub(r"!?\[([^\]]*)\]\(([^)]*)\)", r"\1", part)   # الروابط والصور: النص فقط
        for k, seg in enumerate(part.split("\n")):
            if k:
                out.append("<w:r><w:br/></w:r>")
            if seg:
                out.append(_run(seg, rtl, b, i, code, size, color))
    return "".join(out)


def _run(text: str, rtl: bool, bold: bool, italic: bool, code: bool, size: int | None, color: str) -> str:
    # w:rtl على المقطع العربي فقط؛ الأرقام واللاتينية داخل فقرة عربية يرتّبها خوارزم الاتجاه
    ar = rtl and bool(_AR.search(text))
    props = []
    if code:
        props.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>')
    if bold:
        props.append("<w:b/><w:bCs/>")
    if italic:
        props.append("<w:i/><w:iCs/>")
    if color:
        props.append(f'<w:color w:val="{color}"/>')
    if size:
        props.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
    if ar:
        props.append('<w:rtl/><w:lang w:bidi="ar-SA"/>')
    rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{_x(text)}</w:t></w:r>'


# ---------------------------------------------------------------- الفقرات
def _para(runs: str, rtl: bool, style: str = "", justify: bool = False, ind: tuple[int, int] | None = None,
          shade: str = "", keep_next: bool = False, align: str = "") -> str:
    p = []
    if style:
        p.append(f'<w:pStyle w:val="{style}"/>')
    if keep_next:
        p.append("<w:keepNext/>")
    if rtl:
        p.append("<w:bidi/>")
    if shade:
        p.append(f'<w:shd w:val="clear" w:color="auto" w:fill="{shade}"/>')
    if ind:
        # في الفقرة العربية "left" هي جهة البداية (اليمين) — هكذا يفسّرها Word
        p.append(f'<w:ind w:left="{ind[0]}" w:hanging="{ind[1]}"/>' if ind[1] else f'<w:ind w:left="{ind[0]}"/>')
    if align:
        p.append(f'<w:jc w:val="{align}"/>')
    elif justify:
        p.append('<w:jc w:val="both"/>')
    return f"<w:p><w:pPr>{''.join(p)}</w:pPr>{runs}</w:p>"


def _table(rows: list[list[str]], rtl: bool) -> str:
    ncol = max(len(r) for r in rows)
    grid = "".join('<w:gridCol w:w="2000"/>' for _ in range(ncol))
    trs = []
    for ri, row in enumerate(rows):
        row = row + [""] * (ncol - len(row))
        tcs = []
        for cell in row:
            text = _html_to_text(cell).strip()
            crtl = _is_rtl(text, rtl)
            shade = '<w:shd w:val="clear" w:color="auto" w:fill="E7EDF3"/>' if ri == 0 else ""
            runs = _runs(text, crtl, bold=(ri == 0), size=2 * BODY_PT - 2)
            tcs.append(f'<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/>{shade}</w:tcPr>'
                       f'{_para(runs, crtl, style="TableText")}</w:tc>')
        hdr = "<w:trPr><w:cantSplit/><w:tblHeader/></w:trPr>" if ri == 0 else "<w:trPr><w:cantSplit/></w:trPr>"
        trs.append(f"<w:tr>{hdr}{''.join(tcs)}</w:tr>")
    vis = "<w:bidiVisual/>" if rtl else ""
    return (f'<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="5000" w:type="pct"/>{vis}'
            '<w:tblLayout w:type="autofit"/><w:tblLook w:val="04A0"/></w:tblPr>'
            f"<w:tblGrid>{grid}</w:tblGrid>{''.join(trs)}</w:tbl>"
            + _para("", rtl, style="TableText"))   # فقرة فاصلة: Word يدمج الجدولين المتجاورين بدونها


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)]


def _body(md: str, doc_rtl: bool) -> str:
    md = _COMMENT.sub("", md)
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    i = 0

    def flush() -> None:
        if para:
            text = _html_to_text(" ".join(s.strip() for s in para))
            rtl = _is_rtl(text, doc_rtl)
            out.append(_para(_runs(text, rtl), rtl, justify=rtl))
            para.clear()

    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if not s:
            flush()
        elif s.startswith("```"):
            flush()
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                rtl = _is_rtl(lines[i], False)
                out.append(_para(_runs(lines[i], rtl), rtl, style="Code"))
                i += 1
        elif re.match(r"^#{1,6}\s", s):
            flush()
            level = min(len(s) - len(s.lstrip("#")), 4)
            text = _html_to_text(s.lstrip("#").strip())
            rtl = _is_rtl(text, doc_rtl)
            out.append(_para(_runs(text, rtl), rtl, style=f"Heading{level}", keep_next=True))
        elif _PAGE_MARK.match(s):
            flush()
            rtl = doc_rtl
            out.append(_para(_runs(s.strip("*"), rtl, size=18, color="808080"), rtl, style="PageMark", keep_next=True))
        elif re.match(r"^(-{3,}|\*{3,}|_{3,})$", s):
            flush()
            out.append('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="BFBFBF"/>'
                       "</w:pBdr></w:pPr></w:p>")
        elif s.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not _SEP_ROW.match(lines[i].strip()):
                    rows.append(_split_row(lines[i]))
                i += 1
            i -= 1
            if rows:
                sample = " ".join(c for r in rows[:6] for c in r)
                out.append(_table(rows, _is_rtl(sample, doc_rtl)))
        elif s.startswith(">"):
            flush()
            block = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                block.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            i -= 1
            for q in block:
                text = _html_to_text(q).strip()
                if text:
                    rtl = _is_rtl(text, doc_rtl)
                    out.append(_para(_runs(text, rtl), rtl, style="Quote", shade="F2F2F2"))
        elif re.match(r"^([-*+•]|\d+[.)])\s+", s):
            flush()
            m = re.match(r"^([-*+•]|\d+[.)])\s+(.*)$", s)
            mark = "•" if m.group(1) in "-*+•" else m.group(1)
            text = _html_to_text(m.group(2))
            rtl = _is_rtl(text, doc_rtl)
            out.append(_para(_runs(f"{mark}\t", rtl) + _runs(text, rtl), rtl, style="ListItem", ind=(720, 360)))
        else:
            para.append(ln)
        i += 1
    flush()
    return "".join(out)


# ---------------------------------------------------------------- الحزمة
def _styles(rtl: bool) -> str:
    sz, szc = 2 * BODY_PT, 2 * BODY_PT
    f = f'<w:rFonts w:ascii="{FONT}" w:hAnsi="{FONT}" w:eastAsia="{FONT}" w:cs="{FONT}"/>'

    def h(n: int, pt: int) -> str:
        return (f'<w:style w:type="paragraph" w:styleId="Heading{n}"><w:name w:val="heading {n}"/>'
                '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
                f'<w:pPr><w:keepNext/><w:spacing w:before="{360 - n * 40}" w:after="120"/><w:outlineLvl w:val="{n - 1}"/></w:pPr>'
                f'<w:rPr><w:b/><w:bCs/><w:color w:val="1F3A5F"/><w:sz w:val="{pt * 2}"/><w:szCs w:val="{pt * 2}"/></w:rPr>'
                "</w:style>")

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:docDefaults><w:rPrDefault><w:rPr>{f}<w:sz w:val="{sz}"/><w:szCs w:val="{szc}"/>'
        '<w:lang w:val="en-US" w:eastAsia="en-US" w:bidi="ar-SA"/></w:rPr></w:rPrDefault>'
        '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="300" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
        "</w:docDefaults>"
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
        + h(1, 20) + h(2, 16) + h(3, 14) + h(4, 13) +
        '<w:style w:type="paragraph" w:styleId="PageMark"><w:name w:val="Page Mark"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:spacing w:before="240" w:after="60"/></w:pPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:ind w:left="360" w:right="360"/></w:pPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="ListItem"><w:name w:val="List Item"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:tabs><w:tab w:val="num" w:pos="720"/></w:tabs><w:spacing w:after="60"/></w:pPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="TableText"><w:name w:val="Table Text"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:spacing w:before="40" w:after="40" w:line="240" w:lineRule="auto"/></w:pPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Code"><w:name w:val="Code"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:spacing w:after="0"/></w:pPr><w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>'
        '<w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr></w:style>'
        '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/>'
        '<w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar><w:top w:w="0" w:type="dxa"/>'
        '<w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/><w:right w:w="108" w:type="dxa"/>'
        "</w:tblCellMar></w:tblPr></w:style>"
        '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:basedOn w:val="TableNormal"/>'
        '<w:tblPr><w:tblBorders>'
        + "".join(f'<w:{e} w:val="single" w:sz="4" w:space="0" w:color="A6A6A6"/>'
                  for e in ("top", "left", "bottom", "right", "insideH", "insideV")) +
        "</w:tblBorders></w:tblPr></w:style>"
        "</w:styles>"
    )


_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')


def markdown_to_docx_bytes(md: str, title: str = "", rtl: bool | None = None) -> bytes:
    """rtl=None: يُحدَّد من لغة المستند الغالبة."""
    visible = _COMMENT.sub("", md)
    doc_rtl = (len(_AR.findall(visible)) >= len(_LAT.findall(visible)) and bool(_AR.search(visible))
               if rtl is None else rtl)
    body = _body(md, doc_rtl)
    page = ("<w:sectPr><w:footerReference w:type=\"default\" r:id=\"rId3\"/>"
            '<w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="567" w:footer="567" w:gutter="0"/>'
            + ("<w:bidi/>" if doc_rtl else "") + "</w:sectPr>")
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {_NS}><w:body>'
                f"{body}{page}</w:body></w:document>")
    footer = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:ftr {_NS}>'
              '<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
              '<w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
              '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p></w:ftr>')
    settings = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:settings {_NS}>'
                '<w:themeFontLang w:val="en-US" w:bidi="ar-SA"/>'
                '<w:compat><w:compatSetting w:name="compatibilityMode" '
                'w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat></w:settings>')
    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>' + _x(title) + "</dc:title>"
            '<dc:language>' + ("ar-SA" if doc_rtl else "en-US") + "</dc:language></cp:coreProperties>")
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
          '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
          '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
          '<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
          '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
          "</Types>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            "</Relationships>")
    doc_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>'
                '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>'
                "</Relationships>")
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/styles.xml", _styles(doc_rtl))
        z.writestr("word/settings.xml", settings)
        z.writestr("word/footer1.xml", footer)
        z.writestr("docProps/core.xml", core)
    return buf.getvalue()


def write_docx(md: str, target: Path, title: str = "", rtl: bool | None = None) -> Path:
    target = Path(target)
    atomic_write_bytes(target, markdown_to_docx_bytes(md, title or target.stem, rtl))
    return target


def export_markdown_file(md_path: Path, rtl: bool | None = None) -> Path:
    """يحوّل ملف .md موجود إلى .docx بجانبه."""
    md_path = Path(md_path)
    return write_docx(md_path.read_text(encoding="utf-8"), md_path.with_suffix(".docx"), md_path.stem, rtl)
