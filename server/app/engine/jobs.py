"""Compatibility re-export for the job repository.

New code should import from ``app.repositories.jobs_repository``.
"""

from app.repositories.jobs_repository import JobRepository

# Backwards-compatible aliases
JobRegistry = JobRepository

# Global singleton kept for legacy callers during the refactor.
# This will be removed once all engine modules use dependency injection.
jobs_registry = JobRepository()
