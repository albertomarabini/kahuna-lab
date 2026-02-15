# classes/base_utils.py


import json
import logging
import re

from classes.google_helpers import PROJECT_ID, REGION
from classes.llm_client import ChatLlmClient, LlmClient


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(name)s\n%(message)s\n"
)

logger = logging.getLogger("kahuna_backend")


class BaseUtils():
    llm_timeout:300

    # -----------------------
    # General Utils
    # -----------------------

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
    # General BSS Utils
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

    # -----------------------
    # LLM base plumbing
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
