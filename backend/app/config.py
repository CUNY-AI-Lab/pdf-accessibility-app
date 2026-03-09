from pathlib import Path
from urllib.parse import urlparse

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # pdf-accessibility-app/
PLACEHOLDER_LLM_KEYS = {
    "",
    "ollama",
    "changeme",
    "your-api-key",
    "your_openrouter_api_key",
    "replace_me",
}
LOCAL_LLM_HOSTS = {"localhost", "127.0.0.1", "::1"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    # Database
    database_url: str = f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'pdf_accessibility.db'}"

    # File storage
    upload_dir: Path = BASE_DIR / "data" / "uploads"
    processing_dir: Path = BASE_DIR / "data" / "processing"
    output_dir: Path = BASE_DIR / "data" / "output"

    # LLM (OpenAI-compatible)
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_model: str = "google/gemini-3-flash-preview"
    llm_timeout: int = 120
    llm_strict_validation: bool = True
    auto_approve_generated_alt_text: bool = True
    auto_apply_llm_font_map: bool = True
    auto_apply_grounded_text: bool = True
    auto_apply_table_intelligence: bool = True
    assist_toc_with_llm: bool = True

    # veraPDF
    verapdf_path: str = "verapdf"
    verapdf_flavour: str = "ua1"

    # OCR
    ocr_language: str = "eng"
    ocr_rotate_pages: bool = True
    ocr_deskew: bool = True
    font_remediation_enable_force_ocr: bool = False
    font_remediation_allow_ocr_on_digital: bool = False
    font_remediation_ocr_max_pages: int = 40
    font_remediation_ocr_suspect_max_pages: int = 200

    # Subprocess timeouts (seconds)
    subprocess_timeout_ocr: int = 900  # 15 min — large scanned PDFs
    subprocess_timeout_ghostscript: int = 120  # 2 min
    subprocess_timeout_validation: int = 600  # 10 min — veraPDF on large docs
    subprocess_timeout_preview: int = 30  # single page render

    # Upload limits
    max_upload_size_bytes: int = 500 * 1024 * 1024  # 500 MB

    # Job lifecycle
    job_ttl_hours: int = 24

    # LLM retry
    llm_max_retries: int = 3
    llm_retry_backoff_base: float = 2.0

    # Dev
    debug: bool = False

    @model_validator(mode="after")
    def validate_llm_settings(self):
        base_url = self.llm_base_url.strip()
        model = self.llm_model.strip()
        api_key = self.llm_api_key.strip()

        if not base_url:
            raise ValueError("LLM_BASE_URL must be set")
        if not model:
            raise ValueError("LLM_MODEL must be set")

        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("LLM_BASE_URL must be a valid http(s) URL")

        host = (parsed.hostname or "").lower()
        is_local = host in LOCAL_LLM_HOSTS

        if self.llm_strict_validation and not is_local:
            if api_key.lower() in PLACEHOLDER_LLM_KEYS:
                raise ValueError(
                    "LLM_API_KEY is required for remote LLM endpoints "
                    "(set a real API key in .env)"
                )
            if "gemini" not in model.lower():
                raise ValueError(
                    "Remote LLM endpoint must use a Gemini model "
                    "(expected LLM_MODEL to contain 'gemini')"
                )
        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
