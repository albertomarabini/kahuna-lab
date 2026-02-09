import os
import json
from datetime import timedelta
from typing import Callable

from google.cloud import storage, secretmanager
from google.oauth2 import service_account
from google.auth import default as google_auth_default

import pg8000
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session


class GCConnection:
    def __init__(self) -> None:
        # ---- env config (shared) ----
        self.PROJECT_ID   = os.getenv("PROJECT_ID", "")
        self.BUCKET_NAME  = os.getenv("GCS_BUCKET_NAME", "")
        self.BUCKET       = os.getenv("BUCKET", "")          # your name here; DB_SECRET_ID looks wrong
        self.DB_HOST      = os.getenv("DB_HOST", "")
        self.DB_PORT      = int(os.getenv("DB_PORT", "0"))   # or keep "5432" if you want a real default
        self.DB_NAME      = os.getenv("DB_NAME", "")
        self.DB_USER      = os.getenv("DB_USER", "")
        self.DB_PASSWORD  = os.getenv("DB_PASSWORD", "")
        self.DB_SECRET_ID = os.getenv("DB_SECRET_ID", "")

        # ---- GCP clients ----
        self.bucket_creds = self._build_creds()
        # try:
        #     sa_email = getattr(self.bucket_creds, "service_account_email", None)
        #     print(f"\n******************************\nUsing credentials for service account: {sa_email or '(unknown)'}\n******************************")
        # except Exception:
        #     print("\n******************************\nUNABLE TO RETRIEVE CREDENTIALS\n******************************")
        #     raise

        self.storage_client = storage.Client(credentials=self.bucket_creds)
        self.secret_client = (
            secretmanager.SecretManagerServiceClient(credentials=self.bucket_creds)
            if not self.DB_PASSWORD and self.DB_SECRET_ID
            else None
        )

        # !###############################################
        # !   YOU CONNECT DIRECLY TO GOOGLE OR USE A
        # !   DATABASE_URL IN THE .ENV FILE
        # !###############################################
        self.DATABASE_URL = os.getenv("DATABASE_URL", "")
        self.IS_LOCAL=True
        if not self.DATABASE_URL:
            self.IS_LOCAL=False
            self.DATABASE_URL = f"postgresql+psycopg2://{self.DB_USER}:{self._get_db_password_lazy()}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    # -------- GCP auth / creds --------
    def _build_creds(self):
        key_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        scopes = ["https://www.googleapis.com/auth/cloud-platform"]
        if key_path and os.path.exists(key_path):
            return service_account.Credentials.from_service_account_file(key_path, scopes=scopes)
        creds, _ = google_auth_default(scopes=scopes)
        return creds

    # -------- DB password (Secret Manager) --------
    def _get_db_password_lazy(self) -> str:
        if self.DB_PASSWORD:
            return self.DB_PASSWORD
        if self.secret_client and self.DB_SECRET_ID:
            name = self.secret_client.secret_version_path(self.PROJECT_ID, self.DB_SECRET_ID, "latest")
            resp = self.secret_client.access_secret_version(request={"name": name})
            self.DB_PASSWORD = resp.payload.data.decode("utf-8")
            return self.DB_PASSWORD
        raise RuntimeError("No DB_PASSWORD and no Secret Manager configured")

    # -------- Raw pg8000 connection (JobWorker style) --------
    def db_connect_pg8000(self):
        pw = self._get_db_password_lazy()
        return pg8000.dbapi.connect(
            host=self.DB_HOST,
            port=self.DB_PORT,
            user=self.DB_USER,
            password=pw,
            database=self.DB_NAME,
            timeout=10,
        )

    # -------- SQLAlchemy Session factory --------
    def build_db_session_factory(self) -> Callable[[], Session]:
        if not getattr(self, "_sessionmaker", None):
            engine = create_engine(self.DATABASE_URL, future=True, pool_pre_ping=True)
            self._sessionmaker = sessionmaker(
                bind=engine,
                autoflush=False,
                autocommit=False,
                future=True,
            )

        def _factory() -> Session:
            return self._sessionmaker()

        return _factory

    # -------- Storage helpers --------
    # Examples:
    # gs_url, https_url = self._upload_to_gcs(self.BUCKET_NAME, object_path, result["zip_bytes"]) <----Regular URLs
    # signed_url = self._generate_signed_url(self.BUCKET_NAME, object_path) <--  Public Url from a private bucket
    def upload_to_gcs(self, bucket_name: str, blob_path: str, data: bytes,
                      content_type: str = "application/zip"):
        bucket = self.storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        blob.upload_from_string(data, content_type=content_type)
        return f"gs://{bucket_name}/{blob_path}", f"https://storage.googleapis.com/{bucket_name}/{blob_path}"

    def generate_signed_url(self, bucket_name: str, blob_path: str,
                            expires_in_seconds: int = 604800) -> str | None:
        """
        (7 days) 604800 is the max expiration time for a signed url
        """
        try:
            bucket = self.storage_client.bucket(bucket_name)
            blob = bucket.blob(blob_path)
            return blob.generate_signed_url(
                version="v4",
                expiration=timedelta(seconds=expires_in_seconds),
                method="GET",
                credentials=self.bucket_creds,
            )
        except Exception as e:
            # caller decides if signed URL is required or optional
            import logging
            logging.getLogger("worker").warning("Could not generate signed URL (non-fatal): %s", e)
            return None
