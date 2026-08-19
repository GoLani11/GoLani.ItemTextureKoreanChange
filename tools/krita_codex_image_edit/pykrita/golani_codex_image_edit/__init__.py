try:
    import krita  # type: ignore  # noqa: F401
except ModuleNotFoundError:
    # Keep the protocol and pixel core importable for tests outside Krita.
    pass
else:
    from . import docker  # noqa: F401 - importing registers the Krita docker
