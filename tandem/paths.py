import os
import shlex

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


def shim_command(*args):
    """A shell command line running the shim with `args`, for generated hook configs."""
    shim = "'%s'" % shim_path().replace("'", "'\\''")
    return " ".join([shim] + [shlex.quote(a) for a in args])


def make_private_dir(path):
    """Create `path` (a dir under the home) and every dir down to it, owner-only when tandem creates them.

    The spool holds every prompt typed into a console, briefs and messages.
    """
    home = tandem_home()
    os.makedirs(home, 0o700, exist_ok=True)
    current = home
    for part in os.path.relpath(path, home).split(os.sep):
        if part in ("", "."):
            continue
        current = os.path.join(current, part)
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            pass


def sessions_dir():
    return os.path.join(tandem_home(), "sessions")


def repos_dir():
    return os.path.join(tandem_home(), "repos")
