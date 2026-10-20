# -*- coding: utf-8 -*-
"""hwpx 내보내기. assets/base_template.hwpx(검토보고서 양식)의 문단 서식(제목·큰 제목·소제목·본문)을
복제해 새 본문을 조립한다. 표지 표·목차는 쓰지 않는다. 네임스페이스 보존을 위해 lxml 을 쓴다."""
import copy
import io
import os
import re
import zipfile

from lxml import etree as ET

BASE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(BASE, "assets", "base_template.hwpx")
HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"


def _q(tag):
    return f"{{{HP}}}{tag}"


def _text_of(p):
    return "".join(t.text or "" for t in p.iter(_q("t")))


def _strip_cache(el):
    for parent in list(el.iter()):
        for ch in list(parent):
            if ch.tag == _q("linesegarray"):
                parent.remove(ch)


def _set_text(p, text):
    """문단의 첫 <hp:t> 에 text 를 넣고 나머지 <hp:t> 는 비운다(서식은 그대로)."""
    ts = list(p.iter(_q("t")))
    if not ts:
        run = p.find(_q("run"))
        if run is None:
            run = ET.SubElement(p, _q("run"))
        ts = [ET.SubElement(run, _q("t"))]
    for t in ts:
        for ch in list(t):
            t.remove(ch)
        t.text = ""
    ts[0].text = text
    p.set("pageBreak", "0")
    _strip_cache(p)
    return p


def _protos(root):
    """양식에서 역할별 원형 문단을 찾는다."""
    paras = list(root)
    first = paras[0]                                      # secPr(용지·여백) 보유
    pick = {}
    for p in paras:
        tx = _text_of(p).strip()
        sid = p.get("styleIDRef")
        if tx == "도입 검토보고서" and "title" not in pick:
            pick["title"] = p
        elif tx.startswith("문서번호") and "meta" not in pick:
            pick["meta"] = p
        elif tx == "[검토 대상 업무명]" and "subtitle" not in pick:
            pick["subtitle"] = p
        elif sid == "1" and p.get("pageBreak") == "1" and "h1" not in pick:
            pick["h1"] = p
        elif sid == "2" and "h2" not in pick:
            pick["h2"] = p
        elif sid == "13" and tx.startswith("[이 검토를") and "body" not in pick:
            pick["body"] = p
        elif sid == "0" and not tx and p is not first and p.find(".//" + _q("tbl")) is None and "blank" not in pick:
            pick["blank"] = p
    return first, pick


def build_hwpx(title, subtitle, meta_lines, blocks):
    """blocks: [(kind, text)] kind in h1|h2|body|blank. 결과 hwpx 바이트를 돌려준다."""
    with zipfile.ZipFile(TEMPLATE) as zin:
        names = zin.namelist()
        files = {n: zin.read(n) for n in names}
        infos = {n: zin.getinfo(n) for n in names}

    root = ET.fromstring(files["Contents/section0.xml"])
    first, proto = _protos(root)
    first_copy = copy.deepcopy(first)
    for t in first_copy.iter(_q("t")):
        t.text = ""
    _strip_cache(first_copy)

    for ch in list(root):
        root.remove(ch)
    root.append(first_copy)

    def add(kind, text):
        src = proto.get(kind)
        if src is None:
            src = proto["body"]
        root.append(_set_text(copy.deepcopy(src), text))

    for m in meta_lines:
        add("meta", m)
    add("blank", "")
    add("subtitle", subtitle)
    add("title", title)
    add("blank", "")
    for kind, text in blocks:
        add(kind if kind in ("h1", "h2", "body", "blank") else "body", text)

    files["Contents/section0.xml"] = ET.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True)

    hpf = files["Contents/content.hpf"].decode("utf-8")
    hpf = re.sub(r"<opf:title>.*?</opf:title>", f"<opf:title>{_esc(title)}</opf:title>", hpf, count=1)
    files["Contents/content.hpf"] = hpf.encode("utf-8")
    preview = "\r\n".join([*meta_lines, subtitle, title] + [t for _, t in blocks if t][:40])
    files["Preview/PrvText.txt"] = preview.encode("utf-8")
    if "Preview/PrvImage.png" in files:
        files["Preview/PrvImage.png"] = _blank_png()

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zout:
        for n in names:                                   # mimetype 첫 항목·무압축 유지
            ctype = zipfile.ZIP_STORED if n == "mimetype" else zipfile.ZIP_DEFLATED
            zi = zipfile.ZipInfo(n, date_time=infos[n].date_time)
            zi.compress_type = ctype
            zout.writestr(zi, files[n])
    return out.getvalue()


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _blank_png():
    import struct
    import zlib
    w, h = 1, 1
    raw = b"\x00\xff\xff\xff"
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw[:1] + raw[1:])) + chunk(b"IEND", b""))


def text_to_blocks(body):
    """편집 본문(# 큰 제목, ## 소제목, 나머지 본문) → blocks."""
    blocks = []
    for line in (body or "").splitlines():
        s = line.rstrip()
        if not s.strip():
            blocks.append(("blank", ""))
        elif s.startswith("## "):
            blocks.append(("h2", s[3:].strip()))
        elif s.startswith("# "):
            blocks.append(("h1", s[2:].strip()))
        else:
            blocks.append(("body", s))
    # 연속 빈 줄 정리
    out = []
    for b in blocks:
        if b[0] == "blank" and out and out[-1][0] == "blank":
            continue
        out.append(b)
    return out
