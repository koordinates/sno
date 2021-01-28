import json
import shutil

import pytest

from sno.sqlalchemy import gpkg_engine
from sno.repo import SnoRepo
from sno.exceptions import (
    INVALID_OPERATION,
    NO_IMPORT_SOURCE,
    NO_TABLE,
)

H = pytest.helpers.helpers()

# also in test_structure.py
GPKG_IMPORTS = (
    "archive,gpkg,table",
    [
        pytest.param(
            "gpkg-points", "nz-pa-points-topo-150k.gpkg", H.POINTS.LAYER, id="points"
        ),
        pytest.param(
            "gpkg-polygons",
            "nz-waca-adjustments.gpkg",
            H.POLYGONS.LAYER,
            id="polygons",
        ),
        pytest.param(
            "gpkg-au-census",
            "census2016_sdhca_ot_short.gpkg",
            "census2016_sdhca_ot_ra_short",
            id="au-ra-short",
        ),
        pytest.param("gpkg-spec", "sample1_2.gpkg", "counties", id="spec-counties"),
        pytest.param(
            "gpkg-spec", "sample1_2.gpkg", "countiestbl", id="spec-counties-table"
        ),
        pytest.param("gpkg-stringpk", "stringpk.gpkg", "stringpk", id="stringpk"),
    ],
)


@pytest.mark.slow
def test_init_import_single_table_source(data_archive_readonly, tmp_path, cli_runner):
    with data_archive_readonly("gpkg-points") as data:
        r = cli_runner.invoke(
            [
                "init",
                "--import",
                data / "nz-pa-points-topo-150k.gpkg",
                str(tmp_path / "emptydir"),
            ]
        )
        # You don't have to specify a table if there's only one.
        assert r.exit_code == 0, r
        lines = r.stdout.splitlines()
        assert len(lines) >= 2
        assert "to nz_pa_points_topo_150k/ ..." in lines[1]
        assert lines[-1] == "Creating working copy at emptydir.gpkg ..."


@pytest.mark.slow
def test_init_import_custom_message(data_archive_readonly, tmp_path, cli_runner, chdir):
    with data_archive_readonly("gpkg-points") as data:
        r = cli_runner.invoke(
            [
                "init",
                "-m",
                "Custom message",
                "--import",
                data / "nz-pa-points-topo-150k.gpkg",
                str(tmp_path / "emptydir"),
            ]
        )
        assert r.exit_code == 0, r
        with chdir(tmp_path / "emptydir"):
            r = cli_runner.invoke(["log", "-1"])
        assert r.exit_code == 0, r
        assert "Custom message" in r.stdout


def test_import_table_with_prompt(data_archive_readonly, tmp_path, cli_runner, chdir):
    with data_archive_readonly("gpkg-au-census") as data:
        repo_path = tmp_path / "emptydir"
        r = cli_runner.invoke(["init", repo_path])
        assert r.exit_code == 0
        with chdir(repo_path):
            r = cli_runner.invoke(
                [
                    "import",
                    data / "census2016_sdhca_ot_short.gpkg",
                ],
                input="census2016_sdhca_ot_ced_short\n",
            )
            # Table was specified interactively via prompt
            assert r.exit_code == 0, r
        assert "Tables found:" in r.stdout
        assert (
            "  census2016_sdhca_ot_ced_short - census2016_sdhca_ot_ced_short"
            in r.stdout
        )
        assert "to census2016_sdhca_ot_ced_short/ ..." in r.stdout


def test_import_table_meta_overrides(
    data_archive_readonly, tmp_path, cli_runner, chdir
):
    with data_archive_readonly("gpkg-au-census") as data:
        repo_path = tmp_path / "emptydir"
        r = cli_runner.invoke(["init", repo_path])
        assert r.exit_code == 0
        with chdir(repo_path):
            original_xml_metadata = '<gmd:MD_Metadata xmlns:gco="http://www.isotc211.org/2005/gco" xmlns:gmd="http://www.isotc211.org/2005/gmd" xmlns:gml="http://www.opengis.net/gml" xmlns:gts="http://www.isotc211.org/2005/gts" xmlns:xlink="http://www.w3.org/1999/xlink" xmlns="http://www.isotc211.org/2005/gmd" />'
            table_info_json = json.dumps(
                {
                    "census2016_sdhca_ot_ced_short": {
                        "title": "test title",
                        "description": "test description",
                        "xmlMetadata": original_xml_metadata,
                    }
                }
            )
            r = cli_runner.invoke(
                [
                    "import",
                    data / "census2016_sdhca_ot_short.gpkg",
                    "census2016_sdhca_ot_ced_short",
                    "--table-info",
                    table_info_json,
                ],
            )
            assert r.exit_code == 0, r

            cli_runner.invoke(["checkout"])

            repo = SnoRepo(repo_path)
            wc = repo.working_copy
            with gpkg_engine(wc.path).connect() as db:
                title, description = db.execute(
                    """
                    SELECT c.identifier, c.description
                    FROM gpkg_contents c
                    WHERE c.table_name = 'census2016_sdhca_ot_ced_short'
                    """
                ).fetchone()
                assert title == "census2016_sdhca_ot_ced_short: test title"
                assert description == "test description"

                xml_metadata = db.execute(
                    """
                    SELECT m.metadata
                    FROM gpkg_metadata m JOIN gpkg_metadata_reference r
                    ON m.id = r.md_file_id
                    WHERE r.table_name = 'census2016_sdhca_ot_ced_short'
                    """
                ).fetchone()[0]
                assert xml_metadata == original_xml_metadata


def test_import_table_with_prompt_with_no_input(
    data_archive_readonly, tmp_path, cli_runner, chdir
):
    with data_archive_readonly("gpkg-au-census") as data:
        repo_path = tmp_path / "emptydir"
        r = cli_runner.invoke(["init", repo_path])
        assert r.exit_code == 0
        with chdir(repo_path):
            r = cli_runner.invoke(["import", data / "census2016_sdhca_ot_short.gpkg"])
            # Table was specified interactively via prompt
            assert r.exit_code == NO_TABLE, r
        assert "Tables found:" in r.stdout
        assert (
            "  census2016_sdhca_ot_ced_short - census2016_sdhca_ot_ced_short"
            in r.stdout
        )
        assert "No table specified" in r.stderr


def test_import_replace_existing(
    data_archive,
    tmp_path,
    cli_runner,
    chdir,
):
    with data_archive("gpkg-polygons") as data:
        repo_path = tmp_path / "emptydir"
        r = cli_runner.invoke(["init", repo_path])
        assert r.exit_code == 0
        with chdir(repo_path):
            r = cli_runner.invoke(
                [
                    "import",
                    data / "nz-waca-adjustments.gpkg",
                    "nz_waca_adjustments:mytable",
                ]
            )
            assert r.exit_code == 0, r.stderr

            # Now modify the source GPKG
            with gpkg_engine(data / "nz-waca-adjustments.gpkg").connect() as db:
                db.execute(
                    "UPDATE nz_waca_adjustments SET survey_reference = 'edited' WHERE id = 1424927"
                )

            r = cli_runner.invoke(
                [
                    "import",
                    "--replace-existing",
                    data / "nz-waca-adjustments.gpkg",
                    "nz_waca_adjustments:mytable",
                ]
            )
            assert r.exit_code == 0, r.stderr
            r = cli_runner.invoke(["show", "-o", "json"])
            assert r.exit_code == 0, r.stderr
            output = json.loads(r.stdout)
            assert output["sno.diff/v1+hexwkb"] == {
                "mytable": {
                    "feature": [
                        {
                            "-": {
                                "id": 1424927,
                                "geom": "01060000000100000001030000000100000012000000D2B47A3DAEEB65402E86A80212EF42C01D23796880EB6540D54A46E909EE42C03E7210197BEB6540B164332CEBED42C003ECE8DE70EB6540C99AB69AACED42C0916A8E626FEB654040F4DAAC9EED42C0615CA5D035EB6540F2B295FC50EB42C04AA3B89940EB6540D90F9D94DCEA42C00937B99972EB6540163FEB35F4E942C0B9103A5876EB65408D6D995DE5E942C008A85AD68FEB654069D2CB43DDE942C0D24A26924CEC6540C455AF6CB0EC42C0D21275304CEC6540E6CE3803B6EC42C018EA6B3714EC6540D17726991DEE42C00D91731C00EC65401BE20E8A9CEE42C0EBE45150F7EB6540D10F6A10D4EE42C01C6BD51EEDEB6540CD6886390AEF42C0FB975FA7EBEB6540DB85E63A0DEF42C0D2B47A3DAEEB65402E86A80212EF42C0",
                                "date_adjusted": "2011-03-25T07:30:45Z",
                                "survey_reference": None,
                                "adjusted_nodes": 1122,
                            },
                            "+": {
                                "id": 1424927,
                                "geom": "01060000000100000001030000000100000012000000D2B47A3DAEEB65402E86A80212EF42C01D23796880EB6540D54A46E909EE42C03E7210197BEB6540B164332CEBED42C003ECE8DE70EB6540C99AB69AACED42C0916A8E626FEB654040F4DAAC9EED42C0615CA5D035EB6540F2B295FC50EB42C04AA3B89940EB6540D90F9D94DCEA42C00937B99972EB6540163FEB35F4E942C0B9103A5876EB65408D6D995DE5E942C008A85AD68FEB654069D2CB43DDE942C0D24A26924CEC6540C455AF6CB0EC42C0D21275304CEC6540E6CE3803B6EC42C018EA6B3714EC6540D17726991DEE42C00D91731C00EC65401BE20E8A9CEE42C0EBE45150F7EB6540D10F6A10D4EE42C01C6BD51EEDEB6540CD6886390AEF42C0FB975FA7EBEB6540DB85E63A0DEF42C0D2B47A3DAEEB65402E86A80212EF42C0",
                                "date_adjusted": "2011-03-25T07:30:45Z",
                                "survey_reference": "edited",
                                "adjusted_nodes": 1122,
                            },
                        }
                    ]
                }
            }


def test_import_replace_existing_with_no_changes(
    data_archive,
    tmp_path,
    cli_runner,
    chdir,
):
    with data_archive("gpkg-polygons") as data:
        repo_path = tmp_path / "emptydir"
        r = cli_runner.invoke(["init", repo_path])
        assert r.exit_code == 0
        with chdir(repo_path):
            r = cli_runner.invoke(
                [
                    "import",
                    data / "nz-waca-adjustments.gpkg",
                    "nz_waca_adjustments:mytable",
                ]
            )
            assert r.exit_code == 0, r.stderr

            # now import the same thing over the top (no changes)
            r = cli_runner.invoke(
                [
                    "import",
                    "--replace-existing",
                    data / "nz-waca-adjustments.gpkg",
                    "nz_waca_adjustments:mytable",
                ]
            )
            assert r.exit_code == 44, r.stderr


def test_import_replace_existing_with_compatible_schema_changes(
    data_archive,
    tmp_path,
    cli_runner,
    chdir,
):
    with data_archive("gpkg-polygons") as data:
        repo_path = tmp_path / "emptydir"
        r = cli_runner.invoke(["init", repo_path])
        assert r.exit_code == 0
        with chdir(repo_path):
            r = cli_runner.invoke(
                [
                    "import",
                    data / "nz-waca-adjustments.gpkg",
                    "nz_waca_adjustments:mytable",
                ]
            )
            assert r.exit_code == 0, r.stderr

            # Now replace with a table which
            # * doesn't include the `survey_reference` column
            # * has the columns in a different order
            # * has a new column
            with gpkg_engine(data / "nz-waca-adjustments.gpkg").connect() as db:
                db.execute(
                    """
                        CREATE TABLE IF NOT EXISTS "nz_waca_adjustments_2" (
                            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                            "geom" MULTIPOLYGON,
                            "date_adjusted" DATETIME,
                            "adjusted_nodes" MEDIUMINT,
                            "newcolumn" TEXT
                        );
                    """
                )
                db.execute(
                    """
                        INSERT INTO nz_waca_adjustments_2 (id, geom, date_adjusted, adjusted_nodes, newcolumn)
                            SELECT id, geom, date_adjusted, adjusted_nodes, NULL FROM nz_waca_adjustments;
                    """
                )
                db.execute("""DROP TABLE nz_waca_adjustments;""")
                db.execute(
                    """ALTER TABLE "nz_waca_adjustments_2" RENAME TO "nz_waca_adjustments";"""
                )

            r = cli_runner.invoke(
                [
                    "import",
                    "--replace-existing",
                    data / "nz-waca-adjustments.gpkg",
                    "nz_waca_adjustments:mytable",
                ]
            )
            assert r.exit_code == 0, r.stderr
            r = cli_runner.invoke(["show", "-o", "json"])
            assert r.exit_code == 0, r.stderr
            diff = json.loads(r.stdout)["sno.diff/v1+hexwkb"]["mytable"]

            # The schema changed, but the features didn't.
            assert diff["meta"]["schema.json"]
            assert not diff.get("feature")

            repo = SnoRepo(repo_path)
            head_rs = repo.structure("HEAD")
            old_rs = repo.structure("HEAD^")
            assert head_rs.tree != old_rs.tree
            new_feature_tree = head_rs.tree / "mytable/.sno-dataset/feature"
            old_feature_tree = old_rs.tree / "mytable/.sno-dataset/feature"
            assert new_feature_tree == old_feature_tree


def test_import_replace_existing_with_column_renames(
    data_archive,
    tmp_path,
    cli_runner,
    chdir,
):
    with data_archive("gpkg-polygons") as data:
        repo_path = tmp_path / "emptydir"
        r = cli_runner.invoke(["init", repo_path])
        assert r.exit_code == 0
        with chdir(repo_path):
            r = cli_runner.invoke(
                [
                    "import",
                    data / "nz-waca-adjustments.gpkg",
                    "nz_waca_adjustments:mytable",
                ]
            )
            assert r.exit_code == 0, r.stderr

            # Now reanme
            # * doesn't include the `survey_reference` column
            # * has the columns in a different order
            # * has a new column
            with gpkg_engine(data / "nz-waca-adjustments.gpkg").connect() as db:
                db.execute(
                    """
                    ALTER TABLE "nz_waca_adjustments" RENAME COLUMN "survey_reference" TO "renamed_survey_reference";
                    """
                )

            r = cli_runner.invoke(
                [
                    "import",
                    "--replace-existing",
                    data / "nz-waca-adjustments.gpkg",
                    "nz_waca_adjustments:mytable",
                ]
            )
            assert r.exit_code == 0, r.stderr
            r = cli_runner.invoke(["show", "-o", "json"])
            assert r.exit_code == 0, r.stderr
            diff = json.loads(r.stdout)["sno.diff/v1+hexwkb"]["mytable"]

            # The schema changed, but the features didn't.
            assert diff["meta"]["schema.json"]
            assert not diff.get("feature")

            repo = SnoRepo(repo_path)
            head_rs = repo.structure("HEAD")
            old_rs = repo.structure("HEAD^")
            assert head_rs.tree != old_rs.tree
            new_feature_tree = head_rs.tree / "mytable/.sno-dataset/feature"
            old_feature_tree = old_rs.tree / "mytable/.sno-dataset/feature"
            assert new_feature_tree == old_feature_tree


def test_init_import_table_ogr_types(data_archive_readonly, tmp_path, cli_runner):
    with data_archive_readonly("gpkg-types") as data:
        repo_path = tmp_path / "repo"
        r = cli_runner.invoke(
            [
                "init",
                "--import",
                data / "types.gpkg",
                str(repo_path),
            ],
        )
        assert r.exit_code == 0, r.stderr

        # There's a bunch of wacky types in here, let's check them
        repo = SnoRepo(repo_path)
        wc = repo.working_copy
        with wc.session() as db:
            table_info = [
                dict(row) for row in db.execute("PRAGMA table_info('types');")
            ]
        assert table_info == [
            {
                "cid": 0,
                "name": "fid",
                "type": "INTEGER",
                "notnull": 1,
                "dflt_value": None,
                "pk": 1,
            },
            {
                "cid": 1,
                "name": "int16",
                "type": "SMALLINT",
                "notnull": 0,
                "dflt_value": None,
                "pk": 0,
            },
            {
                "cid": 2,
                "name": "int32",
                "type": "MEDIUMINT",
                "notnull": 0,
                "dflt_value": None,
                "pk": 0,
            },
            {
                "cid": 3,
                "name": "int64",
                "type": "INTEGER",
                "notnull": 0,
                "dflt_value": None,
                "pk": 0,
            },
            {
                "cid": 4,
                "name": "boolean",
                "type": "BOOLEAN",
                "notnull": 0,
                "dflt_value": None,
                "pk": 0,
            },
            {
                "cid": 5,
                "name": "double",
                "type": "REAL",
                "notnull": 0,
                "dflt_value": None,
                "pk": 0,
            },
            {
                "cid": 6,
                "name": "float32",
                "type": "FLOAT",
                "notnull": 0,
                "dflt_value": None,
                "pk": 0,
            },
            {
                "cid": 7,
                "name": "string",
                "type": "TEXT",
                "notnull": 0,
                "dflt_value": None,
                "pk": 0,
            },
            {
                "cid": 8,
                "name": "blob",
                "type": "BLOB",
                "notnull": 0,
                "dflt_value": None,
                "pk": 0,
            },
            {
                "cid": 9,
                "name": "date",
                "type": "DATE",
                "notnull": 0,
                "dflt_value": None,
                "pk": 0,
            },
            {
                "cid": 10,
                "name": "datetime",
                "type": "DATETIME",
                "notnull": 0,
                "dflt_value": None,
                "pk": 0,
            },
            {
                "cid": 11,
                "name": "time",
                "type": "TEXT",
                "notnull": 0,
                "dflt_value": None,
                "pk": 0,
            },
        ]


@pytest.mark.slow
@pytest.mark.parametrize(*GPKG_IMPORTS)
def test_init_import(
    archive,
    gpkg,
    table,
    data_archive,
    tmp_path,
    cli_runner,
    chdir,
):
    """ Import the GeoPackage (eg. `kx-foo-layer.gpkg`) into a Sno repository. """
    with data_archive(archive) as data:
        # list tables
        repo_path = tmp_path / "data.sno"
        repo_path.mkdir()

        r = cli_runner.invoke(
            [
                "init",
                "--import",
                f"gpkg:{data / gpkg}",
                str(repo_path),
            ]
        )
        assert r.exit_code == 0, r
        assert (repo_path / ".sno" / "HEAD").exists()

        repo = SnoRepo(repo_path)
        assert not repo.is_bare
        assert not repo.is_empty

        assert repo.head.name == "refs/heads/master"
        assert repo.head.shorthand == "master"

        # has a single commit
        assert len([c for c in repo.walk(repo.head.target)]) == 1

        # working copy exists
        wc = repo_path / f"{repo_path.stem}.gpkg"
        assert wc.exists() and wc.is_file()
        print("workingcopy at", wc)

        assert repo.config["sno.repository.version"] == "2"
        assert repo.config["sno.workingcopy.path"] == f"{wc.name}"

        with gpkg_engine(wc).connect() as db:
            assert H.row_count(db, table) > 0

            wc_tree_id = db.execute(
                """SELECT value FROM "gpkg_sno_state" WHERE table_name='*' AND key='tree';"""
            ).fetchone()[0]
            assert wc_tree_id == repo.head_tree.hex

            xml_metadata = db.execute(
                f"""
                SELECT m.metadata
                FROM gpkg_metadata m JOIN gpkg_metadata_reference r
                ON m.id = r.md_file_id
                WHERE r.table_name = '{table}'
                """
            ).fetchone()
            if table == "nz_pa_points_topo_150k":
                assert xml_metadata[0].startswith(
                    '<gmd:MD_Metadata xmlns:gco="http://www.isotc211.org/2005/gco"'
                )
            elif table == "nz_waca_adjustments":
                assert xml_metadata[0].startswith(
                    '<GDALMultiDomainMetadata>\n  <Metadata>\n    <MDI key="GPKG_METADATA_ITEM_1">'
                )
            else:
                assert not xml_metadata

            srs_definition = db.execute(
                f"""
                SELECT srs.definition
                FROM gpkg_spatial_ref_sys srs JOIN gpkg_geometry_columns geom
                ON srs.srs_id = geom.srs_id
                WHERE geom.table_name = '{table}'
                """
            ).fetchone()
            if table == "nz_pa_points_topo_150k":
                assert srs_definition[0].startswith(
                    'GEOGCS["WGS 84",\n    DATUM["WGS_1984"'
                )
            elif table == "nz_waca_adjustments":
                assert srs_definition[0].startswith(
                    'GEOGCS["NZGD2000",\n    DATUM["New_Zealand_Geodetic_Datum_2000"'
                )

            H.verify_gpkg_extent(db, table)
        with chdir(repo_path):
            # check that we can view the commit we created
            cli_runner.invoke(["show", "-o", "json"])


def test_init_import_commit_headers(
    data_archive,
    tmp_path,
    cli_runner,
    chdir,
):
    with data_archive("gpkg-points") as data:
        repo_path = tmp_path / "data.sno"
        repo_path.mkdir()

        r = cli_runner.invoke(
            [
                "init",
                "--import",
                str(data / "nz-pa-points-topo-150k.gpkg"),
                str(repo_path),
            ],
            env={
                "GIT_AUTHOR_DATE": "2000-1-1T00:00:00Z",
                "GIT_AUTHOR_NAME": "author",
                "GIT_AUTHOR_EMAIL": "author@example.com",
                "GIT_COMMITTER_DATE": "2010-1-1T00:00:00Z",
                "GIT_COMMITTER_NAME": "committer",
                "GIT_COMMITTER_EMAIL": "committer@example.com",
            },
        )
        assert r.exit_code == 0, r.stderr
        assert (repo_path / ".sno" / "HEAD").exists()
        r = cli_runner.invoke(["-C", str(repo_path), "log", "-o", "json"])
        assert r.exit_code == 0, r.stderr
        log_entry = json.loads(r.stdout)[0]
        log_entry.pop("commit")
        log_entry.pop("abbrevCommit")
        assert log_entry == {
            "message": "Import from nz-pa-points-topo-150k.gpkg:nz_pa_points_topo_150k to nz_pa_points_topo_150k/",
            "refs": ["HEAD -> master"],
            "authorName": "author",
            "authorEmail": "author@example.com",
            "authorTime": "2000-01-01T00:00:00Z",
            "authorTimeOffset": "+00:00",
            "committerEmail": "committer@example.com",
            "committerName": "committer",
            "commitTime": "2010-01-01T00:00:00Z",
            "commitTimeOffset": "+00:00",
            "parents": [],
            "abbrevParents": [],
        }


def test_init_import_name_clash(data_archive, cli_runner):
    """ Import the GeoPackage into a Sno repository of the same name, and checkout a working copy of the same name. """
    with data_archive("gpkg-editing") as data:
        r = cli_runner.invoke(["init", "--import", f"GPKG:editing.gpkg", "editing"])
        repo_path = data / "editing"

        assert r.exit_code == 0, r
        assert (repo_path / ".sno" / "HEAD").exists()

        repo = SnoRepo(repo_path)
        assert not repo.is_bare
        assert not repo.is_empty

        # working copy exists
        wc = repo_path / f"editing.gpkg"
        assert wc.exists() and wc.is_file()
        print("workingcopy at", wc)

        assert repo.config["sno.repository.version"] == "2"
        assert repo.config["sno.workingcopy.path"] == "editing.gpkg"

        with gpkg_engine(wc).connect() as db:
            wc_rowcount = H.row_count(db, "editing")
            assert wc_rowcount > 0

            wc_tree_id = db.execute(
                """SELECT value FROM "gpkg_sno_state" WHERE table_name='*' AND key='tree';"""
            ).fetchone()[0]
            assert wc_tree_id == repo.head_tree.hex

        # make sure we haven't stuffed up the original file
        with gpkg_engine("editing.gpkg").connect() as dbo:
            r = dbo.execute("SELECT 1 FROM sqlite_master WHERE name='gpkg_sno_state';")
            assert not r.fetchone()
            source_rowcount = dbo.execute("SELECT COUNT(*) FROM editing;").fetchone()[0]
            assert source_rowcount == wc_rowcount


@pytest.mark.slow
def test_init_import_errors(data_archive, tmp_path, chdir, cli_runner):
    gpkg = "census2016_sdhca_ot_short.gpkg"

    with data_archive("gpkg-au-census") as data:
        repo_path = tmp_path / "data.sno"
        repo_path.mkdir()
        with chdir(repo_path):
            r = cli_runner.invoke(["init", "--import", f"fred:thingz"])
            assert r.exit_code == NO_IMPORT_SOURCE, r
            assert "fred:thingz' doesn't appear to be valid" in r.stderr

            r = cli_runner.invoke(["init", "--import", f"gpkg:thingz.gpkg"])
            assert r.exit_code == NO_IMPORT_SOURCE, r
            assert "Couldn't find 'thingz.gpkg'" in r.stderr

            # not empty
            (repo_path / "a.file").touch()
            r = cli_runner.invoke(
                ["init", "--import", f"gpkg:{data/gpkg}", str(repo_path)]
            )
            assert r.exit_code == INVALID_OPERATION, r
            assert "isn't empty" in r.stderr


def test_init_empty(tmp_path, cli_runner, chdir):
    """ Create an empty Sno repository. """
    repo_path = tmp_path / "data.sno"
    repo_path.mkdir()

    # empty dir
    r = cli_runner.invoke(["init", str(repo_path)])
    assert r.exit_code == 0, r
    assert (repo_path / ".sno" / "HEAD").exists()

    # makes dir tree
    repo_path = tmp_path / "foo" / "bar" / "wiz.sno"
    r = cli_runner.invoke(["init", str(repo_path)])
    assert r.exit_code == 0, r
    assert (repo_path / ".sno" / "HEAD").exists()

    # current dir
    repo_path = tmp_path / "planet.sno"
    repo_path.mkdir()
    with chdir(repo_path):
        r = cli_runner.invoke(["init"])
        assert r.exit_code == 0, r
        assert (repo_path / ".sno" / "HEAD").exists()

    # dir isn't empty
    repo_path = tmp_path / "tree"
    repo_path.mkdir()
    (repo_path / "a.file").touch()
    r = cli_runner.invoke(["init", str(repo_path)])
    assert r.exit_code == INVALID_OPERATION, r
    assert not (repo_path / ".sno" / "HEAD").exists()

    # current dir isn't empty
    with chdir(repo_path):
        r = cli_runner.invoke(["init"])
        assert r.exit_code == INVALID_OPERATION, r
        assert not (repo_path / ".sno" / "HEAD").exists()


@pytest.mark.slow
def test_init_import_alt_names(data_archive, tmp_path, cli_runner, chdir):
    """ Import the GeoPackage (eg. `kx-foo-layer.gpkg`) into a Sno repository. """
    repo_path = tmp_path / "data.sno"
    repo_path.mkdir()

    r = cli_runner.invoke(["init", str(repo_path), "--workingcopy-path=wc.gpkg"])
    assert r.exit_code == 0, r

    ARCHIVE_PATHS = (
        (
            "gpkg-points",
            "nz-pa-points-topo-150k.gpkg",
            "nz_pa_points_topo_150k",
            "pa_sites",
        ),
        (
            "gpkg-polygons",
            "nz-waca-adjustments.gpkg",
            "nz_waca_adjustments",
            "misc/waca",
        ),
        (
            "gpkg-polygons",
            "nz-waca-adjustments.gpkg",
            "nz_waca_adjustments",
            "other/waca2",
        ),
    )

    for archive, source_gpkg, source_table, import_path in ARCHIVE_PATHS:
        with data_archive(archive) as source_path:
            with chdir(repo_path):
                r = cli_runner.invoke(
                    [
                        "import",
                        f"GPKG:{source_path / source_gpkg}",
                        f"{source_table}:{import_path}",
                    ]
                )
                assert r.exit_code == 0, r

    with chdir(repo_path):
        # working copy exists
        with gpkg_engine("wc.gpkg").connect() as db:

            expected_tables = set(a[3].replace("/", "__") for a in ARCHIVE_PATHS)
            db_tables = set(
                r[0]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table';"
                )
            )
            assert expected_tables <= db_tables

            for gpkg_t in (
                "gpkg_contents",
                "gpkg_geometry_columns",
                "gpkg_metadata_reference",
            ):
                table_list = set(
                    r[0]
                    for r in db.execute(f"SELECT DISTINCT table_name FROM {gpkg_t};")
                )
                assert expected_tables >= table_list, gpkg_t

        r = cli_runner.invoke(["diff"])
        assert r.exit_code == 0, r
        assert r.stdout.splitlines() == []


@pytest.mark.slow
def test_init_import_home_resolve(
    data_archive, tmp_path, cli_runner, chdir, monkeypatch, git_user_config
):
    """ Import from a ~-specified gpkg path """
    repo_path = tmp_path / "data.sno"
    repo_path.mkdir()

    r = cli_runner.invoke(["init", str(repo_path)])
    assert r.exit_code == 0, r

    with data_archive("gpkg-points") as source_path:
        with chdir(repo_path):
            monkeypatch.setenv("HOME", str(source_path))

            # make sure we have a .gitconfig file in HOME,
            # otherwise sno can't find the user information for the commit
            orig_home = git_user_config[2]
            shutil.copy2(orig_home / ".gitconfig", source_path)

            r = cli_runner.invoke(
                [
                    "import",
                    "GPKG:~/nz-pa-points-topo-150k.gpkg",
                    "nz_pa_points_topo_150k",
                ]
            )
            assert r.exit_code == 0, r


@pytest.mark.slow
def test_import_existing_wc(
    data_archive,
    data_working_copy,
    cli_runner,
    insert,
    tmp_path,
    request,
    chdir,
):
    """ Import a new dataset into a repo with an existing working copy. Dataset should get checked out """
    with data_working_copy("points") as (repo_path, wcdb):
        with data_archive("gpkg-polygons") as source_path, chdir(repo_path):
            r = cli_runner.invoke(
                [
                    "import",
                    f"GPKG:{source_path / 'nz-waca-adjustments.gpkg'}",
                    H.POLYGONS.LAYER,
                ]
            )
            assert r.exit_code == 0, r

        repo = SnoRepo(repo_path)
        wc = repo.working_copy
        with wc.session() as db:
            assert H.row_count(db, "nz_waca_adjustments") > 0
        assert wc.get_db_tree() == repo.head_tree.id.hex

        r = cli_runner.invoke(["status"])
        assert r.exit_code == 0, r
        assert r.stdout.splitlines()[-1] == "Nothing to commit, working copy clean"

        with wc.session() as db:
            r = db.execute(
                "DELETE FROM nz_waca_adjustments WHERE rowid IN (SELECT rowid FROM nz_waca_adjustments ORDER BY id LIMIT 10);"
            )
            assert r.rowcount == 10

        with data_archive("gpkg-polygons") as source_path, chdir(repo_path):
            r = cli_runner.invoke(
                [
                    "import",
                    f"GPKG:{source_path / 'nz-waca-adjustments.gpkg'}",
                    f"{H.POLYGONS.LAYER}:waca2",
                ]
            )
            assert r.exit_code == 0, r

        with wc.session() as db:
            assert H.row_count(db, "waca2") > 0
        assert wc.get_db_tree() == repo.head_tree.id.hex

        r = cli_runner.invoke(["status"])
        assert r.exit_code == 0, r
        assert r.stdout.splitlines()[-3:] == [
            "  nz_waca_adjustments:",
            "    feature:",
            "      10 deletes",
        ]


def test_init_import_detached_head(data_working_copy, data_archive, chdir, cli_runner):
    with data_working_copy("points") as (repo_path, wcdb):
        with data_archive("gpkg-polygons") as source_path, chdir(repo_path):
            r = cli_runner.invoke(["checkout", "HEAD^"])
            repo = SnoRepo(repo_path)
            assert repo.head_is_detached
            initial_head = repo.head.target.hex

            r = cli_runner.invoke(
                [
                    "import",
                    f"GPKG:{source_path / 'nz-waca-adjustments.gpkg'}",
                    H.POLYGONS.LAYER,
                ]
            )
            assert r.exit_code == 0, r
            assert repo.head_is_detached
            assert repo.head.target.hex != initial_head
            assert repo.revparse_single("HEAD^").hex == initial_head
