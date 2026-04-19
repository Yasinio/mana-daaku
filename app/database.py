import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DEFAULT_SQLITE_PATH = os.path.join(DATA_DIR, "mana_daakuu.db")
DEFAULT_DATABASE_URL = f"sqlite:///{DEFAULT_SQLITE_PATH}"

raw_database_url = os.getenv("DATABASE_URL", "").strip()

# If DATABASE_URL is missing, use SQLite.
# If DATABASE_URL points to localhost/127.0.0.1, also use SQLite,
# because that local database will not exist on Railway.
if not raw_database_url:
    DATABASE_URL = DEFAULT_DATABASE_URL
elif "localhost" in raw_database_url or "127.0.0.1" in raw_database_url:
    DATABASE_URL = DEFAULT_DATABASE_URL
else:
    DATABASE_URL = raw_database_url

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

