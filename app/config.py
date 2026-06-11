from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    internal_api_secret: str
    allowed_origins: str = "http://localhost:3000"

    supabase_url: str
    supabase_service_role_key: str

    gemini_api_key: str

    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str
    google_students_folder_id: str
    google_calendar_id: str
    google_lec_topic1_file_id: str = ""

    langchain_tracing: bool = False
    langsmith_endpoint: str = ""
    langsmith_api_key: str = ""
    langsmith_project: str = "tuition-agent"


settings = Settings()
