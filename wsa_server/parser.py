"""Response payload extraction and text cleanup for Google Gemini wrb.fr responses."""
from __future__ import annotations

import json
import re

_ZERO_WIDTH_CHARS = "\u200b\u200c\u200d\ufeff"


def clean_gemini_text(text: str, strip: bool = True) -> str:
    text = re.sub(
        r"```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?",
        "",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(rf"(?:^|(?<=\n))([ \t]*(?:`{{3,}}|~{{3,}}))[{_ZERO_WIDTH_CHARS}]+", r"\1", text)
    text = re.sub(
        rf"(?:^|(?<=\n))([ \t]*(?:`{{3,}}|~{{3,}})[ \t]*)_\]([ \t]*(?=\r?\n|$))",
        r"\1\2",
        text,
    )
    text = re.sub(rf"^\s*```[{_ZERO_WIDTH_CHARS}]*(?:tool_call|function_call)[ \t]*(?=[A-Za-z])", "", text)
    return text.strip() if strip else text


def extract_response_text(raw: str) -> str:
    bard_err = re.search(r"BardErrorInfo\s*\[(\d+)\]", raw)
    if bard_err:
        raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{bard_err.group(1)}]")
    texts = []
    for line in raw.split("\n"):
        if '"wrb.fr"' not in line or len(line) < 150:
            continue
        try:
            arr = json.loads(line)
            inner_str = arr[0][2]
            if not inner_str or len(inner_str) < 50:
                continue
            inner = json.loads(inner_str)
            if isinstance(inner, list) and len(inner) > 4 and inner[4]:
                for part in inner[4]:
                    if isinstance(part, list) and len(part) > 1 and isinstance(part[1], list):
                        for t in part[1]:
                            if isinstance(t, str) and t:
                                texts.append(t)
        except (json.JSONDecodeError, IndexError, TypeError):
            pass
    for t in reversed(texts):
        if t.strip():
            return clean_gemini_text(t)
    return ""


def extract_thinking_text(raw_or_cand) -> str:
    """Extract thinking trace from either a candidate list (cand[37][0][0]) or raw wrb.fr string."""
    if isinstance(raw_or_cand, list):
        try:
            if (
                len(raw_or_cand) > 37
                and isinstance(raw_or_cand[37], list)
                and raw_or_cand[37]
                and isinstance(raw_or_cand[37][0], list)
                and raw_or_cand[37][0]
                and isinstance(raw_or_cand[37][0][0], str)
            ):
                return raw_or_cand[37][0][0]
        except Exception:
            pass
        return ""
    if not isinstance(raw_or_cand, str):
        return ""
    thinking = ""
    for line in raw_or_cand.split("\n"):
        if '"wrb.fr"' not in line or len(line) < 100:
            continue
        try:
            arr = json.loads(line)
            inner_str = arr[0][2]
            if not inner_str:
                continue
            inner = json.loads(inner_str)
            if isinstance(inner, list) and len(inner) > 4 and inner[4]:
                t = extract_thinking_text(inner[4][0])
                if t:
                    thinking = t
        except Exception:
            pass
    return thinking.strip()
