#!/usr/bin/env python3
"""
translate_kz_clauses.py

Translate empty Kazakh clause paragraphs in a bilingual Word document by
calling the OpenAI API, preserving run-level formatting from the Russian source.

Usage:
    python3 translate_kz_clauses.py <input.docx> <output.docx> [--api-key KEY]

Environment:
    AI_API_KEY   — OpenAI-compatible API key (or pass --api-key)
    AI_MODEL     — model name (default: gpt-4o)
    AI_BASE_URL  — base URL (default: https://api.openai.com/v1)

Pairs translated (confirmed by document analysis):
    Para [12] "1."  <- translated from Para [18] (RU clause 1)
    Para [14] "2.." <- translated from Para [20] (RU clause 2)
"""

import argparse
import copy
import io
import json
import os
import re
import sys
import zipfile
from urllib.request import Request, urlopen

from lxml import etree as ET

# Word XML namespaces
W       = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_SPC = "http://www.w3.org/XML/1998/namespace"
TAG_P   = f"{{{W}}}p"
TAG_R   = f"{{{W}}}r"
TAG_T   = f"{{{W}}}t"
TAG_RPR = f"{{{W}}}rPr"
TAG_PPR = f"{{{W}}}pPr"
PRESERVE = f"{{{XML_SPC}}}space"

PAIRS = [
    {"kz_idx": 12, "ru_idx": 18},
    {"kz_idx": 14, "ru_idx": 20},
]

SYSTEM_PROMPT = """\
Translate the following Russian legal document paragraph into formal Kazakhstani \
official document Kazakh (is qagazdary tili / delovoy stil kazahskogo yazyka).

Rules:
- Keep ALL placeholders exactly as written: [Sotrudnik], [dolzhnost], [departament],
  [Gorod], [kolichestvodnei], [DenDataNachala], [DenDataKonec], [CelKomandirovaniya],
  [Sotrudnika] and any other token in square brackets -- copy verbatim, do NOT
  translate or alter the placeholder names in any way.
- Keep the company name «Kazahstansko-Kitayskiy Truboprovod» in Russian as-is.
- Keep «TOO» as-is.
- Keep date patterns like "20__ goda" as-is.
- Return ONLY the translated text. No explanation, no alternatives, no commentary.\
"""

SYSTEM_PROMPT = (
    "Translate the following Russian legal document paragraph into formal Kazakhstani "
    "official document Kazakh (\u0456\u0441 \u049b\u0430\u0493\u0430\u0437\u0434\u0430\u0440\u044b "
    "\u0442\u0456\u043b\u0456 / \u0434\u0435\u043b\u043e\u0432\u043e\u0439 \u0441\u0442\u0438\u043b\u044c "
    "\u043a\u0430\u0437\u0430\u0445\u0441\u043a\u043e\u0433\u043e \u044f\u0437\u044b\u043a\u0430).\n\n"
    "Rules:\n"
    "- Keep ALL placeholders exactly as written (tokens inside square brackets like "
    "[\u0421\u043e\u0442\u0440\u0443\u0434\u043d\u0438\u043a], "
    "[\u0434\u043e\u043b\u0436\u043d\u043e\u0441\u0442\u044c], "
    "[\u0434\u0435\u043f\u0430\u0440\u0442\u0430\u043c\u0435\u043d\u0442] etc.) \u2014 "
    "copy verbatim, do NOT translate placeholder names.\n"
    "- Keep the company name \u00ab\u041a\u0430\u0437\u0430\u0445\u0441\u0442\u0430\u043d\u0441\u043a\u043e-"
    "\u041a\u0438\u0442\u0430\u0439\u0441\u043a\u0438\u0439 \u0422\u0440\u0443\u0431\u043e\u043f\u0440\u043e"
    "\u0432\u043e\u0434\u00bb in Russian as-is (registered trade name).\n"
    "- Keep \u00ab\u0422\u041e\u041e\u00bb as-is.\n"
    "- Keep date patterns like \"20__ \u0433\u043e\u0434\u0430\" as-is.\n"
    "- Return ONLY the translated text. No explanation, no alternatives, no commentary."
)

PLACEHOLDER_RE = re.compile(r'\[[^\[\]\n]{1,120}\]')


def ai_translate(text, api_key, model, base_url):
    """Call OpenAI-compatible API to translate one paragraph."""
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": text},
        ],
        "temperature": 0.1,
        "max_tokens": 1024,
    }).encode("utf-8")
    req = Request(f"{base_url}/chat/completions", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    with urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())["choices"][0]["message"]["content"].strip()


def para_full_text(para):
    """Concatenate text of all runs including those nested in w:ins etc."""
    return "".join(
        (t.text or "")
        for r in para.iter(TAG_R)
        for t in r
        if t.tag == TAG_T
    )


def extract_placeholder_fmt(source_para):
    """
    Build dict: placeholder_text -> deepcopy of <w:rPr> (or None if plain).
    First occurrence of each placeholder wins.
    """
    fmt_map = {}
    for run in source_para.iter(TAG_R):
        rpr = run.find(TAG_RPR)
        run_text = "".join((t.text or "") for t in run if t.tag == TAG_T)
        for ph in PLACEHOLDER_RE.findall(run_text):
            if ph not in fmt_map:
                fmt_map[ph] = copy.deepcopy(rpr) if rpr is not None else None
    return fmt_map


def make_run(text, rpr_el=None):
    """Create a <w:r> with optional <w:rPr> and <w:t>."""
    r = ET.Element(TAG_R)
    if rpr_el is not None:
        r.append(copy.deepcopy(rpr_el))
    t = ET.SubElement(r, TAG_T)
    t.text = text
    if text and (text[0] == " " or text[-1] == " "):
        t.set(PRESERVE, "preserve")
    return r


def build_translated_runs(translated_text, placeholder_fmt):
    """
    Split translated_text on placeholder tokens.
    Each plain-text segment -> plain run.
    Each placeholder -> run with formatting copied from source paragraph.
    """
    # Split into alternating [plain, placeholder, plain, placeholder, ...]
    parts = PLACEHOLDER_RE.split(translated_text)
    placeholders = PLACEHOLDER_RE.findall(translated_text)

    runs = []
    for i, segment in enumerate(parts):
        if segment:
            runs.append(make_run(segment, rpr_el=None))
        if i < len(placeholders):
            ph  = placeholders[i]
            rpr = placeholder_fmt.get(ph)
            runs.append(make_run(ph, rpr_el=rpr))
    return runs


def replace_paragraph_runs(target_para, new_runs):
    """
    Remove all existing run-bearing children from target_para,
    then insert new_runs after <w:pPr> (if present).
    """
    # Collect children to remove: direct w:r AND any wrapper (w:ins, w:hyperlink...)
    # that itself contains w:r elements
    to_remove = []
    for ch in list(target_para):
        if ch.tag == TAG_PPR:
            continue
        if ch.tag == TAG_R or any(True for _ in ch.iter(TAG_R)):
            to_remove.append(ch)
    for el in to_remove:
        target_para.remove(el)

    # Insertion index: right after w:pPr
    children = list(target_para)
    ppr = target_para.find(TAG_PPR)
    insert_at = (children.index(ppr) + 1) if ppr is not None else 0

    for i, run in enumerate(new_runs):
        target_para.insert(insert_at + i, run)


def validate_placeholders(source_text, translated_text, label):
    src_ph  = set(PLACEHOLDER_RE.findall(source_text))
    out_ph  = set(PLACEHOLDER_RE.findall(translated_text))
    missing = src_ph - out_ph
    if missing:
        print(f"  WARNING [{label}]: placeholders missing in translation: {missing}",
              file=sys.stderr)
        return False
    extra = out_ph - src_ph
    if extra:
        print(f"  WARNING [{label}]: unexpected placeholders in translation: {extra}",
              file=sys.stderr)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input",      help="Input .docx file")
    parser.add_argument("output",     help="Output .docx file")
    parser.add_argument("--api-key",  default=None, help="OpenAI API key")
    parser.add_argument("--model",    default=None, help="Model name (default: gpt-4o)")
    parser.add_argument("--base-url", default=None, help="API base URL")
    parser.add_argument("--list-paras", action="store_true",
                        help="Print all paragraph texts and exit (useful for index verification)")
    args = parser.parse_args()

    api_key  = args.api_key  or os.environ.get("AI_API_KEY",  "").strip()
    model    = args.model    or os.environ.get("AI_MODEL",    "gpt-4o").strip() or "gpt-4o"
    base_url = (args.base_url or
                os.environ.get("AI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/"))

    # Unpack docx
    print(f"Reading {args.input} ...")
    with zipfile.ZipFile(args.input) as zin:
        names     = zin.namelist()
        zinfo_map = {zi.filename: zi for zi in zin.infolist()}
        files     = {n: zin.read(n) for n in names}

    if "word/document.xml" not in files:
        sys.exit("ERROR: Not a valid .docx (word/document.xml missing)")

    root      = ET.fromstring(files["word/document.xml"])
    all_paras = list(root.iter(TAG_P))
    print(f"Total paragraphs found: {len(all_paras)}")

    # --list-paras: dump all and exit (useful for confirming indices)
    if args.list_paras:
        for i, p in enumerate(all_paras):
            t = para_full_text(p)
            print(f"  [{i:3d}] {t[:100]!r}")
        return

    if not api_key:
        sys.exit("ERROR: AI_API_KEY not set. Use --api-key or export AI_API_KEY=...")

    # Process each pair
    any_changed = False
    for pair in PAIRS:
        kz_idx = pair["kz_idx"]
        ru_idx = pair["ru_idx"]
        label  = f"kz[{kz_idx}]<-ru[{ru_idx}]"

        if ru_idx >= len(all_paras) or kz_idx >= len(all_paras):
            print(f"\n  SKIP {label}: index out of range ({len(all_paras)} paras total)",
                  file=sys.stderr)
            continue

        source_para = all_paras[ru_idx]
        target_para = all_paras[kz_idx]
        source_text = para_full_text(source_para)
        target_text = para_full_text(target_para)

        print(f"\n{'='*60}")
        print(f"Pair {label}")
        print(f"  RU [{ru_idx}]: {source_text[:120]!r}")
        print(f"  KZ [{kz_idx}]: {target_text!r}")

        if not source_text.strip():
            print(f"  SKIP: source paragraph [{ru_idx}] is empty")
            continue

        # Translate
        print(f"  Calling {model} ...")
        try:
            translated = ai_translate(source_text, api_key, model, base_url)
        except Exception as exc:
            print(f"  ERROR: AI call failed: {exc}", file=sys.stderr)
            continue

        print(f"  KZ result: {translated[:120]!r}")

        if not validate_placeholders(source_text, translated, label):
            print(f"  SKIP [{kz_idx}]: placeholder mismatch — keeping original")
            continue

        # Build runs with formatting from source
        placeholder_fmt = extract_placeholder_fmt(source_para)
        print(f"  Placeholder format map: { {k: (v.tag if v is not None else 'plain') for k,v in placeholder_fmt.items()} }")

        new_runs = build_translated_runs(translated, placeholder_fmt)
        print(f"  Replacing para [{kz_idx}] with {len(new_runs)} new runs ...")
        replace_paragraph_runs(target_para, new_runs)
        any_changed = True
        print(f"  Done.")

    if not any_changed:
        print("\nNo paragraphs were changed. Output not written.", file=sys.stderr)
        sys.exit(1)

    # Reserialise and repack
    modified_xml = ET.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for name in names:
            data = modified_xml if name == "word/document.xml" else files[name]
            zout.writestr(zinfo_map[name], data)

    out_bytes = buf.getvalue()
    with open(args.output, "wb") as f:
        f.write(out_bytes)
    print(f"\nOutput written: {args.output} ({len(out_bytes):,} bytes)")

    # Validate output is a readable docx
    try:
        with zipfile.ZipFile(args.output) as zchk:
            ET.fromstring(zchk.read("word/document.xml"))
        print("Validation: XML parses OK")
    except Exception as exc:
        print(f"Validation WARNING: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
