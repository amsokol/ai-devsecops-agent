"""AI DevSecOps agent: deterministic core, knowledge from a versioned library."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ai-devsecops-agent")
except PackageNotFoundError:  # running from a source tree without an installed distribution
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
