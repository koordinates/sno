import subprocess
from pathlib import PureWindowsPath

import pytest

from sno.clone import get_directory_from_url
from sno.sqlalchemy import gpkg_engine
from sno.repo import SnoRepo


H = pytest.helpers.helpers()


def test_clone_empty_repo(tmp_path, cli_runner, chdir):
    src = tmp_path / "src"
    subprocess.check_call(["git", "init", "--bare", str(src)])
    r = cli_runner.invoke(["clone", str(src), tmp_path / "dest"])
    assert r.exit_code == 0


def test_get_directory_from_url():
    assert get_directory_from_url("sno@example.com:def/abc") == "abc"
    assert get_directory_from_url("sno@example.com:abc") == "abc"
    assert get_directory_from_url("https://example.com/def/abc") == "abc"
    assert get_directory_from_url("https://example.com/abc") == "abc"
    assert get_directory_from_url("abc") == "abc"
    assert get_directory_from_url("abc/") == "abc"
    assert get_directory_from_url("def/abc") == "abc"
    assert get_directory_from_url("def/abc/") == "abc"
    assert get_directory_from_url("/def/abc") == "abc"
    assert get_directory_from_url("/def/abc/") == "abc"
    assert get_directory_from_url(PureWindowsPath("C:/def/abc")) == "abc"
    assert get_directory_from_url(PureWindowsPath("C:\\def\\abc")) == "abc"
    assert get_directory_from_url(PureWindowsPath("C:\\def\\abc\\")) == "abc"
    assert get_directory_from_url(PureWindowsPath("C:\\def\\abc/")) == "abc"


@pytest.mark.parametrize(
    "working_copy",
    [
        pytest.param(True, id="with-wc"),
        pytest.param(False, id="without-wc"),
    ],
)
@pytest.mark.parametrize(
    "branch_name,branch_ref",
    [
        ("mytag", "HEAD^"),
        ("master", None),
    ],
)
def test_clone(
    working_copy,
    data_archive,
    tmp_path,
    cli_runner,
    chdir,
    branch_name,
    branch_ref,
):
    with data_archive("points") as remote_path:
        if branch_ref:
            # add a tag
            with chdir(remote_path):
                subprocess.check_output(["git", "branch", branch_name, branch_ref])
        with chdir(tmp_path):
            args = [
                "clone",
                remote_path,
                ("--checkout" if working_copy else "--no-checkout"),
            ]
            if branch_ref:
                args.append(f"--branch={branch_name}")
            r = cli_runner.invoke(args)

            repo_path = tmp_path / "points"
            assert repo_path.is_dir()

        r = subprocess.check_output(
            ["git", "-C", str(repo_path), "config", "--local", "--list"]
        )
        print("git config file:", r.decode("utf-8").splitlines())

        repo = SnoRepo(repo_path)
        assert not repo.is_empty
        assert repo.head.name == f"refs/heads/{branch_name}"

        if branch_ref == "HEAD^":
            assert repo.head_commit.hex == H.POINTS.HEAD1_SHA
        else:
            assert repo.head_commit.hex == H.POINTS.HEAD_SHA

        branch = repo.branches.local[repo.head.shorthand]
        assert branch.is_head()
        assert branch.upstream_name == f"refs/remotes/origin/{branch_name}"

        assert len(repo.remotes) == 1
        remote = repo.remotes["origin"]
        assert remote.url == str(remote_path)
        assert remote.fetch_refspecs == ["+refs/heads/*:refs/remotes/origin/*"]

        wc = repo_path / f"{repo_path.stem}.gpkg"
        if working_copy:
            assert wc.exists() and wc.is_file()

            table = H.POINTS.LAYER
            assert repo.config["sno.repository.version"] == "2"
            assert repo.config["sno.workingcopy.path"] == wc.name

            with gpkg_engine(wc).connect() as db:
                nrows = db.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0]
                assert nrows > 0

                wc_tree_id = db.execute(
                    """SELECT value FROM "gpkg_sno_state" WHERE table_name='*' AND key='tree';""",
                ).fetchone()[0]
                assert wc_tree_id == repo.head_tree.hex
        else:
            assert not wc.exists()


def test_fetch(
    data_archive_readonly,
    data_working_copy,
    cli_runner,
    insert,
    tmp_path,
    request,
):
    with data_working_copy("points") as (path1, wc):
        subprocess.run(["git", "init", "--bare", str(tmp_path)], check=True)

        r = cli_runner.invoke(["remote", "add", "myremote", tmp_path])
        assert r.exit_code == 0, r

        with gpkg_engine(wc).connect() as db:
            commit_id = insert(db)

        r = cli_runner.invoke(["push", "--set-upstream", "myremote", "master"])
        assert r.exit_code == 0, r

    with data_working_copy("points") as (path2, wc):
        repo = SnoRepo(path2)
        h = repo.head.target.hex

        r = cli_runner.invoke(["remote", "add", "myremote", tmp_path])
        assert r.exit_code == 0, r

        r = cli_runner.invoke(["fetch", "myremote"])
        assert r.exit_code == 0, r

        H.git_graph(request, "post-fetch")

        assert repo.head.name == "refs/heads/master"
        assert repo.head.target.hex == h

        remote_branch = repo.lookup_reference_dwim("myremote/master")
        assert remote_branch.target.hex == commit_id

        fetch_head = repo.lookup_reference("FETCH_HEAD")
        assert fetch_head.target.hex == commit_id

        # merge
        r = cli_runner.invoke(["merge", "myremote/master"])
        assert r.exit_code == 0, r

        assert repo.head.name == "refs/heads/master"
        assert repo.head.target.hex == commit_id
        commit = repo.head_commit
        assert len(commit.parents) == 1
        assert commit.parents[0].hex == h


def test_pull(
    data_archive_readonly,
    data_working_copy,
    cli_runner,
    insert,
    tmp_path,
    request,
    chdir,
):
    with data_working_copy("points") as (path1, wc1), data_working_copy("points") as (
        path2,
        wc2,
    ):
        with chdir(path1):
            subprocess.run(["git", "init", "--bare", str(tmp_path)], check=True)
            r = cli_runner.invoke(["remote", "add", "origin", tmp_path])
            assert r.exit_code == 0, r

            r = cli_runner.invoke(["push", "--set-upstream", "origin", "master"])
            assert r.exit_code == 0, r

        with chdir(path2):
            r = cli_runner.invoke(["remote", "add", "origin", tmp_path])
            assert r.exit_code == 0, r

            r = cli_runner.invoke(["fetch", "origin"])
            assert r.exit_code == 0, r

            r = cli_runner.invoke(["branch", "--set-upstream-to=origin/master"])
            assert r.exit_code == 0, r

        with chdir(path1):
            with gpkg_engine(wc1).connect() as db:
                commit_id = insert(db)

            r = cli_runner.invoke(["push"])
            assert r.exit_code == 0, r

        with chdir(path2):
            repo = SnoRepo(path2)
            h = repo.head.target.hex

            r = cli_runner.invoke(["pull"])
            assert r.exit_code == 0, r

            H.git_graph(request, "post-pull")

            remote_branch = repo.lookup_reference_dwim("origin/master")
            assert remote_branch.target.hex == commit_id

            assert repo.head.name == "refs/heads/master"
            assert repo.head.target.hex == commit_id
            commit = repo.head_commit
            assert len(commit.parents) == 1
            assert commit.parents[0].hex == h

            # pull again / no-op
            r = cli_runner.invoke(["branch", "--unset-upstream"])
            assert r.exit_code == 0, r

            r = cli_runner.invoke(["pull"])
            assert r.exit_code == 0, r
            assert repo.head.target.hex == commit_id
