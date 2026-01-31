

import logging
import os
from google.cloud import secretmanager
from google.oauth2 import service_account
from google.auth import default as google_auth_default
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(name)s\n%(message)s\n"
)

logger = logging.getLogger("kahuna_backend")

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
        url = "postgresql+pg8000://postgres:postgres@localhost:5432/stripe_billing_demo"
        logger.info(f"[DB] Using Local Postgress: {url}")
        return create_engine(
            url,
            connect_args={"timeout": 10}
        )

    url = f"postgresql+pg8000://{DB_USER}:{password}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    logger.info(f"[DB] Connecting to Postgres URL: {url}")

    # pg8000 supports 'timeout' in seconds
    return create_engine(
        url,
        connect_args={"timeout": 10},  # fail in 10s instead of hanging forever
    )

def create_session_factory() -> sessionmaker:
    engine = get_db_engine()
    return sessionmaker(bind=engine)
