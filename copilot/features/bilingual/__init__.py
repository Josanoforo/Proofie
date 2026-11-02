"""Spanish version of the statements for a Mexican seller, inside the approval (behind COPILOT_FEATURE_BILINGUAL)."""


def view(db_path, package_id):
    """The interface the package page's include hook calls (`from copilot.features import bilingual`): see
    service.view. The import stays inside, so importing the package alone loads nothing else."""
    from copilot.features.bilingual import service
    return service.view(db_path, package_id)
