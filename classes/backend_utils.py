# classes/backend_utils.py

import asyncio
import json
import logging
import re
import traceback
import commentjson
import yaml

from classes.base_utils import BaseUtils
from chat_prompts.chat_prompts import ASK_LOG_SYNTH_PROMPT, BSS_PROMPT_EXAMPLES, CALL_SEQUENCE_EXTRACTOR_PROMPT, COMP_OWNERSHIP_PROMPT, ENT_ENT_DEP_EXTRACTOR_PROMPT, PROC_PROC_DEP_EXTRACTOR_PROMPT, UI_UI_DEP_EXTRACTOR_PROMPT
from classes.entities import Project
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(name)s\n%(message)s\n"
)

logger = logging.getLogger("kahuna_backend")

class Utils(BaseUtils):
    SessionFactory:None
    llm_timeout: float = 300

    # -----------------------
    # bss_prompt reconstruction
    # -----------------------

    def _bss_example_kwargs(self, bss_schema: dict) -> dict:
        REQUIRED = {
            "A":   {"notes"},
            "UC":  {"definition","flow","notes"},
            "PROC":{"definition","flow","snippets","notes"},
            "COMP":{"definition","kind","notes"},
            "ROLE":{"definition","notes"},
            "UI":  {"definition","snippets","notes"},
            "ENT": {"definition","contract","notes"},
            "INT": {"definition","notes"},
            "API": {"definition","contract","notes"},
            "NFR": {"definition","notes"},
        }

        RULES = {
            # For A: at least 1 example, but each notes field must be >= 1000 chars
            "A":   {"min_ce": 1, "min_chars": {}},
            "UC":  {"min_ce": 3, "min_chars": {"flow": 3000}},
            "PROC":{"min_ce": 3, "min_chars": {"snippets": 1500, "flow": 1500}},
            "COMP":{"min_ce": 3, "min_chars": {}},
            "ROLE":{"min_ce": 3, "min_chars": {}},
            "UI":  {"min_ce": 3, "min_chars": {"snippets": 1500}},
            "ENT": {"min_ce": 3, "min_chars": {"contract": 1000}},
            "INT": {"min_ce": 3, "min_chars": {}},
            "API": {"min_ce": 3, "min_chars": {"contract": 1000}},
            "NFR": {"min_ce": 3, "min_chars": {}},
        }
        # stats: family -> {"ce": int, "chars": {seg: int}}
        stats: dict[str, dict] = {}
        for fam in RULES.keys():
            stats[fam] = {"ce": 0, "chars": {}, "min_notes_len": None}

        for label, item in self._iter_bss_items(bss_schema or {}):
            norm = self._normalize_bss_item(item)
            if norm.get("cancelled") is True:
                continue

            fam = (self._bss_label_type(label) or "").upper()
            if fam not in RULES:
                continue

            definition = norm.get("definition") or ""
            if fam == "A":
                segs = {"notes": definition.strip()}
            else:
                segs_raw = self._split_definition_segments(definition)
                segs = {k.lower(): (v or "").strip() for k, v in (segs_raw or {}).items()}

            required = REQUIRED.get(fam, set())

            is_complete = all((segs.get(k) or "").strip() for k in required)
            if not is_complete:
                continue

            stats[fam]["ce"] += 1

            # special rule for A: track per-example notes length
            if fam == "A":
                notes_len = len(segs.get("notes", ""))
                cur_min = stats[fam]["min_notes_len"]
                stats[fam]["min_notes_len"] = notes_len if cur_min is None else min(cur_min, notes_len)

            # char sums only across CE (for non-A thresholds)
            for seg_name, seg_body in segs.items():
                if not seg_body:
                    continue
                prev = stats[fam]["chars"].get(seg_name, 0)
                stats[fam]["chars"][seg_name] = prev + len(seg_body)

        def family_is_sufficient(fam: str) -> bool:
            rule = RULES[fam]
            # Family A: at least 1 complete example and EACH notes field >= 1000 chars
            if fam == "A":
                if stats[fam]["ce"] < 1:
                    return False
                min_len = stats[fam].get("min_notes_len")
                if min_len is None or min_len < 1000:
                    return False
                return True

            if stats[fam]["ce"] < rule["min_ce"]:
                return False
            for seg, min_chars in (rule["min_chars"] or {}).items():
                if stats[fam]["chars"].get(seg, 0) < int(min_chars):
                    return False
            return True

        out: dict[str, str] = {}

        for fam in RULES.keys():
            key = f"example_{fam}"
            out[key] = "" if family_is_sufficient(fam) else (BSS_PROMPT_EXAMPLES.get(fam) or "")
        return out

    # -----------------------
    # Project persistence
    # -----------------------

    def load_project(self, project_id: str) -> dict:
        session = self.SessionFactory()
        try:
            project = (
                session.query(Project)
                .filter(Project.project_id == str(project_id))
                .one_or_none()
            )
            if not project:
                raise ValueError(f"Project not found: {project_id}")

            schema = project.preliminary_schema or {}
            return schema
        finally:
            session.close()

    def save_project(self, project_id: str, content) -> None:
        if content is None:
            content = {}
        if not isinstance(content, dict):
            raise ValueError("save_project content must be a JSON object (dict)")

        session = self.SessionFactory()
        try:
            project = (
                session.query(Project)
                .filter(Project.project_id == str(project_id))
                .one_or_none()
            )
            if not project:
                raise ValueError(f"Project not found: {project_id}")

            project.preliminary_schema = content
            session.commit()
        finally:
            session.close()

    # -----------------------
    # Model selection helpers
    # -----------------------

    def _detect_llm_model_in_payload(self, payload) -> str | None:
        """
        Look for a model hint in payload using different possible key names.
        Supported: llm_model, model, model_name
        """
        if not isinstance(payload, dict):
            return None

        for key in ("llm_model", "model", "model_name"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        return None

    def _build_llms_for_payload(self, payload):
        """
        Build per-request LLMs from payload if a model is provided.
        Falls back to the default LLMs (read-only; never mutated per request).
        """
        requested = self._detect_llm_model_in_payload(payload)
        model_name = requested or "gemini-2.5-flash-lite"
        print("Model: " + requested + "," + model_name)

        llm, chat_llm = self._build_llms_for_model(model_name)
        if llm is None and chat_llm is None:
            return self.default_llm, self.default_chat_llm

        return (
            llm if llm is not None else self.default_llm,
            chat_llm if chat_llm is not None else self.default_chat_llm,
        )



    # -----------------------
    # BSS CHat Helpers
    # -----------------------

    # def _extract_bss_label_order(self, bss_text: str) -> list[str]:
    #     found = re.findall(r"\*\s+([A-I]\d+_[A-Z0-9_]+):", bss_text or "")
    #     order, seen = [], set()
    #     for x in found:
    #         if x not in seen:
    #             seen.add(x)
    #             order.append(x)
    #     return order

    def _chat_queue_to_messages(self, queue) -> list:
        """
        Convert a metadata.chat_queue array back into LangChain messages.
        """
        out = []
        for item in (queue or []):
            if not isinstance(item, dict):
                continue
            role = (item.get("role") or "").strip().lower()
            content = item.get("content", "")
            if not isinstance(content, str):
                content = str(content)

            if role == "user":
                out.append(HumanMessage(content=content))
            elif role == "assistant":
                out.append(AIMessage(content=content))
            elif role == "system":
                out.append(SystemMessage(content=content))
            else:
                # keep it, but mark as system-ish
                out.append(SystemMessage(content=content))
        return out

    def _messages_to_chat_queue(self, messages, limit: int = 10) -> list[dict]:
        """
        Convert a list of LangChain messages into a compact JSON-ready queue.
        Keeps only the last `limit` messages.
        """
        tail = list(messages or [])[-limit:]
        out: list[dict] = []
        for m in tail:
            if isinstance(m, HumanMessage):
                role = "user"
            elif isinstance(m, AIMessage):
                role = "assistant"
            elif isinstance(m, SystemMessage):
                role = "system"
            else:
                role = "other"
            out.append(
                {
                    "role": role,
                    "content": getattr(m, "content", ""),
                }
            )
        return out


    def _parse_bss_output(self, raw: str, llm=None) -> tuple[dict, str, bool]:
        """
        Parse LLM output in the new BSS format.

        Expected high-level structure:

        CHANGE_PROPOSALS:
        :::[LABEL] status=`...`
        - [segment]:
        body...

        ...

        NEXT_QUESTION:
        <multi-line question text...>

        Rules:
        - Only labels the LLM wants to add/change are present.
        - Only segments it wants to change/add are present.
        - Delete: a block like
            :::[LABEL]
            delete
        - ask_log is append-only; we return it separately as 'ask_log_append'.
        """
        text = (raw or "").strip()
        if not text:
            return {}, "", False

        # We support a tolerant layout:
        # - CHANGE_PROPOSALS: ... (optional)
        # - NEXT_QUESTION:    ... (optional; if missing we derive it from trailing text)

        block_header_re = re.compile(
            r"^:::\[([^\]]+)\](?:\s+status\s*=\s*`([^`]*)`)?\s*$",
            flags=re.MULTILINE,
        )

        # Marker positions (if present)
        m_cp = re.search(r"^CHANGE_PROPOSALS\s*:\s*$", text, flags=re.MULTILINE)
        m_nq = re.search(r"^NEXT_QUESTION\s*:\s*$", text, flags=re.MULTILINE)

        changes_region = ""
        next_question = ""

        if m_nq:
            # Explicit NEXT_QUESTION: label – everything after it is the question
            next_question = text[m_nq.end():].strip()

            if m_cp and m_cp.start() < m_nq.start():
                # CHANGE_PROPOSALS: explicitly present before NEXT_QUESTION:
                changes_region = text[m_cp.end():m_nq.start()].strip()
            else:
                # No explicit CHANGE_PROPOSALS: – treat everything before NEXT_QUESTION: as changes
                changes_region = text[:m_nq.start()].strip()
        else:
            # No explicit NEXT_QUESTION: – derive question from trailing text
            if m_cp:
                changes_candidate = text[m_cp.end():].strip()
            else:
                changes_candidate = text

            matches_all = list(block_header_re.finditer(changes_candidate))
            if not matches_all:
                # Nothing looks like change blocks -> treat entire thing as question
                return {}, changes_candidate.strip(), False

            # We have one or more blocks; treat everything through the end of the last block
            # as change proposals, and any trailing text as the question.
            last_body_end = len(changes_candidate)
            for idx, mh in enumerate(matches_all):
                body_start = mh.end()
                body_end = (
                    matches_all[idx + 1].start()
                    if idx + 1 < len(matches_all)
                    else len(changes_candidate)
                )
                if idx == len(matches_all) - 1:
                    last_body_end = body_end

            changes_region = changes_candidate[:last_body_end].strip()
            next_question = changes_candidate[last_body_end:].strip()

        if not changes_region:
            return {}, next_question, False

        # --- Blocks: :::[LABEL] [status=`...`] ---
        matches = list(block_header_re.finditer(changes_region))
        if not matches:
            # No change proposals; just a follow-up question.
            return {}, next_question, False

        updates: dict[str, dict] = {}

        for i, m in enumerate(matches):
            label = (m.group(1) or "").strip()
            status = m.group(2)
            status = (status or "").strip() if status is not None else None

            if not label or not self._is_bss_label(label):
                continue

            label_type = self._bss_label_type(label) or ""
            allowed_segments = self._bss_allowed_segments_for_type(label_type)

            block_start = m.end()
            block_end = matches[i + 1].start() if i + 1 < len(matches) else len(changes_region)
            block_text = changes_region[block_start:block_end].strip()

            # Delete block: just "delete"
            if re.fullmatch(r"(?is)\s*delete\s*", block_text or ""):
                updates[label] = {"cancelled": True}
                continue

            # Segments: - [name]:
            seg_re = re.compile(r"^-\s*\[([^\]]+)\]\s*:\s*$", flags=re.MULTILINE)
            seg_matches = list(seg_re.finditer(block_text))

            segments: dict[str, str] = {}
            open_items_value: str | None = None
            ask_log_append: str | None = None

            if seg_matches:
                for j, sm in enumerate(seg_matches):
                    seg_name_raw = sm.group(1).strip()
                    seg_key = seg_name_raw.lower()

                    # Enforce allowed segments per family.
                    if seg_key not in allowed_segments:
                        self.color_print(
                            (
                                f"[BSS] Invalid segment '{seg_name_raw}' for label {label} "
                                f"(type {label_type or 'UNKNOWN'}). "
                                f"Allowed segments: {sorted(allowed_segments)}"
                            ),
                            color="red",
                        )
                        # Do not apply this segment.
                        continue

                    seg_body_start = sm.end()
                    seg_body_end = seg_matches[j + 1].start() if j + 1 < len(seg_matches) else len(block_text)
                    body = block_text[seg_body_start:seg_body_end]

                    # Drop leading newline, keep internal newlines as-is
                    if body.startswith("\n"):
                        body = body[1:]
                    body = body.rstrip()
                    if not body:
                        continue

                    if seg_key == "ask_log":
                        ask_log_append = body if ask_log_append is None else (ask_log_append + "\n" + body)
                    elif seg_key == "open_items":
                        open_items_value = body
                    else:
                        segments[seg_key] = body

            patch: dict[str, object] = {}

            if status is not None:
                patch["status"] = status

            if segments:
                patch["segments"] = segments

            if open_items_value is not None:
                patch["open_items"] = open_items_value

            if ask_log_append:
                patch["ask_log_append"] = ask_log_append

            # If nothing meaningful in this block, skip it
            if not patch:
                continue

            updates[label] = patch

        malformed = False
        return updates, next_question, malformed


    # -----------------------
    # Prep doc for LLM
    # -----------------------


    def _bss_current_document_for_prompt(self, bss_schema: dict) -> str:
        """
        Build the BSS document text sent to the LLM in the new format:

        :::[LABEL] status=`...`
        - [segment]:
        body...
        """
        flat: dict[str, dict] = {}
        for label, item in self._iter_bss_items(bss_schema):
            flat[label] = self._normalize_bss_item(item)

        ordered_labels = sorted(flat.keys(), key=self._bss_label_sort_key)

        lines: list[str] = []

        for label in ordered_labels:
            obj = flat[label]
            status = (obj.get("status") or "").strip()

            # -------------------
            # Build logical segments
            # -------------------
            segments: dict[str, str] = {}

            definition_text = obj.get("definition") or ""
            label_type = self._bss_label_type(label)

            if label_type == "A":
                # For A-items, "notes" is effectively the definition
                if definition_text.strip():
                    segments["notes"] = definition_text.strip()
            else:
                segs = self._split_definition_segments(definition_text)
                for seg_name, seg_body in segs.items():
                    # References segment disappears from the LLM-facing document
                    if seg_name == "references":
                        continue
                    segments[seg_name] = seg_body

            open_items = (obj.get("open_items") or "").strip()
            if open_items:
                segments["open_items"] = open_items

            ask_log = (obj.get("ask_log") or "").strip()
            if ask_log:
                segments["ask_log"] = ask_log

            # -------------------
            # Render header
            # -------------------
            lines.append(f":::[{label}] status=`{status}`")

            # Stable segment order; anything unknown comes later
            preferred_order = [
                "kind",
                "definition",
                "flow",
                "notes",
                "contract",
                "contracts",
                "snippets",
                "outcomes",
                "decision",
                "open_items",
                "ask_log",
            ]
            seen = set()

            for key in preferred_order:
                value = segments.get(key)
                if not value or not value.strip():
                    continue
                seen.add(key)
                lines.append(f"- [{key}]:")
                lines.append(value.rstrip())
                lines.append("")  # blank line after segment

            # Any extra segments not in preferred_order
            for key in sorted(segments.keys()):
                if key in seen:
                    continue
                value = segments[key]
                if not value or not value.strip():
                    continue
                lines.append(f"- [{key}]:")
                lines.append(value.rstrip())
                lines.append("")

            # Blank line between items
            lines.append("")

        return "\n".join(lines).strip()


    def _extract_next_question(self, line: str) -> str:
        # line is like: NEXT_QUESTION:"...?"
        s = line[len("NEXT_QUESTION:"):].strip()
        if s.startswith('"') and s.endswith('"') and len(s) >= 2:
            return s[1:-1]
        return s


    def load_bss_schema(self, project_id: str) -> dict:
        session = self.SessionFactory()
        try:
            project = (
                session.query(Project)
                .filter(Project.project_id == str(project_id))
                .one_or_none()
            )
            if not project:
                raise ValueError(f"Project not found: {project_id}")
            return project.bss_schema or {}
        finally:
            session.close()

    def save_bss_schema(self, project_id: str, content) -> None:
        if content is None:
            content = {}
        if not isinstance(content, dict):
            raise ValueError("save_bss_schema content must be a JSON object (dict)")

        session = self.SessionFactory()
        try:
            project = (
                session.query(Project)
                .filter(Project.project_id == str(project_id))
                .one_or_none()
            )
            if not project:
                raise ValueError(f"Project not found: {project_id}")

            project.bss_schema = content
            session.commit()
        finally:
            session.close()


    # -----------------------
    # BSS label/type/section helpers
    # -----------------------

    def _build_registry_ledger(self, bss_schema: dict) -> dict:
        ledger: dict[str, list[str]] = {k: [] for k in self._bss_section_order()}

        # collect labels, skip cancelled
        flat: dict[str, dict] = {}
        for label, item in self._iter_bss_items(bss_schema):
            norm = self._normalize_bss_item(item)
            flat[label] = norm

        for label, item in flat.items():
            if item.get("cancelled") is True:
                continue
            section = self._bss_section_for_label(label)
            if not section:
                continue
            ledger.setdefault(section, []).append(label)

        for section in list(ledger.keys()):
            ledger[section] = sorted(list(dict.fromkeys(ledger[section])), key=self._bss_label_sort_key)

        return ledger

    def _bss_next_indices(self, bss_schema: dict) -> dict[str, int]:
        """
        For each family (UC, PROC, UI, ...) return the next numeric index
        (max existing + 1, or 1 if none).
        """
        from collections import defaultdict
        max_idx = defaultdict(int)

        for label, _ in self._iter_bss_items(bss_schema or {}):
            fam = self._bss_label_type(label)
            if not fam:
                continue
            m = re.match(
                r"^(A|UC|PROC|COMP|ROLE|UI|ENT|INT|API|NFR)-?(\d+)_",
                label.strip(),
                flags=re.IGNORECASE,
            )
            if not m:
                continue
            n = int(m.group(2))
            if n > max_idx[fam]:
                max_idx[fam] = n

        out = {}
        for fam in ("A", "UC", "PROC", "COMP", "ROLE", "UI", "ENT", "INT", "API", "NFR"):
            out[fam] = max_idx[fam] + 1 if max_idx[fam] > 0 else 1
        return out


    def _collect_open_items_by_gravity(self, bss_schema: dict) -> list:
        """
        Scan all BSS items and group their open_items snippets by gravity.
        Each snippet is expected (tolerantly) to look like:
            "gravity:high: some text"
        Multiple snippets are separated by ';'.
        Returns a dict gravity -> list[(label, text)].
        """
        buckets: dict[str, list[tuple[str, str]]] = {
            "sys": [],
            "high": [],
            "med": [],
            "low": [],
        }
        if not isinstance(bss_schema, dict):
            return buckets

        for label, item in self._iter_bss_items(bss_schema):
            norm = self._normalize_bss_item(item)
            if norm.get("cancelled") is True:
                continue
            raw_open = (norm.get("open_items") or "").strip()
            if not raw_open:
                continue
            parts = [p.strip() for p in raw_open.split(";") if p and p.strip()]
            for snippet in parts:
                gravity, text = self._parse_open_item_gravity(snippet)
                if gravity and text:
                    buckets.setdefault(gravity, []).append((label, gravity + ": " + text))

        bucket = []
        for g in ("sys", "high", "med", "low"):
            if buckets.get(g):
                bucket = buckets[g]
        if buckets.get("ongoing", None):
            bucket += buckets["ongoing"]
        return bucket

    def _parse_open_item_gravity(self, snippet: str) -> tuple[str | None, str | None]:
        """
        Best-effort parser for a single open_items snippet.
        Accepts forms like:
            "high: text"
            "med : text"
        Returns (gravity, text) or (None, None) if it cannot be parsed.
        """
        s = (snippet or "").strip()
        if not s:
            return None, None

        m = re.search(
            r"\b(low|med|medium|high|sys|system)\b\s*[:\-]\s*(.+)",
            s,
            flags=re.IGNORECASE,
        )
        if not m:
            return None, None

        level_raw = (m.group(1) or "").lower()
        text = (m.group(2) or "").strip()
        if not text:
            return None, None

        if level_raw == "medium":
            level = "med"
        elif level_raw == "system":
            level = "sys"
        else:
            level = level_raw

        if level not in {"low", "med", "high", "sys"}:
            return None, None

        return level, text


    # !##############################################
    # ! HARD REFERENCE RECONSTRUCTION
    # !##############################################

    def _recompute_bss_dependency_fields(self, bss_schema: dict, root_labels: set[str] | None = None) -> dict:
        # Type-level permission graph:
        # X depends on Y  =>  X -> Y
        # Anything not allowed here will be dropped (no edge) even if present in text.
        allowed_deps = {
            # Project canvas: keep current flexibility.
            "A":   {"UC", "ROLE", "NFR", "PROC", "ENT", "INT", "API", "UI", "COMP"},

            # UC-*: always UC → ROLE/UI/PROC/INT/API/ENT/NFR.
            "UC":  {"ROLE", "UI", "PROC", "INT", "API", "ENT", "NFR"},

            # PROC-*:
            # - always PROC → ENT
            # - PROC ↔ PROC (orchestration)
            # - PROC ↔ API (calls vs implements – refined later)
            # - PROC ↔ INT (only when INT.Kind = outbound – enforced in can_have_child)
            "PROC": {"ENT", "PROC", "API", "INT"},

            # COMP-*:
            # At this stage components only receive NFRs; they do not depend on others.
            "COMP": {"PROC", "ENT", "API", "UI"},

            # ROLE-*: always ROLE → UI.
            "ROLE": {"UI"},

            # UI-*:
            # - always UI → API/PROC
            # - UI → INT allowed (direct call to integration)
            # - UI ↔ UI (embedding/reuse – second pass may refine).
            "UI":   {"API", "PROC", "INT", "UI"},

            # ENT-*:
            # ENT ↔ ENT (PK/FK / composition – second pass decides direction).
            "ENT":  {"ENT"},

            # INT-*:
            # - Inbound: INT → API (Kind=inbound)
            # - Outbound: PROC → INT (Kind=outbound)
            # Direction gates are enforced in can_have_child.
            "INT":  {"API"},

            # API-*:
            # - PROC ↔ API (calls vs implements – refined later).
            "API":  {"PROC"},

            # NFR-*:
            # - always NFR → PROC/COMP/API/UI
            # - UC → NFR handled via UC row above.
            "NFR":  {"PROC", "COMP", "API", "UI"},
        }

        # 1) flatten + normalize
        flat: dict[str, dict] = {}
        label_types: dict[str, str] = {}

        for label, item in self._iter_bss_items(bss_schema):
            norm = self._normalize_bss_item(item)
            if norm.get("cancelled") is True:
                continue
            flat[label] = norm
            t = self._bss_label_type(label)
            if t:
                label_types[label] = t

        all_labels = set(flat.keys())
        # Map for case-insensitive canonicalization: "ROLE-1_CUSTOMER" -> "ROLE-1_Customer"
        canonical_by_upper = {lbl.upper(): lbl for lbl in all_labels}

        def _comp_edge_allowed(src: str, dst: str) -> bool:
            """
            For any pair touching a COMP, only keep the edge if the COMP's own
            text (definition / open_items / user_defined_relationships) mentions
            the other label.
            For non-COMP pairs, keep existing behaviour.
            """
            src_type = label_types.get(src)
            dst_type = label_types.get(dst)

            if src_type != "COMP" and dst_type != "COMP":
                return True

            if src_type == "COMP":
                comp_label, other_label = src, dst
            else:
                comp_label, other_label = dst, src

            # Enforce COMP kind → ownership constraints
            comp_def = (flat.get(comp_label, {}) or {}).get("definition") or ""
            segs = self._split_definition_segments(comp_def)
            kind = (segs.get("kind") or "").strip().lower()
            other_type = label_types.get(other_label)

            if kind:
                # COMP(kind=datastore) can only own ENT-*
                if "datastore" in kind and other_type != "ENT":
                    return False

                # COMP(kind=service|worker|job) cannot own ENT-*
                if any(k in kind for k in ("service", "worker", "job")) and other_type == "ENT":
                    return False

            # Only allow if the COMP node itself references the other label
            return other_label in (refs_by_label.get(comp_label) or [])

        def _int_kind_for_label(lbl: str) -> str | None:
            """
            Returns 'inbound' / 'outbound' / None based on the Kind: segment
            of an INT-* node. If missing or unrecognized, returns None.
            """
            if label_types.get(lbl) != "INT":
                return None
            definition = (flat.get(lbl, {}) or {}).get("definition") or ""
            segs = self._split_definition_segments(definition)
            raw = (segs.get("kind") or "").strip().lower()
            if not raw:
                return None
            if "inbound" in raw:
                return "inbound"
            if "outbound" in raw:
                return "outbound"
            return None

        def can_have_child(parent: str, child: str) -> bool:
            """
            Return True if `parent` is allowed to depend on `child`
            (i.e. edge parent -> child is permitted) under the type rules,
            including INT.Kind semantics.
            """
            pt = label_types.get(parent)
            ct = label_types.get(child)
            if not pt or not ct:
                return False

            # INT-specific rules (no assumptions if Kind is missing):
            # - Inbound: INT → API
            if pt == "INT" and ct == "API":
                return _int_kind_for_label(parent) == "inbound"

            # - Outbound: PROC → INT
            if pt == "PROC" and ct == "INT":
                return _int_kind_for_label(child) == "outbound"

            # All other pairs rely purely on allowed_deps type matrix.
            return ct in allowed_deps.get(pt, set())

        # 2) collect refs per label
        #    - from free-text (definition + open_items)
        #    - plus any explicit user_defined_relationships
        refs_by_label: dict[str, list[str]] = {}

        for label, item in flat.items():
            refs_text = self._extract_reference_labels_from_definition(
                (item.get("definition") or ""),
                (item.get("open_items") or ""),
            )
            refs_udr = self._extract_reference_labels_from_definition(
                (item.get("user_defined_relationships") or "")
            )

            # Union + preserve order: textual refs first, then user-defined
            seen_local: set[str] = set()
            refs_all: list[str] = []
            for r in refs_text + refs_udr:
                r = (r or "").strip()
                if not r:
                    continue
                if r in seen_local:
                    continue
                seen_local.add(r)
                refs_all.append(r)

            resolved: list[str] = []
            for r in refs_all:
                if not r:
                    continue
                # avoid self-dependency (case-insensitive)
                if r.upper() == label.upper():
                    continue

                # Map to canonical label if it exists in the schema
                canon = canonical_by_upper.get(r.upper())
                if not canon:
                    # no such item in the schema → ignore
                    continue

                resolved.append(canon)

            refs_by_label[label] = resolved

        # 3) full vs partial recompute
        roots: set[str] = set()
        if root_labels:
            for lbl in root_labels:
                if not isinstance(lbl, str):
                    continue
                canon = canonical_by_upper.get(lbl.strip().upper())
                if canon:
                    roots.add(canon)

        if not roots:
            # GLOBAL recompute (previous behaviour)
            pair_set: set[frozenset[str]] = set()
            for src, refs in refs_by_label.items():
                for dst in refs:
                    if dst not in all_labels:
                        continue
                    if not _comp_edge_allowed(src, dst):
                        continue
                    pair_set.add(frozenset((src, dst)))

            children: dict[str, set[str]] = {lbl: set() for lbl in flat.keys()}

            for pair in pair_set:
                a, b = tuple(pair)  # exactly 2 elements

                # stable order for tie-breaks
                if self._bss_label_sort_key(b) < self._bss_label_sort_key(a):
                    a, b = b, a

                can_a = can_have_child(a, b)  # a can have b as dependency
                can_b = can_have_child(b, a)  # b can have a as dependency

                # If neither direction is allowed by the rules, drop this pair entirely.
                if not can_a and not can_b:
                    continue

                if can_a and not can_b:
                    parent, child = a, b
                elif can_b and not can_a:
                    parent, child = b, a
                else:
                    # symmetric or both disallowed -> textual direction, else stable
                    a_refs_b = b in refs_by_label.get(a, ())
                    b_refs_a = a in refs_by_label.get(b, ())
                    if a_refs_b and not b_refs_a:
                        parent, child = a, b
                    elif b_refs_a and not a_refs_b:
                        parent, child = b, a
                    else:
                        parent, child = a, b

                children[parent].add(child)

        else:
            # PARTIAL recompute:
            # - Start from existing dependencies as baseline.
            # - Drop all edges touching any root label.
            # - Rebuild edges only for pairs that involve at least one root.
            children: dict[str, set[str]] = {lbl: set() for lbl in flat.keys()}

            # Baseline from existing dependencies
            for label, item in flat.items():
                deps_raw = (item.get("dependencies") or "").strip()
                if not deps_raw:
                    continue
                for tok in deps_raw.split(","):
                    t = tok.strip()
                    if not t:
                        continue
                    canon = canonical_by_upper.get(t.upper())
                    if canon and canon in all_labels and canon.upper() != label.upper():
                        children[label].add(canon)

            roots_upper = {r.upper() for r in roots}

            def is_root(label: str) -> bool:
                return label.upper() in roots_upper

            # Remove all edges that touch any root label; they will be recomputed.
            for parent in list(children.keys()):
                if is_root(parent):
                    children[parent].clear()
                else:
                    children[parent] = {c for c in children[parent] if not is_root(c)}

            # Pairs only where at least one endpoint is a root.
            pair_set: set[frozenset[str]] = set()
            for src, refs in refs_by_label.items():
                for dst in refs:
                    if dst not in all_labels:
                        continue
                    if not (is_root(src) or is_root(dst)):
                        continue
                    if not _comp_edge_allowed(src, dst):
                        continue
                    pair_set.add(frozenset((src, dst)))

            # Orient these pairs and update children on top of baseline.
            for pair in pair_set:
                a, b = tuple(pair)

                if self._bss_label_sort_key(b) < self._bss_label_sort_key(a):
                    a, b = b, a

                can_a = can_have_child(a, b)
                can_b = can_have_child(b, a)

                # If neither direction is allowed, drop the pair altogether.
                if not can_a and not can_b:
                    continue

                if can_a and not can_b:
                    parent, child = a, b
                elif can_b and not can_a:
                    parent, child = b, a
                else:
                    a_refs_b = b in refs_by_label.get(a, ())
                    b_refs_a = a in refs_by_label.get(b, ())
                    if a_refs_b and not b_refs_a:
                        parent, child = a, b
                    elif b_refs_a and not a_refs_b:
                        parent, child = b, a
                    else:
                        parent, child = a, b

                children[parent].add(child)

        # 5) compute parents (dependants) as reverse of children
        parents: dict[str, set[str]] = {lbl: set() for lbl in flat.keys()}
        for p, cs in children.items():
            for c in cs:
                if c in parents:
                    parents[c].add(p)

        # 6) write CSV fields
        for label in flat.keys():
            deps_sorted = sorted(children[label], key=self._bss_label_sort_key)
            depants_sorted = sorted(parents[label], key=self._bss_label_sort_key)
            flat[label]["dependencies"] = ",".join(deps_sorted)
            flat[label]["dependants"] = ",".join(depants_sorted)

        # 7) re-pack into sections
        out: dict[str, dict] = {}
        for section in self._bss_section_order():
            out[section] = {}

        for label, item in flat.items():
            section = self._bss_section_for_label(label) or "A"
            out.setdefault(section, {})
            out[section][label] = item

        result = {k: v for k, v in out.items() if isinstance(v, dict) and v}

        # carry-through auxiliary metadata section unchanged
        if isinstance(bss_schema, dict) and "metadata" in bss_schema:
            result["metadata"] = bss_schema["metadata"]

        return result


    def _escape_bss_freeform_segments_for_ui(self, definition: str) -> str:
        if not isinstance(definition, str) or not definition.strip():
            return definition or ""

        segment_names = [
            "Definition",
            "Flow",
            "Contract",
            "Contracts",
            "Snippets",
            "Outcomes",
            "Decision",
            "Notes",
            "References",
        ]

        # Segment headers start either at beginning, or at an *unescaped* pipe.
        # This prevents "\| Notes:" inside code from being treated as a real delimiter.
        header_re = re.compile(
            r"(^|(?<!\\)\|)\s*(" + "|".join(segment_names) + r")\s*:\s*",
            flags=re.IGNORECASE,
        )

        matches = list(header_re.finditer(definition))
        if not matches:
            return definition

        out = []
        pos = 0

        for i, m in enumerate(matches):
            start = m.start()
            end_header = m.end()
            seg_name = (m.group(2) or "").strip().lower()

            if start > pos:
                out.append(definition[pos:start])

            # Keep header verbatim (includes leading '|' when present)
            out.append(definition[start:end_header])

            body_start = end_header
            body_end = matches[i + 1].start() if i + 1 < len(matches) else len(definition)
            body = definition[body_start:body_end]

            if seg_name in ("contract", "contracts", "snippets"):
                # Make raw code safe for client JSON + client segment parsing:
                # - normalize/escape literal control chars as backslash sequences
                # - escape any *unescaped* pipe so it can't look like a delimiter
                body = body.replace("\r\n", "\n").replace("\r", "\n")
                body = body.replace("\n", "\\n").replace("\t", "\\t")
                body = re.sub(r"(?<!\\)\|", r"\|", body)

            out.append(body)
            pos = body_end

        if pos < len(definition):
            out.append(definition[pos:])

        return "".join(out)

    def _redact_bss_definition_for_ui(self, definition: str) -> str:
        if not isinstance(definition, str) or not definition.strip():
            return definition or ""

        definition = self._escape_bss_freeform_segments_for_ui(definition)

        # Remove the whole "References:" segment (with all its XYZ=[...] arrays),
        # starting either at beginning or at a preceding pipe.
        m = re.search(r"(^|\|)\s*References\s*:\s*", definition, flags=re.IGNORECASE)
        if not m:
            return definition

        start = m.start()  # includes the preceding '|' when present
        end_pipe = definition.find("|", m.end())

        # If no trailing pipe, delete to end-of-string
        if end_pipe < 0:
            return definition[:start].rstrip()

        # If there is a trailing pipe, remove the references segment but keep a single pipe between remaining parts
        before = definition[:start].rstrip()
        after = definition[end_pipe + 1:].lstrip()

        if not after:
            return before
        if not before:
            return after

        return f"{before} | {after}"


    def _redact_bss_schema_for_ui(self, bss_schema: dict) -> dict:
        if not isinstance(bss_schema, dict):
            return {}

        out: dict[str, dict] = {}

        for label, item in self._iter_bss_items(bss_schema):
            norm = self._normalize_bss_item(item)

            # 2) Do not send back nodes where cancelled = true
            if norm.get("cancelled") is True:
                continue

            norm["definition"] = self._redact_bss_definition_for_ui(norm.get("definition", ""))

            section = self._bss_section_for_label(label) or "A"
            out.setdefault(section, {})
            out[section][label] = norm

        # drop empty sections
        out = {k: v for k, v in out.items() if v}
        return out


    def _collect_deleted_labels_from_slot_updates(self, slot_updates: dict) -> set[str]:
        deleted = set()
        for label, obj in (slot_updates or {}).items():
            if not self._is_bss_label(label):
                continue
            if isinstance(obj, dict) and (obj.get("cancelled") is True or obj.get("deleted") is True):
                deleted.add(label)
        return deleted

    def _bss_any_item_references_labels(self, bss_schema: dict, labels: set[str]) -> bool:
        if not labels:
            return False
        for _, item in self._iter_bss_items(bss_schema):
            norm = self._normalize_bss_item(item)
            if norm.get("cancelled") is True:
                continue
            refs = self._extract_reference_labels_from_definition(
                norm.get("definition", ""),
                norm.get("open_items", ""),
            )
            if any(r in labels for r in refs):
                return True
        return False

    def _remove_labels_from_references_segment(self, definition: str, labels_to_remove: set[str]) -> str:
        """
        Removes only the deleted labels from inside the References: ... segment.
        Does nothing if References: is missing.
        Preserves everything outside the References segment.
        """
        if not isinstance(definition, str) or not definition.strip() or not labels_to_remove:
            return definition or ""

        m = re.search(r"(^|\|)\s*References\s*:\s*", definition, flags=re.IGNORECASE)
        if not m:
            return definition

        # boundaries: start at 'References:' (not including preceding pipe), end at next pipe or EoS
        start_refs = m.start(0)
        # keep the exact prefix before the marker
        prefix = definition[:start_refs]

        # locate end of references segment content
        seg_start = m.end()
        next_pipe = definition.find("|", seg_start)
        if next_pipe < 0:
            refs_body = definition[seg_start:]
            suffix = ""
        else:
            refs_body = definition[seg_start:next_pipe]
            suffix = definition[next_pipe:]  # includes the pipe

        # rewrite each XYZ=[...] list by filtering deleted labels
        def repl(match):
            key = match.group(1)
            inner = match.group(2)

            parts = [p.strip() for p in inner.split(",") if p.strip()]
            cleaned = []
            for p in parts:
                if len(p) >= 2 and p[0] == p[-1] and p[0] in ("'", '"'):
                    p2 = p[1:-1].strip()
                else:
                    p2 = p
                # remove only exact label tokens
                if p2 in labels_to_remove:
                    continue
                cleaned.append(p2)

            return f"{key}=[{', '.join(cleaned)}]"

        new_refs_body = re.sub(r"([A-Za-z]+)\s*=\s*\[([^\]]*)\]", repl, refs_body)

        # Recompose with the original "References:" marker text normalized to "References: "
        # (we keep user's prefix/suffix untouched; only references list content changes)
        marker = "References: "
        # Preserve whether there was a preceding pipe right at the marker position
        # If prefix ends with '|' already, keep it as-is.
        if prefix.rstrip().endswith("|"):
            rebuilt = prefix.rstrip() + " " + marker + new_refs_body.strip() + suffix
        else:
            rebuilt = prefix + marker + new_refs_body.strip() + suffix

        return rebuilt

    def _emergency_purge_deleted_labels_from_references(self, bss_schema: dict, deleted_labels: set[str]) -> dict:
        """
        Emergency measure: only called if deleted labels are still referenced somewhere.
        Removes those labels from other items' References segments.
        """
        if not deleted_labels or not isinstance(bss_schema, dict):
            return bss_schema

        out: dict[str, dict] = {}
        for label, item in self._iter_bss_items(bss_schema):
            norm = self._normalize_bss_item(item)
            # skip cancelled nodes (they are already deleted/hidden semantics)
            if norm.get("cancelled") is True:
                continue

            norm["definition"] = self._remove_labels_from_references_segment(
                norm.get("definition", ""),
                deleted_labels,
            )

            section = self._bss_section_for_label(label) or "A"
            out.setdefault(section, {})
            out[section][label] = norm

        # drop empty sections
        out = {k: v for k, v in out.items() if v}

        # carry-through auxiliary metadata section unchanged
        if isinstance(bss_schema, dict) and "metadata" in bss_schema:
            out["metadata"] = bss_schema["metadata"]

        return out


    def _apply_bss_slot_updates(self, current_bss: dict, slot_updates: dict) -> dict:
        """
        Apply LLM-emitted slot updates to the current BSS graph.

        Rules:
        - This function defaults brand-new items to 'draft' once.
        - Segments that are not part of the allowed set for the label family
        are dropped and logged; they are never persisted.
        - cancelled=True means: delete the node from the graph.
        """
        if not isinstance(current_bss, dict):
            current_bss = {}

        for label, patch in (slot_updates or {}).items():
            if not self._is_bss_label(label):
                continue

            section = self._bss_section_for_label(label)
            if not section:
                continue

            # Delete / cancel
            if isinstance(patch, dict) and patch.get("cancelled") is True:
                if section in current_bss and isinstance(current_bss[section], dict):
                    current_bss[section].pop(label, None)
                    if not current_bss[section]:
                        current_bss.pop(section, None)
                continue

            if not isinstance(patch, dict):
                continue

            current_bss.setdefault(section, {})

            # Start from existing canonical representation
            existing_raw = current_bss[section].get(label)
            is_new = existing_raw is None
            existing = self._normalize_bss_item(existing_raw)

            # Status:
            # - UI-owned. LLM patches cannot change status.
            # - For brand new items, default to 'draft' only if still empty.
            if is_new and not (existing.get("status") or "").strip():
                existing["status"] = "draft"

            # Split existing definition into segments
            existing_def = existing.get("definition") or ""
            segments_current = self._split_definition_segments(existing_def)

            # Overlay new definition-related segments (definition, flow, notes, kind, etc.)
            new_segments = patch.get("segments") or {}
            if isinstance(new_segments, dict):
                for seg_name, seg_body in new_segments.items():
                    key = (seg_name or "").strip().lower()
                    if not key:
                        continue
                    if not isinstance(seg_body, str):
                        continue
                    segments_current[key] = seg_body

            # Filter out segments that are not allowed for this family
            label_type = self._bss_label_type(label) or ""
            allowed_segments = self._bss_allowed_segments_for_type(label_type)
            # Only definition-segments live inside "definition"; open_items / ask_log are fields.
            allowed_def_segments = {s for s in allowed_segments if s not in {"open_items", "ask_log"}}

            filtered_segments: dict[str, str] = {}
            for key, val in segments_current.items():
                if key in allowed_def_segments:
                    filtered_segments[key] = val
                else:
                    if (val or "").strip():
                        # Log and drop unknown segment
                        self.color_print(
                            f"[BSS] Dropping unknown segment '{key}' for label {label} (type {label_type})",
                            color="yellow",
                        )

            segments_current = filtered_segments

            # Rebuild definition from filtered segments
            preferred_order = [
                "definition",
                "flow",
                "notes",
                "kind",
                "contract",
                "contracts",
                "snippets",
                "outcomes",
                "decision",
            ]

            pieces: list[str] = []
            used: set[str] = set()

            for key in preferred_order:
                val = (segments_current.get(key) or "").strip()
                if not val:
                    continue
                used.add(key)
                header = key.capitalize()
                pieces.append(f"{header}: {val}")

            # Any extra allowed segment names not in preferred_order
            for key in sorted(segments_current.keys()):
                if key in used:
                    continue
                val = (segments_current.get(key) or "").strip()
                if not val:
                    continue
                header = key.capitalize()
                pieces.append(f"{header}: {val}")

            if pieces:
                existing["definition"] = " | ".join(pieces)
            # else keep existing["definition"] as-is

            # open_items: full replacement if provided
            if "open_items" in patch:
                existing["open_items"] = patch.get("open_items") or ""

            # ask_log: append with '; ' separator if provided
            ask_append = (patch.get("ask_log_append") or "").strip()
            if ask_append:
                prev = (existing.get("ask_log") or "").strip()
                if prev:
                    existing["ask_log"] = f"{prev}; {ask_append}"
                else:
                    existing["ask_log"] = ask_append

            # Never persist deleted; cancelled is the only supported boolean here
            existing["cancelled"] = False

            current_bss[section][label] = existing

        return current_bss
