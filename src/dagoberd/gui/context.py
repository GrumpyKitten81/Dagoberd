"""Per-request GUI service construction."""

from __future__ import annotations

from dagoberd.config import LoadedConfig
from dagoberd.documents.batch import DocumentBatchService
from dagoberd.documents.extraction import create_local_ocr_provider
from dagoberd.documents.matching import DocumentMatcher
from dagoberd.ledger.classification import ClassificationService
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.organization import OrganizationService
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.llm import LLMProvider, create_llm_provider
from dagoberd.profiles import install_builtin_profiles
from dagoberd.reporting.dashboard import DashboardService
from dagoberd.secrets import SecretStore
from dagoberd.services.import_service import ImportService
from dagoberd.services.llm_preview import LLMPreviewService


class GuiContext:
    def __init__(self, loaded: LoadedConfig) -> None:
        self.loaded = loaded
        install_builtin_profiles(self.repository())

    def repository(self) -> LedgerRepository:
        return LedgerRepository(LedgerDatabase(self.loaded.config.database.path))

    def organization(self) -> OrganizationService:
        return OrganizationService(self.repository())

    def dashboard(self) -> DashboardService:
        return DashboardService(self.repository(), self.loaded.config.reporting.base_currency)

    def imports(self) -> ImportService:
        config = self.loaded.config.imports
        return ImportService(
            self.repository(), config.mappings, config.profiles, config.max_file_size_mib
        )

    def documents(self) -> DocumentMatcher:
        config = self.loaded.config.documents
        return DocumentMatcher(
            self.repository(),
            config.storage_dir,
            auto_match_threshold=config.auto_match_threshold,
            auto_match_minimum_gap=config.auto_match_minimum_gap,
            extract_pdf_text=config.extract_pdf_text,
        )

    def document_batches(self) -> DocumentBatchService:
        config = self.loaded.config.documents
        return DocumentBatchService(
            self.repository(),
            config.storage_dir,
            max_file_size_bytes=self.loaded.config.imports.max_file_size_mib * 1024 * 1024,
            ocr_provider=create_local_ocr_provider(
                enabled=config.local_ocr_enabled,
                executable=config.local_ocr_executable,
                pdf_renderer=config.local_ocr_pdf_renderer,
                languages=config.local_ocr_languages,
                timeout_seconds=config.local_ocr_timeout_seconds,
            ),
        )

    def classification(self) -> ClassificationService:
        """DESIGN: cloud LLM stays behind the two-step preview, exactly as in the CLI."""
        if self.loaded.config.llm.enabled and self.loaded.config.llm.mode == "cloud":
            raise ValueError(
                "Cloud LLM classification requires a two-step preview "
                "(classification/llm/preview, then run)"
            )
        return ClassificationService(
            self.repository(),
            self.llm_provider(),
            self.loaded.config.privacy,
            self.loaded.config.llm.model,
        )

    def classification_read_only(self) -> ClassificationService:
        return ClassificationService(
            self.repository(),
            self.llm_provider(),
            self.loaded.config.privacy,
            self.loaded.config.llm.model,
        )

    def llm_provider(self) -> LLMProvider | None:
        return create_llm_provider(
            self.loaded.config,
            SecretStore(dotenv_path=self.loaded.project_path.with_name(".env")),
        )

    def llm_preview(self, *, with_provider: bool) -> LLMPreviewService:
        return LLMPreviewService(
            self.repository(),
            self.llm_provider() if with_provider else None,
            self.loaded.config.llm,
            self.loaded.config.privacy,
        )
