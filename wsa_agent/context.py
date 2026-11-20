"""
Deterministic context transfer, AST/structural file outlining, file staleness ledger,
and complexity classification for single-agent and multi-role subagent workflows.
"""
from __future__ import annotations

import ast
import hashlib
import re
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def compute_sha256_text(text: str) -> str:
    # Normalize CRLF -> LF so raw byte reads and Python universal-newline writes match on Windows.
    norm = (text or "").replace("\r\n", "\n")
    return hashlib.sha256(norm.encode("utf-8", errors="replace")).hexdigest()


def _merge_ranges(ranges: List[List[int]]) -> List[List[int]]:
    if not ranges:
        return []
    ordered = sorted(([int(a), int(b)] for a, b in ranges if int(b) >= int(a)), key=lambda x: (x[0], x[1]))
    merged: List[List[int]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return merged


def _range_covered(ranges: List[List[int]], start_line: int, end_line: int) -> bool:
    for s, e in ranges:
        if s <= start_line and end_line <= e:
            return True
    return False


# ── Language-Aware File Outliner (AST + Structural Regex) ────────────────────

_JS_FN_RE = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(([^)]*)\)"
)
_JS_CONST_FN_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>"
)
_JS_CLASS_RE = re.compile(r"^\s*(?:export\s+(?:default\s+)?)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)")
_HTML_ID_RE = re.compile(r'id=["\']([A-Za-z0-9_-]{2,64})["\']')
_CSS_SELECTOR_RE = re.compile(r"^\s*([.#@]?[A-Za-z0-9_.*:, >~+\[\]=\-\"'()@/]{1,80})\s*\{")
_MD_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
_SH_FN_RE = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_-]*)\s*\(\s*\)\s*\{")


def _extract_python_outline(text: str, max_symbols: int = 80) -> Tuple[List[Dict[str, Any]], List[str], Optional[str]]:
    symbols: List[Dict[str, Any]] = []
    imports: List[str] = []
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return symbols, imports, f"SyntaxError at L{exc.lineno}: {exc.msg}"

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names[:4]:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or "."
            names = ", ".join(a.name for a in node.names[:4])
            imports.append(f"{mod} ({names})")
        elif isinstance(node, ast.ClassDef):
            end_ln = getattr(node, "end_lineno", node.lineno) or node.lineno
            bases = ", ".join(ast.unparse(b) for b in node.bases[:3]) if getattr(node, "bases", None) else ""
            sig = f"class {node.name}({bases})" if bases else f"class {node.name}"
            symbols.append({"kind": "class", "name": node.name, "sig": sig, "start": node.lineno, "end": end_ln})
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sub_end = getattr(sub, "end_lineno", sub.lineno) or sub.lineno
                    arg_names = [a.arg for a in sub.args.args[:6]]
                    if len(sub.args.args) > 6:
                        arg_names.append("...")
                    prefix = "async def" if isinstance(sub, ast.AsyncFunctionDef) else "def"
                    symbols.append({
                        "kind": "method",
                        "name": f"{node.name}.{sub.name}",
                        "sig": f"  {prefix} {node.name}.{sub.name}({', '.join(arg_names)})",
                        "start": sub.lineno,
                        "end": sub_end,
                    })
                    if len(symbols) >= max_symbols:
                        return symbols, imports[:12], None
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_ln = getattr(node, "end_lineno", node.lineno) or node.lineno
            arg_names = [a.arg for a in node.args.args[:6]]
            if len(node.args.args) > 6:
                arg_names.append("...")
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            symbols.append({
                "kind": "func",
                "name": node.name,
                "sig": f"{prefix} {node.name}({', '.join(arg_names)})",
                "start": node.lineno,
                "end": end_ln,
            })
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and (tgt.id.isupper() or tgt.id.startswith("_")):
                    end_ln = getattr(node, "end_lineno", node.lineno) or node.lineno
                    symbols.append({
                        "kind": "const",
                        "name": tgt.id,
                        "sig": tgt.id,
                        "start": node.lineno,
                        "end": end_ln,
                    })
        if len(symbols) >= max_symbols:
            break
    return symbols[:max_symbols], imports[:12], None


def extract_file_outline(path: Path, text: Optional[str] = None, max_symbols: int = 80) -> Dict[str, Any]:
    """Extract deterministic language-aware structural symbols and metadata for `path`."""
    if text is None:
        text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    ext = path.suffix.lower()
    symbols: List[Dict[str, Any]] = []
    imports: List[str] = []
    parse_err: Optional[str] = None

    if ext in (".py", ".pyw"):
        symbols, imports, parse_err = _extract_python_outline(text, max_symbols=max_symbols)
    elif ext in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"):
        for idx, line in enumerate(lines, 1):
            if m := _JS_CLASS_RE.match(line):
                symbols.append({"kind": "class", "name": m.group(1), "sig": f"class {m.group(1)}", "start": idx, "end": idx})
            elif m := _JS_FN_RE.match(line):
                args = " ".join(m.group(2).split())[:48]
                symbols.append({"kind": "func", "name": m.group(1), "sig": f"function {m.group(1)}({args})", "start": idx, "end": idx})
            elif m := _JS_CONST_FN_RE.match(line):
                symbols.append({"kind": "func", "name": m.group(1), "sig": f"const {m.group(1)} = =>", "start": idx, "end": idx})
            if len(symbols) >= max_symbols:
                break
    elif ext in (".html", ".htm"):
        seen_ids = set()
        for idx, line in enumerate(lines, 1):
            for hid in _HTML_ID_RE.findall(line):
                if hid not in seen_ids:
                    seen_ids.add(hid)
                    symbols.append({"kind": "id", "name": f"#{hid}", "sig": f"#{hid}", "start": idx, "end": idx})
                    if len(symbols) >= max_symbols:
                        break
            if len(symbols) >= max_symbols:
                break
    elif ext == ".css":
        for idx, line in enumerate(lines, 1):
            if m := _CSS_SELECTOR_RE.match(line):
                sel = " ".join(m.group(1).split())
                symbols.append({"kind": "selector", "name": sel, "sig": sel, "start": idx, "end": idx})
                if len(symbols) >= max_symbols:
                    break
    elif ext in (".md", ".markdown"):
        for idx, line in enumerate(lines, 1):
            if m := _MD_HEADING_RE.match(line):
                symbols.append({"kind": "heading", "name": m.group(2), "sig": f"{m.group(1)} {m.group(2)}", "start": idx, "end": idx})
                if len(symbols) >= max_symbols:
                    break
    elif ext in (".sh", ".bash", ".ps1"):
        for idx, line in enumerate(lines, 1):
            if m := _SH_FN_RE.match(line):
                symbols.append({"kind": "func", "name": m.group(1), "sig": f"{m.group(1)}()", "start": idx, "end": idx})
                if len(symbols) >= max_symbols:
                    break

    return {
        "ext": ext or "(none)",
        "total_lines": len(lines),
        "bytes": len(text.encode("utf-8", errors="replace")),
        "sha256": compute_sha256_text(text),
        "imports": imports,
        "symbols": symbols,
        "parse_error": parse_err,
    }


def format_file_outline(rel_path: str, outline: Dict[str, Any]) -> str:
    """Format an outline dictionary into a compact, readable line-indexed report."""
    lines = [
        f"{rel_path} ({outline.get('total_lines', 0)} lines, {outline.get('bytes', 0):,} bytes, sha:{str(outline.get('sha256', ''))[:8]})"
    ]
    if outline.get("parse_error"):
        lines.append(f"[Parse Warning] {outline['parse_error']}")
    if outline.get("imports"):
        lines.append("Imports: " + ", ".join(outline["imports"][:10]))
    syms = outline.get("symbols") or []
    if syms:
        lines.append(f"Symbols ({len(syms)}):")
        for sym in syms:
            s = sym.get("start", 1)
            e = sym.get("end", s)
            rng = f"L{s}-{e}" if e != s else f"L{s}"
            lines.append(f"  {rng:>11} | {sym.get('sig') or sym.get('name')}")
    else:
        lines.append("Symbols: (none extracted — use read_file with start_line/end_line)")
    return "\n".join(lines)


# ── Session File Context Ledger ──────────────────────────────────────────────

class FileContextLedger:
    """Tracks read ranges, file sha256/mtime staleness, symbols, and edit history."""

    _MAX_ENTRIES = 250

    def __init__(self, _project_root: Any = None) -> None:
        self.entries: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def clear(self) -> None:
        with self._lock:
            self.entries.clear()

    def _prune_if_needed_locked(self) -> None:
        if len(self.entries) <= self._MAX_ENTRIES:
            return
        excess = len(self.entries) - self._MAX_ENTRIES
        # Evict oldest unedited/unverified entries first to keep edited working-set intact.
        unedited = [
            k for k, v in self.entries.items()
            if not v.get("edited") and not v.get("verified")
        ]
        for k in unedited[:excess]:
            self.entries.pop(k, None)
        while len(self.entries) > self._MAX_ENTRIES:
            oldest = next(iter(self.entries))
            self.entries.pop(oldest, None)

    def _ensure_entry(self, rel_path: str) -> Dict[str, Any]:
        norm = str(rel_path).replace("\\", "/")
        if norm not in self.entries:
            self.entries[norm] = {
                "sha256": "",
                "size": 0,
                "mtime_ns": 0,
                "total_lines": 0,
                "read_ranges": [],
                "symbols": [],
                "created": False,
                "edited": False,
                "verified": False,
                "edit_summaries": [],
            }
            self._prune_if_needed_locked()
        return self.entries[norm]

    def record_read(
        self,
        rel_path: str,
        start_line: int,
        end_line: int,
        raw_text: str,
        abs_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            entry = self._ensure_entry(rel_path)
            if abs_path is not None and abs_path.exists():
                try:
                    st = abs_path.stat()
                    entry["mtime_ns"] = int(st.st_mtime_ns)
                    entry["size"] = int(st.st_size)
                except OSError:
                    pass
            outline = extract_file_outline(abs_path or Path(rel_path), text=raw_text, max_symbols=40)
            new_sha = outline["sha256"]
            if entry["sha256"] and entry["sha256"] != new_sha:
                entry["read_ranges"] = []
            entry["sha256"] = new_sha
            entry["total_lines"] = outline["total_lines"]
            entry["size"] = entry["size"] or outline["bytes"]
            entry["symbols"] = [
                {"name": s["name"], "sig": s["sig"], "start": s["start"], "end": s["end"]}
                for s in outline["symbols"][:30]
            ]
            if end_line >= start_line >= 1:
                entry["read_ranges"] = _merge_ranges(list(entry["read_ranges"]) + [[int(start_line), int(end_line)]])
            return entry

    def record_outline(self, rel_path: str, raw_text: str, abs_path: Optional[Path] = None) -> Dict[str, Any]:
        with self._lock:
            entry = self._ensure_entry(rel_path)
            outline = extract_file_outline(abs_path or Path(rel_path), text=raw_text, max_symbols=50)
            if entry["sha256"] and entry["sha256"] != outline["sha256"]:
                entry["read_ranges"] = []
            entry["sha256"] = outline["sha256"]
            entry["total_lines"] = outline["total_lines"]
            entry["size"] = outline["bytes"]
            entry["symbols"] = [
                {"name": s["name"], "sig": s["sig"], "start": s["start"], "end": s["end"]}
                for s in outline["symbols"][:35]
            ]
            return entry

    def record_write(
        self,
        rel_path: str,
        raw_text: str,
        summary: str = "",
        created: bool = False,
        abs_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            entry = self._ensure_entry(rel_path)
            if abs_path is not None and abs_path.exists():
                try:
                    st = abs_path.stat()
                    entry["mtime_ns"] = int(st.st_mtime_ns)
                    entry["size"] = int(st.st_size)
                except OSError:
                    pass
            outline = extract_file_outline(abs_path or Path(rel_path), text=raw_text, max_symbols=40)
            entry["sha256"] = outline["sha256"]
            entry["total_lines"] = outline["total_lines"]
            entry["size"] = outline["bytes"]
            entry["symbols"] = [
                {"name": s["name"], "sig": s["sig"], "start": s["start"], "end": s["end"]}
                for s in outline["symbols"][:30]
            ]
            if created:
                entry["created"] = True
            entry["edited"] = True
            entry["verified"] = False
            if outline["total_lines"] >= 1:
                entry["read_ranges"] = [[1, outline["total_lines"]]]
            if summary:
                hist = list(entry.get("edit_summaries") or [])
                hist.append(" ".join(summary.split())[:140])
                entry["edit_summaries"] = hist[-4:]
            return entry

    def mark_deleted(self, rel_path: str) -> None:
        with self._lock:
            self.entries.pop(str(rel_path).replace("\\", "/"), None)

    def mark_verified(self, rel_paths: Optional[Iterable[str]] = None) -> None:
        with self._lock:
            if rel_paths is None:
                for rec in self.entries.values():
                    if rec.get("edited"):
                        rec["verified"] = True
                return
            for rp in rel_paths:
                norm = str(rp).replace("\\", "/")
                if norm in self.entries:
                    self.entries[norm]["verified"] = True

    def is_range_covered(
        self,
        rel_path: str,
        start_line: int,
        end_line: int,
        abs_path: Optional[Path] = None,
    ) -> bool:
        with self._lock:
            norm = str(rel_path).replace("\\", "/")
            rec = self.entries.get(norm)
            if not rec or not rec.get("sha256"):
                return False
            rec_sha = rec["sha256"]
            ranges_snap = [list(r) for r in (rec.get("read_ranges") or [])]
        if abs_path is not None and abs_path.is_file():
            try:
                disk_text = abs_path.read_text(encoding="utf-8", errors="replace")
                if compute_sha256_text(disk_text) != rec_sha:
                    return False
            except OSError:
                return False
        return _range_covered(ranges_snap, start_line, end_line)

    def check_edit_safety(
        self,
        rel_path: str,
        abs_path: Path,
        enforce_read_before_edit: bool = False,
    ) -> Tuple[bool, str]:
        """Verify target file isn't stale on disk and wasn't modified completely blind."""
        if not abs_path.exists():
            return True, ""
        norm = str(rel_path).replace("\\", "/")
        try:
            disk_text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return False, f"[Error] Cannot read {rel_path}: {exc}"

        disk_sha = compute_sha256_text(disk_text)
        with self._lock:
            rec = self.entries.get(norm)
            rec_sha = str(rec.get("sha256") or "") if rec else ""
            has_ranges = bool(rec.get("read_ranges")) if rec else False
            was_created = bool(rec.get("created")) if rec else False
        if rec and rec_sha:
            if disk_sha != rec_sha:
                return (
                    False,
                    f"[Error] Stale file context: {norm} changed externally on disk since last read/edit "
                    f"(tracked sha:{rec_sha[:8]} vs disk sha:{disk_sha[:8]}). "
                    f"Call read_file on the target lines of {norm} before patching.",
                )
            if enforce_read_before_edit and not has_ranges and not was_created:
                line_count = len(disk_text.splitlines())
                if line_count > 15 or len(disk_text) > 600:
                    return (
                        False,
                        f"[Error] Read-before-edit guard: you outlined {norm} ({line_count} lines) but haven't read any line range yet. "
                        f"Call read_file on the target lines of {norm} before patching.",
                    )
            return True, ""

        if enforce_read_before_edit:
            line_count = len(disk_text.splitlines())
            if line_count > 15 or len(disk_text) > 600:
                return (
                    False,
                    f"[Error] Read-before-edit guard: {norm} has {line_count} lines and has not been read in this session. "
                    f"Call get_file_outline or read_file on target lines of {norm} before patching.",
                )
        return True, ""

    def to_dict(self) -> Dict[str, Any]:
        # Keep all edited/verified files; fill remainder up to 150 total (LRU-style)
        with self._lock:
            items = list(self.entries.items())
        priority = {k: v for k, v in items if v.get("edited") or v.get("verified")}
        rest = {k: v for k, v in items if k not in priority}
        combined = list(priority.items())[:100] + list(rest.items())[:50]
        return {k: dict(v) for k, v in combined}

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "FileContextLedger":
        ledger = cls()
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, dict):
                    ledger.entries[str(k).replace("\\", "/")] = {
                        "sha256": str(v.get("sha256") or ""),
                        "size": int(v.get("size") or 0),
                        "mtime_ns": int(v.get("mtime_ns") or 0),
                        "total_lines": int(v.get("total_lines") or 0),
                        "read_ranges": list(v.get("read_ranges") or []),
                        "symbols": list(v.get("symbols") or [])[:35],
                        "created": bool(v.get("created")),
                        "edited": bool(v.get("edited")),
                        "verified": bool(v.get("verified")),
                        "edit_summaries": list(v.get("edit_summaries") or [])[-4:],
                    }
        return ledger

    def render_working_set_digest(
        self,
        focus_files: Optional[Iterable[str]] = None,
        max_chars: int = 3600,
    ) -> str:
        with self._lock:
            if not self.entries:
                return ""
            items_snap = list(self.entries.items())
        focus_set = {str(f).replace("\\", "/") for f in (focus_files or []) if f}
        ordered = sorted(
            items_snap,
            key=lambda kv: (
                0 if kv[0] in focus_set else (1 if kv[1].get("edited") else 2),
                kv[0],
            ),
        )
        lines: List[str] = []
        used = 0
        for rel_path, rec in ordered:
            ranges = ",".join(f"L{a}-{b}" for a, b in (rec.get("read_ranges") or [])[:4]) or "outline"
            flags = []
            if rec.get("created"):
                flags.append("new")
            elif rec.get("edited"):
                flags.append("edited")
            if rec.get("verified"):
                flags.append("verified")
            elif rec.get("edited"):
                flags.append("unverified")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            syms = ", ".join(s.get("name", "") for s in (rec.get("symbols") or [])[:6] if s.get("name"))
            sym_part = f" | syms: {syms}" if syms else ""
            sha_short = str(rec.get("sha256") or "")[:8]
            row = f"  - {rel_path} ({rec.get('total_lines', 0)}L, sha:{sha_short}, read:{ranges}){flag_str}{sym_part}"
            if used + len(row) + 1 > max_chars:
                lines.append(f"  ... (+{len(ordered) - len(lines)} more tracked files)")
                break
            lines.append(row)
            used += len(row) + 1
        return "\n".join(lines)


# ── Decision Capture & Structured Context Handoff Packet ─────────────────────

_DECISION_CUE_RE = re.compile(
    r"(?:\b(?:always|never|must|exclusively|only|use|keep|do not|don't|instead of|port|path|strict)\b)",
    re.IGNORECASE,
)


def capture_user_decisions(user_text: str, existing: Optional[List[str]] = None, limit: int = 16) -> List[str]:
    """Deterministically retain concise durable architectural rules stated by the user."""
    decisions = list(existing or [])
    if not user_text or user_text.startswith("["):
        return decisions[-limit:]
    seen = {d.lower() for d in decisions}
    for raw_clause in re.split(r"(?<=[.!?\n])\s+|;\s+", user_text):
        clause = " ".join(raw_clause.split()).strip()
        if len(clause) < 18 or len(clause) > 180:
            continue
        if not _DECISION_CUE_RE.search(clause):
            continue
        if clause.lower() in seen:
            continue
        seen.add(clause.lower())
        decisions.append(clause)
    return decisions[-limit:]


def build_context_packet(
    project_root: Path,
    project_type: str,
    ledger: FileContextLedger,
    decisions: Optional[List[str]] = None,
    plan_items: Optional[List[Dict[str, Any]]] = None,
    written_files: Optional[Iterable[str]] = None,
    verified_files: Optional[Iterable[str]] = None,
    focus_files: Optional[Iterable[str]] = None,
    turn_digest: Optional[List[Dict[str, Any]]] = None,
    design_concepts: Optional[List[Dict[str, Any]]] = None,
    extra_notes: str = "",
) -> str:
    """Build a crisp, token-frugal engineering context transfer packet."""
    sections: List[str] = [
        f"Workspace: {project_root} ({project_type})",
    ]
    if design_concepts:
        from .visual_concept import format_approved_concepts_context
        vc_txt = format_approved_concepts_context(design_concepts)
        if vc_txt:
            sections.append(vc_txt)
    if decisions:
        sections.append("Pinned Decisions & Constraints:\n" + "\n".join(f"  • {d}" for d in decisions[-10:]))
    if plan_items:
        p_lines = []
        for item in plan_items[:12]:
            st = str(item.get("status") or "pending").upper()
            title = str(item.get("title") or item.get("id") or "")
            owned = ", ".join(item.get("owned_files") or [])
            suffix = f" [files: {owned}]" if owned else ""
            p_lines.append(f"  - [{st}] {title}{suffix}")
        sections.append("Active Plan:\n" + "\n".join(p_lines))

    if turn_digest:
        td_lines = []
        for _entry in turn_digest[-6:]:
            _g = " | ".join(str(g)[:80] for g in (_entry.get("goals") or [])[-2:])
            _f = ", ".join(str(f) for f in (_entry.get("written") or [])[:4])
            td_lines.append(f"  \u2022 {_g}" + (f" \u2192 {_f}" if _f else ""))
        sections.append("Prior Turn History (compact):\n" + "\n".join(td_lines))

    # Populate missing ledger outlines deterministically for explicitly focused files
    focus_list = [str(f).replace("\\", "/") for f in (focus_files or []) if f]
    for rel in focus_list[:12]:
        if rel not in ledger.entries:
            p = (project_root / rel).resolve()
            try:
                p.relative_to(project_root.resolve())
            except ValueError:
                continue
            if p.is_file() and p.stat().st_size <= 160_000:
                try:
                    ledger.record_outline(rel, p.read_text(encoding="utf-8", errors="replace"), abs_path=p)
                except OSError:
                    pass

    digest = ledger.render_working_set_digest(focus_files=focus_list, max_chars=3000)
    if digest:
        sections.append("Working-Set File Map (lines, sha, read ranges, symbols):\n" + digest)
    elif written_files or verified_files:
        if written_files:
            sections.append("Modified Files: " + ", ".join(sorted(written_files)[:16]))
        if verified_files:
            sections.append("Verified Files: " + ", ".join(sorted(verified_files)[:16]))

    if extra_notes.strip():
        sections.append("Handoff Notes:\n" + extra_notes.strip()[:4000])
    return "\n\n".join(sections)


# ── Adaptive Orchestration Complexity Classifier ─────────────────────────────

_SIMPLE_PROMPT_RE = re.compile(
    r"^\s*(?:explain|what is|why does|how does|summarize|show me|read|check status|fix typo)\b",
    re.IGNORECASE,
)
_BUILD_VERBS_RE = re.compile(
    r"\b(?:build|create|implement|re[- ]?design|refactor|architect|develop|upgrade|overhaul|migrate|multi[- ]file|full[- ]stack|end[- ]to[- ]end|extend|expand|enhance|research|setup|set\s+up|integrate|integration|clone|install|configure|wire\s*up)\b",
    re.IGNORECASE,
)
_LAYER_TERMS_RE = re.compile(
    r"\b(?:backend|frontend|server|client|database|api|routes?|templates?|css|js|javascript|html|tests?|pytest|cli|extension|ui|ux)\b",
    re.IGNORECASE,
)
_PROJECT_INTENT_RE = re.compile(r"\b(?:build|create|make|develop|design|implement|setup|set\s+up|integrate|clone|install|configure|i\s+want|i(?:'d|\s+would)\s+like)\b[\s\S]{0,100}\b(?:app(?:lication)?|website|web\s*site|project|platform|dashboard|game|system|service|product|tool|repo(?:sitory)?|codebase|model|pipeline)\b", re.I)
_EXTENSIVE_WORK_RE = re.compile(r"\b(?:fix|debug|investigate|implement|add|develop|build|create|refactor|audit|extend|expand|enhance|research|setup|integrate|wire\s*up)\b", re.I)
_EXTENT_RE = re.compile(r"\b(?:detailed|deep(?:ly)?|comprehensive|complex|multiple|multi[- ]step|end[- ]to[- ]end|entire|complete|full|thorough|feature|features|bugs|issues|functionality|capabilities)\b", re.I)
_FAST_INFO_RE = re.compile(
    r"^\s*(?:explain|what\s+(?:is|are|does|was|were)|why\s+(?:does|is|are|did|was)|how\s+(?:does|is|do|can|should\s+(?:i|we))|summarize|define|compare|difference\s+between)\b",
    re.I,
)
_FAST_INFO_FOLLOWUP_RE = re.compile(
    r"(?:\b(?:and\s+(?:then\s+)?|then\s+|now\s+)|[;?]\s*)(?:build|create|implement|fix|refactor|write|make|design)\b",
    re.I,
)
_FAST_SINGLE_STEP_RE = re.compile(
    r"^\s*(?:fix\s+(?:a\s+|the\s+)?typo|rename\s+\S+\s+to\s+\S+|run\s+(?:pytest|tests?|npm\s+test|git\s+\S+)|"
    r"check\s+(?:git\s+)?status|show\s+(?:me\s+)?(?:git\s+)?(?:diff|log|status)|list\s+files|read\s+\S+|format\s+\S+|lint\s+\S+)\b",
    re.I,
)
_AMBIGUOUS_PRODUCT_RE = re.compile(
    r"\b(?:i\s+want|i'?d\s+like|sell|shop|store|website|web\s*site|app(?:lication)?|platform|gallery|portfolio|dashboard)\b",
    re.I,
)


def classify_task_complexity(prompt: str, state: Any = None) -> Dict[str, Any]:
    """Decide whether a request fits direct single-agent execution or multi-role orchestration."""
    text = (prompt or "").strip()
    team_mode = str(getattr(state, "team_mode", "auto") or "auto").lower()
    if getattr(state, "free_mode", False) or team_mode == "off":
        return {"mode": "direct", "score": 0, "reasons": ["team_mode=off or free_mode"], "fast_direct": True}

    if not text:
        return {"mode": "direct", "score": 0, "reasons": ["short prompt"], "fast_direct": True}

    if _FAST_INFO_RE.match(text) and not _FAST_INFO_FOLLOWUP_RE.search(text):
        return {
            "mode": "direct",
            "score": 1,
            "reasons": ["informational request"],
            "fast_direct": len(text) < 260,
        }
    project_match = _PROJECT_INTENT_RE.search(text)
    # A project mentioned as a location is not the object being built.
    project_intent = bool(project_match and not re.search(
        r"\b(?:in|inside|within|for)\s+(?:this|the|my|our|existing)\s+project\b",
        project_match.group(), re.I,
    ))
    extensive_work = bool(_EXTENSIVE_WORK_RE.search(text) and (_EXTENT_RE.search(text) or len(text.split()) >= 80))

    verbs = set(m.lower() for m in _BUILD_VERBS_RE.findall(text))
    layers = set(m.lower() for m in _LAYER_TERMS_RE.findall(text))
    mentioned_files = set(re.findall(r"[A-Za-z0-9_./\\-]+\.(?:py|js|ts|tsx|jsx|css|html|json|sh|ps1|md)\b", text))
    is_fast_single = bool(
        _FAST_SINGLE_STEP_RE.match(text)
        and len(text.split()) <= 20
        and len(text) < 160
        and not project_intent
        and not extensive_work
        and not verbs
        and len(mentioned_files) <= 1
        and len(layers) <= 1
        and not _AMBIGUOUS_PRODUCT_RE.search(text)
    )

    if _SIMPLE_PROMPT_RE.match(text) and len(text) < 220 and not (project_intent or extensive_work):
        return {
            "mode": "direct",
            "score": 1,
            "reasons": ["informational or single-step query"],
            "fast_direct": is_fast_single,
        }

    score = 0
    reasons: List[str] = []
    if project_intent or extensive_work:
        score += 4
        reasons.append("substantial project or multi-step development request")

    if verbs:
        score += 2
        reasons.append(f"build/refactor verbs: {', '.join(sorted(verbs)[:3])}")
    if len(layers) >= 3:
        score += 2
        reasons.append(f"multi-layer scope ({len(layers)} layers)")
    elif len(layers) == 2:
        score += 1
    if len(mentioned_files) >= 3:
        score += 2
        reasons.append(f"{len(mentioned_files)} files referenced")
    if len(text) >= 240:
        score += 1
        reasons.append("multi-clause specification")
    if "\n" in text and len(text.splitlines()) >= 4:
        score += 1
        reasons.append("structured multi-line specification")

    if team_mode == "on":
        mode = "orchestrate" if (score >= 2 or verbs or len(layers) >= 2) else "direct"
        return {"mode": mode, "score": score, "reasons": reasons, "fast_direct": bool(mode == "direct" and is_fast_single)}

    # Auto mode: escalate cleanly only on genuine multi-file/multi-layer build tasks (score >= 4)
    mode = "orchestrate" if score >= 4 else "direct"
    return {
        "mode": mode,
        "score": score,
        "reasons": reasons,
        "fast_direct": bool(mode == "direct" and is_fast_single),
    }
