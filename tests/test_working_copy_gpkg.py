import json
import subprocess
from pathlib import Path

import pytest

import apsw
import pygit2

from sno import gpkg_adapter
from sno.exceptions import INVALID_ARGUMENT, INVALID_OPERATION
from sno.sno_repo import SnoRepo
from sno.structure import RepositoryStructure
from sno.working_copy import WorkingCopy
from sno.working_copy.gpkg import WorkingCopy_GPKG_1
from test_working_copy import compute_approximated_types


H = pytest.helpers.helpers()


@pytest.mark.parametrize(
    "archive,table,commit_sha",
    [
        pytest.param("points", H.POINTS.LAYER, H.POINTS.HEAD_SHA, id="points"),
        pytest.param(
            "polygons", H.POLYGONS.LAYER, H.POLYGONS.HEAD_SHA, id="polygons-pk"
        ),
        pytest.param("table", H.TABLE.LAYER, H.TABLE.HEAD_SHA, id="table"),
    ],
)
@pytest.mark.parametrize("version", [1, 2])
def test_checkout_workingcopy(
    version, archive, table, commit_sha, data_archive, tmp_path, cli_runner, geopackage
):
    """ Checkout a working copy to edit """
    if version == "2":
        archive += "2"
        sno_state_table = "gpkg_sno_state"
    else:
        sno_state_table = ".sno-meta"

    with data_archive(archive) as repo_path:
        H.clear_working_copy()

        repo = SnoRepo(repo_path)
        r = cli_runner.invoke(["checkout"])
        wc = Path(repo.config["sno.workingcopy.path"])
        assert r.exit_code == 0, r
        assert r.stdout.splitlines() == [f"Creating working copy at {wc} ..."]

        assert wc.exists()
        db = geopackage(wc)
        assert H.row_count(db, table) > 0

        assert repo.head.name == "refs/heads/master"
        assert repo.head.shorthand == "master"

        head_tree = repo.head.peel(pygit2.Tree)

        wc_tree_id = (
            db.cursor()
            .execute(
                f"""SELECT value FROM "{sno_state_table}" WHERE table_name='*' AND key='tree';"""
            )
            .fetchone()[0]
        )
        assert wc_tree_id == head_tree.hex

        wc = WorkingCopy.get(repo)
        assert wc.assert_db_tree_match(head_tree)

        rs = RepositoryStructure(repo)
        cols, pk_col = wc._get_columns(rs[table])
        expected_col_spec = f'"{pk_col}" INTEGER PRIMARY KEY AUTOINCREMENT'
        assert cols[pk_col] in (expected_col_spec, f"{expected_col_spec} NOT NULL")


def test_checkout_detached(data_working_copy, cli_runner, geopackage):
    """ Checkout a working copy to edit """
    with data_working_copy("points") as (repo_dir, wc):
        db = geopackage(wc)
        assert H.last_change_time(db) == "2019-06-20T14:28:33.000000Z"

        # checkout the previous commit
        r = cli_runner.invoke(["checkout", H.POINTS.HEAD1_SHA[:8]])
        assert r.exit_code == 0, r
        assert H.last_change_time(db) == "2019-06-11T11:03:58.000000Z"

        repo = SnoRepo(repo_dir)
        assert repo.head.target.hex == H.POINTS.HEAD1_SHA
        assert repo.head_is_detached
        assert repo.head.name == "HEAD"


def test_checkout_references(data_working_copy, cli_runner, geopackage, tmp_path):
    with data_working_copy("points") as (repo_dir, wc):
        db = geopackage(wc)
        repo = SnoRepo(repo_dir)

        # create a tag
        repo.create_reference("refs/tags/version1", repo.head.target)

        subprocess.run(["git", "init", "--bare", str(tmp_path)], check=True)

        r = cli_runner.invoke(["remote", "add", "myremote", tmp_path])
        assert r.exit_code == 0, r

        r = cli_runner.invoke(["push", "myremote", "master"])
        assert r.exit_code == 0, r

        def r_head():
            return (repo.head.name, repo.head.target.hex)

        # checkout the HEAD commit
        r = cli_runner.invoke(["checkout", "HEAD"])
        assert r.exit_code == 0, r
        assert r_head() == ("refs/heads/master", H.POINTS.HEAD_SHA)
        assert not repo.head_is_detached
        assert H.last_change_time(db) == "2019-06-20T14:28:33.000000Z"

        # checkout the HEAD-but-1 commit
        r = cli_runner.invoke(["checkout", "HEAD~1"])
        assert r.exit_code == 0, r
        assert r_head() == ("HEAD", H.POINTS.HEAD1_SHA)
        assert repo.head_is_detached
        assert H.last_change_time(db) == "2019-06-11T11:03:58.000000Z"

        # checkout the master HEAD via branch-name
        r = cli_runner.invoke(["checkout", "master"])
        assert r.exit_code == 0, r
        assert r_head() == ("refs/heads/master", H.POINTS.HEAD_SHA)
        assert not repo.head_is_detached
        assert H.last_change_time(db) == "2019-06-20T14:28:33.000000Z"

        # checkout a short-sha commit
        r = cli_runner.invoke(["checkout", H.POINTS.HEAD1_SHA[:8]])
        assert r.exit_code == 0, r
        assert r_head() == ("HEAD", H.POINTS.HEAD1_SHA)
        assert repo.head_is_detached
        assert H.last_change_time(db) == "2019-06-11T11:03:58.000000Z"

        # checkout the master HEAD via refspec
        r = cli_runner.invoke(["checkout", "refs/heads/master"])
        assert r.exit_code == 0, r
        assert r_head() == ("refs/heads/master", H.POINTS.HEAD_SHA)
        assert not repo.head_is_detached
        assert H.last_change_time(db) == "2019-06-20T14:28:33.000000Z"

        # checkout the tag
        r = cli_runner.invoke(["checkout", "version1"])
        assert r.exit_code == 0, r
        assert r_head() == ("HEAD", H.POINTS.HEAD_SHA)
        assert repo.head_is_detached
        assert H.last_change_time(db) == "2019-06-20T14:28:33.000000Z"

        # checkout the remote branch
        r = cli_runner.invoke(["checkout", "myremote/master"])
        assert r.exit_code == 0, r
        assert r_head() == ("HEAD", H.POINTS.HEAD_SHA)
        assert repo.head_is_detached
        assert H.last_change_time(db) == "2019-06-20T14:28:33.000000Z"


def test_checkout_branch(data_working_copy, geopackage, cli_runner, tmp_path):
    with data_working_copy("points") as (repo_path, wc):
        db = geopackage(wc)

        # creating a new branch with existing name errors
        r = cli_runner.invoke(["checkout", "-b", "master"])
        assert r.exit_code == INVALID_ARGUMENT, r
        assert r.stderr.splitlines()[-1].endswith(
            "A branch named 'master' already exists."
        )

        subprocess.run(["git", "init", "--bare", str(tmp_path)], check=True)
        r = cli_runner.invoke(["remote", "add", "myremote", tmp_path])
        assert r.exit_code == 0, r

        r = cli_runner.invoke(["push", "--set-upstream", "myremote", "master"])
        assert r.exit_code == 0, r

        # new branch
        r = cli_runner.invoke(["checkout", "-b", "foo"])
        assert r.exit_code == 0, r

        repo = SnoRepo(repo_path)
        assert repo.head.name == "refs/heads/foo"
        assert "foo" in repo.branches
        assert repo.head.peel(pygit2.Commit).hex == H.POINTS.HEAD_SHA

        # make some changes
        db = geopackage(wc)
        with db:
            cur = db.cursor()
            cur.execute(H.POINTS.INSERT, H.POINTS.RECORD)
            assert db.changes() == 1

        r = cli_runner.invoke(["commit", "-m", "test1"])
        assert r.exit_code == 0, r

        assert repo.head.peel(pygit2.Commit).hex != H.POINTS.HEAD_SHA

        r = cli_runner.invoke(["checkout", "master"])
        assert r.exit_code == 0, r

        assert repo.head.name == "refs/heads/master"
        assert repo.head.peel(pygit2.Commit).hex == H.POINTS.HEAD_SHA

        # new branch from remote
        r = cli_runner.invoke(["checkout", "-b", "test99", "myremote/master"])
        assert r.exit_code == 0, r
        assert repo.head.name == "refs/heads/test99"
        assert "test99" in repo.branches
        assert repo.head.peel(pygit2.Commit).hex == H.POINTS.HEAD_SHA
        branch = repo.branches["test99"]
        assert branch.upstream_name == "refs/remotes/myremote/master"


def test_switch_branch(data_working_copy, geopackage, cli_runner, tmp_path):
    raise pytest.skip()  # apsw.SQLError: SQLError: Safety level may not be changed inside a transaction
    with data_working_copy("points") as (repo_path, wc):
        db = geopackage(wc)

        # creating a new branch with existing name errors
        r = cli_runner.invoke(["switch", "-c", "master"])
        assert r.exit_code == 2, r
        assert r.stdout.splitlines()[-1].endswith(
            "A branch named 'master' already exists."
        )

        subprocess.run(["git", "init", "--bare", str(tmp_path)], check=True)
        r = cli_runner.invoke(["remote", "add", "myremote", tmp_path])
        assert r.exit_code == 0, r

        r = cli_runner.invoke(["push", "--set-upstream", "myremote", "master"])
        assert r.exit_code == 0, r

        # new branch
        r = cli_runner.invoke(["switch", "-c", "foo"])
        assert r.exit_code == 0, r

        repo = SnoRepo(repo_path)
        assert repo.head.name == "refs/heads/foo"
        assert "foo" in repo.branches
        assert repo.head.peel(pygit2.Commit).hex == H.POINTS.HEAD_SHA

        # make some changes
        db = geopackage(wc)
        with db:
            cur = db.cursor()

            cur.execute(H.POINTS.INSERT, H.POINTS.RECORD)
            assert db.changes() == 1

            cur.execute(f"UPDATE {H.POINTS.LAYER} SET fid=30000 WHERE fid=3;")
            assert db.changes() == 1

        r = cli_runner.invoke(["commit", "-m", "test1"])
        assert r.exit_code == 0, r

        new_commit = repo.head.peel(pygit2.Commit).hex
        assert new_commit != H.POINTS.HEAD_SHA

        r = cli_runner.invoke(["switch", "master"])
        assert r.exit_code == 0, r

        assert H.row_count(db, H.POINTS.LAYER) == H.POINTS.ROWCOUNT

        assert repo.head.name == "refs/heads/master"
        assert repo.head.peel(pygit2.Commit).hex == H.POINTS.HEAD_SHA

        # make some changes
        with db:
            cur = db.cursor()

            cur.execute(H.POINTS.INSERT, H.POINTS.RECORD)
            assert db.changes() == 1

            cur.execute(f"UPDATE {H.POINTS.LAYER} SET fid=40000 WHERE fid=4;")
            assert db.changes() == 1

        r = cli_runner.invoke(["switch", "foo"])
        assert r.exit_code == INVALID_OPERATION, r
        assert "Error: You have uncommitted changes in your working copy." in r.stdout

        r = cli_runner.invoke(["switch", "foo", "--discard-changes"])
        assert r.exit_code == 0, r

        assert H.row_count(db, H.POINTS.LAYER) == H.POINTS.ROWCOUNT + 1

        assert repo.head.name == "refs/heads/foo"
        assert repo.head.peel(pygit2.Commit).hex == new_commit

        # new branch from remote
        r = cli_runner.invoke(["switch", "-c", "test99", "myremote/master"])
        assert r.exit_code == 0, r
        assert repo.head.name == "refs/heads/test99"
        assert "test99" in repo.branches
        assert repo.head.peel(pygit2.Commit).hex == H.POINTS.HEAD_SHA
        branch = repo.branches["test99"]
        assert branch.upstream_name == "refs/remotes/myremote/master"

        assert H.row_count(db, H.POINTS.LAYER) == H.POINTS.ROWCOUNT


@pytest.mark.parametrize(
    "archive,layer",
    [
        pytest.param("points", H.POINTS.LAYER, id="points"),
        pytest.param("polygons", H.POLYGONS.LAYER, id="polygons-pk"),
        pytest.param("table", H.TABLE.LAYER, id="table"),
    ],
)
@pytest.mark.parametrize(
    "via",
    [
        pytest.param("reset", id="via-reset"),
        pytest.param("checkout", id="via-checkout"),
    ],
)
def test_working_copy_reset(
    archive, layer, via, data_working_copy, cli_runner, geopackage
):
    """
    Check that we reset any working-copy changes correctly before doing any new checkout

    We can do this via `sno reset` or `sno checkout --discard-changes HEAD`
    """
    raise pytest.skip()  # apsw.SQLError: SQLError: Safety level may not be changed inside a transaction
    if layer == H.POINTS.LAYER:
        pk_field = H.POINTS.LAYER_PK
        rec = H.POINTS.RECORD
        sql = H.POINTS.INSERT
        del_pk = 5
        upd_field = "t50_fid"
        upd_field_value = 888_888
        upd_pk_range = (10, 15)
        id_chg_pk = 20
    elif layer == H.POLYGONS.LAYER:
        pk_field = H.POLYGONS.LAYER_PK
        rec = H.POLYGONS_RECORD
        sql = H.POLYGONS_INSERT
        del_pk = 1_456_912
        upd_field = "survey_reference"
        upd_field_value = "test"
        upd_pk_range = (1_459_750, 1_460_312)
        id_chg_pk = 1_460_583
    elif layer == H.TABLE.LAYER:
        pk_field = H.TABLE.LAYER_PK
        rec = H.TABLE_RECORD
        sql = H.TABLE_INSERT
        del_pk = 5
        upd_field = "name"
        upd_field_value = "test"
        upd_pk_range = (10, 15)
        id_chg_pk = 20
    else:
        raise NotImplementedError(f"layer={layer}")

    with data_working_copy(archive, force_new=True) as (repo_dir, wc):
        db = geopackage(wc)

        h_before = H.db_table_hash(db, layer, pk_field)
        with db:
            cur = db.cursor()
            try:
                cur.execute(sql, rec)
            except apsw.Error:
                print(sql, rec)
                raise
            assert db.changes() == 1

            cur.execute(f"DELETE FROM {layer} WHERE {pk_field} < {del_pk};")
            assert db.changes() == 4
            cur.execute(
                f"UPDATE {layer} SET {upd_field} = ? WHERE {pk_field}>=? AND {pk_field}<?;",
                [upd_field_value, upd_pk_range[0], upd_pk_range[1]],
            )
            assert db.changes() == 5
            cur.execute(
                f"UPDATE {layer} SET {pk_field}=? WHERE {pk_field}=?;",
                [9998, id_chg_pk],
            )
            assert db.changes() == 1

            cur.execute("""SELECT COUNT(*) FROM ".sno-track";""")
            change_count = cur.fetchone()[0]
            assert change_count == (1 + 4 + 5 + 2)

        if via == "reset":
            # using `sno reset`
            r = cli_runner.invoke(["reset"])
            assert r.exit_code == 0, r
        elif via == "checkout":
            # using `sno checkout --force`

            # this should error
            r = cli_runner.invoke(["checkout", "HEAD"])
            assert r.exit_code == INVALID_OPERATION, r

            cur.execute("""SELECT COUNT(*) FROM ".sno-track";""")
            change_count = cur.fetchone()[0]
            assert change_count == (1 + 4 + 5 + 2)

            # do again with --force
            r = cli_runner.invoke(["checkout", "--force", "HEAD"])
            assert r.exit_code == 0, r
        else:
            raise NotImplementedError(f"via={via}")

        cur.execute("""SELECT COUNT(*) FROM ".sno-track";""")
        change_count = cur.fetchone()[0]
        assert change_count == 0

        h_after = H.db_table_hash(db, layer, pk_field)
        if h_before != h_after:
            cur.execute(
                f"SELECT {pk_field} FROM {layer} WHERE {pk_field}=?;", [rec[pk_field]]
            )
            if cur.fetchone():
                print(
                    "E: Newly inserted row is still there ({pk_field}={rec[pk_field]})"
                )
            cur.execute(f"SELECT COUNT(*) FROM {layer} WHERE {pk_field} < ?;", [del_pk])
            if cur.fetchone()[0] != 4:
                print("E: Deleted rows {pk_field}<{del_pk} still missing")
            cur.execute(
                f"SELECT COUNT(*) FROM {layer} WHERE {upd_field} = ?;",
                [upd_field_value],
            )
            if cur.fetchone()[0] != 0:
                print("E: Updated rows not reset")
            cur.execute(f"SELECT {pk_field} FROM {layer} WHERE {pk_field} = 9998;")
            if cur.fetchone():
                print(
                    "E: Updated pk row is still there ({pk_field}={id_chg_pk} -> 9998)"
                )
            cur.execute(
                f"SELECT {pk_field} FROM {layer} WHERE {pk_field} = ?;", [id_chg_pk]
            )
            if not cur.fetchone():
                print("E: Updated pk row is missing ({pk_field}={id_chg_pk})")

        assert h_before == h_after


def test_switch_with_meta_items(data_working_copy, geopackage, cli_runner):
    with data_working_copy("points2") as (repo, wc):
        db = geopackage(wc)
        cur = db.cursor()
        cur.execute(
            """
            UPDATE gpkg_contents SET identifier = 'new identifier', description='new description'
            """
        )
        r = cli_runner.invoke(["commit", "-m", "change identifier and description"])
        assert r.exit_code == 0, r.stderr
        r = cli_runner.invoke(["checkout", "HEAD^"])
        assert r.exit_code == 0, r.stderr
        cur.execute(
            """
            SELECT identifier, description FROM gpkg_contents
            """
        )
        identifier, description = cur.fetchall()[0]
        assert identifier == "nz_pa_points_topo_150k: NZ Pa Points (Topo, 1:50k)"
        assert description.startswith("Defensive earthworks")

        r = cli_runner.invoke(["checkout", "master"])
        assert r.exit_code == 0, r.stderr
        cur.execute(
            """
            SELECT identifier, description FROM gpkg_contents
            """
        )
        identifier, description = cur.fetchall()[0]
        assert identifier == "nz_pa_points_topo_150k: new identifier"
        assert description == "new description"


def test_switch_with_trivial_schema_change(data_working_copy, geopackage, cli_runner):
    # Column renames are one of the only schema changes we can do without having to recreate the whole table.
    with data_working_copy("points2") as (repo, wc):
        db = geopackage(wc)
        cur = db.cursor()
        cur.execute(
            f"""ALTER TABLE {H.POINTS.LAYER} RENAME name_ascii TO name_latin1"""
        )
        r = cli_runner.invoke(["commit", "-m", "change schema"])
        assert r.exit_code == 0, r.stderr
        r = cli_runner.invoke(["checkout", "HEAD^"])
        assert r.exit_code == 0, r.stderr
        cur.execute(
            f"""SELECT name FROM pragma_table_info('{H.POINTS.LAYER}') WHERE cid = 3;"""
        )
        name = cur.fetchall()[0][0]
        assert name == "name_ascii"

        r = cli_runner.invoke(["checkout", "master"])
        assert r.exit_code == 0, r.stderr
        cur.execute(
            f"""SELECT name FROM pragma_table_info('{H.POINTS.LAYER}') WHERE cid = 3;"""
        )
        name = cur.fetchall()[0][0]
        assert name == "name_latin1"


def test_switch_with_schema_change(data_working_copy, geopackage, cli_runner):
    with data_working_copy("polygons2") as (repo, wc):
        db = geopackage(wc)
        cur = db.cursor()
        cur.execute(f"""ALTER TABLE {H.POLYGONS.LAYER} ADD COLUMN colour TEXT""")
        r = cli_runner.invoke(["commit", "-m", "change schema"])
        assert r.exit_code == 0, r.stderr
        r = cli_runner.invoke(["checkout", "HEAD^"])
        assert r.exit_code == 0, r.stderr
        cur.execute(
            f"""SELECT name, type FROM pragma_table_info('{H.POLYGONS.LAYER}');"""
        )
        result = cur.fetchall()
        assert result == [
            ("id", "INTEGER"),
            ("geom", "MULTIPOLYGON"),
            ("date_adjusted", "DATETIME"),
            ("survey_reference", "TEXT(50)"),
            ("adjusted_nodes", "MEDIUMINT"),
        ]

        r = cli_runner.invoke(["checkout", "master"])
        assert r.exit_code == 0, r.stderr
        cur.execute(
            f"""SELECT name, type FROM pragma_table_info('{H.POLYGONS.LAYER}');"""
        )
        result = cur.fetchall()
        assert result == [
            ("id", "INTEGER"),
            ("geom", "MULTIPOLYGON"),
            ("date_adjusted", "DATETIME"),
            ("survey_reference", "TEXT(50)"),
            ("adjusted_nodes", "MEDIUMINT"),
            ("colour", "TEXT"),
        ]


@pytest.mark.parametrize("repo_version", [1, 2])
def test_switch_pre_import_post_import(
    repo_version, data_working_copy, data_archive_readonly, geopackage, cli_runner
):
    with data_archive_readonly("gpkg-au-census") as data:
        data_wc_archive = "polygons2" if repo_version == 2 else "polygons"
        with data_working_copy(data_wc_archive) as (repo, wc):
            r = cli_runner.invoke(
                [
                    "import",
                    data / "census2016_sdhca_ot_short.gpkg",
                    "census2016_sdhca_ot_ced_short",
                ]
            )
            assert r.exit_code == 0, r.stderr

            db = geopackage(wc)
            cur = db.cursor()

            r = cli_runner.invoke(["checkout", "HEAD^"])
            assert r.exit_code == 0, r.stderr
            cur.execute(
                f"""SELECT COUNT(name) FROM sqlite_master where type='table' AND name='census2016_sdhca_ot_ced_short';"""
            )
            count = cur.fetchall()[0][0]
            assert count == 0

            r = cli_runner.invoke(["checkout", "master"])
            assert r.exit_code == 0, r.stderr
            cur.execute(
                f"""SELECT COUNT(name) FROM sqlite_master where type='table' AND name='census2016_sdhca_ot_ced_short';"""
            )
            count = cur.fetchall()[0][0]
            assert count == 1


def test_switch_xml_metadata_added(data_working_copy, geopackage, cli_runner):
    with data_working_copy("table2") as (repo, wc):
        db = geopackage(wc)
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO gpkg_metadata (id, md_scope, md_standard_uri, mime_type, metadata)
            VALUES (1, "dataset", "http://www.isotc211.org/2005/gmd", "text/xml", "<test metadata>");
            """
        )
        cur.execute(
            """
            INSERT INTO gpkg_metadata_reference (reference_scope, table_name, md_file_id)
            VALUES ("table", "countiestbl", 1);
            """
        )

        r = cli_runner.invoke(["commit", "-m", "change xml metadata"])
        assert r.exit_code == 0, r.stderr
        r = cli_runner.invoke(["checkout", "HEAD^"])
        assert r.exit_code == 0, r.stderr

        xml_metadata = cur.execute(
            """
            SELECT m.metadata
            FROM gpkg_metadata m JOIN gpkg_metadata_reference r
            ON m.id = r.md_file_id
            WHERE r.table_name = 'countiestbl'
            """
        ).fetchone()
        assert not xml_metadata

        r = cli_runner.invoke(["checkout", "master"])
        assert r.exit_code == 0, r.stderr

        xml_metadata = cur.execute(
            """
            SELECT m.metadata
            FROM gpkg_metadata m JOIN gpkg_metadata_reference r
            ON m.id = r.md_file_id
            WHERE r.table_name = 'countiestbl'
            """
        ).fetchone()[0]
        assert xml_metadata == "<test metadata>"


def test_geopackage_locking_edit(
    data_working_copy, geopackage, cli_runner, monkeypatch
):
    with data_working_copy("points") as (repo, wc):
        db = geopackage(wc)

        is_checked = False
        orig_func = WorkingCopy_GPKG_1.write_features

        def _wrap(*args, **kwargs):
            nonlocal is_checked
            if not is_checked:
                with pytest.raises(apsw.BusyError, match=r"database is locked"):
                    db.cursor().execute(
                        "UPDATE gpkg_contents SET table_name=table_name;"
                    )
                is_checked = True

            return orig_func(*args, **kwargs)

        monkeypatch.setattr(WorkingCopy_GPKG_1, "write_features", _wrap)

        r = cli_runner.invoke(["checkout", H.POINTS.HEAD1_SHA])
        assert r.exit_code == 0, r
        assert is_checked

        assert H.last_change_time(db) == "2019-06-11T11:03:58.000000Z"


def test_create_workingcopy(data_working_copy, cli_runner, tmp_path):
    with data_working_copy("points") as (repo_path, _):
        repo = SnoRepo(repo_path)

        r = cli_runner.invoke(["create-workingcopy", "."])
        assert r.exit_code == INVALID_ARGUMENT, r.stderr

        # relative path 1
        new_thingz = Path("new-thingz.gpkg")
        assert not new_thingz.exists()
        r = cli_runner.invoke(["create-workingcopy", new_thingz])
        assert r.exit_code == 0, r.stderr
        assert new_thingz.exists()
        assert repo.config["sno.workingcopy.path"] == str(new_thingz)

        r = cli_runner.invoke(["create-workingcopy", new_thingz])
        assert r.exit_code == 0, r.stderr

        # relative path 2
        other_thingz = Path("other-thingz.gpkg")
        assert not other_thingz.exists()
        r = cli_runner.invoke(["create-workingcopy", Path("../points") / other_thingz])
        assert r.exit_code == 0, r.stderr
        assert not new_thingz.exists()
        assert other_thingz.exists()
        assert repo.config["sno.workingcopy.path"] == str(other_thingz)

        # abs path
        abs_thingz = tmp_path / "abs_thingz.gpkg"
        assert not abs_thingz.exists()
        r = cli_runner.invoke(["create-workingcopy", abs_thingz])
        assert r.exit_code == 0, r.stderr
        assert not other_thingz.exists()
        assert abs_thingz.exists()

        assert repo.config["sno.workingcopy.path"] == str(abs_thingz)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param([], id="head"),
        pytest.param(["-s", H.POINTS.HEAD_SHA], id="prev"),
    ],
)
@pytest.mark.parametrize(
    "pathspec",
    [
        pytest.param([], id="all"),
        pytest.param(["bob"], id="exclude"),
    ],
)
def test_restore(source, pathspec, data_working_copy, cli_runner, geopackage):
    with data_working_copy("points", force_new=True) as (repo_dir, wc):
        layer = H.POINTS.LAYER
        pk_field = H.POINTS.LAYER_PK
        rec = H.POINTS.RECORD
        sql = H.POINTS.INSERT
        del_pk = 5
        upd_field = "t50_fid"
        upd_field_value = 888_888
        upd_pk_range = (10, 15)
        id_chg_pk = 20

        db = geopackage(wc)
        repo = SnoRepo(repo_dir)

        # make some changes
        with db:
            cur = db.cursor()
            cur.execute(f"UPDATE {H.POINTS.LAYER} SET fid=30000 WHERE fid=300;")
            assert db.changes() == 1

        r = cli_runner.invoke(["commit", "-m", "test1"])
        assert r.exit_code == 0, r

        new_commit = repo.head.peel(pygit2.Commit).hex
        assert new_commit != H.POINTS.HEAD_SHA
        print(f"Original commit={H.POINTS.HEAD_SHA} New commit={new_commit}")

        with db:
            cur = db.cursor()
            try:
                cur.execute(sql, rec)
            except apsw.Error:
                print(sql, rec)
                raise
            assert db.changes() == 1

            cur.execute(f"DELETE FROM {layer} WHERE {pk_field} < {del_pk};")
            assert db.changes() == 4
            cur.execute(
                f"UPDATE {layer} SET {upd_field} = ? WHERE {pk_field}>=? AND {pk_field}<?;",
                [upd_field_value, upd_pk_range[0], upd_pk_range[1]],
            )
            assert db.changes() == 5
            cur.execute(
                f"UPDATE {layer} SET {pk_field}=? WHERE {pk_field}=?;",
                [9998, id_chg_pk],
            )
            assert db.changes() == 1

            changes_pre = [
                r[0]
                for r in cur.execute(
                    'SELECT pk FROM ".sno-track" ORDER BY CAST(pk AS INTEGER);'
                )
            ]
            # .sno-track stores pk as strings
            assert changes_pre == [
                "1",
                "2",
                "3",
                "4",
                "10",
                "11",
                "12",
                "13",
                "14",
                "20",
                "9998",
                "9999",
            ]

        # using `sno restore
        r = cli_runner.invoke(["restore"] + source + pathspec)
        assert r.exit_code == 0, r

        changes_post = [
            r[0]
            for r in cur.execute(
                'SELECT pk FROM ".sno-track" ORDER BY CAST(pk AS INTEGER);'
            )
        ]

        cur.execute(
            f"""SELECT value FROM ".sno-meta" WHERE key = 'tree' AND table_name='*';"""
        )
        head_sha = cur.fetchone()[0]

        if pathspec:
            # we restore'd paths other than our test dataset, so all the changes should still be there
            assert changes_post == changes_pre

            if head_sha != new_commit:
                print(f"E: Bad Tree? {head_sha}")

            return

        if source:
            assert changes_post == ["300", "30000"]

            if head_sha != H.POINTS.HEAD_SHA:
                print(f"E: Bad Tree? {head_sha}")

            cur.execute(f"SELECT {pk_field} FROM {layer} WHERE {pk_field} = 300;")
            if not cur.fetchone():
                print("E: Previous PK bad? ({pk_field}=300)")
            return

        assert changes_post == []

        if head_sha != new_commit:
            print(f"E: Bad Tree? {head_sha}")

        cur.execute(
            f"""SELECT value FROM ".sno-meta" WHERE key = 'tree' AND table_name='*';"""
        )
        head_sha = cur.fetchone()[0]
        if head_sha != new_commit:
            print(f"E: Bad Tree? {head_sha}")

        cur.execute(
            f"SELECT {pk_field} FROM {layer} WHERE {pk_field}=?;", [rec[pk_field]]
        )
        if cur.fetchone():
            print("E: Newly inserted row is still there ({pk_field}={rec[pk_field]})")
        cur.execute(f"SELECT COUNT(*) FROM {layer} WHERE {pk_field} < ?;", [del_pk])
        if cur.fetchone()[0] != 4:
            print("E: Deleted rows {pk_field}<{del_pk} still missing")
        cur.execute(
            f"SELECT COUNT(*) FROM {layer} WHERE {upd_field} = ?;",
            [upd_field_value],
        )
        if cur.fetchone()[0] != 0:
            print("E: Updated rows not reset")
        cur.execute(f"SELECT {pk_field} FROM {layer} WHERE {pk_field} = 9998;")
        if cur.fetchone():
            print("E: Updated pk row is still there ({pk_field}={id_chg_pk} -> 9998)")
        cur.execute(
            f"SELECT {pk_field} FROM {layer} WHERE {pk_field} = ?;", [id_chg_pk]
        )
        if not cur.fetchone():
            print("E: Updated pk row is missing ({pk_field}={id_chg_pk})")

        cur.execute(f"SELECT {pk_field} FROM {layer} WHERE {pk_field} = 300;")
        if not cur.fetchone():
            print("E: Previous PK bad? ({pk_field}=300)")


def test_delete_branch(data_working_copy, cli_runner):
    with data_working_copy("points") as (repo_path, wc):
        # prevent deleting the current branch
        r = cli_runner.invoke(["branch", "-d", "master"])
        assert r.exit_code == INVALID_OPERATION, r
        assert "Cannot delete" in r.stderr

        r = cli_runner.invoke(["checkout", "-b", "test"])
        assert r.exit_code == 0, r

        r = cli_runner.invoke(["branch", "-d", "test"])
        assert r.exit_code == INVALID_OPERATION, r

        r = cli_runner.invoke(["checkout", "master"])
        assert r.exit_code == 0, r

        r = cli_runner.invoke(["branch", "-d", "test"])
        assert r.exit_code == 0, r


def test_reset(data_working_copy, cli_runner, geopackage, edit_polygons):
    with data_working_copy("polygons") as (repo_path, wc):
        db = geopackage(wc)
        with db:
            cur = db.cursor()
            edit_polygons(cur)

        r = cli_runner.invoke(["status", "--output-format=json"])
        assert r.exit_code == 0, r
        changes = json.loads(r.stdout)["sno.status/v1"]["workingCopy"]["changes"]
        assert changes == {
            "nz_waca_adjustments": {
                "feature": {"inserts": 1, "updates": 2, "deletes": 5}
            }
        }
        r = cli_runner.invoke(["diff", "--exit-code"])
        assert r.exit_code == 1, r

        r = cli_runner.invoke(["reset"])

        r = cli_runner.invoke(["status", "--output-format=json"])
        assert r.exit_code == 0, r
        changes = json.loads(r.stdout)["sno.status/v1"]["workingCopy"]["changes"]
        assert changes is None
        r = cli_runner.invoke(["diff", "--exit-code"])
        assert r.exit_code == 0, r


def test_approximated_types():
    assert gpkg_adapter.APPROXIMATED_TYPES == compute_approximated_types(
        gpkg_adapter.V2_TYPE_TO_GPKG_TYPE, gpkg_adapter.GPKG_TYPE_TO_V2_TYPE
    )


def test_types_roundtrip(data_working_copy, cli_runner):
    # If type-approximation roundtrip code isn't working,
    # we would get spurious diffs on types that GPKG doesn't support.
    with data_working_copy("types2") as (repo_path, wc):
        r = cli_runner.invoke(["diff", "--exit-code"])
        assert r.exit_code == 0, r.stdout


def test_reset_transaction(data_working_copy, cli_runner, geopackage, edit_points):
    with data_working_copy("points2") as (repo_path, wc):
        db = geopackage(wc)
        with db:
            cur = db.cursor()
            edit_points(cur)

        r = cli_runner.invoke(["status", "--output-format=json"])
        assert r.exit_code == 0, r
        changes = json.loads(r.stdout)["sno.status/v1"]["workingCopy"]["changes"]
        assert changes == {
            H.POINTS.LAYER: {"feature": {"inserts": 1, "updates": 2, "deletes": 5}}
        }

        db = geopackage(wc)
        with db:
            cur = db.cursor()
            # This modification makes the gpkg_sno_state table work like normal for reading,
            # but writing to it will fail due to the CHECK.
            cur.execute("ALTER TABLE gpkg_sno_state RENAME TO gpkg_sno_state_backup;")
            cur.execute("SELECT value FROM gpkg_sno_state_backup;")
            value = cur.fetchone()[0]
            cur.execute(
                f"""
                CREATE TABLE gpkg_sno_state
                    (table_name TEXT NOT NULL, key TEXT NOT NULL, value TEXT NULL CHECK(value = '{value}'));
                """
            )
            cur.execute(
                "INSERT INTO gpkg_sno_state SELECT * FROM gpkg_sno_state_backup;"
            )

        # This should fail and so the entire transaction should be rolled back.
        # Therefore, the GPKG should remain unchanged with 6 uncommitted changes -
        # even though the failed write to gpkg_sno_state happens after the changes
        # are discarded and after working copy is reset to the new commit - all of
        # that will be rolled back.
        with pytest.raises(apsw.ConstraintError):
            r = cli_runner.invoke(["checkout", "HEAD^", "--discard-changes"])

        with db:
            cur = db.cursor()
            cur.execute("DROP TABLE IF EXISTS gpkg_sno_state;")
            cur.execute("ALTER TABLE gpkg_sno_state_backup RENAME TO gpkg_sno_state;")

        r = cli_runner.invoke(["status", "--output-format=json"])
        assert r.exit_code == 0, r
        changes = json.loads(r.stdout)["sno.status/v1"]["workingCopy"]["changes"]
        assert changes == {
            H.POINTS.LAYER: {"feature": {"inserts": 1, "updates": 2, "deletes": 5}}
        }
