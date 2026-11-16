"""Nano Code's public, versioned application package."""

__version__ = "0.6.0"


def __getattr__(name):
    if name == "Session":
        from .runtime import Session
        return Session
    raise AttributeError(name)


__all__ = ["Session", "__version__"]
