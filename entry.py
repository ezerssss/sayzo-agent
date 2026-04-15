"""PyInstaller entry point — runs sayzo_agent as a package."""
from sayzo_agent.__main__ import cli

if __name__ == "__main__":
    cli()
