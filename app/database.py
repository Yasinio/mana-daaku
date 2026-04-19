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

DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
