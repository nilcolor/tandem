import os

_PACKAGE_DIR = os.path.dirname(os.path.realpath(__file__))
REPO_ROOT = os.path.dirname(_PACKAGE_DIR)


def tandem_home():
    override = os.environ.get("TANDEM_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".tandem")


def shim_path():
    # The checkout's own shim, not the ~/.local/bin symlink: generated hook
    # commands must keep working whether or not `make install` was run.
    return os.path.join(REPO_ROOT, "bin", "tandem")


def sessions_dir():
    return os.path.join(tandem_home(), "sessions")


def repos_dir():
    return os.path.join(tandem_home(), "repos")
