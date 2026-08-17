"""The console entry point. `pyproject.toml` names it as `narratarr`.

The container does not use this module. The image runs uvicorn against the
`create_app` factory directly. This entry point exists so that a person who
installs the package with pip can start the same application with one word.
"""

from __future__ import annotations


def main() -> None:
    """Start the Narratarr web application."""
    import uvicorn

    from narratarr.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "narratarr.api:create_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - the container publishes one port
        port=settings.port,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    main()
