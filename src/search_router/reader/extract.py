import io
import json
import re
import sys

from pypdf import PdfReader


def decode_text(content: bytes, charset: str | None = None, html: bool = True) -> str:
    if content.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return content.decode("utf-32")
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content.decode("utf-16")
    if content.startswith(b"\xef\xbb\xbf"):
        return content.decode("utf-8-sig")
    if not charset and html:
        match = re.search(rb'charset\s*=\s*["\']?\s*([A-Za-z0-9._-]+)', content[:4096], re.I)
        charset = match.group(1).decode("ascii") if match else "utf-8"
    return content.decode(charset or "utf-8", errors="strict")


def extract(content: bytes, content_type: str, limit: int, max_pages: int) -> dict:
    warnings = []
    truncated = False
    if content_type == "application/pdf":
        document = PdfReader(io.BytesIO(content))
        if document.is_encrypted:
            raise ValueError("encrypted PDF unsupported")
        parts = []
        size = 0
        for page in document.pages[:max_pages]:
            text = page.extract_text() or ""
            parts.append(text)
            size += len(text) + 1
            if size > limit:
                truncated = True
                break
        text = "\n".join(parts)
        truncated = truncated or len(document.pages) > max_pages
        warnings = ["pdf_text_only_no_ocr_layout_may_differ"]
    elif content_type in {"text/html", "application/xhtml+xml"}:
        raise ValueError("HTML requires the mdream reader")
    else:
        text = content.decode("utf-8", errors="strict")
    return {"text": text[:limit], "truncated": truncated or len(text) > limit, "warnings": warnings}


def main():
    content_type, limit, pages = sys.argv[1:]
    print(json.dumps(extract(sys.stdin.buffer.read(), content_type, int(limit), int(pages))))


if __name__ == "__main__":
    main()
