import json
import re
import shlex
import time

import pytest

import sno
from sno.exceptions import (
    INVALID_ARGUMENT,
    NO_CHANGES,
    NO_DATA,
    NO_REPOSITORY,
    SCHEMA_VIOLATION,
)
from sno.commit import fallback_editor
from sno.sqlalchemy import gpkg_engine
from sno.repo import SnoRepo


H = pytest.helpers.helpers()


@pytest.mark.parametrize(
    "partial",
    [pytest.param(False, id=""), pytest.param(True, id="partial")],
)
@pytest.mark.parametrize(
    "archive,layer",
    [
        pytest.param("points", H.POINTS.LAYER, id="points"),
        pytest.param("polygons", H.POLYGONS.LAYER, id="polygons"),
        pytest.param("table", H.TABLE.LAYER, id="table"),
    ],
)
def test_commit(
    archive,
    layer,
    partial,
    data_working_copy,
    cli_runner,
    request,
    edit_points,
    edit_polygons,
    edit_table,
):
    """ commit outstanding changes from the working copy """

    with data_working_copy(archive) as (repo_dir, wc_path):
        # empty
        r = cli_runner.invoke(["commit", "-m", "test-commit-empty"])
        assert r.exit_code == NO_CHANGES, r
        assert r.stderr.splitlines() == ["Error: No changes to commit"]

        # empty
        r = cli_runner.invoke(["commit", "-m", "test-commit-empty", "--allow-empty"])
        assert r.exit_code == 0, r

        # make some changes
        with gpkg_engine(wc_path).connect() as db:
            try:
                edit_func = locals()[f"edit_{archive}"]
                pk_del = edit_func(db)
            except KeyError:
                raise NotImplementedError(f"No edit_{archive}")

        print(f"deleted fid={pk_del}")

        repo = SnoRepo(repo_dir)
        dataset = repo.datasets()[layer]

        wc = repo.working_copy
        original_change_count = wc.tracking_changes_count(dataset)

        if partial:
            r = cli_runner.invoke(
                ["commit", "-m", "test-commit-1", "-o", "json", f"{layer}:{pk_del}"]
            )
        else:
            r = cli_runner.invoke(["commit", "-m", "test-commit-1", "-o", "json"])

        assert r.exit_code == 0, r
        commit_id = json.loads(r.stdout)["sno.commit/v1"]["commit"]
        print("commit:", commit_id)

        assert str(repo.head.target) == commit_id
        commit = repo.head_commit
        assert commit.message == "test-commit-1"
        assert time.time() - commit.commit_time < 3

        tree = repo.head_tree
        assert dataset.encode_1pk_to_path(pk_del) not in tree

        wc.assert_db_tree_match(tree)
        change_count = wc.tracking_changes_count(dataset)

        if partial:
            # All but one change should still be in the tracking table
            assert change_count == original_change_count - 1

            # Changes should still be visible in the working copy:
            r = cli_runner.invoke(["diff", "--exit-code"])
            assert r.exit_code == 1, r
            assert r.stdout != ""

        else:
            assert (
                change_count == 0
            ), f"Changes still listed in {wc.TRACKING_TABLE} after full commit"

            r = cli_runner.invoke(["diff", "--exit-code"])
            assert r.exit_code == 0, r
            assert r.stdout == ""


def test_tag(data_working_copy, cli_runner):
    """ review commit history """
    with data_working_copy("points") as (repo_dir, wc):
        # create a tag
        r = cli_runner.invoke(["tag", "version1"])
        assert r.exit_code == 0, r

        repo = SnoRepo(repo_dir)
        assert "refs/tags/version1" in repo.references
        ref = repo.lookup_reference_dwim("version1")
        assert ref.target.hex == H.POINTS.HEAD_SHA


def test_commit_message(
    data_working_copy, cli_runner, monkeypatch, tmp_path, edit_points
):
    """ commit message handling """
    editor_in = None
    editor_out = None
    editor_cmd = None

    def monkey_editor(cmdline):
        nonlocal editor_cmd, editor_in
        editor_cmd = cmdline
        print("EDITOR", cmdline)
        editmsg_file = shlex.split(cmdline)[-1]
        with open(editmsg_file, "r+", encoding="utf-8") as ef:
            editor_in = ef.read()
            if editor_out:
                ef.seek(0)
                ef.truncate()
                ef.write(editor_out)
                return 0
            else:
                assert False, "Didn't expect editor to launch"

    monkeypatch.setattr(sno.commit, "run_editor_cmd", monkey_editor)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("GIT_EDITOR", raising=False)

    with data_working_copy("points") as (repo_dir, wc_path):
        repo = SnoRepo(repo_dir)

        def last_message():
            return repo.head_commit.message

        # normal
        r = cli_runner.invoke(
            ["commit", "--allow-empty", "-m", "the messagen\n\n\n\n\n"]
        )
        assert r.exit_code == 0, r
        assert last_message() == "the messagen"

        # E: empty
        r = cli_runner.invoke(["commit", "--allow-empty", "-m", ""])
        assert r.exit_code == INVALID_ARGUMENT, r

        # file
        f_commit_message = str(tmp_path / "commit-message.txt")
        with open(f_commit_message, mode="w", encoding="utf8") as f:
            f.write("\ni am a message\n\n\n")
            f.flush()

        r = cli_runner.invoke(
            ["commit", "--allow-empty", f"--message=@{f_commit_message}"]
        )
        assert r.exit_code == 0, r
        assert last_message() == "i am a message"

        # E: conflict
        r = cli_runner.invoke(
            ["commit", "--allow-empty", f"--message=@{f_commit_message}", "-m", "foo"]
        )
        assert r.exit_code == 0, r
        assert last_message() == "i am a message\n\nfoo"

        # multiple
        r = cli_runner.invoke(
            [
                "commit",
                "--allow-empty",
                "-m",
                "one",
                "-m",
                "two\nthree\n",
                "-m",
                "four\n\n",
            ]
        )
        assert r.exit_code == 0, r
        assert last_message() == "one\n\ntwo\nthree\n\nfour"

        # default editor

        # make some changes
        with gpkg_engine(wc_path).connect() as db:
            edit_points(db)

        editor_out = "I am a message\n#of hope, and\nof warning\n\t\n"
        r = cli_runner.invoke(["commit"])
        assert r.exit_code == 0, r
        editmsg_path = str(repo.gitdir_file("COMMIT_EDITMSG"))
        assert re.match(
            rf'{fallback_editor()} "?{re.escape(editmsg_path)}"?$', editor_cmd
        )
        assert editor_in.splitlines() == [
            "",
            "# Please enter the commit message for your changes. Lines starting",
            "# with '#' will be ignored, and an empty message aborts the commit.",
            "#",
            "# On branch master",
            "#",
            "# Changes to be committed:",
            "#",
            "#   nz_pa_points_topo_150k:",
            "#     feature:",
            "#       1 inserts",
            "#       2 updates",
            "#       5 deletes",
            "#",
        ]

        print(last_message())
        assert last_message() == "I am a message\nof warning"

        monkeypatch.setenv("EDITOR", "/path/to/some/editor -abc")
        editor_out = "sqwark 🐧\n"
        r = cli_runner.invoke(["commit", "--allow-empty"])
        assert r.exit_code == 0, r
        editmsg_path = str(repo.gitdir_file("COMMIT_EDITMSG"))
        assert re.match(
            rf'/path/to/some/editor -abc "?{re.escape(editmsg_path)}"?$', editor_cmd
        )
        assert editor_in == (
            "\n"
            "# Please enter the commit message for your changes. Lines starting\n"
            "# with '#' will be ignored, and an empty message aborts the commit.\n"
            "#\n"
            "# On branch master\n"
            "#\n"
            "# Changes to be committed:\n"
            "#\n"
            "#   No changes (empty commit)\n"
            "#\n"
        )
        print(last_message())
        assert last_message() == "sqwark 🐧"


def test_empty(tmp_path, cli_runner, chdir):
    repo_path = tmp_path / "one.sno"

    # empty repo
    r = cli_runner.invoke(["init", str(repo_path)])
    assert r.exit_code == 0, r
    with chdir(repo_path):
        r = cli_runner.invoke(["commit", "--allow-empty"])
        assert r.exit_code == NO_DATA, r
        assert "Empty repository" in r.stderr

    # empty dir
    empty_path = tmp_path / "two"
    empty_path.mkdir()
    with chdir(empty_path):
        r = cli_runner.invoke(["commit", "--allow-empty"])
        assert r.exit_code == NO_REPOSITORY, r
        assert "not an existing sno repository" in r.stderr


def test_commit_user_info(tmp_path, cli_runner, chdir, data_working_copy):
    with data_working_copy("points") as (repo_dir, wc_path):
        repo = SnoRepo(repo_dir)

        # normal
        r = cli_runner.invoke(
            ["commit", "--allow-empty", "-m", "test"],
            env={
                "GIT_AUTHOR_DATE": "1000000000 +1230",
                "GIT_AUTHOR_NAME": "bob",
                "GIT_AUTHOR_EMAIL": "user@example.com",
            },
        )
        assert r.exit_code == 0, r

        author = repo.head_commit.author
        assert author.name == "bob"
        assert author.email == "user@example.com"
        assert author.time == 1000000000
        assert author.offset == 750


def test_commit_schema_violation(cli_runner, data_working_copy):
    with data_working_copy("points") as (repo_dir, wc_path):
        with gpkg_engine(wc_path).connect() as db:
            db.execute(f"""UPDATE {H.POINTS.LAYER} SET geom="text" WHERE fid=1;""")
            db.execute(f"UPDATE {H.POINTS.LAYER} SET t50_fid=123456789012 WHERE fid=2;")
            db.execute(
                f"""UPDATE {H.POINTS.LAYER} SET macronated="kinda" WHERE fid=3;"""
            )

        r = cli_runner.invoke(["commit", "-m", "test"])
        assert r.exit_code == SCHEMA_VIOLATION, r.stderr
        assert r.stderr.splitlines() == [
            "nz_pa_points_topo_150k: In column 'geom' value 'text' doesn't match schema type geometry",
            "nz_pa_points_topo_150k: In column 't50_fid' value 123456789012 does not fit into an int32: -2147483648 to 2147483647",
            "nz_pa_points_topo_150k: In column 'macronated' value 'kinda' exceeds limit of 1 characters",
            "Error: Schema violation - values do not match schema",
        ]
