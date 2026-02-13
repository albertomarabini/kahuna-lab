import asyncio
import json
import logging
import re
import traceback
import commentjson
import yaml

from classes.chat_prompts import ASK_LOG_SYNTH_PROMPT, BSS_PROMPT_EXAMPLES, CALL_SEQUENCE_EXTRACTOR_PROMPT, COMP_OWNERSHIP_PROMPT, ENT_ENT_DEP_EXTRACTOR_PROMPT, PROC_PROC_DEP_EXTRACTOR_PROMPT, UI_UI_DEP_EXTRACTOR_PROMPT
from classes.entities import Project
from classes.google_helpers import PROJECT_ID, REGION
from classes.history_cache import GLOBAL_BSS_HISTORY_CACHE
from classes.llm_client import ChatLlmClient, LlmClient
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(name)s\n%(message)s\n"
)

logger = logging.getLogger("kahuna_backend")

class Utils():
    SessionFactory:None
    llm_timeout:300
    def color_print(self, text, color=None, end_value=None):
        COLOR_CODES = {
            'black': '30', 'red': '31', 'green': '32', 'yellow': '33', 'blue': '34', 'magenta': '35',
            'cyan': '36', 'white': '37', 'bright_black': '90', 'bright_red': '91', 'bright_green': '92',
            'bright_yellow': '93', 'bright_blue': '94', 'bright_magenta': '95', 'bright_cyan': '96', 'bright_white': '97'
        }
        if color and color.lower() in COLOR_CODES:
            color_code = COLOR_CODES[color.lower()]
            start = f"\033[{color_code}m"
            end = "\033[0m"
            text = f"{start}{text}{end}"
        logger.info(str(text))
        return False

    def clean_triple_backticks(self, code) -> str:
        pattern = r'```[a-zA-Z]*\n?|```\n?'
        return re.sub(pattern, '', code)

    def load_fault_tolerant_json(self, json_str, ensure_ordered=False, llm=None):
        """
        Attempts to load a JSON-like string using pyyaml for fault tolerance.
        Returns a dictionary if successful; otherwise, returns an empty dictionary.
        """
        def sanitize_json_string(input_str):
            """
            Sanitizes a JSON string containing C-like syntax.
            - Escapes problematic characters inside strings.
            - Removes comments safely without altering string content.
            - Preserves the integrity of embedded C-like code.
            Args:
                input_str (str): The raw JSON string to sanitize.
            Returns:
                str: A sanitized JSON string.
            """

            def process_string_segment(match):
                """
                Processes and sanitizes individual JSON string segments.
                - Handles escape sequences and literal newlines inside strings.
                """
                content = match.group(1)  # Extract the string content without surrounding quotes

                # Step 1: Escape unescaped backslashes not part of escape sequences
                content = re.sub(r'(?<!\\)\\(?![bfnrtu"\\/])', r'\\\\', content)

                # Step 2: Replace literal newlines within the string content
                content = re.sub(r'(?<!\\)\n', r'\\n', content)

                # Step 3: Escape unescaped double quotes inside strings
                content = re.sub(r'(?<!\\)"', r'\"', content)

                # Wrap the sanitized content back in double quotes
                return f'"{content}"'

            # Step 1: Remove comments outside of strings
            def remove_comments(input_str):
                """
                Removes comments (single-line // and multi-line /* */) from JSON,
                while ignoring comments inside string literals.
                """
                # Remove single-line comments
                no_single_line_comments = re.sub(r'//.*?$|/\*.*?\*/', '', input_str, flags=re.MULTILINE | re.DOTALL)
                return no_single_line_comments

            # Step 2: Process strings to protect their content
            def sanitize_strings(input_str):
                """
                Sanitizes JSON strings to escape problematic characters.
                """
                sanitized_json = re.sub(r'(?<!\\)"((?:[^"\\]|\\.)*?)"', process_string_segment, input_str, flags=re.DOTALL)
                return sanitized_json
            # Remove code markers
            input_str = self.clean_triple_backticks(input_str)
            # Clean code markers and comments first
            input_str = remove_comments(input_str)
            # Then sanitize strings to ensure proper escaping
            sanitized_json = sanitize_strings(input_str)

            return sanitized_json

        def load_json(json_str, ensure_ordered):
            from collections import OrderedDict
            err, data = "", None
            try:
                if ensure_ordered:
                    data = commentjson.loads(self.clean_triple_backticks(json_str), object_pairs_hook=OrderedDict)
                else:
                    data = commentjson.loads(self.clean_triple_backticks(json_str))
                return data, ""
            except Exception as e:
                err = str(e)
                data = None
            try:
                data = yaml.safe_load(sanitize_json_string(json_str))
                if isinstance(data, str):
                    raise Exception("load_fault_tolerant_json: YAML parsing failed.")
                return data, ""
            except Exception as e:
                err += "\n--\n" + str(e)
                data = None
            return data, err

        data, err = load_json(json_str, ensure_ordered)
        if data:
            return data
        from json_repair import repair_json
        repaired_json_str = repair_json(json_str)
        r_data, r_err = load_json(repaired_json_str, ensure_ordered)
        if r_data:
            return r_data
        self.color_print(f"load_fault_tolerant_json: JSON parsing failed: {r_err}. \nTrying LLM recovery...", color="red")
        prompt = f"""
I encountered an issue while parsing the following JSON data. Here is the original JSON string:
```
{json_str}
```
The error message was: {r_err}
Can you fix it?

Please return the corrected JSON string and nothing else, as further comments would screw up the JSON parsing.
If you think the JSON is correct, please return the JSON as it is. Again, no further comments.
Thanks :)
        """
        if llm:
            repaired_json_str = llm.invoke(prompt)
            r_data, r_err = load_json(repaired_json_str, ensure_ordered)
            if r_data:
                return r_data
        raise Exception(f"load_fault_tolerant_json: JSON parsing failed: {r_err} \n- Original JSON: {json_str}")

    def _coerce_field_to_str(self, value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        try:
            return json.dumps(value, indent=2)
        except TypeError:
            return str(value).strip()

    def unsafe_string_format(self, dest_string, print_unused_keys_report=True, **kwargs):
        """
        Formats a destination string by replacing placeholders with corresponding values from kwargs.
        If any placeholder key in the string is not found in kwargs, it raises a ValueError.

        it works differently from the standard "format" method as instead of looking for all the potential keys, looks only for the keys as passed in kwargs
        """
        # List to track keys that were not found
        missing_keys = []
        # Regex pattern to match placeholders like {key}
        def replacer(match):
            key = match.group(1)
            if key in kwargs:
                return str(kwargs[key])
            else:
                missing_keys.append(key)
                return match.group(0)  # Leave the placeholder unchanged

        pattern = re.compile(r'\{(\w+)\}')
        result = pattern.sub(replacer, dest_string)
        if missing_keys and print_unused_keys_report:
            logger.info(f"\033[93m\033[3mMissing keys within string-to-format in unsafe_string_format: {', '.join(missing_keys)}\033[0m")
            # print(f"\033[93m\033[3mOriginal string: {dest_string}\033[0m", flush=True)
        return result


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
    # LLM plumbing
    # -----------------------

    def _build_llms_for_model(self, model_name: str, timeout: float | None = None):
        """
        Build per-request LLM instances for the given model name.
        Falls back to None/None if creation fails.
        """
        if not timeout:
            timeout = self.llm_timeout
        try:
            llm = LlmClient(
                model_name=model_name,
                vertex_project=PROJECT_ID,
                vertex_region=REGION,
                timeout=timeout
            )
            chat_llm = ChatLlmClient(
                model_name=model_name,
                vertex_project=PROJECT_ID,
                vertex_region=REGION,
                timeout=timeout
            )
            return llm, chat_llm
        except Exception as e:
            logger.info(f"Warning: Could not initialize default LLMs: {e}. ")
            return None, None



    # -----------------------
    # BSS CHat
    # -----------------------

    def _extract_bss_label_order(self, bss_text: str) -> list[str]:
        found = re.findall(r"\*\s+([A-I]\d+_[A-Z0-9_]+):", bss_text or "")
        order, seen = [], set()
        for x in found:
            if x not in seen:
                seen.add(x)
                order.append(x)
        return order

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

    def _split_definition_segments(self, definition: str) -> dict[str, str]:
        """
        Split a legacy pipe-delimited definition string into named segments.

        Example input:
        "Definition: ... | Flow: ... | Notes: ... | References: ..."

        Returns a dict with lowercase keys like:
        {"definition": "...", "flow": "...", "notes": "..."}.
        """
        if not isinstance(definition, str) or not definition.strip():
            return {}

        text = definition
        header_re = re.compile(
            r"(^|(?<!\\)\|)\s*("
            r"Definition|Flow|Contract|Contracts|Snippets|Outcomes|Decision|Notes|References|Kind"
            r")\s*:\s*",
            flags=re.IGNORECASE,
        )

        matches = list(header_re.finditer(text))
        if not matches:
            # No structured segments → treat whole thing as 'definition'
            return {"definition": text.strip()}

        segments: dict[str, str] = {}
        for i, m in enumerate(matches):
            name_raw = (m.group(2) or "").strip()
            key = name_raw.lower()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            if body:
                segments[key] = body

        return segments

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

    def _bss_section_order(self) -> list[str]:
        return [
            "A",
            "UseCases",
            "Processes",
            "Components",
            "Actors",
            "UI",
            "Entities",
            "Integrations",
            "APIs",
            "NFRs",
        ]

    def _is_bss_label(self, label: str) -> bool:
        if not isinstance(label, str) or not label.strip():
            return False
        s = label.strip()
        if re.match(r"^A\d+_[A-Z0-9_]+$", s, flags=re.IGNORECASE):
            return True
        return re.match(r"^(UC|PROC|COMP|ROLE|UI|ENT|INT|API|NFR)-?\d+_[A-Z0-9_]+$", s, flags=re.IGNORECASE) is not None

    def _bss_label_type(self, label: str) -> str | None:
        if not isinstance(label, str):
            return None
        s = label.strip()

        if re.match(r"^A\d+_", s, flags=re.IGNORECASE):
            return "A"

        m = re.match(r"^(UC|PROC|COMP|ROLE|UI|ENT|INT|API|NFR)-?\d+_", s, flags=re.IGNORECASE)
        if not m:
            return None
        return m.group(1).upper()


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

    def _bss_allowed_segments_for_type(self, label_type: str) -> set[str]:
        """
        Allowed *definition* segments per label family, plus open_items / ask_log for all.
        Keys are lowercase.
        """
        lt = (label_type or "").upper()
        base_map = {
            "A":    ["notes"],
            "UC":   ["definition", "flow", "notes"],
            "PROC": ["definition", "flow", "snippets", "notes"],
            "COMP": ["definition", "kind", "notes"],
            "ROLE": ["definition", "notes"],
            "UI":   ["definition", "snippets", "notes"],
            "ENT":  ["definition", "contract", "notes"],
            "INT":  ["definition", "kind", "notes"],
            "API":  ["definition", "contract", "notes"],
            "NFR":  ["definition", "notes"],
        }
        allowed = {s.lower() for s in base_map.get(lt, [])}
        # Always allowed as separate fields
        allowed.update({"open_items", "ask_log"})
        return allowed


    def _bss_section_for_label(self, label: str) -> str | None:
        t = self._bss_label_type(label)
        if not t:
            return None

        mapping = {
            "A": "A",
            "UC": "UseCases",
            "PROC": "Processes",
            "COMP": "Components",
            "ROLE": "Actors",
            "UI": "UI",
            "ENT": "Entities",
            "INT": "Integrations",
            "API": "APIs",
            "NFR": "NFRs",
        }
        return mapping.get(t)

    def _iter_bss_items(self, bss_schema: dict):
        """
        Supports both:
        - new: {SectionName: {LABEL: item, ...}, ...}
        - flat: {LABEL: item, ...}
        - legacy-ish: {AnyKey: {LABEL: item, ...}, ...}
        """
        if not isinstance(bss_schema, dict):
            return

        for k, v in bss_schema.items():
            if isinstance(k, str) and k.lower() == "metadata":
                continue
            if self._is_bss_label(k):
                yield k, v
                continue

            if isinstance(v, dict):
                for k2, v2 in v.items():
                    if self._is_bss_label(k2):
                        yield k2, v2

    def _normalize_bss_item(self, item) -> dict:
        if not isinstance(item, dict):
            return {
                "status": "",
                "definition": self._coerce_field_to_str(item),
                "open_items": "",
                "ask_log": "",
                "cancelled": False,
                "dependencies": "",
                "dependants": "",
            }

        out = dict(item)

        # normalize value/Definition -> definition
        if "definition" not in out and "value" in out:
            out["definition"] = out.pop("value")
        if "definition" not in out and "Definition" in out:
            out["definition"] = out.pop("Definition")
        if "cancelled" not in out and "deleted" in out:
            out["cancelled"] = bool(out.get("deleted"))
        out.pop("deleted", None)

        out["status"] = (out.get("status") or "").strip()
        out["definition"] = self._coerce_field_to_str(out.get("definition"))
        out["open_items"] = self._coerce_field_to_str(out.get("open_items"))
        out["ask_log"] = self._coerce_field_to_str(out.get("ask_log"))
        out["cancelled"] = bool(out.get("cancelled", False))

        # host-maintained fields (default to empty)
        out["dependencies"] = self._coerce_field_to_str(out.get("dependencies"))
        out["dependants"] = self._coerce_field_to_str(out.get("dependants"))



        return out

    def _bss_label_sort_key(self, label: str):
        t = self._bss_label_type(label) or "ZZZ"
        type_order = {
            "A": 0,
            "UC": 1,
            "PROC": 2,
            "COMP": 3,
            "ROLE": 4,
            "UI": 5,
            "ENT": 6,
            "INT": 7,
            "API": 8,
            "NFR": 9,
            "ZZZ": 99,
        }
        m = re.match(r"^(A|UC|PROC|COMP|ROLE|UI|ENT|INT|API|NFR)-?(\d+)_", label.strip(), flags=re.IGNORECASE)
        n = int(m.group(2)) if m else 10**9
        return (type_order.get(t, 99), n, label)

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
        return []

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


    def _extract_reference_labels_from_definition(self, definition: str, open_items: str = "") -> list[str]:
        """
        Extract referenced labels from the item's free-text fields (definition + open_items).
        This function **must never** return None.
        """
        if not isinstance(definition, str):
            definition = "" if definition is None else str(definition)
        if not isinstance(open_items, str):
            open_items = "" if open_items is None else str(open_items)

        text = f"{definition}\n{open_items}"

        label_pattern = re.compile(
            r"\b("
            r"A\d+_[A-Z0-9_]+"
            r"|(?:UC|PROC|COMP|ROLE|UI|ENT|INT|API|NFR)-?\d+_[A-Z0-9_]+"
            r")\b",
            flags=re.IGNORECASE,
        )

        # findall always returns a list, but we also guard with `or []`
        found = label_pattern.findall(text)
        if not found:
            return []

        # keep original spelling; de-dupe case-insensitively
        seen: set[str] = set()
        out: list[str] = []

        for raw in found:
            label = raw.strip()
            if not label:
                continue
            if not self._is_bss_label(label):
                continue

            key = label.upper()
            if key in seen:
                continue
            seen.add(key)
            out.append(label)

        return out



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





    # !##############################################
    # ! CHAT PROMPT SUPPORT
    # !##############################################





    # async def _refine_all_second_pass_relationships_parallel(
    #     self,
    #     bss_schema: dict,
    #     draft_roots: set[str],
    #     model_name: str | None = None,
    # ) -> tuple[dict, list[dict], float]:
    #     if not isinstance(bss_schema, dict) or not draft_roots:
    #         return bss_schema, [], 0.0

    #     configs: list[tuple[tuple[str, str], str]] = [
    #         (("PROC", "PROC"), PROC_PROC_DEP_EXTRACTOR_PROMPT),
    #         (("UI",   "UI"),  UI_UI_DEP_EXTRACTOR_PROMPT),
    #         (("ENT",  "ENT"), ENT_ENT_DEP_EXTRACTOR_PROMPT),
    #     ]

    #     loop = asyncio.get_running_loop()

    #     async def run_core():
    #         return await loop.run_in_executor(
    #             None,
    #             self._refine_int_proc_ui_api_cross_relationships,
    #             bss_schema,
    #             draft_roots,
    #             model_name,
    #         )

    #     async def run_family(family: tuple[str, str], tmpl: str):
    #         return await loop.run_in_executor(
    #             None,
    #             self._reorient_internal_relationships_for_family_pair,
    #             bss_schema,
    #             draft_roots,
    #             family,
    #             tmpl,
    #             model_name,
    #         )

    #     tasks = [run_core()] + [run_family(fam, tmpl) for fam, tmpl in configs]
    #     core_result, *family_results = await asyncio.gather(*tasks)

    #     decisions_core, space_labels, extra_core = core_result
    #     total_cost = extra_core or 0.0
    #     all_decisions: list[tuple[str, str]] = []

    #     if space_labels:
    #         self._clear_modified_core_edges_within_cluster(bss_schema, space_labels)

    #     if decisions_core:
    #         all_decisions.extend(decisions_core)

    #     for decisions, extra in family_results:
    #         if decisions:
    #             all_decisions.extend(decisions)
    #         total_cost += (extra or 0.0)

    #     touched: set[str] = set()
    #     for parent, child in all_decisions:
    #         self._override_dependency_edge(bss_schema, parent, child)
    #         touched.add(parent)
    #         touched.add(child)

    #     if not touched:
    #         return bss_schema, [], total_cost

    #     updated_relationships: list[dict] = []
    #     for lbl in sorted(touched, key=self._bss_label_sort_key):
    #         section = self._bss_section_for_label(lbl)
    #         if not section:
    #             continue
    #         item = self._normalize_bss_item(
    #             (bss_schema.get(section) or {}).get(lbl) or {}
    #         )
    #         updated_relationships.append(
    #             {
    #                 "label": lbl,
    #                 "dependencies": item.get("dependencies", ""),
    #                 "dependants": item.get("dependants", ""),
    #             }
    #         )

    #     return bss_schema, updated_relationships, total_cost



    def _refine_all_second_pass_relationships(
        self,
        bss_schema: dict,
        draft_roots: set[str],
        model_name: str | None = None,
        project_id: str | None = None,
        user_text: str | None = None,
        bot_message: str | None = None,
        turn_labels: set[str] | None = None,
    ) -> tuple[dict, list[dict], float]:
        """
        Orchestrate the full second-pass refinement pipeline for the BSS graph.

        Steps (logically parallel, then applied in order):

        1) Cross-family INT/PROC/UI/API call edges
           - Re-evaluated around INT/PROC/UI/API labels touched in this turn,
             using CALL_SEQUENCE_EXTRACTOR_PROMPT.
        2) Same-family PROC/PROC, UI/UI, ENT/ENT orientations
           - Re-orient ambiguous same-family pairs using their family prompts.
        3) COMP ownership normalisation
           - For PROC/UI/ENT/API referenced by multiple COMPs, pick a single
             owner COMP or mark UNDECIDED.
        4) Ask-log synthesis for items touched in this turn

        All three analyses are computed from the same read-only snapshot of
        the input schema, so they are idempotent and can be parallelised.
        The resulting decisions are then applied linearly to the live schema:

            a) Clear affected cross-family INT/PROC/UI/API edges in the cluster.
            b) Apply all dependency-orientation decisions.
            c) Apply COMP ownership normalisation.

        Returns:
            (updated_schema, updated_relationships_for_touched_labels, total_extra_cost)
        """
        if not isinstance(bss_schema, dict) or not draft_roots:
            return bss_schema, [], 0.0

        # Families that still use the "internal reorientation" mini-prompts
        configs: list[tuple[tuple[str, str], str]] = [
            (("PROC", "PROC"), PROC_PROC_DEP_EXTRACTOR_PROMPT),
            (("UI",   "UI"),   UI_UI_DEP_EXTRACTOR_PROMPT),
            (("ENT",  "ENT"),  ENT_ENT_DEP_EXTRACTOR_PROMPT),
        ]

        total_cost = 0.0

        # ------------------------------------------------------------------
        # 0) Take a read-only snapshot for all analysis steps
        # ------------------------------------------------------------------
        # All refinement helpers below read from this snapshot so that:
        # - They see the same consistent graph.
        # - Their work can be parallelised later without ordering concerns.
        import copy
        schema_snapshot = copy.deepcopy(bss_schema)

        # ------------------------------------------------------------------
        # 1) Cross-family INT/PROC/UI/API call edges (analysis only)
        # ------------------------------------------------------------------
        cross_decisions: list[tuple[str, str]]
        space_labels: set[str]
        modified_core: set[str]

        (
            cross_decisions,
            space_labels,
            modified_core,
            extra_proc_api,
            unknown_callers,
        ) = self._refine_int_proc_ui_api_cross_relationships(
            schema_snapshot,
            draft_roots,
            model_name=model_name,
        )
        total_cost += (extra_proc_api or 0.0)

        # ------------------------------------------------------------------
        # 2) Same-family PROC/PROC, UI/UI, ENT/ENT orientations (analysis only)
        # ------------------------------------------------------------------
        same_family_decisions: list[tuple[str, str]] = []
        same_family_removals: list[tuple[str, str]] = []

        for family, tmpl in configs:
            decisions, removals, extra = self._reorient_internal_relationships_for_family_pair(

                schema_snapshot,
                draft_roots,
                family,
                tmpl,
                model_name=model_name,
            )
            if decisions:
                same_family_decisions.extend(decisions)
            if removals:
                same_family_removals.extend(removals)
            total_cost += (extra or 0.0)

        # ------------------------------------------------------------------
        # 3) COMP ownership normalisation (analysis only)
        # ------------------------------------------------------------------
        ownership_rows, ownership_cost = self._resolve_duplicate_comp_ownership(
            schema_snapshot,
            draft_roots,
            model_name=model_name,
        )
        total_cost += (ownership_cost or 0.0)
        # ------------------------------------------------------------------
        # 4) Ask-log synthesis (analysis only)
        # ------------------------------------------------------------------
        labels_for_asklog = turn_labels or draft_roots or set()
        ask_log_rows, ask_log_cost = self._summarize_asked_question(
            schema_snapshot,
            labels_for_asklog,
            project_id=project_id,
            user_text=user_text,
            bot_message=bot_message,
            model_name=model_name,
        )
        total_cost += (ask_log_cost or 0.0)
        # ------------------------------------------------------------------
        # 5) Progressive application of all decisions to the live schema
        # ------------------------------------------------------------------
        all_decisions: list[tuple[str, str]] = []
        if cross_decisions:
            all_decisions.extend(cross_decisions)
        if same_family_decisions:
            all_decisions.extend(same_family_decisions)

        touched: set[str] = set()

        # 5.a) Clear cross-family edges in the INT/PROC/UI/API cluster
        #      (so cross_decisions can be applied cleanly).
        if space_labels and modified_core:
            self._clear_modified_core_edges_within_cluster(
                bss_schema,
                space_labels=space_labels,
                modified_core=modified_core,
            )
            touched.update(space_labels)

        # 5.b) Apply same-family removals (no new edges, just clean-up)
        if same_family_removals:
            for a, b in same_family_removals:
                self._remove_dependency_edge(bss_schema, a, b)
                touched.add(a)
                touched.add(b)

        # 5.c) Apply all dependency-orientation decisions
        for parent, child in all_decisions:
            self._override_dependency_edge(bss_schema, parent, child)
            touched.add(parent)
            touched.add(child)

        # 5.d) Apply COMP ownership normalisation
        if ownership_rows:
            self._apply_normalized_comp_ownership_rows(bss_schema, ownership_rows)
            # Elements whose ownership was decided/flagged should also
            # be considered "touched" so the client can re-render them.
            for elem_label, _ in ownership_rows:
                if self._is_bss_label(elem_label):
                    touched.add(elem_label)

        # 5.e) Apply ask-log updates (does not affect dependency graph)
        if unknown_callers:
            self._apply_unknown_open_items(bss_schema, unknown_callers)

        if ask_log_rows:
            self._apply_ask_log_rows(bss_schema, ask_log_rows)

        # If nothing was touched at all, we can early-return.
        if not touched:
            return bss_schema, [], total_cost

        # ------------------------------------------------------------------
        # 6) Build the relationships payload for all touched labels
        # ------------------------------------------------------------------
        updated_relationships: list[dict] = []
        for lbl in sorted(touched, key=self._bss_label_sort_key):
            section = self._bss_section_for_label(lbl)
            if not section:
                continue
            item = self._normalize_bss_item(
                (bss_schema.get(section) or {}).get(lbl) or {}
            )
            updated_relationships.append(
                {
                    "label": lbl,
                    "dependencies": item.get("dependencies", ""),
                    "dependants": item.get("dependants", ""),
                }
            )

        return bss_schema, updated_relationships, total_cost



    def _apply_unknown_open_items(
        self,
        bss_schema: dict,
        callers: list[str],
    ) -> None:
        """
        For rows like API-*/INT-*,UNKNOWN we do NOT create graph edges, but we
        add a sys: open_items entry on the caller node.
        """
        if not isinstance(bss_schema, dict) or not callers:
            return

        seen = set()
        for lbl in callers:
            if not self._is_bss_label(lbl):
                continue
            if lbl.upper() in seen:
                continue
            seen.add(lbl.upper())

            t = (self._bss_label_type(lbl) or "").upper()
            if t not in {"API", "INT"}:
                continue

            section = self._bss_section_for_label(lbl)
            if not section:
                continue

            bss_schema.setdefault(section, {})
            item = self._normalize_bss_item(
                (bss_schema[section].get(lbl) or {})
            )

            existing_open = (item.get("open_items") or "").strip()
            if t == "API":
                msg = (
                    f"sys: '{lbl}' has no clearly identified PROC-* triggered "
                )
            else:  # INT
                msg = (
                    f"sys: '{lbl}' (inbound) has no clearly identified API-* callee and not known PROC-* triggered"
                )

            # Avoid duplicating the exact same sys line
            if msg in existing_open:
                continue

            if existing_open:
                item["open_items"] = msg + "\n" + existing_open
            else:
                item["open_items"] = msg

            bss_schema[section][lbl] = item

    def _refine_int_proc_ui_api_cross_relationships(
        self,
        bss_schema: dict,
        draft_roots: set[str],
        model_name: str | None = None,
    ) -> tuple[list[tuple[str, str]], set[str], set[str], float, list[str]]:
        """
        Compute refined cross-family INT/PROC/UI/API call edges around this turn's
        modified INT/PROC/UI/API labels.

        - Builds the "core cluster" (space_labels) of INT/PROC/UI/API nodes that are
          connected to modified_core via existing deps/depants.
        - Selects only allowed cross-family candidate pairs in that cluster that
          touch at least one modified_core label.
        - Builds a CALL_SEQUENCE_EXTRACTOR_PROMPT with:
            * Context from A/COMP nodes that reference any label in the cluster.
            * Per-label INT/PROC/UI/API definition segments for those labels.
        - Parses the LLM output as (dependent, dependency) rows and keeps only
          decisions for the candidate pairs.

        Returns:
        - decisions[(parent, child)]
        - space_labels (INT/PROC/UI/API cluster labels)
        - modified_core (subset of space_labels touched this turn)
        - extra_cost
        - unknown_callers: labels for which the LLM emitted `LABEL,UNKNOWN`
          (we create sys: open_items for these later; no edges are added)
        """
        if not isinstance(bss_schema, dict) or not draft_roots:
            return [], set(), set(), 0.0, []

        # 1) Flatten + type map
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

        if not flat:
            return [], set(), set(), 0.0, []

        core_families = {"INT", "PROC", "UI", "API"}
        core_labels: set[str] = {
            lbl for lbl, t in label_types.items() if t in core_families
        }
        if not core_labels:
             return [], set(), set(), 0.0, []

        # Only consider nodes of these families that were touched this turn
        modified_core: set[str] = {lbl for lbl in draft_roots if lbl in core_labels}
        if not modified_core:
             return [], set(), set(), 0.0, []

        canonical_by_upper = {lbl.upper(): lbl for lbl in flat.keys()}

        def _parse_csv_labels(csv_str: str) -> set[str]:
            out: set[str] = set()
            for part in (csv_str or "").split(","):
                p = (part or "").strip()
                if not p:
                    continue
                canon = canonical_by_upper.get(p.upper())
                if canon:
                    out.add(canon)
            return out

        # 2) Neighbours within INT/PROC/UI/API
        neighbours: dict[str, set[str]] = {lbl: set() for lbl in core_labels}
        for lbl in core_labels:
            norm = flat[lbl]
            deps = _parse_csv_labels(norm.get("dependencies", ""))
            depants = _parse_csv_labels(norm.get("dependants", ""))
            for other in deps | depants:
                if other in core_labels and other != lbl:
                    neighbours[lbl].add(other)

        roots: set[str] = {lbl for lbl in modified_core if neighbours.get(lbl)}
        if not roots:
            return [], set(), set(), 0.0, []

        # 3) Candidate cross-family INT/PROC/UI/API pairs (must touch at least one root)
        allowed_children = {
            "UI":   {"API", "PROC", "INT"},
            "INT":  {"API", "PROC", "UI"},
            "PROC": {"API", "UI",   "INT"},
            "API":  {"UI",  "INT",  "PROC"},
        }

        candidate_pairs: set[frozenset[str]] = set()
        for lbl in roots:
            t_lbl = label_types.get(lbl)
            if t_lbl not in core_families:
                continue
            for other in neighbours.get(lbl, set()):
                if other not in core_labels or other == lbl:
                    continue
                t_other = label_types.get(other)
                if t_other not in core_families:
                    continue
                # Skip same-family edges; those are handled by PROC/PROC, UI/UI queues.
                if t_other not in allowed_children.get(t_lbl, set()):
                    continue
                candidate_pairs.add(frozenset((lbl, other)))

        if not candidate_pairs:
            return [], set(), set(), 0.0, []

        # 4) Section B: all INT/PROC/UI/API nodes around the candidate cluster
        data_labels: set[str] = set()
        for pair in candidate_pairs:
            for lbl in pair:
                data_labels.add(lbl)
                data_labels.update(neighbours.get(lbl, set()))
        data_labels &= core_labels

        if not data_labels:
            return [], set(), set(), 0.0, []

        # 5) Section A: COMP + A* nodes that reference any data label
        data_label_set = set(data_labels)
        info_a_labels: set[str] = set()
        info_comp_labels: set[str] = set()

        for label, norm in flat.items():
            t = label_types.get(label)
            if t not in {"A", "COMP"}:
                continue
            refs = set(
                self._extract_reference_labels_from_definition(
                    norm.get("definition", ""),
                    norm.get("open_items", ""),
                )
            )
            if not refs & data_label_set:
                continue
            if t == "A":
                info_a_labels.add(label)
            else:
                info_comp_labels.add(label)

        info_lines: list[str] = []
        for lbl in sorted(info_a_labels, key=self._bss_label_sort_key):
            defn = (flat[lbl].get("definition") or "").strip()
            if not defn:
                continue
            info_lines.append(f"A-node {lbl}:")
            info_lines.append(defn)
            info_lines.append("")

        for lbl in sorted(info_comp_labels, key=self._bss_label_sort_key):
            defn = (flat[lbl].get("definition") or "").strip()
            if not defn:
                continue
            info_lines.append(f"COMP {lbl}:")
            info_lines.append(defn)
            info_lines.append("")

        info_block = "\n".join(info_lines).strip() or "None"

        # 6) Build Section B data (INT/PROC/UI/API nodes)
        data_lines: list[str] = []
        for lbl in sorted(data_labels, key=self._bss_label_sort_key):
            t = label_types.get(lbl, "")
            norm = flat.get(lbl, {})
            segs = self._split_definition_segments(norm.get("definition", ""))

            data_lines.append(lbl)
            data_lines.append(f"Type: {t}")

            if t == "INT":
                kind = (segs.get("kind") or "").strip()
                if kind:
                    data_lines.append(f"Kind: {kind}")

            definition = (segs.get("definition") or "").strip()
            if definition:
                data_lines.append(f"Definition: {definition}")

            flow = (segs.get("flow") or "").strip()
            if flow:
                data_lines.append(f"Flow: {flow}")

            notes = (segs.get("notes") or "").strip()
            if notes:
                data_lines.append(f"Notes: {notes}")

            # These proved to influence the L:LM in a bad way :)
            # deps = (norm.get("dependencies") or "").strip()
            # depants = (norm.get("dependants") or "").strip()
            # if deps:
            #     data_lines.append(f"Dependencies: {deps}")
            # if depants:
            #     data_lines.append(f"Dependants: {depants}")

            data_lines.append("")

        data_block = "\n".join(data_lines).strip()
        if not data_block:
            return [], set(), set(), 0.0, []

        # 7) Prompt with two sections, expecting "dependent, dependency" rows
        prompt = self.unsafe_string_format(
            CALL_SEQUENCE_EXTRACTOR_PROMPT,
            data_block=data_block,
            info_block=info_block
        )

        model_for_call = model_name or "gemini-2.5-flash-lite"
        llm, _ = self._build_llms_for_model(model_for_call)
        extra_cost = 0.0

        if llm:
            raw_text = llm.invoke(prompt)
            extra_cost = llm.get_accrued_cost()
        else:
            return [], set(), set(), 0.0, []

        pairs, unknown_callers = self._parse_dependency_rows(raw_text)
        if not pairs and not unknown_callers:
            return [], data_labels, modified_core, extra_cost, []

        # Only allow decisions for the candidate pairs we computed
        allowed_pairs_upper = {
            frozenset({a.upper(), b.upper()})
            for a, b in (tuple(p) for p in candidate_pairs)
        }

        decisions_by_pair: dict[frozenset[str], tuple[str, str]] = {}

        for dependent, dependency in pairs:
            dep_canon = canonical_by_upper.get((dependent or "").strip().upper())
            depd_canon = canonical_by_upper.get((dependency or "").strip().upper())
            if not dep_canon or not depd_canon:
                continue
            # Now we allow any INT/PROC/UI/API label in this subflow
            if dep_canon not in core_labels or depd_canon not in core_labels:
                continue

            pair_key_upper = frozenset({dep_canon.upper(), depd_canon.upper()})
            if pair_key_upper not in allowed_pairs_upper:
                continue

            # (dependent, dependency) maps directly to (parent, child)
            decisions_by_pair[pair_key_upper] = (dep_canon, depd_canon)

        decisions = list(decisions_by_pair.values())
        return decisions, data_labels, modified_core, extra_cost, unknown_callers



    def _reorient_internal_relationships_for_family_pair(
        self,
        bss_schema: dict,
        draft_roots: set[str],
        family: tuple[str, str],
        prompt_template: str,
        model_name: str | None = None,
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str]], float]:
        """
        Refine orientation of same-family dependency edges (PROC/PROC, UI/UI, ENT/ENT).

        - Restricts to labels of the given family pair and to those touched in this turn.
        - Uses segment-level references in definitions to find ambiguous pairs where
          both labels reference each other (reciprocal evidence).
        - Builds a small prompt describing only those ambiguous neighbourhoods.
        - Lets the LLM decide which label is the parent for each candidate pair.
        - Returns a list of (parent, child) decisions and the extra LLM cost.

        This function does NOT mutate bss_schema; orientation is applied later.
        """
        if not isinstance(bss_schema, dict) or not draft_roots:
            return [], [], 0.0

        fam_a, fam_b = family
        fam_a = (fam_a or "").upper()
        fam_b = (fam_b or "").upper()

        # 1) Flatten + type map
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

        if not flat:
            return [], [], 0.0

        # Labels belonging to either family
        family_labels: set[str] = {
            lbl for lbl, t in label_types.items()
            if t in (fam_a, fam_b)
        }
        if not family_labels:
            return [], [], 0.0

        modified_family: set[str] = {lbl for lbl in draft_roots if lbl in family_labels}
        if not modified_family:
            return [], [], 0.0

        canonical_by_upper = {lbl.upper(): lbl for lbl in flat.keys()}

        def _parse_csv_labels(csv_str: str) -> set[str]:
            out: set[str] = set()
            for part in (csv_str or "").split(","):
                p = (part or "").strip()
                if not p:
                    continue
                canon = canonical_by_upper.get(p.upper())
                if canon:
                    out.add(canon)
            return out

        # Neighbours restricted to family_labels
        neighbours: dict[str, set[str]] = {lbl: set() for lbl in family_labels}
        for lbl in family_labels:
            norm = flat[lbl]
            deps = _parse_csv_labels(norm.get("dependencies", ""))
            depants = _parse_csv_labels(norm.get("dependants", ""))
            for other in deps | depants:
                if other in family_labels and other != lbl:
                    neighbours[lbl].add(other)

        # Modified labels that actually touch another same-family label
        roots: set[str] = {lbl for lbl in modified_family if neighbours.get(lbl)}
        if not roots:
            return [], [], 0.0

        # Segment-level references between family labels
        (
            segments_text,
            seg_refs_by_seg,
            label_refs,
        ) = self._collect_segment_refs_for_family_labels(
            flat,
            label_types,
            family_labels,
        )

        # All candidate pairs:
        # - at least one endpoint is modified this turn (roots)
        # - connected via deps/depants
        candidate_pairs: set[frozenset[str]] = set()
        for lbl in roots:
            t_lbl = label_types.get(lbl)
            if t_lbl not in (fam_a, fam_b):
                continue
            for other in neighbours.get(lbl, set()):
                t_other = label_types.get(other)
                if not t_other:
                    continue
                if fam_a == fam_b:
                    # same-family (PROC/PROC, UI/UI, ENT/ENT)
                    if t_other == fam_a:
                        candidate_pairs.add(frozenset((lbl, other)))
                else:
                    # cross-family (PROC/API)
                    if {t_lbl, t_other} == {fam_a, fam_b}:
                        candidate_pairs.add(frozenset((lbl, other)))

        if not candidate_pairs:
            return [], [], 0.0

        decision_pairs: set[frozenset[str]] = set(candidate_pairs)

        # 3) Build data payload for the LLM
        lines: list[str] = []

        # 3.a) Explicit list of pairs to evaluate
        lines.append("\n### Label pairs to evaluate")
        for pair in sorted(
            decision_pairs,
            key=lambda p: tuple(
                sorted(tuple(p), key=self._bss_label_sort_key)
            ),
        ):
            a, b = sorted(tuple(pair), key=self._bss_label_sort_key)
            lines.append(f"{a}, {b}")

        # 3.b) Full descriptions for all labels involved in any pair
        involved_labels: set[str] = set()
        for pair in decision_pairs:
            for lbl in pair:
                involved_labels.add(lbl)

        lines.append("")
        lines.append("### Items descriptions:")

        for lbl in sorted(involved_labels, key=self._bss_label_sort_key):
            t = label_types.get(lbl)
            if t not in (fam_a, fam_b):
                continue

            lines.append(f"## {lbl}")
            lines.append(f"Type: {t}")

            segs_text = segments_text.get(lbl, {})
            for seg_name, body in segs_text.items():
                clean_name = seg_name.capitalize()
                lines.append(f"**{clean_name}:**")
                lines.append((body or "").strip() or "(empty)")

            lines.append("")

        data_block = "\n".join(lines).strip()
        if not data_block:
            return [], [], 0.0

        prompt = prompt_template.format(data_block=data_block)

        # 4) LLM call
        model_for_call = model_name or "gemini-2.5-flash-lite"
        llm, _ = self._build_llms_for_model(model_for_call)
        extra_cost = 0.0

        if llm:
            raw_text = llm.invoke(prompt)
            extra_cost = llm.get_accrued_cost()
        else:
            return [], [], 0.0

        decisions_rows, removals_rows = self._parse_pair_relationship_fixes(raw_text)
        if not decisions_rows and not removals_rows:
            return [], [], extra_cost

        # Only allow orientations for pairs we explicitly marked as decision_pairs
        allowed_pairs_upper = {
            frozenset({a.upper(), b.upper()})
            for (a, b) in (tuple(p) for p in decision_pairs)
        }

        decisions_by_pair: dict[frozenset[str], tuple[str, str]] = {}

        removal_pairs: set[frozenset[str]] = set()
        for a, b, parent in decisions_rows:
            a_canon = canonical_by_upper.get((a or "").strip().upper())
            b_canon = canonical_by_upper.get((b or "").strip().upper())
            parent_canon = canonical_by_upper.get((parent or "").strip().upper())

            if not a_canon or not b_canon or not parent_canon:
                continue
            if a_canon not in family_labels or b_canon not in family_labels:
                continue
            if parent_canon not in (a_canon, b_canon):
                continue
            if a_canon == b_canon:
                continue

            pair_key_upper = frozenset({a_canon.upper(), b_canon.upper()})
            if pair_key_upper not in allowed_pairs_upper:
                continue

            child_canon = a_canon if parent_canon == b_canon else b_canon
            decisions_by_pair[pair_key_upper] = (parent_canon, child_canon)

        for a, b in removals_rows:
            a_canon = canonical_by_upper.get((a or "").strip().upper())
            b_canon = canonical_by_upper.get((b or "").strip().upper())
            if not a_canon or not b_canon:
                continue
            if a_canon not in family_labels or b_canon not in family_labels:
                continue
            if a_canon == b_canon:
                continue
            pair_key_upper = frozenset({a_canon.upper(), b_canon.upper()})
            if pair_key_upper not in allowed_pairs_upper:
                continue
            removal_pairs.add(pair_key_upper)

        decisions = list(decisions_by_pair.values())
        removal_list: list[tuple[str, str]] = []
        for pair_key in removal_pairs:
            a_u, b_u = tuple(pair_key)
            a_canon = canonical_by_upper.get(a_u)
            b_canon = canonical_by_upper.get(b_u)
            if not a_canon or not b_canon:
                continue
            removal_list.append((a_canon, b_canon))

        return decisions, removal_list, extra_cost



    def _collect_segment_refs_for_family_labels(
        self,
        flat: dict[str, dict],
        label_types: dict[str, str],
        family_labels: set[str],
    ) -> tuple[dict, dict, dict]:
        """
        Analyze definition segments for a set of labels and collect intra-family references.

        For each label in family_labels:
        - Split its definition into segments (kind/definition/flow/notes/...).
        - For each segment, collect which other labels in family_labels are referenced.
        - Build:
            segments_text[label][segment_name] -> raw segment text
            seg_refs_by_seg[label][segment_name] -> set of referenced labels
            label_refs[label] -> union of all referenced labels across segments

        Used by same-family refinement to find ambiguous pairs with mutual mentions.
        """
        segments_text: dict[str, dict] = {}
        seg_refs_by_seg: dict[str, dict] = {}
        label_refs: dict[str, set] = {}

        for lbl in family_labels:
            norm = flat.get(lbl) or {}
            definition = (norm.get("definition") or "").strip()

            segs = self._split_definition_segments(definition)
            if not segs:
                segs = {"definition": definition}

            segments_text[lbl] = {}
            seg_refs_by_seg[lbl] = {}
            label_refs[lbl] = set()

            for seg_name, seg_body in segs.items():
                body = seg_body or ""
                segments_text[lbl][seg_name] = body

                if not body.strip():
                    continue

                refs = self._extract_reference_labels_from_definition(body)
                refs_in_family = {r for r in refs if r in family_labels}
                if not refs_in_family:
                    continue

                seg_refs_by_seg[lbl][seg_name] = refs_in_family
                label_refs[lbl].update(refs_in_family)

        return segments_text, seg_refs_by_seg, label_refs

    def _remove_dependency_edge(self, bss_schema: dict, label_a: str, label_b: str) -> None:
        """
        Remove any direct dependency edge between two labels (in either direction),
        keeping all other relationships untouched.
        """
        if not isinstance(bss_schema, dict):
            return

        section_a = self._bss_section_for_label(label_a)
        section_b = self._bss_section_for_label(label_b)
        if not section_a or not section_b:
            return

        bss_schema.setdefault(section_a, {})
        bss_schema.setdefault(section_b, {})

        item_a = self._normalize_bss_item((bss_schema[section_a].get(label_a) or {}))
        item_b = self._normalize_bss_item((bss_schema[section_b].get(label_b) or {}))

        def _parse_csv_list(csv_str: str) -> list[str]:
            parts: list[str] = []
            seen: set[str] = set()
            for part in (csv_str or "").split(","):
                p = (part or "").strip()
                if not p:
                    continue
                key = p.upper()
                if key in seen:
                    continue
                seen.add(key)
                parts.append(p)
            return parts

        def _filter(values: list[str], target: str) -> list[str]:
            tgt = target.upper()
            return [v for v in values if v.upper() != tgt]

        deps_a = _filter(_parse_csv_list(item_a.get("dependencies", "")), label_b)
        depants_a = _filter(_parse_csv_list(item_a.get("dependants", "")), label_b)
        deps_b = _filter(_parse_csv_list(item_b.get("dependencies", "")), label_a)
        depants_b = _filter(_parse_csv_list(item_b.get("dependants", "")), label_a)

        item_a["dependencies"] = ",".join(sorted(deps_a, key=self._bss_label_sort_key))
        item_a["dependants"] = ",".join(sorted(depants_a, key=self._bss_label_sort_key))
        item_b["dependencies"] = ",".join(sorted(deps_b, key=self._bss_label_sort_key))
        item_b["dependants"] = ",".join(sorted(depants_b, key=self._bss_label_sort_key))

        bss_schema[section_a][label_a] = item_a
        bss_schema[section_b][label_b] = item_b

    def _parse_pair_relationship_fixes(self, raw: str) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]]]:
        """
        Parse lines of the form:

            LABEL_1, LABEL_2: PARENT_LABEL
            LABEL_3, LABEL_4: NONE

        Returns:
        - decisions: list of (label1, label2, parent_label)
        - removals: list of (label1, label2) for NONE rows
        """
        decisions: list[tuple[str, str, str]] = []
        removals: list[tuple[str, str]] = []
        text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
        for ln in text.split("\n"):
            s = (ln or "").strip()
            if not s:
                continue
            if s.startswith("#"):
                continue
            if "," not in s or ":" not in s:
                continue
            left, parent_part = s.split(":", 1)
            parent_label = parent_part.strip()
            first, second = left.split(",", 1)
            l1 = first.strip()
            l2 = second.strip()
            if not (l1 and l2 and parent_label):
                continue
            if not (
                self._is_bss_label(l1)
                and self._is_bss_label(l2)
            ):
                continue

            if parent_label.upper() == "NONE":
                removals.append((l1, l2))
                continue

            if not self._is_bss_label(parent_label):
                continue

            decisions.append((l1, l2, parent_label))
        return decisions, removals


    def _parse_dependency_rows(self, raw: str) -> tuple[list[tuple[str, str]], list[str]]:
        """
        Parse lines of the form:

            DEPENDENT_LABEL, DEPENDENCY_LABEL

        Returns:
        - list of (dependent_label, dependency_label) where both are BSS labels
        - list of caller labels for rows where callee is the literal UNKNOWN
        """
        pairs: list[tuple[str, str]] = []
        unknown_callers: list[str] = []
        text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
        for ln in text.split("\n"):
            s = (ln or "").strip()
            if not s:
                continue
            if s.startswith("#"):
                continue
            if "," not in s:
                continue
            first, second = s.split(",", 1)
            dependent = (first or "").strip()
            dependency = (second or "").strip()
            if not (dependent and dependency):
                continue
            # SPECIAL CASE: UNKNOWN callee
            if dependency.upper() == "UNKNOWN":
                if self._is_bss_label(dependent):
                    unknown_callers.append(dependent)
                continue

            if not (
                self._is_bss_label(dependent)
                and self._is_bss_label(dependency)
            ):
                continue
            pairs.append((dependent, dependency))
        return pairs, unknown_callers

    def _override_dependency_edge(self, bss_schema: dict, parent: str, child: str) -> None:
        """
        Enforce a directed dependency edge between two labels.

        - Ensures that `parent` has `child` in its dependencies.
        - Ensures that `child` has `parent` in its dependants.
        - Removes any previous dependency/depandant entries that reverse this pair.
        - Keeps all other dependencies/depandants for both items untouched.

        Mutates bss_schema in place.
        """
        if not isinstance(bss_schema, dict):
            return

        section_p = self._bss_section_for_label(parent)
        section_c = self._bss_section_for_label(child)
        if not section_p or not section_c:
            return

        bss_schema.setdefault(section_p, {})
        bss_schema.setdefault(section_c, {})

        item_p = self._normalize_bss_item((bss_schema[section_p].get(parent) or {}))
        item_c = self._normalize_bss_item((bss_schema[section_c].get(child) or {}))

        def _parse_csv_list(csv_str: str) -> list[str]:
            parts = []
            seen = set()
            for part in (csv_str or "").split(","):
                p = (part or "").strip()
                if not p:
                    continue
                key = p.upper()
                if key in seen:
                    continue
                seen.add(key)
                parts.append(p)
            return parts

        # current lists
        deps_p = _parse_csv_list(item_p.get("dependencies", ""))
        deps_c = _parse_csv_list(item_c.get("dependencies", ""))
        deps_p = [x for x in deps_p if x.upper() != child.upper()]
        deps_c = [x for x in deps_c if x.upper() != parent.upper()]

        depants_p = _parse_csv_list(item_p.get("dependants", ""))
        depants_c = _parse_csv_list(item_c.get("dependants", ""))
        depants_p = [x for x in depants_p if x.upper() != child.upper()]
        depants_c = [x for x in depants_c if x.upper() != parent.upper()]

        # add parent -> child dependency
        if all(x.upper() != child.upper() for x in deps_p):
            deps_p.append(child)
        # ensure child has parent as dependant
        if all(x.upper() != parent.upper() for x in depants_c):
            depants_c.append(parent)

        # sort for stability
        deps_p_sorted = sorted(deps_p, key=self._bss_label_sort_key)
        deps_c_sorted = sorted(deps_c, key=self._bss_label_sort_key)
        depants_p_sorted = sorted(depants_p, key=self._bss_label_sort_key)
        depants_c_sorted = sorted(depants_c, key=self._bss_label_sort_key)

        item_p["dependencies"] = ",".join(deps_p_sorted)
        item_p["dependants"] = ",".join(depants_p_sorted)
        item_c["dependencies"] = ",".join(deps_c_sorted)
        item_c["dependants"] = ",".join(depants_c_sorted)

        bss_schema[section_p][parent] = item_p
        bss_schema[section_c][child] = item_c

    def _clear_modified_core_edges_within_cluster(
        self,
        bss_schema: dict,
        space_labels: set[str],
        modified_core: set[str],
    ) -> None:
        """
        Clear cross-family INT/PROC/UI/API edges inside a local cluster before re-write.

        - space_labels: full INT/PROC/UI/API cluster around the modified nodes.
        - modified_core: subset of space_labels that were actually edited this turn.

        Rules:
        - For labels in modified_core: drop ALL cross-family INT/PROC/UI/API edges
          that point to other core labels in space_labels.
        - For other labels in space_labels: drop only cross-family edges that point
          to a modified_core label.
        - Same-family core edges and edges to labels outside space_labels are preserved.

        This prepares the cluster so cross-family edges can then be cleanly re-applied
        from the LLM decisions.
        """
        if (
            not isinstance(bss_schema, dict)
            or not space_labels
            or not modified_core
        ):
            return

        space_upper = {lbl.upper() for lbl in space_labels}
        modified_upper = {lbl.upper() for lbl in modified_core}
        core_families = {"INT", "PROC", "UI", "API"}

        def _parse_csv_list(csv_str: str) -> list[str]:
            parts: list[str] = []
            seen: set[str] = set()
            for part in (csv_str or "").split(","):
                p = (part or "").strip()
                if not p:
                    continue
                key = p.upper()
                if key in seen:
                    continue
                seen.add(key)
                parts.append(p)
            return parts

        for label, _ in self._iter_bss_items(bss_schema):
            if label.upper() not in space_upper:
                continue

            section = self._bss_section_for_label(label)
            if not section:
                continue

            item = self._normalize_bss_item(
                (bss_schema.get(section) or {}).get(label) or {}
            )

            t_label = (self._bss_label_type(label) or "").upper()
            deps = _parse_csv_list(item.get("dependencies", ""))
            depants = _parse_csv_list(item.get("dependants", ""))

            is_modified = label.upper() in modified_upper

            def _filter_edges(edges: list[str]) -> list[str]:
                kept: list[str] = []
                for d in edges:
                    u = d.upper()
                    if u not in space_upper:
                        kept.append(d)
                        continue

                    t_other = (self._bss_label_type(d) or "").upper()
                    if t_label in core_families and t_other in core_families:
                        if is_modified:
                            # Modified node: drop all cross-family core edges.
                            if t_label != t_other:
                                continue
                        else:
                            # Related node: drop only cross-family edges to modified_core.
                            if u in modified_upper and t_label != t_other:
                                continue

                    kept.append(d)
                return kept

            deps = _filter_edges(deps)
            depants = _filter_edges(depants)

            item["dependencies"] = ",".join(
                sorted(deps, key=self._bss_label_sort_key)
            )
            item["dependants"] = ",".join(
                sorted(depants, key=self._bss_label_sort_key)
            )

            bss_schema.setdefault(section, {})
            bss_schema[section][label] = item


    def _resolve_duplicate_comp_ownership(
        self,
        bss_schema: dict,
        draft_roots: set[str],
        model_name: str | None = None,
    ) -> tuple[list[tuple[str, str]], float]:
        """
        Ask the LLM to resolve ownership of PROC/UI/ENT/API elements referenced by
        more than one COMP.

        - Scans all COMP definitions/open_items for structural label references.
        - Builds elems_to_comps: element -> set[COMP that reference it], for
          types in {PROC, UI, ENT, API}.
        - For elements referenced by >1 COMP, builds a COMP_OWNERSHIP_PROMPT
          containing the element plus all mentioning COMPs (and their kind).
        - LLM returns lines: ELEMENT_LABEL: OWNER_COMP_LABEL or ELEMENT_LABEL: UNDECIDED.

        Returns: (ownership_rows, extra_cost) and does NOT mutate bss_schema.
        """
        if not isinstance(bss_schema, dict):
            return [], 0.0

        # 1) Flatten + type map
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

        if not flat:
            return [], 0.0

        comp_labels: set[str] = {lbl for lbl, t in label_types.items() if t == "COMP"}
        if not comp_labels:
            return [], 0.0

        core_families = {"PROC", "UI", "ENT", "API"}

        # 2) Build reverse index: element -> set[COMP]
        elems_to_comps: dict[str, set[str]] = {}

        for comp_lbl in comp_labels:
            norm = flat[comp_lbl]
            definition = norm.get("definition", "") or ""
            open_items = norm.get("open_items", "") or ""

            refs = self._extract_reference_labels_from_definition(
                definition,
                open_items,
            )

            for ref in refs:
                t = label_types.get(ref)
                if t not in core_families:
                    continue
                elems_to_comps.setdefault(ref, set()).add(comp_lbl)

        # Only elements referenced by more than one COMP
        candidate_elems: list[str] = [
            elem for elem, comps in elems_to_comps.items() if len(comps) > 1
        ]
        if not candidate_elems:
            return [], 0.0

        # (Optional) you could restrict to ones touching draft_roots; for now we run on all.
        canonical_by_upper = {lbl.upper(): lbl for lbl in flat.keys()}

        # 3) Build data_block for the LLM
        lines: list[str] = []

        for elem in sorted(candidate_elems, key=self._bss_label_sort_key):
            elem_norm = flat.get(elem, {})
            elem_type = label_types.get(elem, "")
            elem_def = (elem_norm.get("definition") or "").strip()
            elem_open = (elem_norm.get("open_items") or "").strip()

            lines.append(f"## ELEMENT {elem}")
            lines.append(f"Type: {elem_type}")
            if elem_def:
                lines.append("Definition:")
                lines.append(elem_def)
            if elem_open:
                lines.append("Open_items:")
                lines.append(elem_open)
            lines.append("")

            lines.append("### Referenced_by:")
            for comp in sorted(elems_to_comps[elem], key=self._bss_label_sort_key):
                comp_norm = flat.get(comp, {})
                comp_def = (comp_norm.get("definition") or "").strip()
                comp_open = (comp_norm.get("open_items") or "").strip()

                segs = self._split_definition_segments(comp_def)
                kind = (segs.get("kind") or "").strip()

                header = f"- {comp}"
                if kind:
                    header += f" (kind: {kind})"
                lines.append(header)

                if comp_def:
                    lines.append(f"  Definition: {comp_def}")
                if comp_open:
                    # Keep this one-line to avoid flooding the prompt
                    lines.append(f"  Open_items: {comp_open}")
                lines.append("")

            lines.append("")

        data_block = "\n".join(lines).strip()
        if not data_block:
            return [], 0.0

        prompt = self.unsafe_string_format(
            COMP_OWNERSHIP_PROMPT,
            data_block=data_block,
        )

        # 4) LLM call
        model_for_call = model_name or "gemini-2.5-flash-lite"
        llm, _ = self._build_llms_for_model(model_for_call)
        extra_cost = 0.0

        if llm:
            raw_text = llm.invoke(prompt)
            extra_cost = llm.get_accrued_cost()
        else:
            return [], 0.0

        rows = self._parse_comp_ownership_rows(raw_text)
        if not rows:
            return [], extra_cost

        # No mutation here; just pass the raw ownership rows back.
        return rows, extra_cost


    def _parse_comp_ownership_rows(self, raw: str) -> list[tuple[str, str]]:
        """
        Parse LLM output lines of the form:

            ELEMENT_LABEL: OWNER_COMP_LABEL
            ELEMENT_LABEL: UNDECIDED

        Returns a list of (element_label, owner_token), where owner_token is either
        a COMP label or the literal 'UNDECIDED'. Non-BSS element labels are ignored.
        """
        out: list[tuple[str, str]] = []
        text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
        for ln in text.split("\n"):
            s = (ln or "").strip()
            if not s:
                continue
            if s.startswith("#"):
                continue
            if ":" not in s:
                continue
            left, owner_part = s.split(":", 1)
            elem_label = (left or "").strip()
            owner_label = (owner_part or "").strip()
            if not elem_label or not owner_label:
                continue
            # element must look like a BSS label; owner can be UNDECIDED
            if not self._is_bss_label(elem_label):
                continue
            out.append((elem_label, owner_label))
        return out

    def _apply_normalized_comp_ownership_rows(
        self,
        bss_schema: dict,
        ownership_rows: list[tuple[str, str]],
    ) -> None:
        """
        Apply COMP ownership decisions produced by _resolve_duplicate_comp_ownership.

        For each (element_label, owner_token):
        - Rebuild the reverse index elems_to_comps from the current schema.
        - If owner_token == 'UNDECIDED':
            * Mark the element as undecided to later prepend a sys: message.
        - Else, if owner_token is a valid COMP that actually references the element:
            * For all other COMPs that reference the element:
              - Replace occurrences of the structured label (e.g. ENT_3_Customer)
                with a spaced version (e.g. ENT 3 Customer) in definition/open_items,
                so the reference stops being structural.
        - For undecided elements:
            * Prepend a sys: line to open_items describing the unresolved ownership
              and listing the competing COMPs.

        Mutates bss_schema in place (COMP definitions/open_items and element open_items).
        """
        if not isinstance(bss_schema, dict) or not ownership_rows:
            return

        # 1) Flatten + type map
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

        if not flat:
            return

        comp_labels: set[str] = {lbl for lbl, t in label_types.items() if t == "COMP"}
        if not comp_labels:
            return

        core_families = {"PROC", "UI", "ENT", "API"}

        # 2) Rebuild reverse index: element -> set[COMP] that reference it
        elems_to_comps: dict[str, set[str]] = {}

        for comp_lbl in comp_labels:
            norm = flat[comp_lbl]
            definition = norm.get("definition", "") or ""
            open_items = norm.get("open_items", "") or ""

            refs = self._extract_reference_labels_from_definition(
                definition,
                open_items,
            )

            for ref in refs:
                t = label_types.get(ref)
                if t not in core_families:
                    continue
                elems_to_comps.setdefault(ref, set()).add(comp_lbl)

        if not elems_to_comps:
            return

        canonical_by_upper = {lbl.upper(): lbl for lbl in flat.keys()}
        undecided_elems: set[str] = set()

        # 3) Apply ownership decisions
        for elem_raw, owner_raw in ownership_rows:
            elem_canon = canonical_by_upper.get((elem_raw or "").strip().upper())
            if not elem_canon:
                continue

            comps_for_elem = elems_to_comps.get(elem_canon)
            if not comps_for_elem:
                # LLM talked about an element that doesn't match our candidates anymore
                continue

            owner_str = (owner_raw or "").strip()
            if not owner_str:
                continue

            # UNDECIDED: just mark, we will add sys: messages later
            if owner_str.upper() == "UNDECIDED":
                undecided_elems.add(elem_canon)
                continue

            owner_canon = canonical_by_upper.get(owner_str.upper())
            if not owner_canon:
                continue
            if owner_canon not in comps_for_elem:
                # bogus owner; ignore
                continue

            spaced = elem_canon.replace("_", " ")

            # For all non-owner comps: de-label the element mention
            for comp in comps_for_elem:
                if comp == owner_canon:
                    continue

                section_c = self._bss_section_for_label(comp)
                if not section_c:
                    continue

                bss_schema.setdefault(section_c, {})
                comp_item = self._normalize_bss_item(
                    (bss_schema[section_c].get(comp) or {})
                )

                for field in ("definition", "open_items"):
                    body = comp_item.get(field) or ""
                    if not body:
                        continue
                    # simple textual replacement; enough to kill the structured label
                    comp_item[field] = body.replace(elem_canon, spaced)

                bss_schema[section_c][comp] = comp_item

        # 4) Add sys: open_items messages for undecided elements
        for elem in undecided_elems:
            section_e = self._bss_section_for_label(elem)
            if not section_e:
                continue

            bss_schema.setdefault(section_e, {})
            elem_item = self._normalize_bss_item(
                (bss_schema[section_e].get(elem) or {})
            )

            existing_open = (elem_item.get("open_items") or "").strip()
            comp_list = sorted(
                elems_to_comps.get(elem, set()),
                key=self._bss_label_sort_key,
            )
            comps_txt = ", ".join(comp_list)

            msg = (
                f"sys: the ownership of '{elem}' cannot be safely computed "
                f"between {comps_txt}. Please intervene immediately"
            )

            if existing_open:
                elem_item["open_items"] = msg + "\n" + existing_open
            else:
                elem_item["open_items"] = msg

            bss_schema[section_e][elem] = elem_item

    def _summarize_asked_question(
        self,
        schema_snapshot: dict,
        labels: set[str],
        project_id: str | None,
        user_text: str | None,
        bot_message: str | None,
        model_name: str | None = None,
    ) -> tuple[list[tuple[str, str]], float]:
        """
        Ask-log synthesis step using ASK_LOG_SYNTH_PROMPT.

        Inputs:
        - CHAT_HISTORY: recent messages (plus latest user_text / bot_message)
        - CURRENT_ASK_LOG: lines of form <LABEL>:<ask_log> for labels in `labels`

        Output:
        - list of (label, new_ask_log) rows to apply
        - extra LLM cost
        """
        from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

        if (
            not isinstance(schema_snapshot, dict)
            or not labels
            or not project_id
            or not user_text
            or not bot_message
        ):
            return [], 0.0

        # Build CHAT_HISTORY block
        try:
            history_msgs = GLOBAL_BSS_HISTORY_CACHE.snapshot(project_id)
        except Exception:
            history_msgs = []

        # Take only a small suffix to keep prompt compact
        max_history = 6
        recent = history_msgs[-max_history:] if history_msgs else []

        lines: list[str] = []
        for msg in recent:
            role = "other"
            if isinstance(msg, HumanMessage):
                role = "user"
            elif isinstance(msg, AIMessage):
                role = "assistant"
            elif isinstance(msg, SystemMessage):
                role = "system"
            content = getattr(msg, "content", "")
            lines.append(f"{role}: {content}")

        # Append the current turn explicitly (not yet in the cache)
        lines.append("\n\n\n**LAST EXCHANGE**:")
        lines.append(f"user: {user_text}")
        lines.append(f"assistant: {bot_message}")

        chat_history_block = "\n".join(lines).strip()

        # Build CURRENT_ASK_LOG block for the labels of interest
        # (labels are BSS labels, we only include ones present in the snapshot)
        current_ask_lines: list[str] = []
        for lbl in sorted(labels, key=self._bss_label_sort_key):
            section = self._bss_section_for_label(lbl)
            if not section:
                continue
            item = (schema_snapshot.get(section) or {}).get(lbl)
            if item is None:
                continue
            norm = self._normalize_bss_item(item)
            ask_log = (norm.get("ask_log") or "").strip()
            current_ask_lines.append(f"{lbl}:{ask_log}")

        if not current_ask_lines:
            return [], 0.0

        current_ask_block = "\n".join(current_ask_lines)

        prompt = self.unsafe_string_format(
            ASK_LOG_SYNTH_PROMPT,
            CHAT_HISTORY = chat_history_block,
            CURRENT_ASK_LOG=current_ask_block
        )

        model_for_call = model_name or "gemini-2.5-flash-lite"
        llm, _ = self._build_llms_for_model(model_for_call)
        extra_cost = 0.0

        if llm:
            text = llm.invoke(prompt)
            extra_cost = llm.get_accrued_cost()
        else:
            return [], 0.0

        text = self.clean_triple_backticks(text or "").strip()
        if not text:
            return [], extra_cost

        if text.strip().lower() == "none":
            return [], extra_cost

        updates: list[tuple[str, str]] = []
        valid_labels = {lbl for lbl in labels}

        for ln in text.split("\n"):
            s = (ln or "").strip()
            if not s:
                continue
            if s.startswith("#"):
                continue
            if s.lower().startswith("none"):
                continue
            if ":" not in s:
                continue
            label_part, ask_part = s.split(":", 1)
            lbl = (label_part or "").strip()
            ask_log_new = (ask_part or "").strip()
            if not lbl or not ask_log_new:
                continue
            if not self._is_bss_label(lbl):
                continue
            if lbl not in valid_labels:
                continue
            updates.append((lbl, ask_log_new))

        return updates, extra_cost

    def _apply_ask_log_rows(
        self,
        bss_schema: dict,
        ask_log_rows: list[tuple[str, str]],
    ) -> None:
        """
        Apply ask-log updates produced by _summarize_asked_question.
        Does not touch dependencies/depandants.
        """
        if not isinstance(bss_schema, dict) or not ask_log_rows:
            return

        for lbl, ask_log in ask_log_rows:
            if not self._is_bss_label(lbl):
                continue
            section = self._bss_section_for_label(lbl)
            if not section:
                continue
            section_dict = bss_schema.get(section) or {}
            if lbl not in section_dict:
                continue
            item = self._normalize_bss_item(section_dict.get(lbl) or {})
            item["ask_log"] = self._coerce_field_to_str(ask_log)
            bss_schema.setdefault(section, {})
            bss_schema[section][lbl] = item
