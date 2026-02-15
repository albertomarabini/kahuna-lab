# classes/bss_chat_refinement.py


from classes.base_utils import BaseUtils
from chat_prompts.chat_prompts import ASK_LOG_SYNTH_PROMPT, CALL_SEQUENCE_EXTRACTOR_PROMPT, COMP_OWNERSHIP_PROMPT, ENT_ENT_DEP_EXTRACTOR_PROMPT, PROC_PROC_DEP_EXTRACTOR_PROMPT, UI_UI_DEP_EXTRACTOR_PROMPT


class BssChatSupport(BaseUtils):
    SessionFactory:None
    # !##############################################
    # ! CHAT REFINEMENT SUPPORT
    # !##############################################


    # def _refine_all_second_pass_relationships(
    #     self,
    #     bss_schema: dict,
    #     draft_roots: set[str],
    #     model_name: str | None = None,
    #     project_id: str | None = None,
    #     user_text: str | None = None,
    #     bot_message: str | None = None,
    #     turn_labels: set[str] | None = None,
    # ) -> tuple[dict, list[dict], float]:
    #     """
    #     Orchestrate the full second-pass refinement pipeline for the BSS graph.

    #     Steps (logically parallel, then applied in order):

    #     1) Cross-family INT/PROC/UI/API call edges
    #        - Re-evaluated around INT/PROC/UI/API labels touched in this turn,
    #          using CALL_SEQUENCE_EXTRACTOR_PROMPT.
    #     2) Same-family PROC/PROC, UI/UI, ENT/ENT orientations
    #        - Re-orient ambiguous same-family pairs using their family prompts.
    #     3) COMP ownership normalisation
    #        - For PROC/UI/ENT/API referenced by multiple COMPs, pick a single
    #          owner COMP or mark UNDECIDED.
    #     4) Ask-log synthesis for items touched in this turn

    #     All three analyses are computed from the same read-only snapshot of
    #     the input schema, so they are idempotent and can be parallelised.
    #     The resulting decisions are then applied linearly to the live schema:

    #         a) Clear affected cross-family INT/PROC/UI/API edges in the cluster.
    #         b) Apply all dependency-orientation decisions.
    #         c) Apply COMP ownership normalisation.

    #     Returns:
    #         (updated_schema, updated_relationships_for_touched_labels, total_extra_cost)
    #     """
    #     if not isinstance(bss_schema, dict) or not draft_roots:
    #         return bss_schema, [], 0.0

    #     # Families that still use the "internal reorientation" mini-prompts
    #     configs: list[tuple[tuple[str, str], str]] = [
    #         (("PROC", "PROC"), PROC_PROC_DEP_EXTRACTOR_PROMPT),
    #         (("UI",   "UI"),   UI_UI_DEP_EXTRACTOR_PROMPT),
    #         (("ENT",  "ENT"),  ENT_ENT_DEP_EXTRACTOR_PROMPT),
    #     ]

    #     total_cost = 0.0

    #     # ------------------------------------------------------------------
    #     # 0) Take a read-only snapshot for all analysis steps
    #     # ------------------------------------------------------------------
    #     # All refinement helpers below read from this snapshot so that:
    #     # - They see the same consistent graph.
    #     # - Their work can be parallelised later without ordering concerns.
    #     import copy
    #     schema_snapshot = copy.deepcopy(bss_schema)

    #     # ------------------------------------------------------------------
    #     # 1) Cross-family INT/PROC/UI/API call edges (analysis only)
    #     # ------------------------------------------------------------------
    #     cross_decisions: list[tuple[str, str]]
    #     space_labels: set[str]
    #     modified_core: set[str]

    #     (
    #         cross_decisions,
    #         space_labels,
    #         modified_core,
    #         extra_proc_api,
    #         unknown_callers,
    #     ) = self._refine_int_proc_ui_api_cross_relationships(
    #         schema_snapshot,
    #         draft_roots,
    #         model_name=model_name,
    #     )
    #     total_cost += (extra_proc_api or 0.0)

    #     # ------------------------------------------------------------------
    #     # 2) Same-family PROC/PROC, UI/UI, ENT/ENT orientations (analysis only)
    #     # ------------------------------------------------------------------
    #     same_family_decisions: list[tuple[str, str]] = []
    #     same_family_removals: list[tuple[str, str]] = []

    #     for family, tmpl in configs:
    #         decisions, removals, extra = self._reorient_internal_relationships_for_family_pair(

    #             schema_snapshot,
    #             draft_roots,
    #             family,
    #             tmpl,
    #             model_name=model_name,
    #         )
    #         if decisions:
    #             same_family_decisions.extend(decisions)
    #         if removals:
    #             same_family_removals.extend(removals)
    #         total_cost += (extra or 0.0)

    #     # ------------------------------------------------------------------
    #     # 3) COMP ownership normalisation (analysis only)
    #     # ------------------------------------------------------------------
    #     ownership_rows, ownership_cost = self._resolve_duplicate_comp_ownership(
    #         schema_snapshot,
    #         draft_roots,
    #         model_name=model_name,
    #     )
    #     total_cost += (ownership_cost or 0.0)
    #     # ------------------------------------------------------------------
    #     # 4) Ask-log synthesis (analysis only)
    #     # ------------------------------------------------------------------
    #     labels_for_asklog = turn_labels or draft_roots or set()
    #     ask_log_rows, ask_log_cost = self._summarize_asked_question(
    #         schema_snapshot,
    #         labels_for_asklog,
    #         project_id=project_id,
    #         user_text=user_text,
    #         bot_message=bot_message,
    #         model_name=model_name,
    #     )
    #     total_cost += (ask_log_cost or 0.0)
    #     # ------------------------------------------------------------------
    #     # 5) Progressive application of all decisions to the live schema
    #     # ------------------------------------------------------------------
    #     all_decisions: list[tuple[str, str]] = []
    #     if cross_decisions:
    #         all_decisions.extend(cross_decisions)
    #     if same_family_decisions:
    #         all_decisions.extend(same_family_decisions)

    #     touched: set[str] = set()

    #     # 5.a) Clear cross-family edges in the INT/PROC/UI/API cluster
    #     #      (so cross_decisions can be applied cleanly).
    #     if space_labels and modified_core:
    #         self._clear_modified_core_edges_within_cluster(
    #             bss_schema,
    #             space_labels=space_labels,
    #             modified_core=modified_core,
    #         )
    #         touched.update(space_labels)

    #     # 5.b) Apply same-family removals (no new edges, just clean-up)
    #     if same_family_removals:
    #         for a, b in same_family_removals:
    #             self._remove_dependency_edge(bss_schema, a, b)
    #             touched.add(a)
    #             touched.add(b)

    #     # 5.c) Apply all dependency-orientation decisions
    #     for parent, child in all_decisions:
    #         self._override_dependency_edge(bss_schema, parent, child)
    #         touched.add(parent)
    #         touched.add(child)

    #     # 5.d) Apply COMP ownership normalisation
    #     if ownership_rows:
    #         self._apply_normalized_comp_ownership_rows(bss_schema, ownership_rows)
    #         # Elements whose ownership was decided/flagged should also
    #         # be considered "touched" so the client can re-render them.
    #         for elem_label, _ in ownership_rows:
    #             if self._is_bss_label(elem_label):
    #                 touched.add(elem_label)

    #     # 5.e) Apply ask-log updates (does not affect dependency graph)
    #     if unknown_callers:
    #         self._apply_unknown_open_items(bss_schema, unknown_callers)

    #     if ask_log_rows:
    #         self._apply_ask_log_rows(bss_schema, ask_log_rows)

    #     # If nothing was touched at all, we can early-return.
    #     if not touched:
    #         return bss_schema, [], total_cost

    #     # ------------------------------------------------------------------
    #     # 6) Build the relationships payload for all touched labels
    #     # ------------------------------------------------------------------
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
        history_msgs: list | None = None,
        turn_labels: set[str] | None = None,
    ) -> tuple[dict, list[dict], float]:
        """
        Orchestrate the full second-pass refinement pipeline for the BSS graph.
        Runs the actual implementation in an asyncio loop so that the
        LLM-heavy analysis steps can run in parallel.
        """
        if not isinstance(bss_schema, dict) or not draft_roots:
            return bss_schema, [], 0.0

        import asyncio

        return asyncio.run(
            self._refine_all_second_pass_relationships_async(
                bss_schema=bss_schema,
                draft_roots=draft_roots,
                model_name=model_name,
                project_id=project_id,
                user_text=user_text,
                bot_message=bot_message,
                history_msgs=history_msgs,
                turn_labels=turn_labels,
            )
        )

    async def _refine_all_second_pass_relationships_async(
        self,
        bss_schema: dict,
        draft_roots: set[str],
        model_name: str | None = None,
        project_id: str | None = None,
        user_text: str | None = None,
        bot_message: str | None = None,
        history_msgs: list | None = None,
        turn_labels: set[str] | None = None,
    ) -> tuple[dict, list[dict], float]:
        # Families that still use the "internal reorientation" mini-prompts
        configs: list[tuple[tuple[str, str], str]] = [
            (("PROC", "PROC"), PROC_PROC_DEP_EXTRACTOR_PROMPT),
            (("UI",   "UI"),   UI_UI_DEP_EXTRACTOR_PROMPT),
            (("ENT",  "ENT"),  ENT_ENT_DEP_EXTRACTOR_PROMPT),
        ]

        import copy
        import asyncio

        # Read-only snapshot shared by all analysis steps
        schema_snapshot = copy.deepcopy(bss_schema)

        # ------------------------------------------------------------------
        # Parallel analysis on the snapshot (LLM calls in threads)
        # ------------------------------------------------------------------
        cross_task = asyncio.to_thread(
            self._refine_int_proc_ui_api_cross_relationships,
            schema_snapshot,
            draft_roots,
            model_name,
        )

        same_family_tasks = [
            asyncio.to_thread(
                self._reorient_internal_relationships_for_family_pair,
                schema_snapshot,
                draft_roots,
                family,
                tmpl,
                model_name,
            )
            for family, tmpl in configs
        ]

        ownership_task = asyncio.to_thread(
            self._resolve_duplicate_comp_ownership,
            schema_snapshot,
            draft_roots,
            model_name,
        )

        labels_for_asklog = turn_labels or draft_roots or set()
        ask_log_task = asyncio.to_thread(
            self._summarize_asked_question,
            schema_snapshot,
            labels_for_asklog,
            project_id,
            user_text,
            bot_message,
            history_msgs,
            model_name,
        )

        all_tasks = [cross_task] + same_family_tasks + [ownership_task, ask_log_task]
        results = await asyncio.gather(*all_tasks)

        idx = 0
        (
            cross_decisions,
            space_labels,
            modified_core,
            extra_proc_api,
            unknown_callers,
        ) = results[idx]
        idx += 1

        same_family_decisions: list[tuple[str, str]] = []
        same_family_removals: list[tuple[str, str]] = []
        same_family_cost = 0.0

        for _ in configs:
            decisions, removals, extra = results[idx]
            idx += 1
            if decisions:
                same_family_decisions.extend(decisions)
            if removals:
                same_family_removals.extend(removals)
            same_family_cost += (extra or 0.0)

        ownership_rows, ownership_cost = results[idx]
        idx += 1
        ask_log_rows, ask_log_cost = results[idx]

        total_cost = 0.0
        total_cost += (extra_proc_api or 0.0)
        total_cost += (same_family_cost or 0.0)
        total_cost += (ownership_cost or 0.0)
        total_cost += (ask_log_cost or 0.0)

        # ------------------------------------------------------------------
        # Progressive application of all decisions to the live schema
        # ------------------------------------------------------------------
        all_decisions: list[tuple[str, str]] = []
        if cross_decisions:
            all_decisions.extend(cross_decisions)
        if same_family_decisions:
            all_decisions.extend(same_family_decisions)

        touched: set[str] = set()

        # 5.a) Clear cross-family edges in the INT/PROC/UI/API cluster
        if space_labels and modified_core:
            self._clear_modified_core_edges_within_cluster(
                bss_schema,
                space_labels=space_labels,
                modified_core=modified_core,
            )
            touched.update(space_labels)

        # 5.b) Apply same-family removals
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
            for elem_label, _ in ownership_rows:
                if self._is_bss_label(elem_label):
                    touched.add(elem_label)

        # 5.e) Apply ask-log / UNKNOWN updates
        if unknown_callers:
            self._apply_unknown_open_items(bss_schema, unknown_callers)

        if ask_log_rows:
            self._apply_ask_log_rows(bss_schema, ask_log_rows)

        if not touched:
            return bss_schema, [], total_cost

        # 6) Build the relationships payload for all touched labels
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
                    f"sys: '{lbl}' has no PROC-* attached. If none is known attach a stub. If there is one precise the text in both the API-* and the PROC-* to make the relationship unequivocable"
                )
            else:  # INT
                msg = (
                    f"sys: '{lbl}' (inbound) has no  API-* callee and no PROC-* triggered by that API-*. If there is a callee API-* precise the text in both the API-* and the INT-* to make the relationship unequivocable."
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
          within two hops of this turn's modified INT/PROC/UI/API labels.
        - Lets the LLM propose any INT/PROC/UI/API relationships inside that cluster
          (no explicit candidate-pair list in the prompt).
        - Builds a CALL_SEQUENCE_EXTRACTOR_PROMPT with:
            * Context from A/COMP nodes that reference any label in the cluster.
            * Per-label INT/PROC/UI/API definition segments for those labels.
        - Parses the LLM output as (dependent, dependency) rows and keeps only
          relationships between labels in that cluster.

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

        # 3) "Space" = roots + first layer + second layer within INT/PROC/UI/API
        first_layer: set[str] = set()
        for lbl in roots:
            first_layer.update(neighbours.get(lbl, set()))

        second_layer: set[str] = set()
        for lbl in first_layer:
            second_layer.update(neighbours.get(lbl, set()))

        # All INT/PROC/UI/API nodes within two hops of a modified core label
        data_labels: set[str] = (roots | first_layer | second_layer) & core_labels

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

        def _extract_relevant_snippets(text: str, label_pool: set[str]) -> str:
            if not text:
                return ""
            lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            kept: list[str] = []
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                for tok in label_pool:
                    # cheap but effective: only keep lines that mention a cluster label
                    if tok in stripped:
                        kept.append(line)
                        break
            return "\n".join(kept).strip()

        info_lines: list[str] = []
        for lbl in sorted(info_a_labels, key=self._bss_label_sort_key):
            defn = (flat[lbl].get("definition") or "").strip()
            open_items = (flat[lbl].get("open_items") or "").strip()
            snippet_def = _extract_relevant_snippets(defn, data_label_set)
            snippet_open = _extract_relevant_snippets(open_items, data_label_set)
            combined = "\n".join(x for x in (snippet_def, snippet_open) if x).strip()
            if not combined:
                continue
            info_lines.append(f"A-node {lbl}:")
            info_lines.append(combined)
            info_lines.append("")

        for lbl in sorted(info_comp_labels, key=self._bss_label_sort_key):
            defn = (flat[lbl].get("definition") or "").strip()
            open_items = (flat[lbl].get("open_items") or "").strip()
            snippet_def = _extract_relevant_snippets(defn, data_label_set)
            snippet_open = _extract_relevant_snippets(open_items, data_label_set)
            combined = "\n".join(x for x in (snippet_def, snippet_open) if x).strip()
            if not combined:
                continue
            info_lines.append(f"COMP {lbl}:")
            info_lines.append(combined)
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

        print("_refine_int_proc_ui_api_cross_relationships:" + raw_text)

        pairs, unknown_callers = self._parse_dependency_rows(raw_text)

        # Second layer is considered "stable": drop UNKNOWN rows for
        # labels that live only in the second layer (not root, not first).
        stable_second_layer = (second_layer - roots - first_layer)
        stable_second_layer_upper = {lbl.upper() for lbl in stable_second_layer}

        filtered_unknown_callers: list[str] = []
        for lbl in unknown_callers:
            key = (lbl or "").strip().upper()
            # if the caller is a stable second-layer node, ignore this UNKNOWN
            if key in stable_second_layer_upper:
                continue
            filtered_unknown_callers.append(lbl)
        unknown_callers = filtered_unknown_callers

        if not pairs and not unknown_callers:
            return [], data_labels, modified_core, extra_cost, []

        # Accept any INT/PROC/UI/API pair fully inside the 2-hop cluster
        decisions_by_pair: dict[frozenset[str], tuple[str, str]] = {}

        for dependent, dependency in pairs:
            dep_canon = canonical_by_upper.get((dependent or "").strip().upper())
            depd_canon = canonical_by_upper.get((dependency or "").strip().upper())
            if not dep_canon or not depd_canon:
                continue

            # still restrict to core INT/PROC/UI/API labels
            if dep_canon not in core_labels or depd_canon not in core_labels:
                continue
            # and to the local 2-hop cluster
            if dep_canon not in data_labels or depd_canon not in data_labels:
                continue

            key = frozenset({dep_canon.upper(), depd_canon.upper()})
            # dependent = parent, dependency = child
            decisions_by_pair[key] = (dep_canon, depd_canon)

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

        print("_reorient_internal_relationships_for_family_pair:" + raw_text)

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

        print("_resolve_duplicate_comp_ownership:" + raw_text)

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
                f"sys: the ownership of '{elem}' cannot be safely computed between {comps_txt}. Ask the user immediately"
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
        history_msgs: list | None,
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
            or history_msgs is None
        ):
            return [], 0.0

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
