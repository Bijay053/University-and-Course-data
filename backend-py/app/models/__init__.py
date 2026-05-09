"""Re-export every model so ``from app.models import University`` works
everywhere (and so Alembic autogenerate sees them). Models are mapped
column-for-column to the existing Postgres schema; we never own DDL — the
existing Drizzle migrations remain the source of truth.
"""
from app.models.academic_level_option import AcademicLevelOption
from app.models.discovery_failure_alert import DiscoveryFailureAlert
from app.models.central_page_cache import CentralPageCache
from app.models.academic_requirement import AcademicRequirement
from app.models.acronym import CourseAcronymOption
from app.models.assessment_note import AssessmentNote
from app.models.audit import CourseAuditLog
from app.models.bulk_session import BulkSession
from app.models.course import Course
from app.models.english_requirement import EnglishRequirement
from app.models.evidence import ScrapedFieldEvidence
from app.models.fee import Fee
from app.models.field_approval import CourseFieldApproval
from app.models.field_conflict import FieldConflict
from app.models.import_job import ImportJob
from app.models.intake import Intake
from app.models.scholarship import Scholarship
from app.models.scrape_feedback import ScrapeFeedback
from app.models.scrape_run_alert import ScrapeRunAlert
from app.models.scrape_run_metrics import ScrapeRunMetrics
from app.models.scrape_run_summary import ScrapeRunSummary
from app.models.gemini_call_log import GeminiCallLog
from app.models.scrape_runtime import ScrapeRuntimeJob, ScrapeRuntimeLog
from app.models.scraped_course import ScrapedCourse
from app.models.scraping_change import ScrapingChange
from app.models.scraping_job import ScrapingJob
from app.models.university import University
from app.models.university_field_baseline import UniversityFieldBaseline
from app.models.user import PasswordResetToken, User, UserPermission

__all__ = [
    "AcademicLevelOption",
    "DiscoveryFailureAlert",
    "GeminiCallLog",
    "AcademicRequirement",
    "CentralPageCache",
    "AssessmentNote",
    "BulkSession",
    "Course",
    "CourseAcronymOption",
    "CourseAuditLog",
    "CourseFieldApproval",
    "EnglishRequirement",
    "Fee",
    "FieldConflict",
    "ImportJob",
    "Intake",
    "Scholarship",
    "ScrapeFeedback",
    "ScrapeRunAlert",
    "ScrapeRunMetrics",
    "ScrapeRunSummary",
    "ScrapeRuntimeJob",
    "ScrapeRuntimeLog",
    "ScrapedCourse",
    "ScrapedFieldEvidence",
    "ScrapingChange",
    "ScrapingJob",
    "University",
    "UniversityFieldBaseline",
]
