"""Re-export every model so ``from app.models import University`` works
everywhere (and so Alembic autogenerate sees them). Models are mapped
column-for-column to the existing Postgres schema; we never own DDL — the
existing Drizzle migrations remain the source of truth.
"""
from app.models.academic_level_option import AcademicLevelOption
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
from app.models.scrape_runtime import ScrapeRuntimeJob, ScrapeRuntimeLog
from app.models.scraped_course import ScrapedCourse
from app.models.scraping_change import ScrapingChange
from app.models.scraping_job import ScrapingJob
from app.models.university import University

__all__ = [
    "AcademicLevelOption",
    "AcademicRequirement",
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
    "ScrapeRuntimeJob",
    "ScrapeRuntimeLog",
    "ScrapedCourse",
    "ScrapedFieldEvidence",
    "ScrapingChange",
    "ScrapingJob",
    "University",
]
