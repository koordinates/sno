import re
import subprocess

import pygit2

from .exec import execvp
from .timestamps import tz_offset_to_minutes


def get_head_tree(repo):
    """
    Returns the tree at the current repo HEAD.
    If there is no commit at HEAD - ie, head_is_unborn - returns None.
    """
    return None if repo.head_is_unborn else repo.head.peel(pygit2.Tree)


def get_head_commit(repo):
    """
    Returns the commit at the current repo HEAD.
    If there is no commit at HEAD - ie, head_is_unborn - returns None.
    """
    return None if repo.head_is_unborn else repo.head.peel(pygit2.Commit)


def get_head_branch(repo):
    """
    Returns the branch that HEAD is currently on.
    If HEAD is detached - meaning not on any branch - returns None
    """
    return None if repo.head_is_detached else repo.references["HEAD"].target


def get_head_branch_shorthand(repo):
    """
    Returns the shorthand for the branch that HEAD is currently on.
    If HEAD is detached - meaning not on any branch - returns None
    """
    return (
        None
        if repo.head_is_detached
        else repo.references["HEAD"].target.rsplit("/", 1)[-1]
    )


_GIT_VAR_OUTPUT_RE = re.compile(
    r"^(?P<name>.*) <(?P<email>[^>]*)> (?P<time>\d+) (?P<offset>[+-]?\d+)$"
)


def _signature(repo, var_name, **overrides):
    # 'git var' lets us use the environment variables to
    # control the user info, e.g. GIT_AUTHOR_DATE.
    # libgit2/pygit2 doesn't handle those env vars at all :(
    output = subprocess.check_output(
        ["git", "var", var_name], cwd=repo.path, encoding="utf8"
    )
    m = _GIT_VAR_OUTPUT_RE.match(output)
    kwargs = m.groupdict()
    kwargs["time"] = int(kwargs["time"])
    kwargs["offset"] = tz_offset_to_minutes(kwargs["offset"])
    kwargs.update(overrides)
    return pygit2.Signature(**kwargs)


def author_signature(repo, **overrides):
    return _signature(repo, "GIT_AUTHOR_IDENT", **overrides)


def committer_signature(repo, **overrides):
    return _signature(repo, "GIT_COMMITTER_IDENT", **overrides)


def gc(repo, *args, use_subprocess=True):
    """
    Runs git-gc on the repository
    """
    args = ["git", "-C", repo.path, "gc", *args]
    if use_subprocess:
        subprocess.run(args)
    else:
        execvp("git", args)
