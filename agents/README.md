# SDLC Specialized Pipeline Agents

This directory stores the specific logic layouts, prompts, and access controls for the respective personas orchestrating the SDLC pipeline loops.

No direct builds take place here. Instead, these configurations are dynamically mounted by the core backend (`sdlc-factory`) when state progressions switch between domains (e.g. `planner -> coder -> tester`).

## Adding New Roles
To add a new AI persona, define a new sub-folder containing its localized prompts and tool-access JSON constraints, then ensure the corresponding pipeline hook in `state.py` matches the new directory's schema mapping.
