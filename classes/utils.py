import json
import logging
import re
import traceback
import commentjson
import yaml

from classes.entities import Project
from classes.google_helpers import PROJECT_ID, REGION
from classes.llm_client import ChatLlmClient, LlmClient
from classes.schema_manager import SchemaManager

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

    def _safe_error_response(self, current_schema, error: Exception):
        logger.info(f"[handle_chat] Error: {error}")
        traceback.print_exc()

        return {
            "bot_message": (
                "I ran into an internal error while processing your request. "
                "Nothing was changed in the requirements. Please retry your last message."
            ),
            "updated_schema": current_schema,
            "discrepancies": [],
            "schema_change_description": "",
            "updated_project_description": ""
        }

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
    # Direct Schema Edit
    # -----------------------


    def handle_direct_schema_command(self, project_id: str, payload):
        current_schema = self.load_project(project_id)
        current_schema_json = json.dumps(current_schema)

        schema_manager = SchemaManager(self.default_llm)

        updated_schema_str, discrepancies, _ = schema_manager.apply_commands_to_schema(
            current_schema_json,
            json.dumps(payload),
            self.requirements_schema
        )
        updated_schema = json.loads(updated_schema_str)
        if not discrepancies:
            self.save_project(project_id, updated_schema)

        return {
            "updated_schema": updated_schema,
            "discrepancies": discrepancies,
        }

    def handle_delete_node(self, project_id: str, payload):
        path = (payload or {}).get("path", "") or ""
        if not path:
            current_schema = self.load_project(project_id)
            return self._safe_error_response(current_schema, ValueError("Missing 'path' in delete_node payload"))

        if path.startswith("$."):
            normalized_path = path
        elif path.startswith("$"):
            normalized_path = "$." + path[1:]
        else:
            normalized_path = "$." + path

        current_schema = self.load_project(project_id)
        current_schema_json = json.dumps(current_schema)

        delete_command = json.dumps({
            "delete": [
                {"path": normalized_path}
            ]
        })

        schema_manager = SchemaManager(self.default_llm)

        try:
            updated_schema_str, discrepancies, _ = schema_manager.apply_commands_to_schema(
                current_schema_json,
                delete_command,
                self.requirements_schema
            )
            updated_schema = json.loads(updated_schema_str)
            if not discrepancies:
                self.save_project(project_id, updated_schema)

            return {
                "bot_message": f"Node at path '{normalized_path}' has been deleted." if not discrepancies
                               else f"Could not safely delete node at '{normalized_path}'.",
                "updated_schema": updated_schema,
                "discrepancies": discrepancies,
            }
        except Exception as e:
            return self._safe_error_response(current_schema, e)



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
        malformed_json_found = False
        PARSE_ERROR_SUFFIX = " <== This JSON format cannot be parsed. Please correct it as part of your next task"

        updates: dict[str, object] = {}
        text = (raw or "").strip()

        # --- Extract NEXT_QUESTION (take the last occurrence) ---
        nq_idx = text.rfind("NEXT_QUESTION:")
        if nq_idx < 0:
            raise ValueError("Missing NEXT_QUESTION in orchestrator output")

        nq_line = text[nq_idx:].splitlines()[0]
        next_question = nq_line.split(":", 1)[1].strip() if ":" in nq_line else ""
        if len(next_question) >= 2 and next_question[0] == next_question[-1] and next_question[0] in ("'", '"'):
            next_question = next_question[1:-1].strip()

        items_text = text[:nq_idx].rstrip()
        if not items_text:
            return updates, next_question, False

        # --- Find label headers at start-of-line, and require '"status":' right after ':' ---
        label_header_re = re.compile(
            r'(?m)^\s*('
            r'(?:A\d+_[A-Z0-9_]+)'
            r'|'
            r'(?:(?:UC|PROC|COMP|ROLE|UI|ENT|INT|API|NFR)-?\d+_[A-Z0-9_]+)'
            r')\s*:(?=\s*"status"\s*:)',
            flags=re.IGNORECASE,
        )

        matches = list(label_header_re.finditer(items_text))
        if not matches:
            return updates, next_question, False

        for i, m in enumerate(matches):
            label = (m.group(1) or "").strip()
            start = m.start()
            body_start = m.end()
            body_end = matches[i + 1].start() if i + 1 < len(matches) else len(items_text)

            body = items_text[body_start:body_end].strip()
            if not self._is_bss_label(label):
                continue

            # Accept either already-braced object or the new "fields only" object.
            candidate = body
            if candidate.startswith("{") and candidate.endswith("}"):
                obj_str = candidate
            else:
                # tolerate trailing commas
                candidate = candidate.rstrip().rstrip(",")
                obj_str = "{" + candidate + "}"

            try:
                obj = self.load_fault_tolerant_json(obj_str, llm=llm)
                if isinstance(obj, dict):
                    if "value" in obj and "definition" not in obj:
                        obj["definition"] = obj.pop("value")
                    if "Definition" in obj and "definition" not in obj:
                        obj["definition"] = obj.pop("Definition")
                    updates[label] = obj
                else:
                    updates[label] = obj_str + PARSE_ERROR_SUFFIX
                    malformed_json_found = True
            except Exception:
                updates[label] = obj_str + PARSE_ERROR_SUFFIX
                malformed_json_found = True

        return updates, next_question, malformed_json_found




    def _bss_current_document_for_prompt(self, bss_schema: dict) -> str:
        flat: dict[str, dict] = {}
        for label, item in self._iter_bss_items(bss_schema):
            flat[label] = self._normalize_bss_item(item)

        ordered_labels = sorted(flat.keys(), key=self._bss_label_sort_key)

        lines: list[str] = []
        for label in ordered_labels:
            obj = flat[label]
            s = json.dumps(obj, ensure_ascii=False)
            if s.startswith("{") and s.endswith("}"):
                s = s[1:-1]
            lines.append(f"{label}:{s}")

        return "\n".join(lines)



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

    def _extract_reference_labels_from_definition(self, definition: str) -> list[str]:
        if not isinstance(definition, str) or not definition.strip():
            return []

        m = re.search(r"(^|\|)\s*References\s*:\s*", definition, flags=re.IGNORECASE)
        if not m:
            return []

        tail = definition[m.end():]
        bar_i = tail.find("|")
        refs_segment = tail[:bar_i] if bar_i >= 0 else tail

        out: list[str] = []
        for name, inner in re.findall(r"([A-Za-z]+)\s*=\s*\[([^\]]*)\]", refs_segment):
            parts = [p.strip() for p in inner.split(",")]
            for p in parts:
                if not p:
                    continue
                if len(p) >= 2 and p[0] == p[-1] and p[0] in ("'", '"'):
                    p = p[1:-1].strip()
                if self._is_bss_label(p):
                    out.append(p)

        # de-dupe, preserve order
        seen = set()
        deduped = []
        for x in out:
            if x not in seen:
                seen.add(x)
                deduped.append(x)
        return deduped


    def _recompute_bss_dependency_fields(self, bss_schema: dict) -> dict:
        allowed_deps = {
            "A":   {"UC", "ROLE", "NFR", "PROC", "ENT", "INT", "API", "UI", "COMP"},
            "UC":  {"ROLE", "UI", "PROC", "API", "ENT", "INT", "NFR", "COMP"},
            "PROC":{"COMP", "ENT", "INT", "API", "NFR", "ROLE", "PROC"},
            "COMP":{"COMP", "INT", "NFR", "API", "ENT", "UI"},
            "ROLE":{"UI"},
            "UI":  {"API", "NFR"},
            "ENT": {"ENT"},
            "INT": set(),
            "API": {"ENT", "NFR"},
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

        def can_have_child(parent: str, child: str) -> bool:
            pt = label_types.get(parent)
            ct = label_types.get(child)
            return bool(pt and ct and ct in allowed_deps.get(pt, set()))

        # 2) collect refs per label + track missing refs
        refs_by_label: dict[str, list[str]] = {}
        missing_deps: dict[str, set[str]] = {lbl: set() for lbl in flat.keys()}

        for label, item in flat.items():
            refs = self._extract_reference_labels_from_definition(
                (item.get("definition") or "")
            )
            resolved: list[str] = []
            for r in refs:
                r = (r or "").strip()
                if not r or r == label:
                    continue
                if r in all_labels:
                    resolved.append(r)
                else:
                    # referenced label not in schema -> keep as a dependency of label
                    missing_deps[label].add(r)
            refs_by_label[label] = resolved

        # 3) build set of unordered pairs {A,B} for existing labels
        pair_set: set[frozenset[str]] = set()
        for src, refs in refs_by_label.items():
            for dst in refs:
                if dst in all_labels:
                    pair_set.add(frozenset((src, dst)))

        # 4) orient each pair ONCE into children edges
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

        # 5) add missing referenced labels so refs never disappear
        for label, miss in missing_deps.items():
            children[label].update(miss)

        # 6) compute parents (dependants) as reverse of children
        parents: dict[str, set[str]] = {lbl: set() for lbl in flat.keys()}
        for p, cs in children.items():
            for c in cs:
                if c in parents:
                    parents[c].add(p)

        # 7) write CSV fields
        for label in flat.keys():
            deps_sorted = sorted(children[label], key=self._bss_label_sort_key)
            depants_sorted = sorted(parents[label], key=self._bss_label_sort_key)
            flat[label]["dependencies"] = ",".join(deps_sorted)
            flat[label]["dependants"] = ",".join(depants_sorted)

        # 8) re-pack into sections
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
            refs = self._extract_reference_labels_from_definition(norm.get("definition", ""))
            if any(r in labels for r in refs):
                return True

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


