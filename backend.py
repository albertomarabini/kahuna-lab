import os, re
import time
import json
import traceback
from datetime import datetime
import commentjson
from sqlalchemy import create_engine, Column, Integer, String, Text, ForeignKey
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import SQLAlchemyError
from langchain_google_vertexai import VertexAI, ChatVertexAI
from langchain_core.messages import HumanMessage, AIMessage
from langchain_community.chat_message_histories.in_memory import ChatMessageHistory
from google.cloud import secretmanager
from google.oauth2 import service_account
from google.auth import default as google_auth_default
import yaml
import logging
import sys

import logging

logging.basicConfig(
    level=logging.DEBUG,  # or INFO if it's too noisy
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("kahuna_backend")

from classes.schema_manager import SchemaManager
from classes.backend_prompts import CHAT_PROMPT, SCHEMA_UPDATE_PROMPT
from dotenv import load_dotenv
load_dotenv()

# --- Configuration ---
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "your-project-id")
REGION = os.getenv("GOOGLE_CLOUD_REGION", "us-central1")

DB_HOST             = os.environ.get("DB_HOST", "localhost")
DB_PORT             = int(os.environ.get("DB_PORT", "5432"))
DB_NAME             = os.environ["DB_NAME"]
DB_USER             = os.environ["DB_USER"]
DB_PASSWORD         = os.environ.get("DB_PASSWORD")
DB_SECRET_ID        = os.environ.get("DB_SECRET_ID")

IS_LOCAL_DB = (DB_HOST == "localhost")

EVENTS_REQUEST_DIR = "events/request"
EVENTS_RESPONSE_DIR = "events/response"

# --- Database Setup ---
Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)

class Project(Base):
    __tablename__ = 'projects'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    user_id = Column(Integer, ForeignKey('users.id'))
    content = Column(Text, nullable=False) # Stores requirements.json content


def _build_creds():
    key_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    if key_path and os.path.exists(key_path):
        return service_account.Credentials.from_service_account_file(key_path, scopes=scopes)
    creds, _ = google_auth_default(scopes=scopes)
    return creds


def get_db_password() -> str:
    global DB_PASSWORD

    if DB_PASSWORD:
        return DB_PASSWORD

    if DB_SECRET_ID:
        creds = _build_creds()
        client = secretmanager.SecretManagerServiceClient(credentials=creds)
        name = client.secret_version_path(PROJECT_ID, DB_SECRET_ID, "latest")
        resp = client.access_secret_version(request={"name": name})
        DB_PASSWORD = resp.payload.data.decode("utf-8")
        return DB_PASSWORD

    raise RuntimeError("No DB_PASSWORD and no Secret Manager configured")


def get_db_engine():
    password = get_db_password()

    if DB_HOST == "localhost":
        url = "sqlite:///requirements.db"
        logger.info(f"[DB] Using SQLite URL: {url}")
        return create_engine(url)

    url = f"postgresql+pg8000://{DB_USER}:{password}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    logger.info(f"[DB] Connecting to Postgres URL: {url}")

    # pg8000 supports 'timeout' in seconds
    return create_engine(
        url,
        connect_args={"timeout": 10},  # fail in 10s instead of hanging forever
    )

# --- Backend Logic ---

class Backend:
    def __init__(self):
        self.engine = get_db_engine()
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        # Initialize LLM
        try:
            self.llm = VertexAI(
                project=PROJECT_ID,
                location=REGION,
                model_name="gemini-2.5-flash-lite",
            )
            self.chat_llm = ChatVertexAI(
                project=PROJECT_ID,
                location=REGION,
                model="gemini-2.5-flash-lite",
            )
        except Exception as e:
            logger.info(f"Warning: Could not initialize VertexAI: {e}. Using mock.")
            self.llm = None
            self.chat_llm = None
        self.schema_manager = SchemaManager(self.llm)
        self.histories = {}

        # Load Requirements Schema
        with open("./classes/requirements_schema.json", "r") as f:
            self.requirements_schema = json.load(f)

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
            logger.info(f"\033[93m\033[3mMissing keys within string-to-format in unsafe_string_format: {', '.join(missing_keys)}\033[0m", flush=True)
            # print(f"\033[93m\033[3mOriginal string: {dest_string}\033[0m", flush=True)
        return result

    def process_request(self, request_file):
        try:
            with open(os.path.join(EVENTS_REQUEST_DIR, request_file), "r") as f:
                request_data = json.load(f)

            try:
                preview = json.dumps(request_data, indent=2)
            except Exception:
                preview = str(request_data)
            logger.debug("process_request request payload=\n%s", preview)

            request_type = request_data.get("type")
            username = request_data.get("username")
            project_name = request_data.get("project_name")
            payload = request_data.get("payload")

            response_data = {
                "status": "success",
                "message": "",
                "username": username,
                "project_name": project_name,
            }

            if request_type == "load_project":
                response_data["data"] = {}
                response_data["data"]["updated_schema"] = self.load_project(username, project_name)
            elif request_type == "save_project":
                self.save_project(username, project_name, payload)
                response_data["message"] = "Project saved."
            elif request_type == "chat":
                response_data["data"] = self.handle_chat(username, project_name, payload)
            elif request_type == "add_comment":
                response_data["data"] = self.handle_comment(username, project_name, payload)
            elif request_type == "delete_node":
                response_data["data"] = self.handle_delete_node(username, project_name, payload)
            elif request_type == "update_node":
                response_data["data"] = self.handle_direct_schema_command(username, project_name, payload)
            elif request_type == "is_worker_busy":
                response_data["data"] = self.is_worker_busy()
            elif request_type == "submit_job":
                response_data["data"] = self.handle_submit_job(payload)
            elif request_type == "job_status":
                response_data["data"] = self.handle_job_status(payload)
            else:
                response_data["status"] = "error"
                response_data["message"] = f"Unknown request type: {request_type}"

            def _sanitize_for_filename(s: str) -> str:
                if not s:
                    return ""
                # letters, digits, _ . - only
                return re.sub(r"[^A-Za-z0-9_.-]", "_", s)

            # Write response
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            safe_user = _sanitize_for_filename(username)
            safe_project = _sanitize_for_filename(project_name)
            response_filename = f"{safe_user}__{safe_project}__{timestamp}.json"
            with open(os.path.join(EVENTS_RESPONSE_DIR, response_filename), "w") as f:
                json.dump(response_data, f)

            try:
                preview = json.dumps(response_data, indent=2)
            except Exception:
                preview = str(response_data)
            logger.debug("process_request response payload=\n%s", preview)


            # Clean up request
            os.remove(os.path.join(EVENTS_REQUEST_DIR, request_file))

        except Exception as e:
            logger.info(f"Error processing request {request_file}: {e}")
            traceback.logger.info_exc()

    def load_project(self, username, project_name):
        session = self.Session()
        try:
            user = session.query(User).filter_by(username=username).first()
            if not user:
                user = User(username=username)
                session.add(user)
                session.commit()

            project = session.query(Project).filter_by(user_id=user.id, name=project_name).first()
            if not project:
                initial_content = json.dumps({
                    "Project": {
                        "description": "",
                        "CoreDataStructures": {},
                        "APIEndpoints": {},
                        "ExternalInterfaces": {},
                        "UserStories": {},
                        "NonFunctionalRequirements": {},
                        "UIComponents": {},
                        "TechnologiesInvolved": {}
                    }
                })
                project = Project(name=project_name, user_id=user.id, content=initial_content)
                session.add(project)
                session.commit()

            content = project.content  # materialize before closing session
        finally:
            session.close()

        return json.loads(content)

    def save_project(self, username, project_name, content):
        session = self.Session()
        user = session.query(User).filter_by(username=username).first()
        project = session.query(Project).filter_by(user_id=user.id, name=project_name).first()
        project.content = json.dumps(content)
        session.commit()
        session.close()

    def _conversation_key(self, username, project_name):
        return f"{username}::{project_name}"

    def _get_history(self, username, project_name) -> ChatMessageHistory:
        key = self._conversation_key(username, project_name)
        if key not in self.histories:
            self.histories[key] = ChatMessageHistory()
        return self.histories[key]

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

    def load_fault_tolerant_json(self, json_str, ensure_ordered=False):
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
        repaired_json_str = self.llm.invoke(prompt)
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

    def _run_schema_update_with_retry(self, current_schema, combined_instruction: str, max_retries: int = 3):
        current_schema_json = json.dumps(current_schema)
        last_discrepancies = []
        last_llm_response = ""

        for attempt in range(1, max_retries + 1):
            if last_discrepancies:
                # Enrich the original combined_instruction with validation feedback
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

            if self.llm:
                llm_resp_obj = self.llm.invoke(schema_prompt)
            else:
                llm_resp_obj = '{"insert": [], "update": [], "delete": []}'

            if isinstance(llm_resp_obj, str):
                llm_response = llm_resp_obj
            else:
                llm_response = getattr(llm_resp_obj, "content", str(llm_resp_obj))

            last_llm_response = llm_response

            updated_schema_str, last_discrepancies, _ = self.schema_manager.apply_commands_to_schema(
                current_schema_json, llm_response, self.requirements_schema
            )

            if not last_discrepancies:
                updated_schema = json.loads(updated_schema_str)
                return updated_schema, []

        # If we get here, all attempts failed
        raise Exception(f"Schema update failed after {max_retries} attempts. Discrepancies: {last_discrepancies}")

    def _build_llms_for_model(self, model_name: str):
        """
        Build per-request LLM instances for the given model name.
        Falls back to None/None if creation fails.
        """
        try:
            self.llm = VertexAI(
                project=PROJECT_ID,
                location=REGION,
                model_name=model_name,
            )
            self.chat_llm = ChatVertexAI(
                project=PROJECT_ID,
                location=REGION,
                model=model_name,
            )
        except Exception as e:
            logger.info(f"Warning: Could not initialize VertexAI model '{model_name}': {e}. Using mock.")
            raise e

    def handle_chat(self, username, project_name, payload):
        # Extract data from payload
        message = (payload or {}).get("text", "") or ""
        llm_model = (payload or {}).get("llm_model") or "gemini-2.5-flash-lite"
        self._build_llms_for_model(llm_model)

        # Load current schema from DB
        current_schema = self.load_project(username, project_name)
        history = self._get_history(username, project_name)

        try:
            # ---------- 1) CHAT + CHANGE REASONING CALL ----------
            chat_prompt = self.unsafe_string_format(
                CHAT_PROMPT,
                current_schema_json=json.dumps(current_schema, indent=2),
                validation_schema_json=json.dumps(self.requirements_schema, indent=2),
                user_message=message
            )

            if self.chat_llm:
                messages_for_llm = list(history.messages)
                messages_for_llm.append(HumanMessage(content=chat_prompt))
                raw = self.chat_llm.invoke(messages_for_llm)
            else:
                raw = '{"assistant_message": "Mock reply", "schema_change_description": "", "updated_project_description": ""}'

            raw = getattr(raw, "content", str(raw))
            try:
                chat_obj = self.load_fault_tolerant_json(raw)
            except json.JSONDecodeError:
                chat_obj = {
                    "assistant_message": raw,
                    "schema_change_description": "",
                    "updated_project_description": ""
                }

            assistant_message = self._coerce_field_to_str((chat_obj.get("assistant_message") or "")).strip()
            schema_change_description = self._coerce_field_to_str((chat_obj.get("schema_change_description") or "")).strip()
            updated_project_description = self._coerce_field_to_str((chat_obj.get("updated_project_description") or "")).strip()

            # Update LC history (no DB)
            if assistant_message:
                history.add_message(HumanMessage(content=message))
                history.add_message(AIMessage(content=assistant_message))
            else:
                raise Exception()

            updated_schema = current_schema
            discrepancies = []

            # ---------- 2) SCHEMA UPDATE CALL (only if changes are needed) ----------
            if schema_change_description or updated_project_description:
                # Reuse the existing "big" prompt, but feed it a combined description
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
                    current_schema=current_schema,
                    combined_instruction=combined_instruction,
                    max_retries=3,
                )
                self.save_project(username, project_name, updated_schema)

            # ---------- FINAL RESPONSE TO FRONTEND ----------
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

    def handle_comment(self, username, project_name, payload):
        path = payload.get("path")
        comment = payload.get("comment")
        current_schema = self.load_project(username, project_name)

        try:
            # Grab the current item so the LLM sees exactly what it's touching
            current_item = self.schema_manager.get_fuzzy_nested_node(
                current_schema,
                path.split(".")
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

            # Run through the same robust schema update pipeline with retries
            updated_schema, discrepancies = self._run_schema_update_with_retry(
                current_schema=current_schema,
                combined_instruction=combined_instruction,
                max_retries=3,
            )

            self.save_project(username, project_name, updated_schema)

            return {
                "bot_message": f"I've processed your comment on {path}.",
                "updated_schema": updated_schema,
                "discrepancies": discrepancies,
            }

        except Exception as e:
            # Reuse the same safe error response shape as handle_chat
            return self._safe_error_response(current_schema, e)

    def handle_direct_schema_command(self, username, project_name, payload):
        # Load current schema (dict)
        current_schema = self.load_project(username, project_name)
        current_schema_json = json.dumps(current_schema)

        updated_schema_str, discrepancies, _ = self.schema_manager.apply_commands_to_schema(
            current_schema_json,
            json.dumps(payload),
            self.requirements_schema
        )
        updated_schema = json.loads(updated_schema_str)
        if not discrepancies:
            self.save_project(username, project_name, updated_schema)

        return {
            "updated_schema": updated_schema,
            "discrepancies": discrepancies,
        }


    def handle_delete_node(self, username, project_name, payload):
        path = (payload or {}).get("path", "") or ""
        if not path:
            # reuse safe response shape
            current_schema = self.load_project(username, project_name)
            return self._safe_error_response(current_schema, ValueError("Missing 'path' in delete_node payload"))

        # Normalize path: ensure it starts with "$."
        if path.startswith("$."):
            normalized_path = path
        elif path.startswith("$"):
            normalized_path = "$." + path[1:]
        else:
            normalized_path = "$." + path

        # Load current schema (dict)
        current_schema = self.load_project(username, project_name)
        current_schema_json = json.dumps(current_schema)

        # Build a minimal delete command for SchemaManager
        delete_command = json.dumps({
            "delete": [
                {"path": normalized_path}
            ]
        })
        try:
            updated_schema_str, discrepancies, _ = self.schema_manager.apply_commands_to_schema(
                current_schema_json,
                delete_command,
                self.requirements_schema
            )
            updated_schema = json.loads(updated_schema_str)
            if not discrepancies:
                self.save_project(username, project_name, updated_schema)
            return {
                "bot_message": f"Node at path '{normalized_path}' has been deleted." if not discrepancies
                               else f"Could not safely delete node at '{normalized_path}'.",
                "updated_schema": updated_schema,
                "discrepancies": discrepancies,
            }
        except Exception as e:
            return self._safe_error_response(current_schema, e)

    def is_worker_busy(self):
        if IS_LOCAL_DB:
            return {"is_worker_busy": True}
        conn = None
        try:
            conn = self.engine.raw_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT is_busy FROM worker_state WHERE id = 'singleton'")
            row = cursor.fetchone()
            cursor.close()

            is_busy = bool(row and row[0])
            return {"is_worker_busy": is_busy}

        except Exception as e:
            self.color_print(f"is_worker_busy(): DB error -> {e}", color="red")
            return {"is_worker_busy": True}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def handle_submit_job(self, payload):
        if IS_LOCAL_DB:
            # In local / debug mode, remote worker queue is disabled.
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

        # Force a default model if not provided (similar spirit to the admin check)
        client_payload.setdefault("model", "gemini-2.5-flash-lite")

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

    def run(self):
        logger.info("Backend running...")
        if not os.path.exists(EVENTS_REQUEST_DIR):
            os.makedirs(EVENTS_REQUEST_DIR)
        if not os.path.exists(EVENTS_RESPONSE_DIR):
            os.makedirs(EVENTS_RESPONSE_DIR)

        while True:
            files = sorted(os.listdir(EVENTS_REQUEST_DIR))
            for file in files:
                if file.startswith("server_"):
                    self.process_request(file)
            time.sleep(1)

if __name__ == "__main__":
    backend = Backend()
    backend.run()
