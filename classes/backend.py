# classes/backend.py

from decimal import Decimal
import os
import json
import re
import traceback
import asyncio
import logging
import contextvars

from sqlalchemy.orm import sessionmaker

from classes.entities import Base, Job, Project
from classes.google_helpers import IS_LOCAL_DB, create_session_factory
from classes.history_cache import GLOBAL_HISTORY_CACHE, GLOBAL_BSS_HISTORY_CACHE
from classes.idempotency_cache import IDEMPOTENCY_CACHE
from classes.pending_charge_recorder import record_pending_charge


from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from classes.chat_prompts import BSS_PROMPT
from classes.ingestion_prompts import BSS_UC_EXTRACTOR_PROMPT, UC_COVERAGE_AUDITOR_PROMPT

CHAT_PROMPT = ""
SCHEMA_UPDATE_PROMPT = ""

from classes.utils import Utils
from classes.schema_manager import SchemaManager

from dotenv import load_dotenv

load_dotenv()
CURRENCY = os.getenv("CURRENCY")

logger = logging.getLogger("kahuna_backend")

_job_ctx_var = contextvars.ContextVar("job_ctx", default=None)



class Backend(Utils):
    def __init__(self):
        self.SessionFactory = create_session_factory()

        # Initialize default LLMs (used as fallback only)
        try:
            default_model = "gemini-2.5-flash-lite"
            self.default_llm, self.default_chat_llm = self._build_llms_for_model(default_model)
        except Exception as e:
            logger.info(f"Warning: Could not initialize VertexAI: {e}. Using mock.")
            self.default_llm = None
            self.default_chat_llm = None

        # Load Requirements Schema
        with open("./classes/requirements_schema.json", "r") as f:
            self.requirements_schema = json.load(f)

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

            elif request_type == "chat":
                response_data["data"] = self.handle_chat(project_id, payload)

            elif request_type == "add_comment":
                response_data["data"] = self.handle_comment(project_id, payload)

            elif request_type == "delete_node":
                response_data["data"] = self.handle_delete_node(project_id, payload)

            elif request_type == "update_node" or request_type == "add_node":
                response_data["data"] = self.handle_direct_schema_command(project_id, payload)

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


    def _run_schema_update_with_retry(
        self,
        schema_manager,
        llm,
        current_schema,
        combined_instruction: str,
        max_retries: int = 3
    ):
        current_schema_json = json.dumps(current_schema)
        last_discrepancies = []
        last_llm_response = ""

        for attempt in range(1, max_retries + 1):
            if last_discrepancies:
                retry_instruction = f"""
{combined_instruction}

VALIDATION FEEDBACK FROM EXECUTION ENGINE (attempt {attempt - 1}):
These discrepancies were found when applying your previous commands:
{json.dumps(last_discrepancies, indent=2)}

YOUR PREVIOUS COMMAND JSON (for reference):
{last_llm_response}

You must fix the problems above and propose a corrected set of commands.
"""
            else:
                retry_instruction = combined_instruction

            schema_prompt = self.unsafe_string_format(
                SCHEMA_UPDATE_PROMPT,
                current_schema_json=current_schema_json,
                validation_schema_json=json.dumps(self.requirements_schema, indent=2),
                combined_instruction=retry_instruction
            )

            if llm:
                llm_resp_obj = llm.invoke(schema_prompt)
            else:
                llm_resp_obj = '{"insert": [], "update": [], "delete": []}'

            if isinstance(llm_resp_obj, str):
                llm_response = llm_resp_obj
            else:
                llm_response = getattr(llm_resp_obj, "content", str(llm_resp_obj))

            last_llm_response = llm_response

            updated_schema_str, last_discrepancies, _ = schema_manager.apply_commands_to_schema(
                current_schema_json, llm_response, self.requirements_schema
            )

            if not last_discrepancies:
                updated_schema = json.loads(updated_schema_str)
                return updated_schema, []

        raise Exception(f"Schema update failed after {max_retries} attempts. Discrepancies: {last_discrepancies}")

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

        prompt = self.unsafe_string_format(
            BSS_PROMPT,
            USER_QUESTION=user_text,
            CURRENT_DOCUMENT=current_document,
            REGISTRY_LEDGER=registry_ledger_json,
        )

        print("===============Prompt\n\n" + prompt)

        if chat_llm:
            messages_for_llm = GLOBAL_BSS_HISTORY_CACHE.snapshot(project_id)
            messages_for_llm.append(HumanMessage(content=prompt))
            raw = chat_llm.invoke(messages_for_llm)
            raw = getattr(raw, "content", str(raw))
        else:
            raw = 'NEXT_QUESTION:"Mock question?"'

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

        # POST: recompute host-maintained dependency fields for ALL items
        updated_bss = self._recompute_bss_dependency_fields(updated_bss)

        self.save_bss_schema(project_id, updated_bss)

        # Compute amount/currency from the LLM client.

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
            "bot_message": next_question,
            "bss_schema": self._redact_bss_schema_for_ui(updated_bss),
            "bss_labels": self._bss_labels,
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


    def _apply_bss_slot_updates(self, current_bss: dict, slot_updates: dict) -> dict:
        if not isinstance(current_bss, dict):
            current_bss = {}

        for label, obj in (slot_updates or {}).items():
            if not self._is_bss_label(label):
                continue

            section = self._bss_section_for_label(label)
            if not section:
                continue

            norm = self._normalize_bss_item(obj)

            # If cancelled -> physically remove
            if norm.get("cancelled") is True:
                if section in current_bss and isinstance(current_bss[section], dict):
                    current_bss[section].pop(label, None)
                    if not current_bss[section]:
                        current_bss.pop(section, None)
                continue

            current_bss.setdefault(section, {})
            current_bss[section][label] = norm

        return current_bss

    def handle_chat(self, project_id: str, payload):
        payload = payload or {}
        message = (payload or {}).get("text", "") or ""

        llm, chat_llm = self._build_llms_for_payload(payload)

        current_schema = self.load_project(project_id)
        schema_manager = SchemaManager(llm)

        try:
            chat_prompt = self.unsafe_string_format(
                CHAT_PROMPT,
                current_schema_json=json.dumps(current_schema, indent=2),
                validation_schema_json=json.dumps(self.requirements_schema, indent=2),
                user_message=message
            )

            if chat_llm:
                messages_for_llm = GLOBAL_HISTORY_CACHE.snapshot(project_id)
                messages_for_llm.append(HumanMessage(content=chat_prompt))
                raw = chat_llm.invoke(messages_for_llm)
            else:
                raw = '{"assistant_message": "Mock reply", "schema_change_description": "", "updated_project_description": ""}'

            raw = getattr(raw, "content", str(raw))

            assistant_message, schema_change_description, updated_project_description = "", "", ""
            try:
                chat_obj = self.load_fault_tolerant_json(raw, llm=llm)
                assistant_message = self._coerce_field_to_str((chat_obj.get("assistant_message") or "")).strip()
                schema_change_description = self._coerce_field_to_str((chat_obj.get("schema_change_description") or "")).strip()
                updated_project_description = self._coerce_field_to_str((chat_obj.get("updated_project_description") or "")).strip()
                logger.info(f"Schema Changes Required: {schema_change_description}")
            except Exception as e:
                logger.error(f"Error While Processing Chat Message\n{e}\n{raw}")

            if assistant_message:
                GLOBAL_HISTORY_CACHE.append_turn(project_id, message, assistant_message)
            else:
                raise Exception("No assistant_message parsed from LLM response")

            updated_schema = current_schema
            discrepancies = []

            if schema_change_description or updated_project_description:
                combined_instruction = f"""
Schema change description (natural language plan of changes):
{schema_change_description or "(none)"}

Updated project description suggestion:
{updated_project_description or "(none)"}

IMPORTANT FOR THIS STEP:
- Implement the schema_change_description against the current schema.
- If updated_project_description is not empty, you MUST set $.Project.description
  exactly to that string in the mandatory update for $.Project.
"""
                updated_schema, discrepancies = self._run_schema_update_with_retry(
                    schema_manager=schema_manager,
                    llm=llm,
                    current_schema=current_schema,
                    combined_instruction=combined_instruction,
                    max_retries=3,
                )
                self.save_project(project_id, updated_schema)

            return {
                "bot_message": assistant_message,
                "updated_schema": updated_schema,
                "discrepancies": discrepancies,
                "schema_change_description": schema_change_description,
                "updated_project_description": updated_project_description
            }
        except Exception as e:
            logger.warning(f"Error while executing a prompt {e}")
            return self._safe_error_response(current_schema, e)

    def handle_comment(self, project_id: str, payload):
        payload = payload or {}
        path = payload.get("path")
        comment = payload.get("comment")

        current_schema = self.load_project(project_id)

        llm, _ = self._build_llms_for_payload(payload)
        schema_manager = SchemaManager(llm)

        try:
            current_item = schema_manager.get_fuzzy_nested_node(
                current_schema,
                (path or "").split(".")
            )

            combined_instruction = f"""
A user added a comment to a specific item in the requirements schema.

Item path (dot-notation as used in the UI): {path}

User comment:
{comment}

Current item content (as currently stored in the schema):
{json.dumps(current_item, indent=2)}

Your job:

- Interpret the user comment as a request to adjust ONLY this item
  (and any strictly necessary, directly related sub-entities).
- Propose insert/update/delete operations that:
  - keep the schema valid against the Validation Rules,
  - preserve all existing data structures, file structures, data schemas,
    and code snippets VERBATIM unless the comment clearly asks to change them,
  - update this item's description/body. so that it reflects
    the intent of the comment in a detailed, implementation-oriented way.
- Do NOT invent unrelated entities or groups.

MANDATORY:
- As always, include the mandatory update operation for $.Project that refreshes
  $.Project.description to reflect the change introduced by this comment.
"""
            updated_schema, discrepancies = self._run_schema_update_with_retry(
                schema_manager=schema_manager,
                llm=llm,
                current_schema=current_schema,
                combined_instruction=combined_instruction,
                max_retries=3,
            )

            self.save_project(project_id, updated_schema)

            return {
                "bot_message": f"I've processed your comment on {path}.",
                "updated_schema": updated_schema,
                "discrepancies": discrepancies,
            }

        except Exception as e:
            return self._safe_error_response(current_schema, e)

    # !##############################################
    # ! Handle Jobs
    # !##############################################

    def handle_submit_job(self, project_id: str, payload: dict) -> dict:
        """
        Enqueue a long-running pipeline job.
        Guards so that a user / project cannot have more than one active job
        (QUEUED or RUNNING) at a time.
        """
        if IS_LOCAL_DB:
            return {
                "job_id": None,
                "state": "disabled_in_local_mode",
                "status": "Remote worker queue is disabled when using local DB.",
            }

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

        self.emit("ingestion_status", {"pct": 0, "stage": "start", "note": "Starting ingestion"})

        llm, _ = self._build_llms_for_payload(payload)  # direct (non-chat) calls only
        if not llm:
            raise RuntimeError("No LLM available for ingestion")

        self.emit("ingestion_status", {"pct": 10, "stage": "load", "note": "Loading project state"})
        _ = self.load_project(project_id)  # currently unused, but keeps symmetry with other flows

        max_attempts = 3
        report = "None"

        usecases_by_label = {}
        connected = {}  # label -> set(responsibility_str)
        last_completion = None
        last_missing_block = ""

        for attempt in range(1, max_attempts + 1):
            self.emit("ingestion_status", {"pct": 20, "stage": "extract", "note": f"Extracting use cases (attempt {attempt}/{max_attempts})"})

            prompt1 = self.unsafe_string_format(
                BSS_UC_EXTRACTOR_PROMPT,
                prd=prd,
                report=report or "None",
            )
            raw = llm.invoke(prompt1)
            # raw = raw if isinstance(raw, str) else getattr(raw, "content", str(raw))

            parsed_ucs, parsed_connected = self._ingestion_parse_extractor_output(raw)

            # merge usecases
            for uc in parsed_ucs:
                label = uc.get("label")
                if not label:
                    continue
                prev = usecases_by_label.get(label) or {}
                usecases_by_label[label] = self._ingestion_merge_usecase(prev, uc)

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

            self.emit("ingestion_status", {"pct": 45, "stage": "parsed", "note": f"Parsed {len(parsed_ucs)} use cases (total {len(usecases_by_label)})"})

            # self.emit("ingestion_status", {"pct": 55, "stage": "audit", "note": f"Auditing coverage (attempt {attempt}/{max_attempts})"})
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

            self.emit("ingestion_status", {"pct": 70, "stage": "audit_done", "note": f"Coverage: {completion_pct if completion_pct is not None else 'unknown'}%"})

            if completion_pct is not None and completion_pct >= 95:
                break

            report = self._ingestion_build_report(usecases_by_label, connected, last_missing_block)
            self.emit("ingestion_status", {"pct": 80, "stage": "reflect", "note": "Preparing refinement report for next attempt"})

        self.emit("ingestion_status", {"pct": 100, "stage": "done", "note": "Ingestion completed"})

        # finalize payloads
        usecases = self._ingestion_sorted_usecases(usecases_by_label)
        connected_items = self._ingestion_sorted_connected_items(connected)

        return {
            "bot_message": "Done",
            "completion_pct": last_completion,
            "missing_use_cases": last_missing_block,
            "usecases": usecases,
            "connected_items": connected_items,
        }

    # -----------------------
    # Ingestion helpers
    # -----------------------

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
            if _UC_LABEL_RE.fullmatch((label or "").strip()):
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
            if _UC_LABEL_RE.fullmatch((label or "").strip()):
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
                        if self._CONNECTED_LABEL_RE.fullmatch((maybe_label or "").strip()):
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
                    if self._CONNECTED_LABEL_RE.fullmatch(t):
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
