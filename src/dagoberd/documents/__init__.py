"""Local document persistence and explainable transaction matching."""

from .batch import DocumentBatchService
from .extraction import LocalOCRProvider, TesseractOCRProvider, create_local_ocr_provider
from .matching import DocumentMatcher
from .models import DocumentBatchExecution, DocumentBatchPreview, DocumentFilePreview

__all__ = [
    "DocumentBatchExecution",
    "DocumentBatchPreview",
    "DocumentBatchService",
    "DocumentFilePreview",
    "DocumentMatcher",
    "LocalOCRProvider",
    "TesseractOCRProvider",
    "create_local_ocr_provider",
]
