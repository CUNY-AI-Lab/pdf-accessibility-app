from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # pdf-accessibility-app/


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    # Database
    database_url: str = f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'pdf_accessibility.db'}"

    # File storage
    upload_dir: Path = BASE_DIR / "data" / "uploads"
    processing_dir: Path = BASE_DIR / "data" / "processing"
    output_dir: Path = BASE_DIR / "data" / "output"

    # LLM (OpenAI-compatible)
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "ollama"
    llm_model: str = "llava"
    llm_timeout: int = 120

    # veraPDF
    verapdf_path: str = "verapdf"
    verapdf_flavour: str = "ua1"

    # OCR
    ocr_language: str = "eng"

    # Dev
    debug: bool = False


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
