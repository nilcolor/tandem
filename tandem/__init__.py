__version__ = "0.1.0.dev0"


class TandemError(Exception):
    """A user-facing failure: the CLI prints `tandem: <message>` and exits 1."""
