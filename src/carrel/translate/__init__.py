"""Translation package."""

from carrel.translate.glossary import Glossary, build_glossary
from carrel.translate.qa import QAIssue, QAReport, check_block, check_module
from carrel.translate.term_review import TermReviewer, TermReviewResult
from carrel.translate.translator import ModuleTranslation, Translator

__all__ = [
    "Glossary",
    "ModuleTranslation",
    "QAIssue",
    "QAReport",
    "TermReviewer",
    "TermReviewResult",
    "Translator",
    "build_glossary",
    "check_block",
    "check_module",
]
