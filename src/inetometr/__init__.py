"""Inetometr package."""

__all__ = ["main"]


def main() -> None:
    """Консольная точка входа inetometr: запуск сервера."""
    from .server import main as server_main
    server_main()
