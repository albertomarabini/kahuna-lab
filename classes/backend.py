# classes/backend.py

import os
import json
import re
import traceback
import asyncio
import logging
import contextvars

from sqlalchemy.orm import sessionmaker

from classes.entities import Base, Job, Project
from classes.GCConnection_hlpr import GCConnection
from classes.history_cache import GLOBAL_BSS_HISTORY_CACHE
from classes.idempotency_cache import IDEMPOTENCY_CACHE
from classes.pending_charge_recorder import record_pending_charge


from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from classes.chat_prompts import BSS_PROMPT
from classes.ingestion_prompts import BSS_CANONICALIZER_PROMPT, BSS_UC_EXTRACTOR_PROMPT, UC_COVERAGE_AUDITOR_PROMPT, epistemic_2_rules


from classes.utils import Utils

from dotenv import load_dotenv

load_dotenv()
CURRENCY = os.getenv("CURRENCY")

logger = logging.getLogger("kahuna_backend")

_job_ctx_var = contextvars.ContextVar("job_ctx", default=None)



class Backend(Utils):
    def __init__(self):
        self.SessionFactory = GCConnection().build_db_session_factory()

        # Initialize default LLMs (used as fallback only)
        try:
            default_model = "gemini-2.5-flash-lite"
            self.default_llm, self.default_chat_llm = self._build_llms_for_model(default_model)
        except Exception as e:
            logger.info(f"Warning: Could not initialize VertexAI: {e}. Using mock.")
            self.default_llm = None
            self.default_chat_llm = None


        # BSS Chat
        self._bss_labels = self._bss_section_order()

    # Optional: call this from anywhere inside Backend handlers to stream updates.
    # Example:
    #   self.emit("chat_progress", {"pct": 50, "note": "halfway"})
    def emit(self, msg_type: str, payload: dict) -> None:
        ctx = _job_ctx_var.get()
        if ctx is None:
            raise RuntimeError("Backend.emit() called outside of a queue job context")
        ctx.emit(msg_type, payload)

    def _process_request_data(self, request_data: dict) -> dict:
        """
        Core request handling logic.
        Takes a parsed JSON dict and returns the response_data dict.
        """
        try:
            request_type = request_data.get("type")
            payload = request_data.get("payload")
            project_id = str(request_data.get("sender_id"))

            try:
                preview = json.dumps(request_data, indent=2)
            except Exception:
                preview = str(request_data)

            logger.debug(f"process_request request {preview}")

            response_data = {
                "status": "success",
                "message": "",
                "project_id": project_id,
            }

            if request_type == "load_project":
                response_data["data"] = {}
                response_data["data"]["updated_schema"] = self.load_project(project_id)
                response_data["data"]["bss_schema"] = self._redact_bss_schema_for_ui(
                    self.load_bss_schema(project_id)
                )
                response_data["data"]["bss_labels"] = self._bss_labels

            elif request_type == "save_project":
                self.save_project(project_id, payload)
                response_data["message"] = "Project saved."

            elif request_type == "bss_chat":
                response_data["data"] = self.handle_bss_chat(project_id, payload)

            elif request_type == "edit_bss_document":
                response_data["data"] = self.handle_edit_bss_document(project_id, payload)

            elif request_type == "edit_bss_node":
                response_data["data"] = self.handle_edit_bss_node(project_id, payload)

            elif request_type == "create_bss_relationship":
                response_data["data"] = self.handle_create_relationship(project_id, payload)

            elif request_type == "remove_bss_relationship":
                response_data["data"] = self.handle_remove_bss_relationship(project_id, payload)

            elif request_type == "ingestion":
                response_data["data"] = self.handle_ingestion(project_id, payload)

            else:
                response_data["status"] = "error"
                response_data["message"] = f"Unknown request type: {request_type}"

            try:
                preview = json.dumps(response_data, indent=2)
            except Exception:
                preview = str(response_data)

            logger.debug(f"response {preview}")

            return response_data

        except Exception as e:
            logger.info(f"Error while processing request data: {e}")
            traceback.print_exc()
            raise

    # -----------------------
    # Handlers
    # -----------------------

    def handle_bss_chat(self, project_id: str, payload):
        payload = payload or {}
        user_text = (payload.get("text") or "").strip()

        llm, chat_llm = self._build_llms_for_payload(payload)

        current_bss = self.load_bss_schema(project_id)

        # PRE: build prompt inputs
        current_document = self._bss_current_document_for_prompt(current_bss)
        registry_ledger = self._build_registry_ledger(current_bss)
        registry_ledger_json = json.dumps(registry_ledger, indent=2)

        example_kwargs = self._bss_example_kwargs(current_bss)

        prompt = self.unsafe_string_format(
            BSS_PROMPT,
            USER_QUESTION=user_text,
            CURRENT_DOCUMENT=current_document,
            REGISTRY_LEDGER=registry_ledger_json,
            **example_kwargs,
        )

        print("===============Prompt\n\n" + prompt)

        if chat_llm:
            messages_for_llm = GLOBAL_BSS_HISTORY_CACHE.snapshot(project_id)
            messages_for_llm.append(HumanMessage(content=prompt))
            raw = chat_llm.invoke(messages_for_llm)
            raw = getattr(raw, "content", str(raw))

        print("===============Server Response\n\n" + raw)

        raw_clean = self.clean_triple_backticks(raw).strip()

        slot_updates, next_question, malformed_json_found = self._parse_bss_output(raw_clean, llm=llm)

        # Emergency self-heal: ask the LLM to fix malformed items immediately.
        # This exchange is NOT written to bss_history_cache.
        if malformed_json_found and chat_llm:
            fix_directive = (
                "There are malformed items in the document / your last output. "
                "Please fix them and re-emit a corrected response in the exact required format:\n"
                "- One or more '<LABEL>:\"status\":...,\"definition\":...,...' lines (no outer braces)\n"
                "- Exactly one 'NEXT_QUESTION: ...' line\n"
                "No extra commentary, no code fences."
            )

            messages_fix = GLOBAL_BSS_HISTORY_CACHE.snapshot(project_id)
            messages_fix.append(HumanMessage(content=prompt))
            messages_fix.append(AIMessage(content=raw_clean))
            messages_fix.append(HumanMessage(content=fix_directive))

            raw2 = chat_llm.invoke(messages_fix)
            raw2 = getattr(raw2, "content", str(raw2))
            raw2 = self.clean_triple_backticks(raw2).strip()

            slot_updates, next_question, malformed_json_found = self._parse_bss_output(raw2, llm=llm)

        # If still malformed after retry, do not mutate DB or history; return current state.
        if malformed_json_found:
            return {
                "bot_message": "I produced malformed structured output. Please resend your last message.",
                "bss_schema": self._redact_bss_schema_for_ui(current_bss),
                "bss_labels": self._bss_labels,
            }

        deleted_labels = self._collect_deleted_labels_from_slot_updates(slot_updates)

        updated_bss = self._apply_bss_slot_updates(current_bss, slot_updates)

        # Emergency cleanup: only if something was deleted AND it is still referenced elsewhere
        if deleted_labels and self._bss_any_item_references_labels(updated_bss, deleted_labels):
            updated_bss = self._emergency_purge_deleted_labels_from_references(updated_bss, deleted_labels)

        # Identify labels whose relationships the LLM is allowed to edit:
        # labels that are in slot_updates and are currently in 'draft' status
        # (this includes new items, which _apply_bss_slot_updates creates as draft).
        draft_roots: set[str] = set()
        locked_for_relationships: set[str] = set()

        for label in (slot_updates or {}).keys():
            if not self._is_bss_label(label):
                continue
            section = self._bss_section_for_label(label)
            if not section:
                continue
            item = (updated_bss.get(section) or {}).get(label)
            if not item:
                continue

            norm = self._normalize_bss_item(item)
            status_now = (norm.get("status") or "").strip().lower()

            if status_now == "draft":
                draft_roots.add(label)
            else:
                # LLM emitted this label, but it is not in draft → frozen for relationships
                locked_for_relationships.add(label)

        if locked_for_relationships:
            next_question += f"\n\nMessage from Host: the LLM tried to modify the following items: {', '.join(list(locked_for_relationships))} that have been locked. \nIf you agree with this change please change the permissions and ask the LLM to try again."


        # POST: recompute host-maintained dependency fields.
        #
        # - If we deleted labels, keep the old behaviour: full recompute
        #   so everything drops references to removed nodes.
        # - Otherwise, only recompute relationships in the local region
        #   around labels that were:
        #     - already in draft before this turn, and
        #     - emitted in this turn (draft_roots).
        if deleted_labels:
            updated_bss = self._recompute_bss_dependency_fields(updated_bss)
        elif draft_roots:
            updated_bss = self._recompute_bss_dependency_fields(
                updated_bss,
                root_labels=draft_roots,
            )
        # else: no relationship recompute (non-draft items' relationships stay frozen)

        self.save_bss_schema(project_id, updated_bss)

        # 1) Early callback: send updated doc + bot reply immediately
        #    (intermediate event; final HTTP response will only contain
        #    relationship diffs + heartbeat).
        self.emit(
            "chat_early_callback",
            {
                "bot_message": next_question,
                "bss_schema": self._redact_bss_schema_for_ui(updated_bss),
                "bss_labels": self._bss_labels,
            },
        )

        # 2) Refine COMP <-> API relationship directions using a second LLM call.
        updated_bss, updated_relationships = self._refine_proc_api_relationships(
            updated_bss,
            draft_roots=draft_roots,
            llm_client=chat_llm,
        )

        # Persist any relationship changes derived from the COMP/API tie-break step.
        if updated_relationships:
            self.save_bss_schema(project_id, updated_bss)

        # 3) Compute amount/currency from the LLM client (both calls).

        idempotency_key = record_pending_charge(
            self.SessionFactory,
            project_id=str(project_id),
            amount=chat_llm.get_accrued_cost(),
            currency=CURRENCY
        )

        # Store idempotency_key
        IDEMPOTENCY_CACHE.add(idempotency_key)

        # Store chat history + heartbeat together in the global history cache
        # (see note below on HistoryCache)
        GLOBAL_BSS_HISTORY_CACHE.append_turn(
            project_id,
            user_text,
            next_question,
        )

        return {
            "updated_relationships": updated_relationships,
            "heartbeat": idempotency_key,
        }



    def handle_edit_bss_document(self, project_id: str, payload):
        payload = payload or {}
        label = (payload.get("label") or payload.get("Label") or "").strip()
        content = payload.get("content")

        if not label or not self._is_bss_label(label):
            raise ValueError(f"Unknown or missing BSS label: {label}")

        if not isinstance(content, dict):
            raise ValueError("edit_bss_document payload.content must be an object")

        section = self._bss_section_for_label(label)
        if not section:
            raise ValueError(f"Could not map label to section: {label}")

        new_status = content.get("status")
        new_definition = content.get("definition")
        if new_definition is None:
            new_definition = content.get("value")  # back-compat for UI callers still sending "value"

        cancelled = bool(content.get("cancelled", False))

        if new_status is None and new_definition is None and ("cancelled" not in content):
            raise ValueError("edit_bss_document payload.content must include 'status' and/or 'definition'/'value' and/or 'cancelled'")

        current_bss = self.load_bss_schema(project_id)
        if not isinstance(current_bss, dict):
            current_bss = {}

        current_bss.setdefault(section, {})

        # Start from existing canonical representation
        existing = self._normalize_bss_item(current_bss[section].get(label))

        if cancelled:
            current_bss[section].pop(label, None)
            if not current_bss[section]:
                current_bss.pop(section, None)
        else:
            if new_status is not None:
                existing["status"] = new_status
            if new_definition is not None:
                existing["definition"] = self._coerce_field_to_str(new_definition)

            # Never persist deleted; cancelled is the only supported boolean here
            existing["cancelled"] = False

            current_bss[section][label] = existing

        # Keep host-maintained fields consistent
        current_bss = self._recompute_bss_dependency_fields(current_bss)

        self.save_bss_schema(project_id, current_bss)

        return {
            "bss_schema": self._redact_bss_schema_for_ui(current_bss),
            "bss_labels": self._bss_labels,
        }


    def handle_edit_bss_node(self, project_id: str, payload: dict) -> dict:
        """
        Edit a single BSS node's definition segments and/or status.

        Payload:
          {
            "label": "UC-1_Browse_Catalog",
            "status": "draft",        # optional
            "segments": { ... }       # optional; full definition if present
          }
        """
        payload = payload or {}
        label = (payload.get("label") or "").strip()
        if not label or not self._is_bss_label(label):
            raise ValueError(f"Unknown or missing BSS label: {label}")

        segments = payload.get("segments")
        if segments is not None and not isinstance(segments, dict):
            raise ValueError("edit_bss_node payload.segments must be an object when present")

        new_status = (payload.get("status") or "").strip() or None

        current_bss = self.load_bss_schema(project_id)
        if not isinstance(current_bss, dict):
            current_bss = {}

        section = self._bss_section_for_label(label)
        if not section:
            raise ValueError(f"Could not map label to section: {label}")

        if label not in (current_bss.get(section) or {}):
            raise ValueError(f"Label does not exist in schema: {label}")

        existing = self._normalize_bss_item(current_bss[section].get(label))
        old_status = (existing.get("status") or "").strip().lower()

        # 1) Rebuild definition from provided segments (if any)
        if segments is not None:
            segs_norm: dict[str, str] = {}
            for k, v in segments.items():
                key = (k or "").strip().lower()
                if not key:
                    continue
                val = "" if v is None else str(v)
                segs_norm[key] = val.strip()

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

            parts: list[str] = []
            used: set[str] = set()

            for key in preferred_order:
                val = segs_norm.get(key, "")
                if not val.strip():
                    continue
                used.add(key)
                header = key.capitalize()
                parts.append(f"{header}: {val}")

            for key in sorted(segs_norm.keys()):
                if key in used:
                    continue
                val = segs_norm.get(key, "")
                if not val.strip():
                    continue
                header = key.capitalize()
                parts.append(f"{header}: {val}")

            existing["definition"] = " | ".join(parts) if parts else ""

        # 2) Status logic
        if new_status is not None:
            existing["status"] = new_status
        else:
            # User edited definition but did not explicitly set status:
            # if it was draft, promote to partial.
            if segments is not None and old_status == "draft":
                existing["status"] = "partial"

        existing["cancelled"] = False
        current_bss.setdefault(section, {})
        current_bss[section][label] = existing

        # 3) Recompute graph with existing status semantics
        updated_bss = self._recompute_bss_dependency_fields(current_bss)
        self.save_bss_schema(project_id, updated_bss)

        # UI does not care about the response body
        return {}


    def handle_create_relationship(self, project_id: str, payload: dict) -> dict:
        """
        Create a user-defined relationship between two existing BSS labels.

        Semantics: source_label depends on target_label.

        Accepted payload keys:
        - from_label / from
        - to_label   / to
        """
        payload = payload or {}
        src = (payload.get("from_label") or payload.get("from") or "").strip()
        dst = (payload.get("to_label") or payload.get("to") or "").strip()

        if not src or not dst:
            raise ValueError(
                "create_relationship payload must include 'from_label'/'from' and 'to_label'/'to'"
            )
        if not self._is_bss_label(src):
            raise ValueError(f"Invalid source label: {src}")
        if not self._is_bss_label(dst):
            raise ValueError(f"Invalid target label: {dst}")

        current_bss = self.load_bss_schema(project_id)
        if not isinstance(current_bss, dict):
            current_bss = {}

        # Ensure both nodes exist in the current schema
        existing_labels = {label for label, _ in self._iter_bss_items(current_bss)}
        if src not in existing_labels:
            raise ValueError(f"Source label does not exist in schema: {src}")
        if dst not in existing_labels:
            raise ValueError(f"Target label does not exist in schema: {dst}")

        src_section = self._bss_section_for_label(src)
        if not src_section:
            raise ValueError(f"Could not map source label to section: {src}")

        current_bss.setdefault(src_section, {})
        src_item = self._normalize_bss_item(current_bss[src_section].get(src))

        # Normalize & extend user_defined_relationships
        udr_raw = (src_item.get("user_defined_relationships") or "").strip()
        udr_labels = self._extract_reference_labels_from_definition(udr_raw)

        dst_norm = dst.strip()
        if dst_norm not in udr_labels:
            udr_labels.append(dst_norm)

        udr_labels_sorted = sorted(udr_labels, key=self._bss_label_sort_key)
        src_item["user_defined_relationships"] = ",".join(udr_labels_sorted)

        current_bss[src_section][src] = src_item

        # Recompute dependency/dependant fields to include this relationship
        updated_bss = self._recompute_bss_dependency_fields(current_bss)
        self.save_bss_schema(project_id, updated_bss)

        # Return only the two affected nodes with their edge fields,
        # to avoid sending the whole document back.
        def _extract_edges(label: str) -> dict:
            section = self._bss_section_for_label(label)
            if not section:
                raise ValueError(f"Could not map label to section after update: {label}")
            item = self._normalize_bss_item(
                (updated_bss.get(section) or {}).get(label) or {}
            )
            return {
                "label": label,
                "dependencies": item.get("dependencies", ""),
                "dependants": item.get("dependants", ""),
            }

        return {
            "from": _extract_edges(src),
            "to": _extract_edges(dst),
        }


    def handle_remove_bss_relationship(self, project_id: str, payload: dict) -> dict:
        """
        Remove a user-defined relationship between two existing BSS labels.

        Semantics: remove the edge 'source_label depends on target_label'.
        """
        payload = payload or {}
        src = (payload.get("from_label") or payload.get("from") or "").strip()
        dst = (payload.get("to_label") or payload.get("to") or "").strip()

        if not src or not dst:
            raise ValueError(
                "remove_bss_relationship payload must include 'from_label'/'from' and 'to_label'/'to'"
            )
        if not self._is_bss_label(src):
            raise ValueError(f"Invalid source label: {src}")
        if not self._is_bss_label(dst):
            raise ValueError(f"Invalid target label: {dst}")

        current_bss = self.load_bss_schema(project_id)
        if not isinstance(current_bss, dict):
            current_bss = {}

        existing_labels = {label for label, _ in self._iter_bss_items(current_bss)}
        if src not in existing_labels:
            raise ValueError(f"Source label does not exist in schema: {src}")
        if dst not in existing_labels:
            raise ValueError(f"Target label does not exist in schema: {dst}")

        src_section = self._bss_section_for_label(src)
        dst_section = self._bss_section_for_label(dst)
        if not src_section or not dst_section:
            raise ValueError(f"Could not map labels to sections: {src}, {dst}")

        current_bss.setdefault(src_section, {})
        current_bss.setdefault(dst_section, {})

        src_item = self._normalize_bss_item(current_bss[src_section].get(src))
        dst_item = self._normalize_bss_item(current_bss[dst_section].get(dst))

        # Remove dst from src.dependencies
        deps_raw = (src_item.get("dependencies") or "").strip()
        deps_labels = self._extract_reference_labels_from_definition(deps_raw)

        deps_filtered = [d for d in deps_labels if d.upper() != dst.upper()]
        src_item["dependencies"] = ",".join(deps_filtered)

        # Flip draft -> partial for both nodes if touched by user
        if (src_item.get("status") or "").strip().lower() == "draft":
            src_item["status"] = "partial"
        if (dst_item.get("status") or "").strip().lower() == "draft":
            dst_item["status"] = "partial"

        current_bss[src_section][src] = src_item
        current_bss[dst_section][dst] = dst_item

        updated_bss = self._recompute_bss_dependency_fields(current_bss)
        self.save_bss_schema(project_id, updated_bss)

        def _extract_node(label: str) -> dict:
            section = self._bss_section_for_label(label)
            item = self._normalize_bss_item(
                (updated_bss.get(section) or {}).get(label) or {}
            )
            return {
                "label": label,
                "status": item.get("status", ""),
                "dependencies": item.get("dependencies", ""),
                "dependants": item.get("dependants", ""),
            }

        return {
            "from": _extract_node(src),
            "to": _extract_node(dst),
        }



    def _apply_bss_slot_updates(self, current_bss: dict, slot_updates: dict) -> dict:
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
            # - brand new items are always created as 'draft'
            # - for existing items we honor an explicit status from the patch
            status = patch.get("status")
            if is_new:
                existing["status"] = "draft"
            elif isinstance(status, str) and status.strip():
                existing["status"] = status.strip()

            # Split existing definition into segments
            existing_def = existing.get("definition") or ""
            segments_current = self._split_definition_segments(existing_def)

            # Overlay new definition-related segments (definition, flow, notes, kind, etc.)
            new_segments = patch.get("segments") or {}
            if isinstance(new_segments, dict):
                for seg_name, seg_body in new_segments.items():
                    key = seg_name.lower()
                    if not isinstance(seg_body, str):
                        continue
                    segments_current[key] = seg_body

            # Log missing required segments for this label type
            self._log_missing_required_segments(label, segments_current)

            # Rebuild definition from segments_current
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

            # Any extra segment names not in preferred_order
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
                oi = patch.get("open_items") or ""
                existing["open_items"] = oi

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



    # !##############################################
    # ! Handle Jobs
    # !##############################################

    def handle_submit_job(self, project_id: str, payload: dict) -> dict:
        """
        Enqueue a long-running pipeline job.
        Guards so that a user / project cannot have more than one active job
        (QUEUED or RUNNING) at a time.
        """

        payload = payload or {}

        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("submit_job payload.prompt is required (non-empty string)")

        session = self.SessionFactory()
        try:
            # 1) Resolve project + owning user
            project = (
                session.query(Project)
                .filter(Project.project_id == str(project_id))
                .one_or_none()
            )
            if project is None:
                raise ValueError(f"Project not found: {project_id}")

            user_id = project.user_id
            active_states = ("QUEUED", "RUNNING")

            # 2) Any active job for this project?
            existing_for_project = (
                session.query(Job)
                .filter(
                    Job.project_id == str(project_id),
                    Job.state.in_(active_states),
                )
                .first()
            )

            # 3) Any active job for this user (across any project)?
            existing_for_user = (
                session.query(Job)
                .join(Project, Project.project_id == Job.project_id)
                .filter(
                    Project.user_id == user_id,
                    Job.state.in_(active_states),
                )
                .first()
            )

            if existing_for_project or existing_for_user:
                return {
                    "job_id": None,
                    "state": "rejected",
                    "status": "Another job is already queued or running for this user/project.",
                }

            # 4) No active jobs → enqueue new one
            model_config = self._detect_llm_model_in_payload(payload) or "gemini-2.5-flash-lite"

            job = Job(
                project_id=str(project_id),
                prompt=prompt,
                payload=payload,
                state="QUEUED",
                status="Queued",
                model_config=model_config,
            )
            session.add(job)
            session.commit()

            return {
                "job_id": str(job.job_id),
                "state": job.state,
                "status": job.status,
            }
        finally:
            session.close()


    # !##############################################
    # ! INGESTION
    # !##############################################

    def handle_ingestion(self, project_id: str, payload):
        payload = payload or {}
        prd = (payload.get("message") or "").strip()
        if not prd:
            raise ValueError("ingestion payload.prd is required (non-empty string)")

        llm, _ = self._build_llms_for_payload(payload)  # direct (non-chat) calls only
        if not llm:
            raise RuntimeError("No LLM available for ingestion")

        self.emit("ingestion_status", {"note": "Starting ingestion, please wait"})
        _ = self.load_project(project_id)  # currently unused, but keeps symmetry with other flows

        max_attempts = 2
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

        # canonicalization passes per family
        current_bss = {}  # start from empty BSS graph
        execution_order = ["COMP", "PROC", "UI", "INT", "API", "ENT", "ROLE", "NFR", "UC", "A"]

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
                if family == "UC":
                    # UC family: UCs come from `usecases` (ore),
                    # all non-UC context comes from canonical `current_bss`.
                    if not family_items:
                        break

                    family_labels: set[str] = {
                        (uc.get("label") or "").strip()
                        for uc in family_items
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
                        bss_context = self._bss_current_document_for_prompt(current_bss)
                    else:
                        bss_context = ""

                    uc_blocks_str = "None"
                    family_items_str = self._ingestion_format_uc_items(family_items)

                elif family == "A":
                    ...
                    if current_bss:
                        full_doc = self._bss_current_document_for_prompt(current_bss)
                        bss_context = full_doc if full_doc.strip() else ""
                    else:
                        bss_context = ""

                    # A-* should see canonical info only via `bss_context`,
                    # keep RELATED_ITEMS purely for ore (none here).
                    related_items_str = "None"

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
                    if not family_items:
                        break

                    family_labels: set[str] = {
                        (item.get("label") or "").strip()
                        for item in family_items
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
                if family != "A":
                    handled_labels = set(slot_updates.keys())
                    prev_len = len(family_items)
                    family_items = [
                        item
                        for item in family_items
                        if (item.get("label") or "").strip() not in handled_labels
                    ]
                    if len(family_items) == prev_len:
                        break
                if not family_items:
                    break

        # Optional PROC<->API relationship refinement using the same LLM client.
        # In ingestion everything is effectively "new", so we allow refinement
        # across all PROC/API items (subject to the internal guards).
        self.emit("ingestion_status", {"note": f"Refining"})
        proc_api_roots: set[str] = {
            lbl
            for lbl, _ in self._iter_bss_items(current_bss)
            if self._bss_label_type(lbl) in ("PROC", "API")
        }
        if proc_api_roots:
            current_bss, _ = self._refine_proc_api_relationships(
                current_bss,
                draft_roots=proc_api_roots,
                llm_client=llm,
            )

        # finally persist
        self.save_bss_schema(project_id, current_bss)

        idempotency_key = record_pending_charge(
            self.SessionFactory,
            project_id=str(project_id),
            amount=llm.get_accrued_cost(),
            currency=CURRENCY,
        )

        IDEMPOTENCY_CACHE.add(idempotency_key)

        return {
            "bss_schema": self._redact_bss_schema_for_ui(current_bss),
            "bss_labels": self._bss_labels,
            "heartbeat": idempotency_key,
        }




    # -----------------------
    # Ingestion helpers
    # -----------------------

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
