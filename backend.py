import os
import json
import traceback
import asyncio
import logging
import re

from sqlalchemy.orm import sessionmaker

from classes.entities import Base, QueueMessage
from classes.google_helpers import IS_LOCAL_DB, get_db_engine
from classes.history_cache import HistoryCache


from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from classes.chat_prompts import (
    BSS_PROMPT,
    BSS_INSTRUCTIONS,
    BSS_TEXT_SCHEMA,
)

CHAT_PROMPT =""
SCHEMA_UPDATE_PROMPT=""

from classes.utils import Utils

from classes.schema_manager import SchemaManager

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(name)s\n%(message)s\n"
)
logger = logging.getLogger("kahuna_backend")

QUEUE_RECEIVER_ID = os.getenv("QUEUE_RECEIVER_ID")

# --- Backend Logic ---

class Backend(Utils):
    def __init__(self):
        self.engine = get_db_engine()
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        # Initialize default LLMs (used as fallback only)
        try:
            default_model = "gemini-2.5-flash-lite"
            self.default_llm, self.default_chat_llm = self._build_llms_for_model(default_model)
        except Exception as e:
            logger.info(f"Warning: Could not initialize VertexAI: {e}. Using mock.")
            self.default_llm = None
            self.default_chat_llm = None

        self.history_cache = HistoryCache(ttl_seconds=24 * 3600, max_tokens=8000)

        # Load Requirements Schema
        with open("./classes/requirements_schema.json", "r") as f:
            self.requirements_schema = json.load(f)

        # BSS Chat
        self.bss_history_cache = HistoryCache(ttl_seconds=24 * 3600, max_tokens=8000)
        self._bss_labels = self._bss_section_order()

    # -----------------------
    # Queue worker entrypoint
    # -----------------------

    def process_queue_job(self, job: dict) -> None:
        session = self.Session()
        try:
            project_id = str(job.get("sender_id"))
            msg_type = job.get("type") or "unknown"

            response_data = self._process_request_data(job)

            response_msg = QueueMessage(
                sender_id=str(job.get("receiver_id")),  # this worker
                receiver_id=project_id,                 # original sender project_id (your contract)
                type=f"{msg_type}_response",
                payload=response_data,
            )
            session.add(response_msg)
            session.commit()

        except Exception as e:
            logger.info(f"Error processing queue job {job.get('id')}: {e}")
            session.rollback()
            traceback.print_exc()
        finally:
            session.close()


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

            elif request_type == "submit_job":
                response_data["data"] = self.handle_submit_job(payload)

            elif request_type == "job_status":
                response_data["data"] = self.handle_job_status(payload)

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
            BSS_TEXT_SCHEMA=BSS_TEXT_SCHEMA,
            CURRENT_DOCUMENT=current_document,
            REGISTRY_LEDGER=registry_ledger_json,
        )

        print("===============Prompt\n\n" + prompt)

        if chat_llm:
            messages_for_llm = [SystemMessage(content=BSS_INSTRUCTIONS)]
            messages_for_llm.extend(self.bss_history_cache.snapshot(project_id))
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

            messages_fix = [SystemMessage(content=BSS_INSTRUCTIONS)]
            messages_fix.extend(self.bss_history_cache.snapshot(project_id))
            messages_fix.append(HumanMessage(content=prompt))
            messages_fix.append(AIMessage(content=raw_clean))      # the malformed output
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

        self.bss_history_cache.append_turn(project_id, user_text, next_question)

        return {
            "bot_message": next_question,
            "bss_schema": self._redact_bss_schema_for_ui(updated_bss),
            "bss_labels": self._bss_labels,
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
                messages_for_llm = self.history_cache.snapshot(project_id)
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
                self.history_cache.append_turn(project_id, message, assistant_message)
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


    # -----------------------
    # jobs
    # -----------------------


    def handle_submit_job(self, payload):
        if IS_LOCAL_DB:
            return {
                "job_id": None,
                "status": "disabled_in_local_mode",
                "message": "Remote worker queue is disabled when using local DB."
            }

        client_payload = payload or {}
        if not isinstance(client_payload, dict):
            return {
                "job_id": None,
                "status": "error",
                "message": "submit_job payload must be a JSON object"
            }

        existing_model = self._detect_llm_model_in_payload(client_payload)
        if not existing_model:
            client_payload["model"] = "gemini-2.5-flash-lite"

        job_id = f"job_{os.urandom(8).hex()}"

        conn = None
        try:
            conn = self.engine.raw_connection()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO jobs (job_id, status, client_request_data) VALUES (%s, %s, %s)",
                (job_id, "PENDING", json.dumps(client_payload))
            )
            conn.commit()
            cursor.close()

            return {
                "job_id": job_id,
                "status": "PENDING",
                "message": "Job submitted successfully. Check status with request_type='job_status'."
            }

        except Exception as e:
            self.color_print(f"handle_submit_job(): DB error -> {e}", color="red")
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            return {
                "job_id": None,
                "status": "error",
                "message": f"Error submitting job: {e}"
            }
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def handle_job_status(self, payload):
        if IS_LOCAL_DB:
            return {
                "job_id": None,
                "status": "disabled_in_local_mode",
                "message": "Remote worker queue is disabled when using local DB."
            }

        job_id = (payload or {}).get("job_id")
        logger.info(f"handle_job_status: {payload}")
        if not job_id:
            return {
                "job_id": None,
                "status": "error",
                "message": "Missing 'job_id' in job_status payload"
            }

        conn = None
        try:
            conn = self.engine.raw_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT status, result_url, error_message, created_at, updated_at "
                "FROM jobs WHERE job_id = %s",
                (job_id,)
            )
            row = cursor.fetchone()
            cursor.close()

            if not row:
                return {
                    "job_id": job_id,
                    "status": "not_found",
                    "result_url": None,
                    "error_message": "Job not found",
                    "created_at": None,
                    "updated_at": None,
                }

            status, result_url, error_message, created_at, updated_at = row

            return {
                "job_id": job_id,
                "status": status,
                "result_url": result_url,
                "error_message": error_message,
                "created_at": created_at.isoformat() if created_at else None,
                "updated_at": updated_at.isoformat() if updated_at else None,
            }

        except Exception as e:
            self.color_print(f"handle_job_status(): DB error -> {e}", color="red")
            return {
                "job_id": job_id,
                "status": "error",
                "result_url": None,
                "error_message": str(e),
                "created_at": None,
                "updated_at": None,
            }
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def run(self, poll_interval: float = 1.0, max_concurrent: int = 4) -> None:
        if not QUEUE_RECEIVER_ID:
            raise RuntimeError("QUEUE_RECEIVER_ID env var is required for DB queue mode")

        guard = AsyncGuard(
            backend=self,
            receiver_id=QUEUE_RECEIVER_ID,
            poll_interval=poll_interval,
            max_concurrent=max_concurrent,
        )
        asyncio.run(guard.run())


class Executor:
    def __init__(self, backend: "Backend"):
        self.backend = backend

    def execute(self, job: dict) -> None:
        self.backend.process_queue_job(job)


class AsyncGuard:
    def __init__(
        self,
        backend: "Backend",
        receiver_id: str,
        poll_interval: float = 1.0,
        max_concurrent: int = 4,
    ):
        self.backend = backend
        self.receiver_id = receiver_id
        self.poll_interval = poll_interval
        self.max_concurrent = max_concurrent
        self._in_flight = set()

    async def _run_executor_for_message(self, job: dict) -> None:
        executor = Executor(self.backend)
        try:
            await asyncio.to_thread(executor.execute, job)
        finally:
            self._in_flight.discard(job["id"])


    async def run(self) -> None:
        logger.info(
            "AsyncGuard running – receiver_id=%s (max_concurrent=%d)",
            self.receiver_id,
            self.max_concurrent,
        )

        while True:
            # REQUIRED: sweep stale history every cycle
            removed = self.backend.history_cache.sweep_expired()
            if removed:
                logger.debug("HistoryCache sweep: removed %d expired histories", removed)
            removed2 = self.backend.bss_history_cache.sweep_expired()
            if removed2:
                logger.debug("BSS HistoryCache sweep: removed %d expired histories", removed2)

            available_slots = self.max_concurrent - len(self._in_flight)
            if available_slots <= 0:
                print("enqueing")
                await asyncio.sleep(self.poll_interval)
                continue

            session = self.backend.Session()
            try:
                rows = (
                    session.query(QueueMessage)
                    .filter(QueueMessage.receiver_id == str(self.receiver_id))
                    .order_by(QueueMessage.created_at.asc())
                    .with_for_update(skip_locked=True)
                    .limit(available_slots)
                    .all()
                )

                # make plain jobs BEFORE deleting ORM instances
                jobs = [
                    {
                        "id": r.id,
                        "sender_id": r.sender_id,
                        "receiver_id": r.receiver_id,
                        "type": r.type,
                        "payload": r.payload,
                    }
                    for r in rows
                ]

                for r in rows:
                    session.delete(r)

                session.commit()
            finally:
                session.close()


            if not jobs:
                await asyncio.sleep(self.poll_interval)
                continue

            for job in jobs:
                if job["id"] in self._in_flight:
                    continue
                print("available")
                self._in_flight.add(job["id"])
                asyncio.create_task(self._run_executor_for_message(job))


            await asyncio.sleep(self.poll_interval)


if __name__ == "__main__":
    backend = Backend()
    backend.run()
