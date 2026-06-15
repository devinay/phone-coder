"""Tool modules for voice coding cockpit.

Each tool module provides a factory function that creates tool functions
for a specific domain. The factory pattern allows dependency injection
and avoids circular imports.

Available factories:
- create_shell_tools(router): run_command, send_input, capture_output, find_directory
- create_web_tools(): web_search, fetch_url
- create_image_tools(...): search_images, select_image, resize_image, etc.
- create_diagram_tools(...): move_diagram, enter_diagram_focus, exit_diagram_focus, revert_diagram_edit
- create_doc_tools(...): list_doc_projects, enter_doc_mode, exit_doc_mode, read_doc, write_to_doc, edit_doc, insert_diagram, update_diagram
"""

from .diagram_tools import create_diagram_tools
from .doc_tools import create_doc_tools
from .image_tools import create_image_tools
from .shell_tools import create_shell_tools
from .web_tools import create_web_tools

__all__ = [
    "create_shell_tools",
    "create_web_tools",
    "create_image_tools",
    "create_diagram_tools",
    "create_doc_tools",
]
