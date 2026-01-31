import logging
import os
import json

from classes.utils import Utils
from classes.google_helpers import IS_LOCAL_DB, get_db_engine

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("kahuna_backend")

class JobService(Utils):
    def __init__(self):
        self.engine = get_db_engine()

    def submit_job(self, payload):
        if IS_LOCAL_DB:
            return {
                "job_id": None,
                "status": "disabled_in_local_mode",
                "message": "Remote worker queue is disabled when using local DB.",
            }

        client_payload = payload or {}
        if not isinstance(client_payload, dict):
            return {
                "job_id": None,
                "status": "error",
                "message": "submit_job payload must be a JSON object",
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
                (job_id, "PENDING", json.dumps(client_payload)),
            )
            conn.commit()
            cursor.close()

            return {
                "job_id": job_id,
                "status": "PENDING",
                "message": "Job submitted successfully. Check status with request_type='job_status'.",
            }

        except Exception as e:
            self.color_print(f"submit_job(): DB error -> {e}", color="red")
            if conn is not None:
                conn.rollback()
            return {
                "job_id": None,
                "status": "error",
                "message": f"Error submitting job: {e}",
            }
        finally:
            if conn is not None:
                conn.close()

    def job_status(self, payload):
        if IS_LOCAL_DB:
            return {
                "job_id": None,
                "status": "disabled_in_local_mode",
                "message": "Remote worker queue is disabled when using local DB.",
            }

        job_id = (payload or {}).get("job_id")
        self._logger.info(f"job_status: {payload}")
        if not job_id:
            return {
                "job_id": None,
                "status": "error",
                "message": "Missing 'job_id' in job_status payload",
            }

        conn = None
        try:
            conn = self.engine.raw_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT status, result_url, error_message, created_at, updated_at "
                "FROM jobs WHERE job_id = %s",
                (job_id,),
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
            self.color_print(f"job_status(): DB error -> {e}", color="red")
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
                conn.close()
