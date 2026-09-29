"""Agent runtime perimeter (M6).

This package holds the prompt folders (handbook + 11 class folders) and the
roster seeder.  The Agent Manager itself is a deterministic L4 service and
lives in ``atc.core.agent_manager``; agents never import this package — they
only ever see the files here and their own workspace.
"""

from .seed import AGENT_CLASSES, HANDBOOK, seed_agent_classes, write_prompt_folders

__all__ = ["AGENT_CLASSES", "HANDBOOK", "seed_agent_classes", "write_prompt_folders"]
