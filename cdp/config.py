"""Application configuration using Pydantic BaseSettings."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central configuration for the Customer Data Platform."""

    # Application
    APP_NAME: str = "Customer Data Platform"
    DEBUG: bool = False

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://cdp:cdp@localhost:5432/cdp"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Authentication / JWT
    SECRET_KEY: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # AI / ML
    AI_MODEL_PATH: str = "./models"

    # Cookie / Tracking
    COOKIE_DOMAIN: str = "localhost"
    COOKIE_MAX_AGE: int = 365 * 24 * 60 * 60  # 1 year in seconds
    CONSENT_REQUIRED: bool = True

    # SMTP / Email
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""

    # AWS
    AWS_ACCESS_KEY: str = ""
    AWS_SECRET_KEY: str = ""
    AWS_REGION: str = "us-east-1"
    S3_BUCKET: str = ""

    # Celery
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
    }


settings = Settings()
