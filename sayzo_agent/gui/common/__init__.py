"""Shared scaffolding for the pywebview-hosted GUI surfaces.

Both ``gui/setup`` (first-run wizard) and ``gui/settings`` (Settings window)
host React inside pywebview and need the same handful of helpers — asset
path resolution, PKCE login orchestration. This package owns those, so the
two bridges stay thin and the contract for adding a third pywebview window
in the future is one import away.
"""
