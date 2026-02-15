# classes/backend.py

import os
import json
import re
import traceback
import asyncio
import logging
import contextvars

from sqlalchemy.orm import sessionmaker

from classes.bss_chat_refinement import BssChatSupport
from classes.bss_ingestion import BSSIngestion
from classes.entities import Base, Job, Project
from classes.GCConnection_hlpr import get_session_factory
from classes.history_cache import GLOBAL_BSS_HISTORY_CACHE
from classes.idempotency_cache import IDEMPOTENCY_CACHE
from classes.pending_charge_recorder import record_pending_charge


from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from chat_prompts.chat_prompts import BSS_PROMPT

from classes.backend_utils import Utils

from dotenv import load_dotenv

load_dotenv()
CURRENCY = os.getenv("CURRENCY")

logger = logging.getLogger("kahuna_backend")

_job_ctx_var = contextvars.ContextVar("job_ctx", default=None)


class Backend(Utils, BssChatSupport):
    def __init__(self):
        self.SessionFactory = get_session_factory()
        self.llm_timeout=300
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

            logger.debug(f"process_request  {preview}")

            response_data = {
                "status": "success",
                "message": "",
                "project_id": project_id,
            }

            if request_type == "load_project":
                response_data["data"] = {}

                # preliminary schema
                response_data["data"]["updated_schema"] = self.load_project(project_id)
                # full BSS with metadata
                raw_bss = self.load_bss_schema(project_id)
                # send metadata only on load_project
                response_data["data"]["metadata"] = raw_bss.get("metadata", None)
                # document for the editor: redacted, no metadata
                response_data["data"]["bss_schema"] = self._redact_bss_schema_for_ui(raw_bss)
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
            elif request_type == "save_state":
                response_data["data"] = self.handle_save_state(project_id, payload)
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
        chat_queue = None

        llm, chat_llm = self._build_llms_for_payload(payload)
        current_bss = self.load_bss_schema(project_id)

        # PRE: build prompt inputs
        current_document = self._bss_current_document_for_prompt(current_bss)
        registry_ledger = self._build_registry_ledger(current_bss)
        registry_ledger_json = json.dumps(registry_ledger, indent=2)
        open_items = self._collect_open_items_by_gravity(current_bss)
        open_items_block = "\n".join(f"{a}: {b}" for (a, b) in open_items)
        next_indices = self._bss_next_indices(current_bss)
        next_indices_str = ", ".join(f"{fam}:{idx}" for fam, idx in sorted(next_indices.items()))

        example_kwargs = self._bss_example_kwargs(current_bss)

        prompt = self.unsafe_string_format(
            BSS_PROMPT,
            USER_QUESTION=user_text,
            CURRENT_DOCUMENT=current_document,
            REGISTRY_LEDGER=registry_ledger_json,
            OPEN_ITEMS_BY_GRAVITY=open_items_block,
            NEXT_LABEL_INDICES=next_indices_str,
            **example_kwargs,
        )

        if chat_llm:
            messages_for_llm = GLOBAL_BSS_HISTORY_CACHE.snapshot(project_id)
            if not messages_for_llm and isinstance(current_bss, dict):
                metadata = current_bss.get("metadata")
                if isinstance(metadata, dict):
                    stored_queue = metadata.get("chat_queue")
                    if isinstance(stored_queue, list):
                        messages_for_llm = self._chat_queue_to_messages(stored_queue)
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

        # Store chat history + heartbeat together in the global history cache
        # (see note below on HistoryCache)
        GLOBAL_BSS_HISTORY_CACHE.append_turn(
            project_id,
            user_text,
            next_question,
        )

        # Persist current chat queue into metadata.chat_queue
        # build chat_queue from history only (no prompt)
        hist_msgs = GLOBAL_BSS_HISTORY_CACHE.snapshot(project_id)
        chat_queue = self._messages_to_chat_queue(hist_msgs, limit=10)
        metadata = updated_bss.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["chat_queue"] = chat_queue
        updated_bss["metadata"] = metadata

        self.save_bss_schema(project_id, updated_bss)

        # ###############################################################
        # 1) Early callback: send updated doc + bot reply immediately
        #    (intermediate event; final HTTP response will only contain
        #    relationship diffs + heartbeat).
        # ###############################################################
        self.emit(
            "chat_early_callback",
            {
                "bot_message": next_question,
                "bss_schema": self._redact_bss_schema_for_ui(updated_bss),
                "bss_labels": self._bss_labels,
            },
        )

        # 2) Second-pass relationship refinement (PROC↔API, PROC↔PROC, UI↔UI, ENT↔ENT).
        updated_bss, updated_relationships, refine_extra_cost = self._refine_all_second_pass_relationships(
            updated_bss,
            draft_roots=draft_roots,
            model_name=self._detect_llm_model_in_payload(payload) or "gemini-2.5-flash-lite",
            project_id=str(project_id),
            user_text=user_text,
            bot_message=next_question,
            history_msgs=hist_msgs,
            turn_labels=draft_roots,
        )

        # Persist any relationship changes derived from the tie-break step.
        if updated_relationships:
            self.save_bss_schema(project_id, updated_bss)

        # 3) Compute amount/currency from the LLM client (both calls).
        main_cost = chat_llm.get_accrued_cost() if chat_llm else 0
        total_cost = float(main_cost) + float(refine_extra_cost or 0.0)
        idempotency_key = record_pending_charge(
            self.SessionFactory,
            project_id=str(project_id),
            amount=total_cost,
            currency=CURRENCY
        )

        # Store idempotency_key
        IDEMPOTENCY_CACHE.add(idempotency_key)

        return {
            "updated_relationships": updated_relationships,
            "heartbeat": idempotency_key,
        }

    def handle_save_state(self, project_id: str, payload: dict) -> dict:
        """
        Save arbitrary UI state into bss_schema.metadata.ui_state.

        Payload:
          {
            "state": <any JSON-serializable blob>
          }
        """
        payload = payload or {}
        if "state" not in payload:
            raise ValueError("save_state payload.state is required")

        state_blob = payload["state"]

        current_bss = self.load_bss_schema(project_id)
        if not isinstance(current_bss, dict):
            current_bss = {}

        metadata = current_bss.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        metadata["ui_state"] = state_blob
        current_bss["metadata"] = metadata

        self.save_bss_schema(project_id, current_bss)

        return {"ok": True}


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


    # !##############################################
    # ! Handle Jobs
    # !##############################################

    # def submit_job(self, payload):
    #     if IS_LOCAL_DB:
    #         return {
    #             "job_id": None,
    #             "status": "disabled_in_local_mode",
    #             "message": "Remote worker queue is disabled when using local DB.",
    #         }

    #     client_payload = payload or {}
    #     if not isinstance(client_payload, dict):
    #         return {
    #             "job_id": None,
    #             "status": "error",
    #             "message": "submit_job payload must be a JSON object",
    #         }

    #     existing_model = self._detect_llm_model_in_payload(client_payload)
    #     if not existing_model:
    #         client_payload["model"] = "gemini-2.5-flash-lite"

    #     job_id = f"job_{os.urandom(8).hex()}"

    #     conn = None
    #     try:
    #         conn = self.engine.raw_connection()
    #         cursor = conn.cursor()
    #         cursor.execute(
    #             "INSERT INTO jobs (job_id, status, client_request_data) VALUES (%s, %s, %s)",
    #             (job_id, "PENDING", json.dumps(client_payload)),
    #         )
    #         conn.commit()
    #         cursor.close()

    #         return {
    #             "job_id": job_id,
    #             "status": "PENDING",
    #             "message": "Job submitted successfully. Check status with request_type='job_status'.",
    #         }

    #     except Exception as e:
    #         self.color_print(f"submit_job(): DB error -> {e}", color="red")
    #         if conn is not None:
    #             conn.rollback()
    #         return {
    #             "job_id": None,
    #             "status": "error",
    #             "message": f"Error submitting job: {e}",
    #         }
    #     finally:
    #         if conn is not None:
    #             conn.close()

    # def job_status(self, payload):
    #     if IS_LOCAL_DB:
    #         return {
    #             "job_id": None,
    #             "status": "disabled_in_local_mode",
    #             "message": "Remote worker queue is disabled when using local DB.",
    #         }

    #     job_id = (payload or {}).get("job_id")
    #     self._logger.info(f"job_status: {payload}")
    #     if not job_id:
    #         return {
    #             "job_id": None,
    #             "status": "error",
    #             "message": "Missing 'job_id' in job_status payload",
    #         }

    #     conn = None
    #     try:
    #         conn = self.engine.raw_connection()
    #         cursor = conn.cursor()
    #         cursor.execute(
    #             "SELECT status, result_url, error_message, created_at, updated_at "
    #             "FROM jobs WHERE job_id = %s",
    #             (job_id,),
    #         )
    #         row = cursor.fetchone()
    #         cursor.close()

    #         if not row:
    #             return {
    #                 "job_id": job_id,
    #                 "status": "not_found",
    #                 "result_url": None,
    #                 "error_message": "Job not found",
    #                 "created_at": None,
    #                 "updated_at": None,
    #             }

    #         status, result_url, error_message, created_at, updated_at = row

    #         return {
    #             "job_id": job_id,
    #             "status": status,
    #             "result_url": result_url,
    #             "error_message": error_message,
    #             "created_at": created_at.isoformat() if created_at else None,
    #             "updated_at": updated_at.isoformat() if updated_at else None,
    #         }

    #     except Exception as e:
    #         self.color_print(f"job_status(): DB error -> {e}", color="red")
    #         return {
    #             "job_id": job_id,
    #             "status": "error",
    #             "result_url": None,
    #             "error_message": str(e),
    #             "created_at": None,
    #             "updated_at": None,
    #         }
    #     finally:
    #         if conn is not None:
    #             conn.close()



    # !##############################################
    # ! INGESTION
    # !##############################################

    def handle_ingestion(self, project_id: str, payload):
        ingestion_handler = BSSIngestion()
        current_bss, total_cost= ingestion_handler.handle_ingestion(payload, self.emit)

        # finally persist
        self.save_bss_schema(project_id, current_bss)

        idempotency_key = record_pending_charge(
            self.SessionFactory,
            project_id=str(project_id),
            amount=total_cost,
            currency=CURRENCY,
        )

        IDEMPOTENCY_CACHE.add(idempotency_key)

        return {
            "bss_schema": self._redact_bss_schema_for_ui(current_bss),
            "bss_labels": self._bss_labels,
            "heartbeat": idempotency_key,
        }
