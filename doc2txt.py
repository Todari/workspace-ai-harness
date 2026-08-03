#!/usr/bin/env python3
"""한글(HWP 5.x / HWPX)·docx 문서에서 본문 텍스트를 뽑는다.

macOS에는 hwp를 읽는 기본 도구가 없어서 매번 파서를 다시 만들게 된다. 이 스크립트가
그 역할을 대신한다. 정부·기관 공고문이 대부분 hwp라 창업대회·지원사업 자료를 읽을 때 쓴다.

    python3 doc2txt.py "공고문.hwp"
    python3 doc2txt.py "양식.hwpx" "보고서.docx"

HWP 5.x는 OLE 복합문서(zlib 압축 섹션), HWPX와 docx는 ZIP+XML이다. PDF는 지원하지 않는다
(Read 도구가 PDF를 직접 읽으므로 그쪽을 쓸 것).
"""
import os
import re
import struct
import sys
import zipfile
import zlib

HWPTAG_PARA_TEXT = 67


def from_hwp(path):
    try:
        import olefile
    except ImportError:
        return ("[olefile 미설치] python3 -m pip install olefile 후 다시 실행하세요.")
    ole = olefile.OleFileIO(path)
    try:
        header = ole.openstream("FileHeader").read()
        compressed = bool(struct.unpack("<I", header[36:40])[0] & 1)
        out = []
        for stream in sorted(s for s in ole.listdir() if s and s[0] == "BodyText"):
            data = ole.openstream(stream).read()
            if compressed:
                data = zlib.decompress(data, -15)
            i = 0
            while i < len(data) - 4:
                rec = struct.unpack("<I", data[i:i + 4])[0]
                tag, size = rec & 0x3FF, (rec >> 20) & 0xFFF
                i += 4
                if tag == HWPTAG_PARA_TEXT:
                    text = data[i:i + size].decode("utf-16le", errors="ignore")
                    # 제어 문자(표·개체 마커)는 버리고 실제 글자만 남긴다
                    text = "".join(c for c in text if ord(c) > 31 or c in "\n\t")
                    if text.strip():
                        out.append(text)
                i += size
        return "\n".join(out)
    finally:
        ole.close()


def _unescape(text):
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                 ("&apos;", "'"), ("&amp;", "&")):
        text = text.replace(a, b)
    return text


def from_hwpx(path):
    out = []
    with zipfile.ZipFile(path) as z:
        for name in sorted(n for n in z.namelist()
                           if re.search(r"Contents/section\d+\.xml$", n)):
            xml = z.read(name).decode("utf-8", errors="ignore")
            xml = re.sub(r"<hp:lineBreak[^>]*/>|</hp:p>", "\n", xml)
            for m in re.finditer(r"<hp:t[^>]*>(.*?)</hp:t>", xml, re.S):
                out.append(re.sub(r"<[^>]+>", "", m.group(1)))
            out.append("\n")
    return _unescape("".join(out))


def from_docx(path):
    out = []
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
    xml = re.sub(r"</w:p>|<w:br[^>]*/>", "\n", xml)
    for m in re.finditer(r"<w:t[^>]*>(.*?)</w:t>", xml, re.S):
        out.append(re.sub(r"<[^>]+>", "", m.group(1)))
    return _unescape("".join(out))


def _is_mojibake(line):
    """ASCII 바이트를 UTF-16LE로 잘못 읽어 생긴 한자 덩어리를 걸러낸다.

    HWP 본문 스트림에는 텍스트 태그를 달고 있지만 실제로는 내부 식별자인 레코드가 섞여 있다.
    한글도 ASCII도 없이 CJK 한자만 늘어선 줄이 그 흔적이다. 한자어가 섞인 정상 문장은
    한글이나 ASCII를 포함하므로 걸러지지 않는다.
    """
    if not line or any("가" <= c <= "힣" for c in line):
        return False
    if any(c.isascii() and c.isalnum() for c in line):
        return False
    cjk = sum(1 for c in line if "一" <= c <= "鿿")
    return cjk >= 2


def extract(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".hwp":
        text = from_hwp(path)
    elif ext == ".hwpx":
        text = from_hwpx(path)
    elif ext == ".docx":
        text = from_docx(path)
    else:
        return f"[지원하지 않는 형식: {ext}] hwp·hwpx·docx만 처리한다. PDF는 Read 도구를 쓸 것."
    lines = (re.sub(r"[ \t ]+", " ", l).strip() for l in text.splitlines())
    return "\n".join(l for l in lines if l and not _is_mojibake(l))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    for path in sys.argv[1:]:
        if len(sys.argv) > 2:
            print(f"\n{'=' * 70}\n{os.path.basename(path)}\n{'=' * 70}")
        if not os.path.exists(path):
            print(f"[없음] {path}")
            continue
        print(extract(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
