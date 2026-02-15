# classes/bss_ingestion.py

import json
import re
from typing import Any, Callable, Mapping

from classes.backend_utils import Utils
from classes.bss_chat_refinement import BssChatSupport
from chat_prompts.ingestion_prompts import BSS_CANONICALIZER_PROMPT, BSS_UC_EXTRACTOR_PROMPT, UC_COVERAGE_AUDITOR_PROMPT, epistemic_2_rules
from classes.llm_client import LlmClient
from classes.model_props import get_model_max_threshold


class BSSIngestion(Utils, BssChatSupport):
    emit:Callable[[str, dict[str, Any]], None]
    def handle_ingestion(
        self,
        payload: Mapping[str, Any],
        emit: Callable[[str, dict[str, Any]], None],
    ) -> tuple[dict, float]:
        self.emit = emit
        payload = payload or {}
        prd = (payload.get("message") or "").strip()
        if not prd:
            raise ValueError("ingestion payload.prd is required (non-empty string)")

        llm, _ = self._build_llms_for_payload(payload)  # direct (non-chat) calls only
        if not llm:
            raise RuntimeError("No LLM available for ingestion")

        self.emit("ingestion_status", {"note": "Starting ingestion, please wait"})

        max_attempts = 2
        max_items_per_family_call  = get_model_max_threshold(llm.model_name)
        report = "None"

        usecases_by_label = {}
        connected = {}  # label -> set(responsibility_str)
        last_completion = None
        last_missing_block = ""
        attempt = 0
        prev_completion_pct = 0

        while True:
            prompt1 = self.unsafe_string_format(
                BSS_UC_EXTRACTOR_PROMPT,
                prd=prd,
                report=report or "None",
                max_ingested_uc = str(max_items_per_family_call)
            )
            print("===============Prompt\n\n" + prompt1)
            raw = llm.invoke(prompt1)
            # raw = raw if isinstance(raw, str) else getattr(raw, "content", str(raw))
            print("===============Server Response\n\n" + raw)
            parsed_ucs, parsed_connected = self._ingestion_parse_extractor_output(raw)

            # merge usecases
            for uc in parsed_ucs:
                label = uc.get("label")
                if not label:
                    continue
                prev = usecases_by_label.get(label) or {}
                usecases_by_label[label] = self._ingestion_merge_usecase(prev, uc)

            self.emit("ingestion_status", {"note": f"Parsed {len(parsed_ucs)} usecase(s){' more' if len(usecases_by_label.keys()) else ''}"})

            # merge connected responsibilities
            for item in parsed_connected:
                lbl = item.get("label")
                if not lbl:
                    continue
                s = connected.setdefault(lbl, set())
                for r in (item.get("responsibilities") or []):
                    r2 = (r or "").strip()
                    if r2:
                        s.add(r2)

            ucs_raw = self._ingestion_concat_uc_raw(usecases_by_label)
            prompt2 = self.unsafe_string_format(
                UC_COVERAGE_AUDITOR_PROMPT,
                prd=prd,
                ucs=ucs_raw,
                max_ingested_uc = str(max_items_per_family_call)
            )
            raw2 = llm.invoke(prompt2)
            # raw2 = raw2 if isinstance(raw2, str) else getattr(raw2, "content", str(raw2))

            completion_pct, missing_block = self._ingestion_parse_auditor_output(raw2)
            last_completion = completion_pct
            last_missing_block = missing_block or ""

            self.emit("ingestion_status", {"note": f"Total Coverage: {int(completion_pct) if completion_pct is not None else 'unknown'}%"})
            if completion_pct is None:
                completion_pct = prev_completion_pct
            if completion_pct >= 99:
                break
            if completion_pct <= prev_completion_pct:
                attempt+=1
            else:
                attempt=0
            if attempt == max_attempts:
                break
            prev_completion_pct = completion_pct
            report = self._ingestion_build_report(usecases_by_label, connected, last_missing_block)

        # finalize payloads
        usecases = self._ingestion_sorted_usecases(usecases_by_label)
        connected_items = self._ingestion_sorted_connected_items(connected)

        # -------------------------
        # Second pass: canonicalization per family
        # -------------------------
        current_bss = {}  # start from empty BSS graph
        execution_order = ["COMP", "NFR", "ENT", "API", "ROLE", "UI", "INT", "PROC", "UC", "A"]

        # index non-UC connected items once
        connected_index = {
            (item.get("label") or "").strip(): item
            for item in connected_items
            if (item.get("label") or "").strip()
        }

        for n, family in enumerate(execution_order):
            # -------------------------
            # Family-specific selection
            # -------------------------
            if family == "UC":
                family_items = list(usecases)
            elif family == "A":
                family_items = []
            else:
                family_items: list[dict] = []
                for item in connected_items:
                    lbl = (item.get("label") or "").strip()
                    if lbl.startswith(f"{family}-"):
                        family_items.append(item)

            orig_len = len(family_items)

            while True:
                if family != "A" and not family_items:
                    break

                # Bound how many ITEMS_OF_FAMILY we send in this call.
                if family != "A":
                    if (
                        max_items_per_family_call > 0
                        and len(family_items) > max_items_per_family_call
                    ):
                        batch_items = family_items[: max_items_per_family_call]
                    else:
                        batch_items = list(family_items)
                else:
                    batch_items: list[dict] = []

                if family == "UC":
                    # UC family: UCs come from `usecases` (ore),
                    # all non-UC context comes from canonical `current_bss`.
                    if not batch_items:
                        break

                    family_labels: set[str] = {
                        (uc.get("label") or "").strip()
                        for uc in batch_items
                        if (uc.get("label") or "").strip()
                    }


                    # UC canonicalization does NOT use uc_blocks as separate context,
                    # we pass them as ITEMS_OF_FAMILY instead.
                    uc_blocks: list[dict] = []

                    # No ore related_items for UC; all related info is canonical from BSS.
                    related_items: list[dict] = []
                    related_items_str = "None"

                    # Canonical context: entire current_bss.
                    if current_bss:
                        bss_context = self._ingestion_build_uc_bss_context_for_batch(
                            current_bss=current_bss,
                            batch_ucs=batch_items,
                        )
                    else:
                        bss_context = ""

                    uc_blocks_str = "None"
                    family_items_str = self._ingestion_format_uc_items(batch_items)

                elif family == "A":
                    # A-family: no ITEMS_OF_FAMILY list; we synthesize A1..A4
                    # based on UC data + original text.
                    if current_bss:
                        bss_context = self._ingestion_build_A_family_context(current_bss)
                    else:
                        bss_context = ""

                    # A-* should see canonical info only via `bss_context`,
                    # keep RELATED_ITEMS purely for ore (none here).
                    related_items_str = "None"
                    uc_blocks_str = "None"

                    family_items_str = """
    In this run there is no precise ITEMS_OF_FAMILY binding.
    Instead you will have to emit the following items:
    A1_PROJECT_CANVAS
    A2_TECHNOLOGICAL_INTEGRATIONS
    A3_TECHNICAL_CONSTRAINTS
    A4_ACCEPTANCE_CRITERIA

    Based on the attached Epistemic-2 format rules

    """

                else:
                    # Non-UC, non-A families: COMP / INT / API / ENT / ROLE / UI / PROC / NFR
                    if not batch_items:
                        break

                    family_labels: set[str] = {
                        (item.get("label") or "").strip()
                        for item in batch_items
                        if (item.get("label") or "").strip()
                    }

                    # UCs that mention any of these labels
                    uc_blocks: list[dict] = []
                    for uc in usecases:
                        rel = uc.get("related_items") or []
                        if any(lbl in family_labels for lbl in rel):
                            uc_blocks.append(uc)

                    # related_items (bidirectional) from connected_items
                    related_labels = self._ingestion_collect_related_labels_for_family(
                        family_labels=family_labels,
                        connected_items=connected_items,
                    )

                    # Split related labels into:
                    # - canonical: already synthesized in current_bss
                    # - ore: only present in connected_items
                    canonical_labels: set[str] = {
                        lbl
                        for lbl, _ in self._iter_bss_items(current_bss)
                    }

                    canonical_related_labels = {
                        lbl for lbl in related_labels if lbl in canonical_labels
                    }
                    ore_related_labels = {
                        lbl for lbl in related_labels if lbl not in canonical_labels
                    }

                    # Ore-only related_items (raw responsibilities)
                    related_items: list[dict] = [
                        connected_index[lbl]
                        for lbl in sorted(ore_related_labels)
                        if lbl in connected_index
                    ]

                    # Canonical context: subset of current_bss for this family + canonical-related labels
                    bss_context = self._ingestion_build_bss_context_for_family(
                        current_bss=current_bss,
                        family_labels=family_labels,
                        extra_labels=canonical_related_labels,
                        uc_blocks=uc_blocks,
                    )

                    uc_blocks_str = self._ingestion_format_uc_blocks(uc_blocks)
                    family_items_str = self._ingestion_format_items_list(family_items)
                    related_items_str = self._ingestion_format_items_list(related_items) if related_items else "None"

                # -------------------------
                # Common canonicalizer call
                # -------------------------
                epistemic_2 = epistemic_2_rules.get(family) or ""

                prompt3 = self.unsafe_string_format(
                    BSS_CANONICALIZER_PROMPT,
                    prd=prd,
                    uc_blocks=uc_blocks_str,
                    related_items=related_items_str,
                    items_of_family=family_items_str,
                    epistemic_2=epistemic_2,
                    bss_context=bss_context or "None",
                )
                approx_pct = 0
                if orig_len:
                    done = orig_len - len(family_items)
                    frac = done / orig_len
                    approx_pct = int(frac * 10)

                approx_pct += n * 10
                self.emit("ingestion_status", {"note": f"Ingested items {int(approx_pct)}%"})
                print("===============Prompt\n\n" + prompt3)
                raw3 = llm.invoke(prompt3)
                raw3 = raw3 if isinstance(raw3, str) else getattr(raw3, "content", str(raw3))
                print("===============Server Response\n\n" + raw3)

                slot_updates, _, malformed = self._parse_bss_output(raw3, llm=llm)
                # if malformed:
                #     raise ValueError(f"Canonicalizer produced malformed output for family {family}")
                if not slot_updates:
                    break
                current_bss = self._apply_bss_slot_updates(current_bss, slot_updates)
                current_bss = self._recompute_bss_dependency_fields(current_bss)

                # Remove family_items that were just handled in this canonicalizer call

                # Remove family_items that were just handled in this canonicalizer call
                if family != "A":
                    handled_labels = set(slot_updates.keys())
                    if handled_labels:
                        prev_len = len(family_items)
                        family_items = [
                            item
                            for item in family_items
                            if (item.get("label") or "").strip()
                            not in handled_labels
                        ]
                        # If the model did not touch any of the batch items,
                        # avoid looping forever.
                        if len(family_items) == prev_len:
                            break
                    else:
                        break

                if family == "A":
                    # A-family runs only a single pass.
                    break

        # Relationship refinement using the same LLM client.
        # across all relevant items (PROC/API/UI/ENT).
        self.emit("ingestion_status", {"note": f"Refining"})
        all_roots: set[str] = {
            lbl
            for lbl, _ in self._iter_bss_items(current_bss)
            if self._bss_label_type(lbl) in ("PROC", "API", "UI", "ENT")
        }
        refine_extra_cost = 0.0
        if all_roots:
            model_name = self._detect_llm_model_in_payload(payload) or "gemini-2.5-flash-lite"
            current_bss, _, refine_extra_cost = self._refine_all_second_pass_relationships(
                current_bss,
                draft_roots=all_roots,
                model_name=model_name,
            )

        main_cost = llm.get_accrued_cost() if llm else 0
        total_cost = float(main_cost) + float(refine_extra_cost or 0.0)

        return current_bss, total_cost


    # -----------------------
    # Ingestion helpers
    # -----------------------

    def _ingestion_build_uc_bss_context_for_batch(
        self,
        current_bss: dict,
        batch_ucs: list[dict],
    ) -> str:
        """
        Build BSS context for UC canonicalization:
        only canonical items that are connected to the provided UC batch
        via their related_items.
        """
        if not current_bss or not batch_ucs:
            return ""

        related_labels: set[str] = set()
        for uc in batch_ucs:
            for lbl in (uc.get("related_items") or []):
                t = (lbl or "").strip()
                if t:
                    related_labels.add(t)

        if not related_labels:
            return ""

        # Reuse the same minimal-context helper, but we only care about
        # canonical items referenced by these UCs.
        return self._ingestion_build_bss_context_for_family(
            current_bss=current_bss,
            family_labels=set(),
            extra_labels=related_labels,
            uc_blocks=batch_ucs,
        )

    def _ingestion_build_A_family_context(self, current_bss: dict) -> str:
        """
        Build BSS context for A-family canonicalization:
        include only UC items from the canonical BSS graph.
        """
        if not current_bss:
            return ""

        subset: dict[str, dict] = {}
        for label, item in self._iter_bss_items(current_bss):
            if (self._bss_label_type(label) or "").upper() != "UC":
                continue
            section = self._bss_section_for_label(label) or "UC"
            subset.setdefault(section, {})
            subset[section][label] = item

        if not subset:
            return ""

        return self._bss_current_document_for_prompt(subset)


    def _ingestion_format_uc_items(self, ucs: list[dict]) -> str:
        """
        Format UC items for the canonicalizer ITEMS_OF_FAMILY slot.

        Only the following are emitted per UC:
        - label
        - flow
        - notes
        - related_items (CSV)
        """
        lines: list[str] = []

        for uc in ucs:
            label = (uc.get("label") or "").strip()
            if not label:
                continue

            flow = (uc.get("flow") or "").strip()
            notes = (uc.get("notes") or "").strip()
            related_items = [x.strip() for x in (uc.get("related_items") or []) if x and x.strip()]
            related_csv = ", ".join(related_items) if related_items else ""

            lines.append(f"{label}")
            if flow:
                lines.append("Flow:")
                lines.append(flow)
            if notes:
                lines.append("Notes:")
                lines.append(notes)
            if related_csv:
                lines.append(f"Related_items: {related_csv}")

            lines.append("")  # blank line between UCs

        return "\n".join(lines).strip()

    def _ingestion_build_bss_context_for_family(
        self,
        current_bss: dict,
        family_labels: set[str],
        extra_labels: set[str],
        uc_blocks: list[dict],
    ) -> str:
        """
        Build a minimal BSS document (via _bss_current_document_for_prompt)
        containing canonical items related to this family.

        - canonical = already synthesized items in current_bss
        - family_labels = labels of items we are currently canonicalizing
        - extra_labels = other canonical labels related to this family
        - uc_blocks = UC ore; we only use their related_items when those labels
          already exist in current_bss
        """
        if not current_bss:
            return ""

        # Collect candidate labels
        context_labels: set[str] = set()

        # 1) family labels (if already canonical)
        for lbl in family_labels:
            lbl = (lbl or "").strip()
            if lbl:
                context_labels.add(lbl)

        # 2) explicitly provided canonical-related labels
        for lbl in (extra_labels or set()):
            lbl = (lbl or "").strip()
            if lbl:
                context_labels.add(lbl)

        # 3) labels referenced by UC blocks (only if canonical)
        for uc in uc_blocks:
            for lbl in (uc.get("related_items") or []):
                lbl = (lbl or "").strip()
                if lbl:
                    context_labels.add(lbl)

        # Only keep labels that actually exist in current_bss
        existing_labels: set[str] = {lbl for lbl, _ in self._iter_bss_items(current_bss)}
        context_labels &= existing_labels

        if not context_labels:
            return ""

        # Extract just these labels from current_bss, preserving sections
        subset: dict[str, dict] = {}
        for label, item in self._iter_bss_items(current_bss):
            if label not in context_labels:
                continue
            section = self._bss_section_for_label(label) or "A"
            subset.setdefault(section, {})
            subset[section][label] = item

        if not subset:
            return ""

        return self._bss_current_document_for_prompt(subset)


    def _ingestion_collect_related_labels_for_family(
        self,
        family_labels: set[str],
        connected_items: list[dict],
    ) -> set[str]:
        """
        Return labels (any family) that are related to family_labels via
        responsibilities, in *either* direction.
        """
        related: set[str] = set()
        label_index = {
            (item.get("label") or ""): item
            for item in connected_items
            if (item.get("label") or "")
        }

        # Regex for label-like tokens
        label_re = re.compile(
            r"(?:ROLE|PROC|COMP|UI|API|INT|ENT|NFR|UC)-\d+_[A-Za-z0-9_]+"
        )

        # (a) reverse: items whose responsibilities mention any family label
        for item in connected_items:
            lbl = (item.get("label") or "").strip()
            if not lbl or lbl in family_labels:
                continue
            for resp in (item.get("responsibilities") or []):
                txt = resp or ""
                if any(fam_lbl in txt for fam_lbl in family_labels):
                    related.add(lbl)
                    break

        # (b) forward: labels referenced inside family items' responsibilities
        for item in connected_items:
            lbl = (item.get("label") or "").strip()
            if lbl not in family_labels:
                continue
            for resp in (item.get("responsibilities") or []):
                for tok in label_re.findall(resp or ""):
                    tok = tok.strip()
                    if (
                        tok
                        and tok not in family_labels
                        and tok in label_index
                    ):
                        related.add(tok)

        # never treat family labels themselves as "related"
        return {lbl for lbl in related if lbl not in family_labels}



    def _ingestion_format_uc_blocks(self, uc_blocks: list[dict]) -> str:
        parts: list[str] = []
        for uc in uc_blocks:
            raw = (uc.get("raw") or "").strip()
            if raw:
                parts.append(raw)
        return "\n\n".join(parts)

    def _ingestion_format_items_list(self, items: list[dict]) -> str:
        # compact, stable, JSON – not Python repr
        return json.dumps(items, indent=2, ensure_ascii=False)


    def _ingestion_strip_bullet(self, line: str) -> str:
        s = (line or "").lstrip()
        if s.startswith("*") or s.startswith("-"):
            s = s[1:].lstrip()
        return s

    def _ingestion_parse_bracket_csv(self, lines: list[str], start_idx: int) -> tuple[list[str], int]:
        # expects something like: "Primary actors: [A, B]" possibly spanning multiple lines until ']'
        buf = self._ingestion_strip_bullet(lines[start_idx]).strip()
        if "[" not in buf:
            return [], start_idx + 1
        chunk = buf[buf.find("[") + 1 :]
        i = start_idx
        while "]" not in chunk and i + 1 < len(lines):
            i += 1
            chunk += " " + self._ingestion_strip_bullet(lines[i]).strip()
        if "]" in chunk:
            chunk = chunk[: chunk.find("]")]
        parts = []
        for p in chunk.split(","):
            t = (p or "").strip()
            if t:
                parts.append(t)
        return parts, i + 1

    def _ingestion_parse_extractor_output(self, raw: str) -> tuple[list[dict], list[dict]]:
        _UC_LABEL_RE = re.compile(r"UC-\d+_[A-Za-z0-9_]+")
        _CONNECTED_LABEL_RE = re.compile(r"(?:ROLE|PROC|COMP|UI|API|INT|ENT|NFR|UC)-\d+_[A-Za-z0-9_]+")
        # purge empty lines from getgo
        text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
        lines0 = [ln for ln in text.split("\n")]
        lines = [ln for ln in lines0 if (ln or "").strip() != ""]

        # split by UC labels (line-by-line scan)
        blocks = []
        cur = []
        for ln in lines:
            if _UC_LABEL_RE.fullmatch((ln or "").strip()):
                if cur:
                    blocks.append(cur)
                cur = [ln.strip()]
            else:
                if cur:
                    cur.append(ln)
        if cur:
            blocks.append(cur)

        usecases = []
        connected_map = {}  # label -> set(resp)

        for block in blocks:
            label = (block[0] or "").strip()
            if not _UC_LABEL_RE.fullmatch((label or "").strip()):
                continue

            related = set()

            uc = {
                "label": label,
                "completeness": "",
                "primary_actors": [],
                "secondary_actors": [],
                "interaction_points": [],
                "entities": [],
                "nfrs": [],
                "flow": "",
                "notes": "",
                "raw": "\n".join(block),
                "related_items": [],
            }

            section = None  # None | "flow" | "ledger" | "notes"
            i = 1
            ledger_current_label = None
            ledger_buf = []
            ledger_labels = set()

            def flush_ledger():
                nonlocal ledger_current_label, ledger_buf
                if ledger_current_label and ledger_buf:
                    txt = " ".join([x.strip() for x in ledger_buf if x.strip()]).strip()
                    if txt:
                        connected_map.setdefault(ledger_current_label, set()).add(txt)
                ledger_current_label = None
                ledger_buf = []

            while i < len(block):
                ln = block[i]
                s = self._ingestion_strip_bullet(ln).strip()

                # section switches (tolerant spacing)
                low = s.lower()
                if low.startswith("flow:"):
                    flush_ledger()
                    section = "flow"
                    tail = s[5:].strip()
                    if tail:
                        uc["flow"] = (uc["flow"] + ("\n" if uc["flow"] else "") + tail)
                    i += 1
                    continue
                if low.startswith("responsibilities ledger:"):
                    flush_ledger()
                    section = "ledger"
                    i += 1
                    continue
                if low.startswith("notes:"):
                    flush_ledger()
                    section = "notes"
                    tail = s[6:].strip()
                    if tail:
                        uc["notes"] = (uc["notes"] + ("\n" if uc["notes"] else "") + tail)
                    i += 1
                    continue

                # top fields
                if section is None:
                    if low.startswith("completeness:"):
                        uc["completeness"] = s.split(":", 1)[1].strip() if ":" in s else ""
                        i += 1
                        continue
                    if low.startswith("primary actors:"):
                        vals, ni = self._ingestion_parse_bracket_csv(block, i)
                        uc["primary_actors"] = vals
                        i = ni
                        continue
                    if low.startswith("secondary actors:"):
                        vals, ni = self._ingestion_parse_bracket_csv(block, i)
                        uc["secondary_actors"] = vals
                        i = ni
                        continue
                    if low.startswith("interaction points:"):
                        vals, ni = self._ingestion_parse_bracket_csv(block, i)
                        uc["interaction_points"] = vals
                        i = ni
                        continue
                    if low.startswith("entities:"):
                        vals, ni = self._ingestion_parse_bracket_csv(block, i)
                        uc["entities"] = vals
                        i = ni
                        continue
                    if low.startswith("nfrs:"):
                        vals, ni = self._ingestion_parse_bracket_csv(block, i)
                        uc["nfrs"] = vals
                        i = ni
                        continue

                # flow capture
                if section == "flow":
                    uc["flow"] = (uc["flow"] + ("\n" if uc["flow"] else "") + s)
                    i += 1
                    continue

                # ledger capture: "* LABEL: text..." with multiline continuation
                if section == "ledger":
                    # detect "LABEL:" at start of stripped line
                    colon = s.find(":")
                    if colon > 0:
                        maybe_label = s[:colon].strip()
                        rest = s[colon + 1 :].strip()
                        if _CONNECTED_LABEL_RE.fullmatch((maybe_label or "").strip()):
                            flush_ledger()
                            ledger_current_label = maybe_label
                            ledger_labels.add(maybe_label)
                            connected_map.setdefault(maybe_label, set())  # create record even if no text
                            if rest:
                                ledger_buf.append(rest)
                            i += 1
                            continue
                    # continuation
                    if ledger_current_label:
                        ledger_buf.append(s)
                    i += 1
                    continue

                # notes capture
                if section == "notes":
                    uc["notes"] = (uc["notes"] + ("\n" if uc["notes"] else "") + s)
                    i += 1
                    continue

                i += 1

            flush_ledger()

            # single place: build UC↔related_items and ensure corresponding connected_map records exist
            related = set(ledger_labels)
            for arr_name in ("primary_actors", "secondary_actors", "interaction_points", "entities", "nfrs"):
                for tok in (uc.get(arr_name) or []):
                    t = (tok or "").strip()
                    if _CONNECTED_LABEL_RE.fullmatch(t):
                        related.add(t)
                        connected_map.setdefault(t, set())
            uc["related_items"] = sorted(related)

            usecases.append(uc)

        connected_items = []
        for lbl, s in connected_map.items():
            connected_items.append({"label": lbl, "responsibilities": sorted(list(s))})

        return usecases, connected_items

    def _ingestion_parse_auditor_output(self, raw: str) -> tuple[float | None, str]:
        text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        completion = None
        missing_lines = []
        in_missing = False

        for ln in lines:
            low = ln.lower()
            if "completion:" in low and "%" in ln and completion is None:
                # very tolerant parse: take first number before '%'
                before = ln[: ln.find("%")]
                digits = []
                dot_used = False
                for ch in before:
                    if ch.isdigit():
                        digits.append(ch)
                    elif ch == "." and not dot_used:
                        digits.append(ch)
                        dot_used = True
                try:
                    completion = float("".join(digits)) if digits else None
                except Exception:
                    completion = None
            if low.startswith("missing use cases:") or "missing use cases:" in low:
                in_missing = True
                # capture tail after colon if present
                if ":" in ln:
                    tail = ln.split(":", 1)[1].strip()
                    if tail:
                        missing_lines.append(tail)
                continue
            if in_missing:
                missing_lines.append(ln)

        return completion, "\n".join(missing_lines).strip()

    def _ingestion_concat_uc_raw(self, usecases_by_label: dict) -> str:
        labels = sorted(list(usecases_by_label.keys()))
        chunks = []
        for lbl in labels:
            raw = (usecases_by_label.get(lbl) or {}).get("raw") or ""
            raw = raw.strip()
            if raw:
                chunks.append(raw)
        return "\n\n".join(chunks)

    def _ingestion_merge_usecase(self, prev: dict, cur: dict) -> dict:
        out = dict(prev or {})
        for k in ("label", "completeness", "flow", "notes", "raw"):
            v = (cur.get(k) or "").strip() if isinstance(cur.get(k), str) else cur.get(k)
            if v:
                out[k] = v
        for k in ("primary_actors", "secondary_actors", "interaction_points", "entities", "nfrs", "related_items"):
            a = list(out.get(k) or [])
            b = list(cur.get(k) or [])
            seen = set(a)
            for x in b:
                if x not in seen:
                    seen.add(x)
                    a.append(x)
            out[k] = a
        return out

    def _ingestion_build_report(self, usecases_by_label: dict, connected: dict, missing_block: str) -> str:
        # 1) usecases so far: <Label>:<Flow>
        uc_lines = []
        for lbl in sorted(list(usecases_by_label.keys())):
            flow = ((usecases_by_label.get(lbl) or {}).get("flow") or "").strip()
            if flow:
                uc_lines.append(f"{lbl}:{flow}")
            else:
                uc_lines.append(f"{lbl}:")

        # 2) connected items: <Label>:<CSV responsibilities>
        ci_lines = []
        for lbl in sorted(list(connected.keys())):
            resp = sorted(list(connected.get(lbl) or set()))
            ci_lines.append(f"{lbl}:{', '.join(resp)}")

        # 3) missing use cases from auditor
        miss = (missing_block or "").strip()

        return (
            "USECASES_RECORDED_SO_FAR:\n" +
            "\n".join(uc_lines) +
            "\n\nCONNECTED_ITEMS_RESPONSIBILITIES_SO_FAR:\n" +
            "\n".join(ci_lines) +
            "\n\nMISSING_USE_CASES:\n" +
            str(miss if miss else "None")
        )

    def _ingestion_sorted_usecases(self, usecases_by_label: dict) -> list[dict]:
        return [usecases_by_label[lbl] for lbl in sorted(list(usecases_by_label.keys()))]

    def _ingestion_sorted_connected_items(self, connected: dict) -> list[dict]:
        out = []
        for lbl in sorted(list(connected.keys())):
            out.append({"label": lbl, "responsibilities": sorted(list(connected.get(lbl) or set()))})
        return out
