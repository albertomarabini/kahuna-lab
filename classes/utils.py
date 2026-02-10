import json
import logging
import re
import traceback
import commentjson
import yaml

from classes.chat_prompts import BSS_PROMPT_EXAMPLES
from classes.entities import Project
from classes.google_helpers import PROJECT_ID, REGION
from classes.llm_client import ChatLlmClient, LlmClient

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(name)s\n%(message)s\n"
)

logger = logging.getLogger("kahuna_backend")

class Utils():
    SessionFactory:None
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

    def _build_llms_for_model(self, model_name: str):
        """
        Build per-request LLM instances for the given model name.
        Falls back to None/None if creation fails.
        """
        try:
            llm = LlmClient(
                model_name=model_name,
                vertex_project=PROJECT_ID,
                vertex_region=REGION,
            )
            chat_llm = ChatLlmClient(
                model_name=model_name,
                vertex_project=PROJECT_ID,
                vertex_region=REGION,
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

    def _log_missing_required_segments(self, label: str, segments: dict[str, str]) -> None:
        """
        After applying a patch and rebuilding segments, log if mandatory
        segments are missing for a given label type.
        """
        label_type = self._bss_label_type(label) or ""

        required_by_type: dict[str, list[str]] = {
            # Project-level canvas: at least some notes
            "A": ["notes"],
            # Use cases: we expect a definition and a flow
            "UC": ["definition", "flow"],
            # All others: at least a definition
            "COMP": ["definition"],
            "PROC": ["definition"],
            "ROLE": ["definition"],
            "UI": ["definition"],
            "ENT": ["definition"],
            "INT": ["definition"],
            "API": ["definition"],
            "NFR": ["definition"],
        }

        required = required_by_type.get(label_type, [])
        if not required:
            return

        missing = [
            name for name in required
            if not (segments.get(name) or "").strip()
        ]
        if missing:
            logger.warning(
                f"[BSS] Missing expected segments for {label} (type {label_type}): "
         ", ".join(missing)
            )



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
            "INT":  ["definition", "notes"],
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
        allowed_deps = {
            "A":   {"UC", "ROLE", "NFR", "PROC", "ENT", "INT", "API", "UI", "COMP"},
            "UC":  {"ROLE", "UI", "PROC", "API", "ENT", "INT", "NFR", "COMP"},
            "PROC":{"COMP", "ENT", "INT", "API", "NFR", "ROLE", "PROC", "UI"},
            "COMP":{"COMP", "INT", "NFR", "API", "ENT", "UI"},
            "ROLE":{"UI"},
            "UI":  {"API", "NFR", "COMP"},
            "ENT": {"ENT"},
            "INT": {"API"},
            "API": {"ENT", "NFR", "COMP"},
            "NFR": set(),
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

        def can_have_child(parent: str, child: str) -> bool:
            pt = label_types.get(parent)
            ct = label_types.get(child)
            return bool(pt and ct and ct in allowed_deps.get(pt, set()))

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
                    if dst in all_labels:
                        pair_set.add(frozenset((src, dst)))

            children: dict[str, set[str]] = {lbl: set() for lbl in flat.keys()}

            for pair in pair_set:
                a, b = tuple(pair)  # exactly 2 elements

                # stable order for tie-breaks
                if self._bss_label_sort_key(b) < self._bss_label_sort_key(a):
                    a, b = b, a

                can_a = can_have_child(a, b)  # a can have b as dependency
                can_b = can_have_child(b, a)  # b can have a as dependency

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
                    pair_set.add(frozenset((src, dst)))

            # Orient these pairs and update children on top of baseline.
            for pair in pair_set:
                a, b = tuple(pair)

                if self._bss_label_sort_key(b) < self._bss_label_sort_key(a):
                    a, b = b, a

                can_a = can_have_child(a, b)
                can_b = can_have_child(b, a)

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

        return {k: v for k, v in out.items() if isinstance(v, dict) and v}





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
        return out


    def _refine_proc_api_relationships(
        self,
        bss_schema: dict,
        draft_roots: set[str],
        llm_client,
    ) -> tuple[dict, list[dict]]:
        """
        Second-pass relationship refinement for PROC <-> API edges.

        - Only considers labels that:
          - are in draft_roots AND are PROC or API, and
          - any PROC/API directly connected to them via dependencies/dependants.
        - Asks the LLM to decide the correct parent (dependant) for each
          PROC/API pair and adjusts dependencies/dependants accordingly.
        - Returns (updated_schema, updated_relationships_list).

        updated_relationships_list is a list of:
          { "label": ..., "dependencies": ..., "dependants": ... }
        for all labels whose relationships were changed.
        """
        if not llm_client or not isinstance(bss_schema, dict) or not draft_roots:
            return bss_schema, []

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
            return bss_schema, []

        # Only PROC/API labels
        proc_api_labels: set[str] = {
            lbl
            for lbl, t in label_types.items()
            if t in ("PROC", "API")
        }
        if not proc_api_labels:
            return bss_schema, []

        # Draft items of family PROC/API that were emitted in this turn
        modified_proc_api: set[str] = {
            lbl for lbl in draft_roots if lbl in proc_api_labels
        }
        if not modified_proc_api:
            return bss_schema, []

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

        # Neighbours restricted to PROC/API
        neighbours: dict[str, set[str]] = {lbl: set() for lbl in proc_api_labels}
        for lbl in proc_api_labels:
            norm = flat[lbl]
            deps = _parse_csv_labels(norm.get("dependencies", ""))
            depants = _parse_csv_labels(norm.get("dependants", ""))
            for other in deps | depants:
                if other in proc_api_labels and other != lbl:
                    neighbours[lbl].add(other)

        # Modified PROC/API that actually touch another PROC/API
        roots: set[str] = {
            lbl for lbl in modified_proc_api if neighbours.get(lbl)
        }
        if not roots:
            return bss_schema, []

        # Segment-level references between PROC/API labels
        segments_text, seg_refs_by_seg, label_refs = self._collect_segment_refs_for_proc_api(
            flat,
            label_types,
            proc_api_labels,
        )

        # All PROC–API pairs where:
        # - at least one endpoint is modified this turn (roots)
        # - they are connected via deps/depants
        candidate_pairs: set[frozenset[str]] = set()
        for lbl in roots:
            t_lbl = label_types.get(lbl)
            if t_lbl not in ("PROC", "API"):
                continue
            for other in neighbours.get(lbl, set()):
                t_other = label_types.get(other)
                if t_other not in ("PROC", "API"):
                    continue
                if t_other == t_lbl:
                    continue
                candidate_pairs.add(frozenset((lbl, other)))

        if not candidate_pairs:
            return bss_schema, []

        # Decide which pairs we will actually try to orient.
        decision_pairs: set[frozenset[str]] = set()
        dropped_pairs: set[frozenset[str]] = set()

        roots_list = list(roots)

        for pair in candidate_pairs:
            a, b = tuple(pair)
            # a,b could be in any order; just use label_refs symmetry
            a_refs = label_refs.get(a, set())
            b_refs = label_refs.get(b, set())

            a_has_b = b in a_refs
            b_has_a = a in b_refs
            reciprocal = a_has_b and b_has_a

            # "somebody else referencing them": another modified PROC/API
            # whose segments mention either a or b
            third_party = False
            for lbl in roots_list:
                if lbl in pair:
                    continue
                refs = label_refs.get(lbl, set())
                if a in refs or b in refs:
                    third_party = True
                    break

            if reciprocal:
                # Safe to ask the model to orient this pair
                decision_pairs.add(pair)
            else:
                if not third_party:
                    # No reciprocal segment refs and nobody else anchors them:
                    # too risky → don't even present this pair as a connection.
                    dropped_pairs.add(pair)
                else:
                    # Keep as context only (via other items), but do NOT ask the
                    # model to change this direct dependency.
                    dropped_pairs.add(pair)

        if not decision_pairs:
            # No pair that we trust the model to touch
            return bss_schema, []

        # 3) Build data payload for the LLM
        #    - only segments that actually reference other PROC/API labels
        #    - adjacency lists exclude dropped_pairs
        lines: list[str] = []

        # adjacency for the prompt, excluding dropped pairs
        # only maintain adjacency for ROOT labels (modified this turn)
        adj_for_prompt: dict[str, set[str]] = {lbl: set() for lbl in roots}
        for pair in candidate_pairs:
            if pair in dropped_pairs:
                continue
            a, b = tuple(pair)
            if a in adj_for_prompt:
                adj_for_prompt[a].add(b)
            if b in adj_for_prompt:
                adj_for_prompt[b].add(a)

        for lbl in sorted(roots, key=self._bss_label_sort_key):
            t = label_types.get(lbl)
            if t not in ("PROC", "API"):
                continue

            seg_refs = seg_refs_by_seg.get(lbl, {})
            if not seg_refs:
                # 4) If no segment references any PROC/API → transmit nothing
                continue

            opp_type = "API" if t == "PROC" else "PROC"
            opp_neighbors = sorted(
                [
                    other
                    for other in adj_for_prompt.get(lbl, set())
                    if label_types.get(other) == opp_type
                ],
                key=self._bss_label_sort_key,
            )

            lines.append("## " + lbl)
            lines.append(f"Type: {t}")

            if opp_neighbors:
                header = "Connected_APIs" if t == "PROC" else "Connected_PROCs"
                lines.append(f"### {header}:")
                for other in opp_neighbors:
                    lines.append(f"- {other}")

            # Only emit segments that reference at least one PROC/API label
            segs_text = segments_text.get(lbl, {})
            for seg_name, body in segs_text.items():
                if seg_name not in seg_refs:
                    continue
                clean_name = seg_name.capitalize()
                lines.append(f"**{clean_name}:**")
                lines.append((body or "").strip() or "(empty)")

            lines.append("")  # blank line

        data_block = "\n".join(lines).strip()
        if not data_block:
            return bss_schema, []

        prompt = f"""
You resolve the direction of dependencies between processes (PROC-*) and APIs (API-*).

Each item below is either a PROC or an API. For each item you see:
- its label and family (PROC or API),
- its full definition/notes,
- the items of the opposite family it is directly connected with.

For every connected PROC/API pair, decide which label is the PARENT (the dependant)
and which is the CHILD (the dependency), using this convention:

- Parent depends on child.
- Children are dependencies.
- Parents are dependants.

HOW TO DECIDE

Read the natural language around each PROC/API pair from BOTH sides.

Think in terms of who CALLS whom:

- The caller (initiator) is the PARENT (depends on the other).
- The callee (implementation/handler) is the CHILD (dependency).

Treat the PROCESS as PARENT and the API as CHILD when the text suggests the process is the caller or client, for example if it says the process:
- "calls", "invokes", "uses", "sends requests to" the API, or
- "uses <API> to do X", or
- "sends an action to <API>", or
- more generally: the process is the one initiating a request to that API.

Treat the API as PARENT and the PROCESS as CHILD when the text suggests the API is only an inbound carrier whose requests are handled by the process, for example if it says the API:
- is "implemented by", "handled by", "executed by", "backed by" that process, or
- "receives a request and forwards it to <PROC>", or
- "is consumed by <PROC>", "is routed to <PROC>", or
- "<PROC> is the backend for <API>".

If wording is vague ("works with", "interacts with", "connected to") and you cannot confidently tell who is the caller and who is the handler, skip that pair.

Output rules (very important):

- You may output zero or more lines. If the output is zero lines just return `None.`
- Each line MUST have this exact format:

  <LABEL_1>, <LABEL_2>: <PARENT_LABEL>

- <LABEL_1> and <LABEL_2> must be the two labels of the pair (order does not matter).
- <PARENT_LABEL> must be exactly one of <LABEL_1> or <LABEL_2>.
- If you are not confident about a pair, DO NOT emit a line for it.
- No extra text, no explanations, no code fences.

Here is the data:

{data_block}

        """.strip()

        from langchain_core.messages import HumanMessage

        # Support both chat-style and completion-style clients.
        if isinstance(llm_client, ChatLlmClient):
            raw = llm_client.invoke([HumanMessage(content=prompt)])
            raw_text = getattr(raw, "content", str(raw))
        else:
            # Assume a completion-style client that accepts a plain string.
            raw_text = llm_client.invoke(prompt)

        pairs = self._parse_comp_api_relationship_fixes(raw_text)
        if not pairs:
            return bss_schema, []

        # Only allow orientations for pairs we explicitly marked as decision_pairs
        allowed_pairs_upper = {
            frozenset({a.upper(), b.upper()})
            for (a, b) in (
                tuple(p) for p in decision_pairs
            )
        }

        # 4) Apply orientation decisions
        touched: set[str] = set()


        for a, b, parent in pairs:
            a_canon = canonical_by_upper.get((a or "").strip().upper())
            b_canon = canonical_by_upper.get((b or "").strip().upper())
            parent_canon = canonical_by_upper.get((parent or "").strip().upper())

            if not a_canon or not b_canon or not parent_canon:
                continue
            if a_canon not in proc_api_labels or b_canon not in proc_api_labels:
                continue
            if parent_canon not in (a_canon, b_canon):
                continue
            if a_canon == b_canon:
                continue

            pair_key_upper = frozenset({a_canon.upper(), b_canon.upper()})
            if pair_key_upper not in allowed_pairs_upper:
                # Either a dropped pair or completely unrelated to this turn
                continue

            # child is "the other one"
            child_canon = a_canon if parent_canon == b_canon else b_canon

            self._override_dependency_edge(bss_schema, parent_canon, child_canon)
            touched.add(parent_canon)
            touched.add(child_canon)

        if not touched:
            return bss_schema, []

        # 5) Build updated_relationships payload for the client
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

        return bss_schema, updated_relationships

    def _collect_segment_refs_for_proc_api(
        self,
        flat: dict[str, dict],
        label_types: dict[str, str],
        proc_api_labels: set[str],
    ) -> tuple[dict, dict, dict]:
        """
        For each PROC/API label:
        - split its definition into segments
        - for each segment, collect which other PROC/API labels it references
        Returns:
          segments_text[label][seg_name] -> text
          seg_refs_by_seg[label][seg_name] -> set[labels]
          label_refs[label] -> set[labels]  (union over segments)
        """
        segments_text: dict[str, dict] = {}
        seg_refs_by_seg: dict[str, dict] = {}
        label_refs: dict[str, set] = {}

        for lbl in proc_api_labels:
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
                refs_in_family = {r for r in refs if r in proc_api_labels}
                if not refs_in_family:
                    continue

                seg_refs_by_seg[lbl][seg_name] = refs_in_family
                label_refs[lbl].update(refs_in_family)

        return segments_text, seg_refs_by_seg, label_refs


    def _parse_comp_api_relationship_fixes(self, raw: str) -> list[tuple[str, str, str]]:
        """
        Parse lines of the form:

            LABEL_1, LABEL_2: PARENT_LABEL

        Returns a list of (label1, label2, parent_label).
        """
        out: list[tuple[str, str, str]] = []
        text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
        for ln in text.split("\n"):
            s = (ln or "").strip()
            if not s:
                continue
            if s.startswith("#"):
                continue
            # very tolerant split: first comma, then colon
            if "," not in s or ":" not in s:
                continue
            left, parent_part = s.split(":", 1)
            parent_label = parent_part.strip()
            first, second = left.split(",", 1)
            l1 = first.strip()
            l2 = second.strip()
            if not (l1 and l2 and parent_label):
                continue
            if not (self._is_bss_label(l1) and self._is_bss_label(l2) and self._is_bss_label(parent_label)):
                continue
            out.append((l1, l2, parent_label))
        return out

    def _override_dependency_edge(self, bss_schema: dict, parent: str, child: str) -> None:
        """
        Force orientation of the edge between parent and child:

        - parent depends on child
        - child has parent as dependant

        Other dependencies/dependants remain unchanged.
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

