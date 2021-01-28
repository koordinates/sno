import pytest


from sno.exceptions import INVALID_OPERATION, NO_BRANCH, NO_COMMIT
from sno.sqlalchemy import gpkg_engine
from sno.repo import SnoRepo
from sno.structs import CommitWithReference


@pytest.mark.parametrize(
    "working_copy",
    [
        pytest.param(True, id="with-wc"),
        pytest.param(False, id="without-wc"),
    ],
)
def test_checkout_branches(data_archive, cli_runner, chdir, tmp_path, working_copy):
    with data_archive("points") as remote_path:

        r = cli_runner.invoke(["checkout", "-b", "one"])
        assert r.exit_code == 0, r.stderr
        r = cli_runner.invoke(["checkout", "HEAD^"])
        assert r.exit_code == 0, r.stderr
        r = cli_runner.invoke(["switch", "--create", "two"])
        assert r.exit_code == 0, r.stderr

        r = cli_runner.invoke(["checkout", "one", "-b", "three"])
        assert r.exit_code == 0, r.stderr
        r = cli_runner.invoke(["switch", "two", "--create", "four"])
        assert r.exit_code == 0, r.stderr

        repo = SnoRepo(remote_path)
        one = CommitWithReference.resolve(repo, "one")
        two = CommitWithReference.resolve(repo, "two")
        three = CommitWithReference.resolve(repo, "three")
        four = CommitWithReference.resolve(repo, "four")

        assert one.commit.hex == three.commit.hex
        assert two.commit.hex == four.commit.hex

        assert one.commit.hex != two.commit.hex

        wc_flag = "--checkout" if working_copy else "--no-checkout"
        r = cli_runner.invoke(["clone", remote_path, tmp_path, wc_flag])
        repo = SnoRepo(tmp_path)

        head = CommitWithReference.resolve(repo, "HEAD")

        with chdir(tmp_path):

            r = cli_runner.invoke(["branch"])
            assert r.exit_code == 0, r.stderr
            assert r.stdout.splitlines() == ["* four"]

            # Commit hex is not a branch name, can't be switched:
            r = cli_runner.invoke(["switch", head.commit.hex])
            assert r.exit_code == NO_BRANCH, r.stderr
            # But can be checked out:
            r = cli_runner.invoke(["checkout", head.commit.hex])
            assert r.exit_code == 0, r.stderr

            r = cli_runner.invoke(["checkout", "zero"])
            assert r.exit_code == NO_COMMIT, r.stderr
            r = cli_runner.invoke(["switch", "zero"])
            assert r.exit_code == NO_BRANCH, r.stderr

            r = cli_runner.invoke(["checkout", "one", "--no-guess"])
            assert r.exit_code == NO_COMMIT, r.stderr
            r = cli_runner.invoke(["switch", "one", "--no-guess"])
            assert r.exit_code == NO_BRANCH, r.stderr

            r = cli_runner.invoke(["checkout", "one"])
            assert r.exit_code == 0, r.stderr
            assert (
                r.stdout.splitlines()[0]
                == "Creating new branch 'one' to track 'origin/one'..."
            )
            assert (
                CommitWithReference.resolve(repo, "HEAD").commit.hex == one.commit.hex
            )
            r = cli_runner.invoke(["switch", "two"])
            assert r.exit_code == 0, r.stderr
            assert (
                r.stdout.splitlines()[0]
                == "Creating new branch 'two' to track 'origin/two'..."
            )
            assert (
                CommitWithReference.resolve(repo, "HEAD").commit.hex == two.commit.hex
            )

            r = cli_runner.invoke(["branch"])
            assert r.exit_code == 0, r.stderr
            assert r.stdout.splitlines() == ["  four", "  one", "* two"]


def test_reset(data_working_copy, cli_runner, edit_points):
    with data_working_copy("points") as (repo_path, wc):
        with gpkg_engine(wc).connect() as db:
            edit_points(db)

        r = cli_runner.invoke(["diff", "--exit-code"])
        assert r.exit_code == 1
        r = cli_runner.invoke(["log", "--oneline", "--decorate=short"])
        assert r.exit_code == 0, r.stderr
        assert r.stdout.splitlines() == [
            "0c64d82 (HEAD -> master) Improve naming on Coromandel East coast",
            "7bc3b56 Import from nz-pa-points-topo-150k.gpkg",
        ]

        r = cli_runner.invoke(["reset", "HEAD^"])
        assert r.exit_code == INVALID_OPERATION
        assert "You have uncommitted changes in your working copy." in r.stderr

        r = cli_runner.invoke(["reset", "HEAD^", "--discard-changes"])
        assert r.exit_code == 0, r.stderr

        r = cli_runner.invoke(["diff", "--exit-code"])
        assert r.exit_code == 0
        r = cli_runner.invoke(["log", "--oneline", "--decorate=short"])
        assert r.exit_code == 0, r.stderr
        assert r.stdout.splitlines() == [
            "7bc3b56 (HEAD -> master) Import from nz-pa-points-topo-150k.gpkg",
        ]
