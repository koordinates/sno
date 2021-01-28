import contextlib
import itertools
import os
import re
import subprocess

from osgeo import gdal, ogr

import pygit2
import pytest

from sno import fast_import, gpkg
from sno.dataset2 import Dataset2
from sno.exceptions import INVALID_OPERATION
from sno.sqlalchemy import gpkg_engine
from sno.geometry import ogr_to_gpkg_geom, gpkg_geom_to_ogr
from sno.ogr_import_source import OgrImportSource, PostgreSQLImportSource
from sno.repo import SnoRepo


H = pytest.helpers.helpers()

# copied from test_init.py
GPKG_IMPORTS = (
    "archive,source_gpkg,table",
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
        pytest.param("gpkg-spec", "sample1_2.gpkg", "counties", id="spec_counties"),
        pytest.param(
            "gpkg-spec", "sample1_2.gpkg", "countiestbl", id="spec_counties_table"
        ),
    ],
)


def _import_check(repo_path, table, source_gpkg):
    repo = SnoRepo(repo_path)
    dataset = repo.datasets()[table]
    assert dataset.VERSION == 2

    with gpkg_engine(source_gpkg).connect() as db:
        num_rows = db.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0]

    o = subprocess.check_output(["git", "ls-tree", "-r", "-t", "HEAD", table])
    print("\n".join(l.decode("utf8") for l in o.splitlines()[:20]))

    if dataset.VERSION != 2:
        raise NotImplementedError(dataset.VERSION)

    re_paths = r"^\d{6} blob [0-9a-f]{40}\t%s/.sno-dataset/feature/.*$" % table
    git_paths = [m for m in re.findall(re_paths, o.decode("utf-8"), re.MULTILINE)]
    assert len(git_paths) == num_rows

    num_features = dataset.feature_count
    assert num_features == num_rows

    return dataset


def normalise_feature(row):
    row = dict(row)
    if "geom" in row:
        # We import via OGR, which strips envelopes by default
        row["geom"] = ogr_to_gpkg_geom(
            gpkg_geom_to_ogr(row["geom"], parse_crs=True),
        )
    return row


def without_ids(column_dicts):
    # Note that datasets use lru-caches for meta items, so we make sure not to modify the meta items.
    return [col_without_id(c) for c in column_dicts]


def col_without_id(column_dict):
    result = column_dict.copy()
    result.pop("id")
    return result


@pytest.mark.slow
@pytest.mark.parametrize(
    GPKG_IMPORTS[0],
    [
        *GPKG_IMPORTS[1],
        pytest.param(
            "gpkg-points", "nz-pa-points-topo-150k.gpkg", H.POINTS.LAYER, id="empty"
        ),
    ],
)
def test_import(
    archive,
    source_gpkg,
    table,
    data_archive,
    tmp_path,
    cli_runner,
    chdir,
    benchmark,
    request,
    monkeypatch,
):
    """ Import the GeoPackage (eg. `kx-foo-layer.gpkg`) into a Sno repository. """
    param_ids = H.parameter_ids(request)

    # wrap the fast_import_tables function with benchmarking
    orig_import_func = fast_import.fast_import_tables

    def _benchmark_import(*args, **kwargs):
        # one round/iteration isn't very statistical, but hopefully crude idea
        return benchmark.pedantic(
            orig_import_func, args=args, kwargs=kwargs, rounds=1, iterations=1
        )

    monkeypatch.setattr(fast_import, "fast_import_tables", _benchmark_import)

    with data_archive(archive) as data:
        # list tables
        repo_path = tmp_path / "data.sno"
        repo_path.mkdir()

        with gpkg_engine(data / source_gpkg).connect() as db:
            if param_ids[-1] == "empty":
                print(f"emptying table {table}...")
                db.execute(f"DELETE FROM {table};")

            num_rows = db.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0]
        benchmark.group = f"test_import - {param_ids[-1]} (N={num_rows})"

        if param_ids[-1] == "empty":
            assert num_rows == 0

        with chdir(repo_path):
            r = cli_runner.invoke(["init"])
            assert r.exit_code == 0, r

            repo = SnoRepo(repo_path)
            assert repo.is_empty

            r = cli_runner.invoke(["import", str(data / source_gpkg), table])
            assert r.exit_code == 0, r

            assert not repo.is_empty
            assert repo.head.name == "refs/heads/master"
            assert repo.head.shorthand == "master"

            # has a single commit
            assert len(list(repo.walk(repo.head.target))) == 1

            dataset = _import_check(repo_path, table, f"{data / source_gpkg}")

            with gpkg_engine(data / source_gpkg).connect() as db:
                pk_field = gpkg.pk(db, table)

                if num_rows > 0:
                    # compare the first feature in the repo against the source DB
                    feature = next(dataset.features())

                    row = normalise_feature(
                        db.execute(
                            f"SELECT * FROM {table} WHERE {pk_field}=?;",
                            [feature[pk_field]],
                        ).fetchone()
                    )
                    feature = normalise_feature(feature)
                    print("First Feature:", feature, row)
                    assert feature == row

                    # compare a source DB feature against the repo feature
                    row = normalise_feature(
                        db.execute(
                            f"SELECT * FROM {table} ORDER BY {pk_field} LIMIT 1 OFFSET {min(97,num_rows-1)};"
                        ).fetchone()
                    )

                    for feature in dataset.features():
                        if feature[pk_field] == row[pk_field]:
                            feature = normalise_feature(feature)
                            assert feature == row
                            break
                    else:
                        pytest.fail(
                            f"Couldn't find repo feature {pk_field}={row[pk_field]}"
                        )


def _compare_ogr_and_gpkg_meta_items(dataset, gpkg_dataset):
    """
    Compares the meta items from an OGR import, with those from a GPKG import.
    There are all sorts of caveats to the meta item emulation, and this attempts
    to avoid them when comparing.
    """
    ds_schema = without_ids(dataset.get_meta_item("schema.json"))
    gpkg_schema = without_ids(gpkg_dataset.get_meta_item("schema.json"))

    # SHP/TAB always call the primary key "FID"
    for col in gpkg_schema:
        if col["name"] == gpkg_dataset.primary_key:
            col["name"] = "FID"

    # Check the fields are in the right order. Ignore truncation and capitalisation
    ds_names = [col["name"][:8] for col in ds_schema]
    gpkg_names = [col["name"][:8] for col in gpkg_schema]
    assert ds_names == gpkg_names

    # now we've checked order, we can key by name
    ds_schema = {col.pop("name"): col for col in ds_schema}
    gpkg_schema = {col.pop("name"): col for col in gpkg_schema}

    # SHP/TAB field names must be <10 chars
    # When they're truncated, an underscore and integer suffix gets appended.
    # so the fieldname itself is truncated to 8 chars.
    remove_prefixes = set()
    for k in list(gpkg_schema.keys()):
        if k not in gpkg_schema:
            # already removed
            continue
        if len(k) > 10:
            remove_prefixes.add(k[:8])

    remove_prefixes = tuple(remove_prefixes)
    for k in list(gpkg_schema.keys()):
        if k.startswith(remove_prefixes):
            gpkg_schema.pop(k)
    for k in list(ds_schema.keys()):
        if k.startswith(remove_prefixes):
            ds_schema.pop(k)


@pytest.mark.slow
@pytest.mark.parametrize(
    "archive,source_gpkg,table",
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
            "gpkg-points", "nz-pa-points-topo-150k.gpkg", H.POINTS.LAYER, id="empty"
        ),
    ],
)
@pytest.mark.parametrize(
    "source_format,source_ogr_driver",
    [
        ("SHP", "ESRI Shapefile"),
        # https://github.com/koordinates/sno/issues/86
        # This test starts by converting a GPKG into a TAB, and then imports then TAB.
        # But the TAB ended up with very broken SRS info, and then during import GDAL
        # failed to find an EPSG code for the projection.
        # We can't currently work around this so we're disabling it.
        # A future release might add handling via an option (--srs=epsg:4167 for example)
        # ('TAB', 'MapInfo File')
    ],
    ids=["SHP"],
)
def test_import_from_non_gpkg(
    archive,
    source_gpkg,
    table,
    data_archive,
    tmp_path,
    cli_runner,
    chdir,
    request,
    source_format,
    source_ogr_driver,
):
    """
    Import something else into a Sno repository.
    """
    param_ids = H.parameter_ids(request)

    with data_archive(archive) as data:
        with gpkg_engine(data / source_gpkg).connect() as db:
            if param_ids[-1] == "empty":
                print(f"emptying table {table}...")
                db.execute(f"DELETE FROM {table};")

            num_rows = db.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0]

        if param_ids[-1] == "empty":
            assert num_rows == 0

        # First, import the original GPKG to one repo
        gpkg_repo_path = tmp_path / "gpkg"
        gpkg_repo_path.mkdir()
        with chdir(gpkg_repo_path):
            r = cli_runner.invoke(["init"])
            assert r.exit_code == 0, r
            r = cli_runner.invoke(["import", data / source_gpkg, table])
            assert r.exit_code == 0, r

        gpkg_repo = SnoRepo(gpkg_repo_path)
        gpkg_dataset = gpkg_repo.datasets()[table]

        # convert to a new format using OGR
        source_filename = tmp_path / f"data.{source_format.lower()}"
        gdal.VectorTranslate(
            str(source_filename),
            gdal.OpenEx(str(data / source_gpkg)),
            format=source_ogr_driver,
            layers=[table],
        )
        repo_path = tmp_path / "non-gpkg"
        repo_path.mkdir()
        with chdir(repo_path):
            r = cli_runner.invoke(["init"])
            assert r.exit_code == 0, r

            repo = SnoRepo(repo_path)
            assert repo.is_empty

            # Import from SHP/TAB/something into sno
            r = cli_runner.invoke(
                [
                    "import",
                    str(source_filename),
                    f"data:{table}",
                ]
            )
            assert r.exit_code == 0, r

            assert not repo.is_empty
            assert repo.head.name == "refs/heads/master"
            assert repo.head.shorthand == "master"

            # has a single commit
            assert len([c for c in repo.walk(repo.head.target)]) == 1

            dataset = _import_check(repo_path, table, f"{data / source_gpkg}")

            # Compare the meta items to the GPKG-imported ones
            repo = SnoRepo(repo_path)
            dataset = repo.datasets()[table]

            _compare_ogr_and_gpkg_meta_items(dataset, gpkg_dataset)

            if num_rows > 0:
                # compare the first feature in the repo against the source DB
                got_feature = next(dataset.features())
                pk = got_feature[dataset.primary_key]

                src_ds = ogr.Open(str(source_filename))
                src_layer = src_ds.GetLayer(0)
                assert src_layer.GetFeatureCount() == num_rows

                f = src_layer.GetFeature(pk)
                expected_feature = {
                    f.GetFieldDefnRef(i).GetName(): f.GetField(i)
                    for i in range(f.GetFieldCount())
                }
                if "date_adjus" in expected_feature:
                    expected_feature["date_adjus"] = expected_feature[
                        "date_adjus"
                    ].replace("/", "-")
                expected_feature["FID"] = f.GetFID()
                if src_layer.GetGeomType() != ogr.wkbNone:
                    g = f.GetGeometryRef()
                    if g:
                        g.AssignSpatialReference(src_layer.GetSpatialRef())
                    expected_feature["geom"] = ogr_to_gpkg_geom(g)

                assert normalise_feature(got_feature) == expected_feature


def test_shp_import_meta(
    data_archive,
    tmp_path,
    cli_runner,
    request,
):
    with data_archive("gpkg-polygons") as data:
        # convert to SHP using OGR
        source_filename = tmp_path / "nz_waca_adjustments.shp"
        gdal.VectorTranslate(
            str(source_filename),
            gdal.OpenEx(str(data / "nz-waca-adjustments.gpkg")),
            format="ESRI Shapefile",
            layers=["nz_waca_adjustments"],
        )

        # now import the SHP
        repo_path = tmp_path / "repo"
        r = cli_runner.invoke(["init", "--import", source_filename, str(repo_path)])
        assert r.exit_code == 0, r

        # now check metadata
        path = "nz_waca_adjustments"
        repo = SnoRepo(repo_path)
        dataset = repo.datasets()[path]

        meta_items = dict(dataset.meta_items())
        assert set(meta_items) == {
            "description",
            "schema.json",
            "title",
            "crs/EPSG:4167.wkt",
        }
        schema = without_ids(dataset.get_meta_item("schema.json"))
        assert schema == [
            {"name": "FID", "dataType": "integer", "primaryKeyIndex": 0, "size": 64},
            {
                "name": "geom",
                "dataType": "geometry",
                "geometryType": "POLYGON",
                "geometryCRS": "EPSG:4167",
            },
            {"name": "date_adjus", "dataType": "date"},
            {"name": "survey_ref", "dataType": "text", "length": 50},
            {
                "name": "adjusted_n",
                "dataType": "integer",
                "size": 32,
            },
        ]


def quote_ident(part):
    """
    SQL92 conformant identifier quoting, for use with OGR-dialect SQL
    (and most other dialects)
    """
    part = part.replace('"', '""')
    return f'"{part}"'


@pytest.fixture()
def postgis_layer(postgis_db, data_archive):
    postgres_conn_str = PostgreSQLImportSource.postgres_url_to_ogr_conn_str(
        os.environ["SNO_POSTGRES_URL"]
    )

    @contextlib.contextmanager
    def _postgis_layer(archive_name, gpkg_name, table):
        with data_archive(archive_name) as data:
            src_ds = gdal.OpenEx(str(data / gpkg_name), gdal.OF_VERBOSE_ERROR)
            dest_ds = gdal.OpenEx(
                postgres_conn_str,
                gdal.OF_VERBOSE_ERROR | gdal.OF_UPDATE,
                ["PostgreSQL"],
            )

            gdal.VectorTranslate(
                dest_ds,
                src_ds,
                format="PostgreSQL",
                accessMode="overwrite",
                layerCreationOptions=["LAUNDER=NO"],
                layers=[table],
            )
        yield
        with postgis_db.cursor() as c:
            c.execute(
                f"""
                DROP TABLE IF EXISTS {quote_ident(table)} CASCADE
                """
            )

    return _postgis_layer


def _test_postgis_import(
    repo_path,
    cli_runner,
    chdir,
    *,
    table_name,
    pk_name="id",
    pk_size=64,
    import_args=(),
):
    r = cli_runner.invoke(["init", repo_path])
    assert r.exit_code == 0, r
    with chdir(repo_path):
        r = cli_runner.invoke(
            [
                "import",
                os.environ["SNO_POSTGRES_URL"],
                table_name,
                *import_args,
            ]
        )
        assert r.exit_code == 0, r
    # now check metadata
    repo = SnoRepo(repo_path)
    dataset = repo.datasets()[table_name]

    meta_items = dict(dataset.meta_items())
    assert set(meta_items.keys()) == {
        "description",
        "schema.json",
        "title",
        "crs/EPSG:4167.wkt",
    }
    schema = without_ids(dataset.get_meta_item("schema.json"))
    assert schema == [
        {
            "name": pk_name,
            "dataType": "integer",
            "primaryKeyIndex": 0,
            "size": pk_size,
        },
        {
            "name": "geom",
            "dataType": "geometry",
            "geometryType": "MULTIPOLYGON",
            "geometryCRS": "EPSG:4167",
        },
        {"name": "date_adjusted", "dataType": "timestamp"},
        {"name": "survey_reference", "dataType": "text", "length": 50},
        {
            "name": "adjusted_nodes",
            "dataType": "integer",
            "size": 32,
        },
    ]


def test_postgis_import(
    postgis_layer,
    data_archive,
    tmp_path,
    cli_runner,
    request,
    chdir,
):
    with postgis_layer(
        "gpkg-polygons", "nz-waca-adjustments.gpkg", "nz_waca_adjustments"
    ):
        _test_postgis_import(
            tmp_path / "repo", cli_runner, chdir, table_name="nz_waca_adjustments"
        )


def test_postgis_import_from_view(
    postgis_db,
    postgis_layer,
    data_archive,
    tmp_path,
    cli_runner,
    request,
    chdir,
):
    with postgis_layer(
        "gpkg-polygons", "nz-waca-adjustments.gpkg", "nz_waca_adjustments"
    ):
        c = postgis_db.cursor()
        c.execute(
            """
            CREATE VIEW nz_waca_adjustments_view AS (
                SELECT * FROM nz_waca_adjustments
            )
        """
        )
        _test_postgis_import(
            tmp_path / "repo",
            cli_runner,
            chdir,
            table_name="nz_waca_adjustments_view",
            pk_name="id",
            pk_size=32,
            import_args=["--primary-key=id"],
        )


def test_postgis_import_from_view_with_ogc_fid(
    postgis_db,
    postgis_layer,
    data_archive,
    tmp_path,
    cli_runner,
    request,
    chdir,
):
    with postgis_layer(
        "gpkg-polygons", "nz-waca-adjustments.gpkg", "nz_waca_adjustments"
    ):
        c = postgis_db.cursor()

        c.execute(
            """
            CREATE VIEW nz_waca_adjustments_view AS (
                SELECT id AS ogc_fid, date_adjusted, survey_reference, adjusted_nodes, geom
                FROM nz_waca_adjustments
            )
        """
        )
        _test_postgis_import(
            tmp_path / "repo",
            cli_runner,
            chdir,
            table_name="nz_waca_adjustments_view",
            pk_name="ogc_fid",
            import_args=["--primary-key=ogc_fid"],
        )


def test_postgis_import_from_view_no_pk(
    postgis_db,
    postgis_layer,
    data_archive,
    tmp_path,
    cli_runner,
    request,
    chdir,
):
    repo_path = tmp_path / "repo"
    with postgis_layer(
        "gpkg-polygons", "nz-waca-adjustments.gpkg", "nz_waca_adjustments"
    ):
        c = postgis_db.cursor()
        c.execute(
            """
            CREATE VIEW nz_waca_adjustments_view AS (
                SELECT date_adjusted, survey_reference, adjusted_nodes, geom
                FROM nz_waca_adjustments
                WHERE id % 3 != 0
            )
        """
        )
        _test_postgis_import(
            repo_path,
            cli_runner,
            chdir,
            table_name="nz_waca_adjustments_view",
            pk_name="generated-pk",
        )

        repo = SnoRepo(repo_path)
        dataset = repo.datasets()["nz_waca_adjustments_view"]
        initial_pks = [f["generated-pk"] for f in dataset.features()]
        assert len(initial_pks) == 161
        assert max(initial_pks) == 161
        assert sorted(initial_pks) == list(range(1, 161 + 1))

        c.execute("DROP VIEW nz_waca_adjustments_view;")
        c.execute(
            """
            CREATE VIEW nz_waca_adjustments_view AS (
                SELECT date_adjusted, survey_reference, adjusted_nodes, geom
                FROM nz_waca_adjustments
                WHERE id % 3 != 1
            )
        """
        )

        r = cli_runner.invoke(
            [
                "--repo",
                str(repo_path.resolve()),
                "import",
                os.environ["SNO_POSTGRES_URL"],
                "nz_waca_adjustments_view",
                "--replace-existing",
                "--similarity-detection-limit=10",
            ]
        )
        assert r.exit_code == 0, r.stderr
        repo = SnoRepo(repo_path)
        dataset = repo.datasets()["nz_waca_adjustments_view"]
        new_pks = [f["generated-pk"] for f in dataset.features()]

        assert len(new_pks) == 159
        assert max(new_pks) == 228
        assert len(set(initial_pks) & set(new_pks)) == 92
        # 228 features total - but 161 are in the first group and 159 are in the second group
        # Means 92 features are in both, and should be imported with the same PK both times
        # 159 + 161 is 320, which is 92 more features than the actual total of 228

        # This is similar enough to be detected as an edit - only one field is different.
        c.execute(
            "UPDATE nz_waca_adjustments SET survey_reference='foo' WHERE id=1424927;"
        )
        # This is similar enough to be detected as an edit - only one field is different.
        c.execute(
            "UPDATE nz_waca_adjustments SET adjusted_nodes=12345678 WHERE id=1443053;"
        )
        # This will not be detected as an edit - two fields are different,
        # so it looks like one feature is deleted and a different one is inserted.
        c.execute(
            "UPDATE nz_waca_adjustments SET survey_reference='bar', adjusted_nodes=87654321 WHERE id=1452332;"
        )

        r = cli_runner.invoke(
            [
                "--repo",
                str(repo_path.resolve()),
                "import",
                os.environ["SNO_POSTGRES_URL"],
                "nz_waca_adjustments_view",
                "--replace-existing",
                "--similarity-detection-limit=10",
            ]
        )
        assert r.exit_code == 0, r.stderr
        r = cli_runner.invoke(["--repo", str(repo_path.resolve()), "show"])
        assert r.exit_code == 0, r.stderr
        # Two edits and one insert + delete:
        assert r.stdout.splitlines()[-19:] == [
            "",
            "--- nz_waca_adjustments_view:feature:1",
            "+++ nz_waca_adjustments_view:feature:1",
            "-                         survey_reference = ␀",
            "+                         survey_reference = foo",
            "--- nz_waca_adjustments_view:feature:2",
            "+++ nz_waca_adjustments_view:feature:2",
            "-                           adjusted_nodes = 1238",
            "+                           adjusted_nodes = 12345678",
            "--- nz_waca_adjustments_view:feature:3",
            "-                                     geom = MULTIPOLYGON(...)",
            "-                            date_adjusted = 2011-06-07T15:22:58Z",
            "-                         survey_reference = ␀",
            "-                           adjusted_nodes = 558",
            "+++ nz_waca_adjustments_view:feature:229",
            "+                                     geom = MULTIPOLYGON(...)",
            "+                            date_adjusted = 2011-06-07T15:22:58Z",
            "+                         survey_reference = bar",
            "+                           adjusted_nodes = 87654321",
        ]


def test_pk_encoding():
    ds = Dataset2(None, "mytable")

    assert (
        ds.encode_1pk_to_path(492183) == "mytable/.sno-dataset/feature/72/91/kc4AB4KX"
    )
    assert (
        ds.decode_path_to_1pk("mytable/.sno-dataset/feature/72/91/kc4AB4KX") == 492183
    )

    assert (
        ds.encode_1pk_to_path("Dave") == "mytable/.sno-dataset/feature/b2/fe/kaREYXZl"
    )
    assert (
        ds.decode_path_to_1pk("mytable/.sno-dataset/feature/b2/fe/kaREYXZl") == "Dave"
    )

    enc = [(i, ds.encode_1pk_to_path(i)) for i in range(-50000, 50000, 23)]
    assert len(set([k for i, k in enc])) == len(enc)

    for i, k in enc:
        assert ds.decode_path_to_1pk(k) == i


@pytest.mark.slow
@pytest.mark.parametrize(*GPKG_IMPORTS)
@pytest.mark.parametrize("profile", ["get_feature_by_pk", "get_feature_from_data"])
def test_feature_find_decode_performance(
    profile,
    archive,
    source_gpkg,
    table,
    data_archive,
    data_imported,
    benchmark,
    request,
):
    """ Check single-feature decoding performance """
    param_ids = H.parameter_ids(request)
    benchmark.group = (
        f"test_feature_find_decode_performance - {profile} - {param_ids[-1]}"
    )

    repo_path = data_imported(archive, source_gpkg, table)
    repo = SnoRepo(repo_path)
    tree = repo.head_tree / "mytable"
    dataset = repo.datasets()["mytable"]

    with data_archive(archive) as data:
        with gpkg_engine(data / source_gpkg).connect() as db:
            num_rows = db.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0]
            pk_field = gpkg.pk(db, table)
            pk = db.execute(
                f"SELECT {pk_field} FROM {table} ORDER BY {pk_field} LIMIT 1 OFFSET {min(97,num_rows-1)};"
            ).fetchone()[0]

    if profile == "get_feature_by_pk":
        benchmark(dataset.get_feature, pk)

    elif profile == "get_feature_from_data":
        feature_path = dataset.encode_1pk_to_path(pk, relative=True)
        feature_data = memoryview(tree / feature_path)

        benchmark(dataset.get_feature, path=feature_path, data=feature_data)
    else:
        raise NotImplementedError(f"Unknown profile: {profile}")


@pytest.mark.slow
def test_import_multiple(data_archive, chdir, cli_runner, tmp_path):
    repo_path = tmp_path / "data.sno"
    repo_path.mkdir()

    with chdir(repo_path):
        r = cli_runner.invoke(["init"])
        assert r.exit_code == 0, r

    repo = SnoRepo(repo_path)
    assert repo.is_empty

    LAYERS = (
        ("gpkg-points", "nz-pa-points-topo-150k.gpkg", H.POINTS.LAYER),
        ("gpkg-polygons", "nz-waca-adjustments.gpkg", H.POLYGONS.LAYER),
    )

    datasets = []
    for i, (archive, source_gpkg, table) in enumerate(LAYERS):
        with data_archive(archive) as data:
            with chdir(repo_path):
                r = cli_runner.invoke(["import", f"GPKG:{data / source_gpkg}", table])
                assert r.exit_code == 0, r

                datasets.append(
                    _import_check(
                        repo_path,
                        table,
                        f"{data / source_gpkg}",
                    )
                )

                assert len([c for c in repo.walk(repo.head.target)]) == i + 1

                if i + 1 == len(LAYERS):
                    r = cli_runner.invoke(
                        ["import", f"GPKG:{data / source_gpkg}", table]
                    )
                    assert r.exit_code == INVALID_OPERATION

    # has two commits
    assert len([c for c in repo.walk(repo.head.target)]) == len(LAYERS)

    tree = repo.head_tree

    for i, ds in enumerate(datasets):
        assert ds.path == LAYERS[i][2]

        feature = next(ds.features())
        f_path = ds.encode_1pk_to_path(feature[ds.primary_key])
        assert tree / f_path


def test_import_into_empty_branch(data_archive, cli_runner, chdir, tmp_path):
    repo_path = tmp_path / "data.sno"
    repo_path.mkdir()

    r = cli_runner.invoke(["init", "--bare", repo_path])
    assert r.exit_code == 0

    with data_archive("gpkg-points") as data:
        with chdir(repo_path):
            r = cli_runner.invoke(["import", data / "nz-pa-points-topo-150k.gpkg"])
            assert r.exit_code == 0, r

            # delete the master branch.
            # HEAD still points to it, but that's okay - this just means
            # the branch is empty.
            # We still need to be able to import from this state.
            repo = SnoRepo(repo_path)
            repo.references.delete("refs/heads/master")
            assert repo.head_is_unborn

            r = cli_runner.invoke(["import", data / "nz-pa-points-topo-150k.gpkg"])
            assert r.exit_code == 0, r

            repo = SnoRepo(repo_path)
            assert repo.head_commit


@pytest.mark.slow
@pytest.mark.parametrize(*GPKG_IMPORTS)
def test_write_feature_performance(
    archive,
    source_gpkg,
    table,
    data_archive,
    tmp_path,
    cli_runner,
    chdir,
    benchmark,
    request,
):
    """ Per-feature import performance. """
    param_ids = H.parameter_ids(request)

    with data_archive(archive) as data:
        # list tables
        repo_path = tmp_path / "data.sno"
        repo_path.mkdir()

        benchmark.group = f"test_write_feature_performance - {param_ids[-1]}"

        with chdir(repo_path):
            r = cli_runner.invoke(["init"])
            assert r.exit_code == 0, r

            repo = SnoRepo(repo_path)

            source = OgrImportSource.open(data / source_gpkg, table=table)
            with source:
                dataset = Dataset2(None, table)
                feature_iter = itertools.cycle(source.features())

                index = pygit2.Index()

                encode_kwargs = {"schema": source.schema}

                def _write_feature():
                    feature = next(feature_iter)
                    dest_path, dest_data = dataset.encode_feature(
                        feature, **encode_kwargs
                    )
                    blob_id = repo.create_blob(dest_data)
                    entry = pygit2.IndexEntry(
                        f"{dataset.path}/{dest_path}", blob_id, pygit2.GIT_FILEMODE_BLOB
                    )
                    index.add(entry)

                benchmark(_write_feature)


@pytest.mark.slow
def test_fast_import(data_archive, tmp_path, cli_runner, chdir):
    table = H.POINTS.LAYER
    with data_archive("gpkg-points") as data:
        # list tables
        repo_path = tmp_path / "data.sno"
        repo_path.mkdir()

        with chdir(repo_path):
            r = cli_runner.invoke(["init"])
            assert r.exit_code == 0, r

            repo = SnoRepo(repo_path)

            source = OgrImportSource.open(
                data / "nz-pa-points-topo-150k.gpkg", table=table
            )

            fast_import.fast_import_tables(repo, [source])

            assert not repo.is_empty
            assert repo.head.name == "refs/heads/master"
            assert repo.head.shorthand == "master"

            dataset = repo.datasets()[table]
            assert dataset.VERSION == 2

            # has a single commit
            assert len([c for c in repo.walk(repo.head.target)]) == 1
            assert list(dataset.meta_items())

            # has the right number of features
            feature_count = sum(1 for f in dataset.features())
            assert feature_count == source.feature_count
