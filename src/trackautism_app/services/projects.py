"""Project save/load orchestration for local standalone use."""

from trackautism_app.models.project_state import ProjectState


def new_project(name: str = "Untitled analysis") -> ProjectState:
    """Create a minimal in-memory project state."""
    return ProjectState(project_name=name)
