"""Project state models shared by desktop and server modes."""

from dataclasses import dataclass


@dataclass(slots=True)
class ProjectState:
    """Serializable project state for save/resume workflows.

    This intentionally uses only the standard library for now so the desktop
    prototype can start without requiring third-party packages.
    """

    project_name: str = "Untitled analysis"
    data_root: str | None = None
    current_step: str = "home"
