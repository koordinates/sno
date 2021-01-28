import re
import subprocess

import pytest

from sno.sqlalchemy import gpkg_engine


H = pytest.helpers.helpers()

GPKG_IMPORTS = (
    "archive,gpkg,table_ref",
    [
        pytest.param(
            "gpkg-points", "nz-pa-points-topo-150k.gpkg", "POINTS", id="points"
        ),
        pytest.param(
            "gpkg-polygons", "nz-waca-adjustments.gpkg", "POLYGONS", id="polygons"
        ),
    ],
)


@pytest.mark.slow
@pytest.mark.e2e
@pytest.mark.parametrize(*GPKG_IMPORTS)
def test_e2e(
    archive,
    gpkg,
    table_ref,
    data_archive,
    tmp_path,
    chdir,
    cli_runner,
    insert,
    request,
):
    metadata = H.metadata(table_ref)
    table = metadata.LAYER
    row_count = metadata.ROWCOUNT

    repo_path = tmp_path / "myproject.sno"
    repo_path.mkdir()

    remote_path = tmp_path / "myremote.sno"
    remote_path.mkdir()
    with chdir(remote_path):
        # initialise empty repo for remote
        subprocess.run(["git", "init", "--bare", str(remote_path)], check=True)

    with data_archive(archive) as data:
        with chdir(repo_path):
            # initialise empty repo
            r = cli_runner.invoke(["init"])
            assert r.exit_code == 0

            # import data
            r = cli_runner.invoke(["import", f"GPKG:{data / gpkg}", table])
            assert r.exit_code == 0

            # check there's a commit
            r = cli_runner.invoke(["log"])
            assert r.exit_code == 0
            assert "Import from " in r.stdout
            sha_import = r.stdout.splitlines()[0].split()[1]
            print("Imported SHA:", sha_import)

            # checkout a working copy
            r = cli_runner.invoke(["checkout"])
            assert r.exit_code == 0
            working_copy = repo_path / "myproject.gpkg"
            assert working_copy.exists()

            # check we have the right data in the WC
            with gpkg_engine(working_copy).connect() as db:
                assert H.row_count(db, table) == row_count

            # create & switch to a new branch
            r = cli_runner.invoke(["switch", "-c", "edit-1"])
            assert r.exit_code == 0
            assert r.stdout.splitlines()[0] == "Creating new branch 'edit-1'..."

            r = cli_runner.invoke(["status"])
            assert r.exit_code == 0
            assert r.stdout.splitlines()[0] == "On branch edit-1"

            # make an edit
            with gpkg_engine(working_copy).connect() as db:
                insert(db, commit=False)

            r = cli_runner.invoke(["diff"])
            assert r.exit_code == 0
            assert re.match(fr"\+\+\+ {table}:feature:\d+$", r.stdout.splitlines()[0])

            # commit it
            r = cli_runner.invoke(["commit", "-m", "commit-1"])
            assert r.exit_code == 0
            sha_edit1 = r.stdout.splitlines()[-1].split()[1]
            print("Edit SHA:", sha_edit1)

            # go back to master
            r = cli_runner.invoke(["switch", "master"])
            assert r.exit_code == 0
            assert r.stdout.splitlines()[0] == f"Updating {working_copy.name} ..."

            r = cli_runner.invoke(["status"])
            assert r.exit_code == 0
            assert r.stdout.splitlines()[0] == "On branch master"

            # merge it
            r = cli_runner.invoke(["merge", "edit-1", "--no-ff", "-m", "merge-1"])
            assert r.exit_code == 0
            assert "Fast-forward" not in r.stdout
            sha_merge1 = r.stdout.splitlines()[-1].split(" ")[-1]
            print("Merge SHA:", sha_merge1)

            H.git_graph(request, "post edit-1 merge", count=10)

            # add a remote
            r = cli_runner.invoke(["remote", "add", "myremote", remote_path])
            assert r.exit_code == 0

            # push
            r = cli_runner.invoke(["push", "--set-upstream", "myremote", "master"])
            assert r.exit_code == 0
            assert re.match(
                r"Branch '?master'? set up to track remote branch '?master'? from '?myremote'?\.$",
                r.stdout.splitlines()[0],
            )

            # check reflog
            r = cli_runner.invoke(["reflog"])
            assert r.exit_code == 0
            assert [x.split(" ", 1)[1] for x in r.stdout.splitlines()][0:4] == [
                "HEAD@{0}: commit (merge): merge-1",
                "HEAD@{1}: checkout: moving from edit-1 to master",
                "HEAD@{2}: commit: commit-1",
                "HEAD@{3}: checkout: moving from master to edit-1",
            ]
