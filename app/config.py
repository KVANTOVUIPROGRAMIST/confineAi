import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')


@dataclass
class Settings:
    database_url: str = os.getenv('DATABASE_URL', 'sqlite:///./data/confine.db')
    environment: str = os.getenv('APP_ENV', 'development')
    app_url: str = os.getenv('APP_URL', os.getenv('RENDER_EXTERNAL_URL', 'http://localhost:8000')).rstrip('/')
    ai_provider: str = os.getenv('AI_PROVIDER', 'gemini')
    ai_model: str = os.getenv('AI_MODEL', 'gemini-3.5-flash-lite')
    assignment_model: str = os.getenv('AI_ASSIGNMENT_MODEL', 'gemini-3.8-flash' if os.getenv('AI_PROVIDER', 'gemini') == 'gemini' else os.getenv('AI_MODEL', 'gpt-5-mini'))
    gemini_key: str = os.getenv('GEMINI_API_KEY', '')
    openai_key: str = os.getenv('OPENAI_API_KEY', '')
    beta_enabled: bool = os.getenv('BETA_ENABLED', 'true').lower() == 'true'
    beta_credits: int = int(os.getenv('BETA_MONTHLY_RESPONSES', '300'))
    stripe_key: str = os.getenv('STRIPE_SECRET_KEY', '')
    stripe_webhook: str = os.getenv('STRIPE_WEBHOOK_SECRET', '')
    stripe_price: str = os.getenv('STRIPE_STUDENT_PRICE_ID', '')
    stripe_topup: str = os.getenv('STRIPE_TOPUP_PRICE_ID', '')
    max_upload_bytes: int = 10 * 1024 * 1024
    max_storage_bytes: int = 100 * 1024 * 1024
    max_pages: int = 60
    max_questions: int = 30
    max_material_chars: int = 250_000

    @property
    def ai_ready(self):
        return bool(self.gemini_key if self.ai_provider == 'gemini' else self.openai_key)

    @property
    def billing_ready(self):
        return bool(not self.beta_enabled and self.stripe_key and self.stripe_webhook and self.stripe_price)


settings = Settings()
