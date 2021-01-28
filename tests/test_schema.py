import json

import click
import pytest

from sno.diff_output import schema_diff_as_text
from sno.sqlalchemy import gpkg_engine
from sno.schema import Schema, ColumnSchema


H = pytest.helpers.helpers()


@pytest.fixture
def pki(gen_uuid):
    def _pki(pk_index):
        # Returns an arbitrary ColumnSchema, but with the given pk_index property.
        id = gen_uuid()
        return ColumnSchema(id, id[:8], "integer", pk_index)

    return _pki


def test_valid_schemas(pki):
    Schema([pki(None), pki(None), pki(None)])
    Schema([pki(2), pki(1), pki(0)])
    Schema([pki(None), pki(1), pki(None), pki(0)])


def test_invalid_schemas(pki):
    with pytest.raises(ValueError):
        Schema([pki(0), pki(None), pki(2)])
    with pytest.raises(ValueError):
        Schema([pki(0), pki(1), pki(1)])
    with pytest.raises(ValueError):
        Schema([pki(-1), pki(None), pki(None)])


def test_align_schema(gen_uuid):
    old_schema = Schema(
        [
            ColumnSchema(gen_uuid(), "ID", "integer", 0),
            ColumnSchema(gen_uuid(), "first_name", "text", None),
            ColumnSchema(gen_uuid(), "last_name", "text", None),
            ColumnSchema(gen_uuid(), "date_of_birth", "date", None),
        ]
    )
    new_schema = Schema(
        [
            ColumnSchema(gen_uuid(), "personnel_id", "integer", 0),
            ColumnSchema(gen_uuid(), "tax_file_number", "text", None),
            ColumnSchema(gen_uuid(), "last_name", "text", None),
            ColumnSchema(gen_uuid(), "first_name", "text", None),
            ColumnSchema(gen_uuid(), "middle_names", "text", None),
        ]
    )
    aligned_schema = old_schema.align_to_self(new_schema)

    assert [c.name for c in aligned_schema] == [
        "personnel_id",
        "tax_file_number",
        "last_name",
        "first_name",
        "middle_names",
    ]

    aligned = {}
    for old_col in old_schema:
        for aligned_col in aligned_schema:
            if aligned_col.id == old_col.id:
                aligned[old_col.name] = aligned_col.name

    assert aligned == {
        "ID": "personnel_id",
        "first_name": "first_name",
        "last_name": "last_name",
    }

    diff_counts = old_schema.diff_type_counts(aligned_schema)
    assert diff_counts == {
        "inserts": 2,
        "deletes": 1,
        "name_updates": 1,
        "position_updates": 1,
        "type_updates": 0,
        "pk_updates": 0,
    }


def test_align_schema_type_changed(gen_uuid):
    # Make sure we don't align columns if they are different types:
    old_schema = Schema(
        [
            ColumnSchema(gen_uuid(), "ID", "integer", 0),
            ColumnSchema(gen_uuid(), "col1", "numeric", None),
        ]
    )
    new_schema = Schema(
        [
            ColumnSchema(gen_uuid(), "ID", "integer", 0),
            ColumnSchema(gen_uuid(), "col1", "timestamp", None),
        ]
    )
    aligned_schema = old_schema.align_to_self(
        new_schema, approximated_types={"numeric": "text"}
    )

    aligned = {}
    for old_col in old_schema:
        for aligned_col in aligned_schema:
            if aligned_col.id == old_col.id:
                aligned[old_col.name] = aligned_col.name

    assert aligned == {
        "ID": "ID",
    }

    # But we do align them if they are approximated:
    new_schema = Schema(
        [
            ColumnSchema(gen_uuid(), "ID", "integer", 0),
            ColumnSchema(gen_uuid(), "col1", "text", None),
        ]
    )
    aligned_schema = old_schema.align_to_self(
        new_schema, approximated_types={"numeric": "text"}
    )

    aligned = {}
    for old_col in old_schema:
        for aligned_col in aligned_schema:
            if aligned_col.id == old_col.id:
                aligned[old_col.name] = aligned_col.name

    assert aligned == {"ID": "ID", "col1": "col1"}


def edit_points_schema(db):
    db.execute(f"""ALTER TABLE "{H.POINTS.LAYER}" ADD COLUMN "colour" TEXT(32);""")
    INSERT = f"""
        INSERT INTO "{H.POINTS.LAYER}" (fid, geom, t50_fid, name_ascii, macronated, name, colour)
        VALUES (:fid, GeomFromEWKT(:geom), :t50_fid, :name_ascii, :macronated, :name, :colour);
        """
    RECORD = {
        "fid": 9999,
        "geom": "POINT(0 0)",
        "t50_fid": 9_999_999,
        "name_ascii": "Te Motu-a-kore",
        "macronated": False,
        "name": "Te Motu-a-kore",
        "colour": "red",
    }
    r = db.execute(INSERT, RECORD)
    assert r.rowcount == 1
    r = db.execute(f"UPDATE {H.POINTS.LAYER} SET name='test' WHERE fid=1;")
    assert r.rowcount == 1
    r = db.execute(
        f"UPDATE {H.POINTS.LAYER} SET name='blue house', colour='blue' WHERE fid=2;"
    )
    assert r.rowcount == 1


def edit_polygons_schema(db):
    db.execute(f"""ALTER TABLE "{H.POLYGONS.LAYER}" ADD COLUMN "colour" TEXT(32);""")
    db.execute(
        f"""ALTER TABLE "{H.POLYGONS.LAYER}" RENAME COLUMN "survey_reference" TO "surv_ref";"""
    )

    INSERT = f"""
        INSERT INTO "{H.POLYGONS.LAYER}" (id, geom, date_adjusted, surv_ref, adjusted_nodes, colour)
        VALUES (:id, GeomFromEWKT(:geom), :date_adjusted, :surv_ref, :adjusted_nodes, :colour);
        """
    RECORD = {
        "id": 9_999_999,
        "geom": "POLYGON((0 0, 0 0.001, 0.001 0.001, 0.001 0, 0 0))",
        "date_adjusted": "2019-07-05T13:04:00+01:00",
        "surv_ref": "Null Island™ 🗺",
        "adjusted_nodes": 123,
        "colour": None,
    }

    r = db.execute(INSERT, RECORD)
    assert r.rowcount == 1
    r = db.execute(f"UPDATE {H.POLYGONS.LAYER} SET surv_ref='test' WHERE id=1443053;")
    assert r.rowcount == 1
    r = db.execute(f"UPDATE {H.POLYGONS.LAYER} SET colour='yellow' WHERE id=1452332;")
    assert r.rowcount == 1


def edit_table_schema(db):
    db.execute(f"""ALTER TABLE "{H.TABLE.LAYER}" ADD COLUMN "COLOUR" TEXT(32);""")

    INSERT = f"""
        INSERT INTO {H.TABLE.LAYER}
            (OBJECTID, NAME, STATE_NAME, STATE_FIPS, CNTY_FIPS, FIPS, AREA, POP1990, POP2000, POP90_SQMI, Shape_Leng, Shape_Area, COLOUR)
        VALUES
            (:OBJECTID, :NAME, :STATE_NAME, :STATE_FIPS, :CNTY_FIPS, :FIPS, :AREA, :POP1990, :POP2000, :POP90_SQMI, :Shape_Leng, :Shape_Area, :COLOUR);
        """

    r = db.execute(INSERT, {**H.TABLE.RECORD, "COLOUR": "blue"})
    assert r.rowcount == 1
    r = db.execute(f"UPDATE {H.TABLE.LAYER} SET OBJECTID=9998 WHERE OBJECTID=1;")
    assert r.rowcount == 1
    r = db.execute(f"UPDATE {H.TABLE.LAYER} SET name='test' WHERE OBJECTID=2;")
    assert r.rowcount == 1
    r = db.execute(f"UPDATE {H.TABLE.LAYER} SET COLOUR='white' WHERE OBJECTID=3;")
    assert r.rowcount == 1


DIFF_OUTPUT_FORMATS = ["text", "geojson", "json"]


@pytest.mark.parametrize("output_format", DIFF_OUTPUT_FORMATS)
def test_edit_schema_points(output_format, data_working_copy, cli_runner):
    with data_working_copy("points") as (repo, wc_path):
        # empty
        r = cli_runner.invoke(["diff", "--output-format=text", "--exit-code"])
        assert r.exit_code == 0, r

        # make some changes
        with gpkg_engine(wc_path).connect() as db:
            edit_points_schema(db)

        r = cli_runner.invoke(
            ["diff", f"--output-format={output_format}", "--output=-"]
        )

        check_points_diff_output(r, output_format)

        r = cli_runner.invoke(["commit", "-m", "schema change + feature changes"])
        r = cli_runner.invoke(["diff", "--output-format=quiet"])
        assert r.exit_code == 0, r

        r = cli_runner.invoke(
            ["diff", "HEAD^...HEAD", f"--output-format={output_format}", "--output=-"]
        )
        check_points_diff_output(r, output_format)


@pytest.mark.parametrize("output_format", DIFF_OUTPUT_FORMATS)
def test_edit_schema_polygons(output_format, data_working_copy, cli_runner):
    with data_working_copy("polygons") as (repo, wc_path):
        # empty
        r = cli_runner.invoke(["diff", "--output-format=quiet"])
        assert r.exit_code == 0, r

        # make some changes
        with gpkg_engine(wc_path).connect() as db:
            edit_polygons_schema(db)

        r = cli_runner.invoke(
            ["diff", f"--output-format={output_format}", "--output=-"]
        )

        check_polygons_diff_output(r, output_format)

        r = cli_runner.invoke(["commit", "-m", "schema change + feature changes"])
        r = cli_runner.invoke(["diff", "--output-format=quiet"])
        assert r.exit_code == 0, r

        r = cli_runner.invoke(
            ["diff", "HEAD^...HEAD", f"--output-format={output_format}", "--output=-"]
        )
        check_polygons_diff_output(r, output_format)


@pytest.mark.parametrize("output_format", DIFF_OUTPUT_FORMATS)
def test_edit_schema_table(output_format, data_working_copy, cli_runner):
    with data_working_copy("table") as (repo, wc_path):
        # empty
        r = cli_runner.invoke(["diff", "--output-format=quiet"])
        assert r.exit_code == 0, r

        # make some changes
        with gpkg_engine(wc_path).connect() as db:
            edit_table_schema(db)

        r = cli_runner.invoke(
            ["diff", f"--output-format={output_format}", "--output=-"]
        )
        assert r.exit_code == 0, r

        if output_format == "text":
            orig_diff_output = r.stdout.splitlines()
        else:
            orig_diff_output = json.loads(r.stdout)

        r = cli_runner.invoke(["commit", "-m", "schema change + feature changes"])
        r = cli_runner.invoke(["diff", "--output-format=quiet"])
        assert r.exit_code == 0, r

        r = cli_runner.invoke(
            ["diff", "HEAD^...HEAD", f"--output-format={output_format}", "--output=-"]
        )
        assert r.exit_code == 0, r
        if output_format == "text":
            committed_diff_output = r.stdout.splitlines()
        else:
            committed_diff_output = json.loads(r.stdout)

        assert committed_diff_output == orig_diff_output


def check_points_diff_output(r, output_format):
    if output_format == "text":
        assert r.exit_code == 0, r
        assert r.stdout.splitlines() == [
            "--- nz_pa_points_topo_150k:meta:schema.json",
            "+++ nz_pa_points_topo_150k:meta:schema.json",
            "  [",
            "    {",
            '      "id": "e97b4015-2765-3a33-b174-2ece5c33343b",',
            '      "name": "fid",',
            '      "dataType": "integer",',
            '      "primaryKeyIndex": 0,',
            '      "size": 64',
            "    },",
            "    {",
            '      "id": "f488ae9b-6e15-1fe3-0bda-e0d5d38ea69e",',
            '      "name": "geom",',
            '      "dataType": "geometry",',
            '      "geometryType": "POINT",',
            '      "geometryCRS": "EPSG:4326"',
            "    },",
            "    {",
            '      "id": "4a1c7a86-c425-ea77-7f1a-d74321a10edc",',
            '      "name": "t50_fid",',
            '      "dataType": "integer",',
            '      "size": 32',
            "    },",
            "    {",
            '      "id": "d2a62351-a66d-bde2-ce3e-356fec9641e9",',
            '      "name": "name_ascii",',
            '      "dataType": "text",',
            '      "length": 75',
            "    },",
            "    {",
            '      "id": "c3389414-a511-5385-7dcd-891c4ead1663",',
            '      "name": "macronated",',
            '      "dataType": "text",',
            '      "length": 1',
            "    },",
            "    {",
            '      "id": "45b00eaa-5700-662d-8a21-9614e40c437b",',
            '      "name": "name",',
            '      "dataType": "text",',
            '      "length": 75',
            "    },",
            "+   {",
            '+     "id": "28c65b9a-2cd2-5507-a5b1-b3267c513fc3",',
            '+     "name": "colour",',
            '+     "dataType": "text",',
            '+     "length": 32',
            "+   },",
            "  ]",
            "--- nz_pa_points_topo_150k:feature:1",
            "+++ nz_pa_points_topo_150k:feature:1",
            "-                                     name = ␀",
            "+                                     name = test",
            "+                                   colour = ␀",
            "--- nz_pa_points_topo_150k:feature:2",
            "+++ nz_pa_points_topo_150k:feature:2",
            "-                                     name = ␀",
            "+                                     name = blue house",
            "+                                   colour = blue",
            "+++ nz_pa_points_topo_150k:feature:9999",
            "+                                     geom = POINT(...)",
            "+                                  t50_fid = 9999999",
            "+                               name_ascii = Te Motu-a-kore",
            "+                               macronated = 0",
            "+                                     name = Te Motu-a-kore",
            "+                                   colour = red",
        ]
    elif output_format == "json":
        assert r.exit_code == 0, r
        assert json.loads(r.stdout) == {
            "sno.diff/v1+hexwkb": {
                "nz_pa_points_topo_150k": {
                    "meta": {
                        "schema.json": {
                            "-": [
                                {
                                    "dataType": "integer",
                                    "id": "e97b4015-2765-3a33-b174-2ece5c33343b",
                                    "name": "fid",
                                    "primaryKeyIndex": 0,
                                    "size": 64,
                                },
                                {
                                    "dataType": "geometry",
                                    "geometryCRS": "EPSG:4326",
                                    "geometryType": "POINT",
                                    "id": "f488ae9b-6e15-1fe3-0bda-e0d5d38ea69e",
                                    "name": "geom",
                                },
                                {
                                    "dataType": "integer",
                                    "id": "4a1c7a86-c425-ea77-7f1a-d74321a10edc",
                                    "name": "t50_fid",
                                    "size": 32,
                                },
                                {
                                    "dataType": "text",
                                    "id": "d2a62351-a66d-bde2-ce3e-356fec9641e9",
                                    "length": 75,
                                    "name": "name_ascii",
                                },
                                {
                                    "dataType": "text",
                                    "id": "c3389414-a511-5385-7dcd-891c4ead1663",
                                    "length": 1,
                                    "name": "macronated",
                                },
                                {
                                    "dataType": "text",
                                    "id": "45b00eaa-5700-662d-8a21-9614e40c437b",
                                    "length": 75,
                                    "name": "name",
                                },
                            ],
                            "+": [
                                {
                                    "dataType": "integer",
                                    "id": "e97b4015-2765-3a33-b174-2ece5c33343b",
                                    "name": "fid",
                                    "primaryKeyIndex": 0,
                                    "size": 64,
                                },
                                {
                                    "dataType": "geometry",
                                    "geometryCRS": "EPSG:4326",
                                    "geometryType": "POINT",
                                    "id": "f488ae9b-6e15-1fe3-0bda-e0d5d38ea69e",
                                    "name": "geom",
                                },
                                {
                                    "dataType": "integer",
                                    "id": "4a1c7a86-c425-ea77-7f1a-d74321a10edc",
                                    "name": "t50_fid",
                                    "size": 32,
                                },
                                {
                                    "dataType": "text",
                                    "id": "d2a62351-a66d-bde2-ce3e-356fec9641e9",
                                    "length": 75,
                                    "name": "name_ascii",
                                },
                                {
                                    "dataType": "text",
                                    "id": "c3389414-a511-5385-7dcd-891c4ead1663",
                                    "length": 1,
                                    "name": "macronated",
                                },
                                {
                                    "dataType": "text",
                                    "id": "45b00eaa-5700-662d-8a21-9614e40c437b",
                                    "length": 75,
                                    "name": "name",
                                },
                                {
                                    "dataType": "text",
                                    "id": "28c65b9a-2cd2-5507-a5b1-b3267c513fc3",
                                    "length": 32,
                                    "name": "colour",
                                },
                            ],
                        }
                    },
                    "feature": [
                        {
                            "+": {
                                "colour": None,
                                "fid": 1,
                                "geom": "010100000097F3EF201223664087D715268E0043C0",
                                "macronated": "N",
                                "name": "test",
                                "name_ascii": None,
                                "t50_fid": 2426271,
                            },
                            "-": {
                                "fid": 1,
                                "geom": "010100000097F3EF201223664087D715268E0043C0",
                                "macronated": "N",
                                "name": None,
                                "name_ascii": None,
                                "t50_fid": 2426271,
                            },
                        },
                        {
                            "+": {
                                "colour": "blue",
                                "fid": 2,
                                "geom": "0101000000E702F16784226640ADE666D77CFE42C0",
                                "macronated": "N",
                                "name": "blue house",
                                "name_ascii": None,
                                "t50_fid": 2426272,
                            },
                            "-": {
                                "fid": 2,
                                "geom": "0101000000E702F16784226640ADE666D77CFE42C0",
                                "macronated": "N",
                                "name": None,
                                "name_ascii": None,
                                "t50_fid": 2426272,
                            },
                        },
                        {
                            "+": {
                                "colour": "red",
                                "fid": 9999,
                                "geom": "010100000000000000000000000000000000000000",
                                "macronated": "0",
                                "name": "Te Motu-a-kore",
                                "name_ascii": "Te Motu-a-kore",
                                "t50_fid": 9999999,
                            }
                        },
                    ],
                }
            }
        }
    elif output_format == "geojson":
        assert r.exit_code == 0, r
        assert (
            "Warning: meta changes aren't included in GeoJSON output: schema.json"
            in r.stderr
        )
        assert json.loads(r.stdout) == {
            "features": [
                {
                    "geometry": {
                        "coordinates": [177.0959629713586, -38.00433803621768],
                        "type": "Point",
                    },
                    "id": "U-::1",
                    "properties": {
                        "fid": 1,
                        "macronated": "N",
                        "name": None,
                        "name_ascii": None,
                        "t50_fid": 2426271,
                    },
                    "type": "Feature",
                },
                {
                    "geometry": {
                        "coordinates": [177.0959629713586, -38.00433803621768],
                        "type": "Point",
                    },
                    "id": "U+::1",
                    "properties": {
                        "colour": None,
                        "fid": 1,
                        "macronated": "N",
                        "name": "test",
                        "name_ascii": None,
                        "t50_fid": 2426271,
                    },
                    "type": "Feature",
                },
                {
                    "geometry": {
                        "coordinates": [177.0786628443959, -37.9881848576018],
                        "type": "Point",
                    },
                    "id": "U-::2",
                    "properties": {
                        "fid": 2,
                        "macronated": "N",
                        "name": None,
                        "name_ascii": None,
                        "t50_fid": 2426272,
                    },
                    "type": "Feature",
                },
                {
                    "geometry": {
                        "coordinates": [177.0786628443959, -37.9881848576018],
                        "type": "Point",
                    },
                    "id": "U+::2",
                    "properties": {
                        "colour": "blue",
                        "fid": 2,
                        "macronated": "N",
                        "name": "blue house",
                        "name_ascii": None,
                        "t50_fid": 2426272,
                    },
                    "type": "Feature",
                },
                {
                    "geometry": {"coordinates": [0.0, 0.0], "type": "Point"},
                    "id": "I::9999",
                    "properties": {
                        "colour": "red",
                        "fid": 9999,
                        "macronated": "0",
                        "name": "Te Motu-a-kore",
                        "name_ascii": "Te Motu-a-kore",
                        "t50_fid": 9999999,
                    },
                    "type": "Feature",
                },
            ],
            "type": "FeatureCollection",
        }


def check_polygons_diff_output(r, output_format):
    if output_format == "text":
        assert r.exit_code == 0, r
        assert r.stdout.splitlines() == [
            "--- nz_waca_adjustments:meta:schema.json",
            "+++ nz_waca_adjustments:meta:schema.json",
            "  [",
            "    {",
            '      "id": "79d3c4ca-3abd-0a30-2045-45169357113c",',
            '      "name": "id",',
            '      "dataType": "integer",',
            '      "primaryKeyIndex": 0,',
            '      "size": 64',
            "    },",
            "    {",
            '      "id": "c1d4dea1-c0ad-0255-7857-b5695e3ba2e9",',
            '      "name": "geom",',
            '      "dataType": "geometry",',
            '      "geometryType": "MULTIPOLYGON",',
            '      "geometryCRS": "EPSG:4167"',
            "    },",
            "    {",
            '      "id": "d3d4b64b-d48e-4069-4bb5-dfa943d91e6b",',
            '      "name": "date_adjusted",',
            '      "dataType": "timestamp"',
            "    },",
            "    {",
            '      "id": "dff34196-229d-f0b5-7fd4-b14ecf835b2c",',
            '-     "name": "survey_reference",',
            '+     "name": "surv_ref",',
            '      "dataType": "text",',
            '      "length": 50,',
            "    },",
            "    {",
            '      "id": "13dc4918-974e-978f-05ce-3b4321077c50",',
            '      "name": "adjusted_nodes",',
            '      "dataType": "integer",',
            '      "size": 32',
            "    },",
            "+   {",
            '+     "id": "50851f24-296b-89a9-56b1-b474bf9474fb",',
            '+     "name": "colour",',
            '+     "dataType": "text",',
            '+     "length": 32',
            "+   },",
            "  ]",
            "--- nz_waca_adjustments:feature:1443053",
            "+++ nz_waca_adjustments:feature:1443053",
            "-                         survey_reference = ␀",
            "+                                 surv_ref = test",
            "+                                   colour = ␀",
            "--- nz_waca_adjustments:feature:1452332",
            "+++ nz_waca_adjustments:feature:1452332",
            "-                         survey_reference = ␀",
            "+                                 surv_ref = ␀",
            "+                                   colour = yellow",
            "+++ nz_waca_adjustments:feature:9999999",
            "+                                     geom = POLYGON(...)",
            "+                            date_adjusted = 2019-07-05T13:04:00+01:00",
            "+                                 surv_ref = Null Island™ 🗺",
            "+                           adjusted_nodes = 123",
            "+                                   colour = ␀",
        ]
    elif output_format == "json":
        assert r.exit_code == 0, r
        assert json.loads(r.stdout) == {
            "sno.diff/v1+hexwkb": {
                "nz_waca_adjustments": {
                    "meta": {
                        "schema.json": {
                            "-": [
                                {
                                    "dataType": "integer",
                                    "id": "79d3c4ca-3abd-0a30-2045-45169357113c",
                                    "name": "id",
                                    "primaryKeyIndex": 0,
                                    "size": 64,
                                },
                                {
                                    "dataType": "geometry",
                                    "geometryCRS": "EPSG:4167",
                                    "geometryType": "MULTIPOLYGON",
                                    "id": "c1d4dea1-c0ad-0255-7857-b5695e3ba2e9",
                                    "name": "geom",
                                },
                                {
                                    "dataType": "timestamp",
                                    "id": "d3d4b64b-d48e-4069-4bb5-dfa943d91e6b",
                                    "name": "date_adjusted",
                                },
                                {
                                    "dataType": "text",
                                    "id": "dff34196-229d-f0b5-7fd4-b14ecf835b2c",
                                    "length": 50,
                                    "name": "survey_reference",
                                },
                                {
                                    "dataType": "integer",
                                    "id": "13dc4918-974e-978f-05ce-3b4321077c50",
                                    "name": "adjusted_nodes",
                                    "size": 32,
                                },
                            ],
                            "+": [
                                {
                                    "dataType": "integer",
                                    "id": "79d3c4ca-3abd-0a30-2045-45169357113c",
                                    "name": "id",
                                    "primaryKeyIndex": 0,
                                    "size": 64,
                                },
                                {
                                    "dataType": "geometry",
                                    "geometryCRS": "EPSG:4167",
                                    "geometryType": "MULTIPOLYGON",
                                    "id": "c1d4dea1-c0ad-0255-7857-b5695e3ba2e9",
                                    "name": "geom",
                                },
                                {
                                    "dataType": "timestamp",
                                    "id": "d3d4b64b-d48e-4069-4bb5-dfa943d91e6b",
                                    "name": "date_adjusted",
                                },
                                {
                                    "dataType": "text",
                                    "id": "dff34196-229d-f0b5-7fd4-b14ecf835b2c",
                                    "length": 50,
                                    "name": "surv_ref",
                                },
                                {
                                    "dataType": "integer",
                                    "id": "13dc4918-974e-978f-05ce-3b4321077c50",
                                    "name": "adjusted_nodes",
                                    "size": 32,
                                },
                                {
                                    "dataType": "text",
                                    "id": "50851f24-296b-89a9-56b1-b474bf9474fb",
                                    "length": 32,
                                    "name": "colour",
                                },
                            ],
                        }
                    },
                    "feature": [
                        {
                            "+": {
                                "adjusted_nodes": 1238,
                                "colour": None,
                                "date_adjusted": "2011-05-10T12:09:10Z",
                                "geom": "0106000000010000000103000000010000000B000000DDEF0B89EEC665400CAB8C50D98E43C0AA7883AEBCC66540F6237BC40C8843C0D25EEE2300C7654002A1BF90B18543C0218DAFE279C76540391485E7938543C09EE81AACF7C76540E85798D99E8843C02E055F7296C765405BFD22B2598D43C0EA119EE595C765406BD26D895C8D43C087CDFB1423C76540723E2B1FB88E43C08DFCB0941BC7654054B82FB1C38E43C0A00948100AC76540FB04E1A5D38E43C0DDEF0B89EEC665400CAB8C50D98E43C0",
                                "id": 1443053,
                                "surv_ref": "test",
                            },
                            "-": {
                                "adjusted_nodes": 1238,
                                "date_adjusted": "2011-05-10T12:09:10Z",
                                "geom": "0106000000010000000103000000010000000B000000DDEF0B89EEC665400CAB8C50D98E43C0AA7883AEBCC66540F6237BC40C8843C0D25EEE2300C7654002A1BF90B18543C0218DAFE279C76540391485E7938543C09EE81AACF7C76540E85798D99E8843C02E055F7296C765405BFD22B2598D43C0EA119EE595C765406BD26D895C8D43C087CDFB1423C76540723E2B1FB88E43C08DFCB0941BC7654054B82FB1C38E43C0A00948100AC76540FB04E1A5D38E43C0DDEF0B89EEC665400CAB8C50D98E43C0",
                                "id": 1443053,
                                "survey_reference": None,
                            },
                        },
                        {
                            "+": {
                                "adjusted_nodes": 558,
                                "colour": "yellow",
                                "date_adjusted": "2011-06-07T15:22:58Z",
                                "geom": "01060000000100000001030000000100000018000000C43FCCA465D7654049FCE5EE4E6642C031DD1F0460D765406D606177F06542C064343C0760D765408E68DDEBED6542C0774AC25F66D7654003E4041CD46542C00442E6DF6AD765405B0AD914C76542C00F9E1F7B6BD76540B7354771C56542C099152AB96BD76540ED1D93E0C46542C03E5700F86CD76540F85610F9C16542C01E90DF366ED76540FDC68D11BF6542C056546E3273D765402D735F73B36542C056C5C5E175D76540EFB2BA30AD6542C06AC54D4277D76540182AC9FAA96542C09C400C8977D7654048F61C62A96542C03590D37C7AD76540168A743FA76542C0F38A07DA7CD7654069796568AA6542C0FF12A7497FD76540FD8AFFFBAF6542C0D5F5B5BE91D765406A7190D0F26542C049E06AF891D76540BCC23B6FF56542C08B3858D991D76540B6662B2FF96542C07E0C0C0F90D76540E2CF4B20006642C03FF664C98ED7654020CAD027046642C020E67C7C74D765406A7528F9476642C052A1D0E771D76540D9BFA1A64C6642C0C43FCCA465D7654049FCE5EE4E6642C0",
                                "id": 1452332,
                                "surv_ref": None,
                            },
                            "-": {
                                "adjusted_nodes": 558,
                                "date_adjusted": "2011-06-07T15:22:58Z",
                                "geom": "01060000000100000001030000000100000018000000C43FCCA465D7654049FCE5EE4E6642C031DD1F0460D765406D606177F06542C064343C0760D765408E68DDEBED6542C0774AC25F66D7654003E4041CD46542C00442E6DF6AD765405B0AD914C76542C00F9E1F7B6BD76540B7354771C56542C099152AB96BD76540ED1D93E0C46542C03E5700F86CD76540F85610F9C16542C01E90DF366ED76540FDC68D11BF6542C056546E3273D765402D735F73B36542C056C5C5E175D76540EFB2BA30AD6542C06AC54D4277D76540182AC9FAA96542C09C400C8977D7654048F61C62A96542C03590D37C7AD76540168A743FA76542C0F38A07DA7CD7654069796568AA6542C0FF12A7497FD76540FD8AFFFBAF6542C0D5F5B5BE91D765406A7190D0F26542C049E06AF891D76540BCC23B6FF56542C08B3858D991D76540B6662B2FF96542C07E0C0C0F90D76540E2CF4B20006642C03FF664C98ED7654020CAD027046642C020E67C7C74D765406A7528F9476642C052A1D0E771D76540D9BFA1A64C6642C0C43FCCA465D7654049FCE5EE4E6642C0",
                                "id": 1452332,
                                "survey_reference": None,
                            },
                        },
                        {
                            "+": {
                                "adjusted_nodes": 123,
                                "colour": None,
                                "date_adjusted": "2019-07-05T13:04:00+01:00",
                                "geom": "01030000000100000005000000000000000000000000000000000000000000000000000000FCA9F1D24D62503FFCA9F1D24D62503FFCA9F1D24D62503FFCA9F1D24D62503F000000000000000000000000000000000000000000000000",
                                "id": 9999999,
                                "surv_ref": "Null Island™ 🗺",
                            }
                        },
                    ],
                }
            }
        }
    elif output_format == "geojson":
        assert r.exit_code == 0, r
        assert (
            "Warning: meta changes aren't included in GeoJSON output: schema.json"
            in r.stderr
        )
        assert json.loads(r.stdout) == {
            "features": [
                {
                    "geometry": {
                        "coordinates": [
                            [
                                [
                                    [174.2166180833, -39.1160069167],
                                    [174.2105324333, -39.0628896333],
                                    [174.2187671333, -39.0444813667],
                                    [174.2336286, -39.0435761833],
                                    [174.2489834333, -39.0673477167],
                                    [174.2371150833, -39.1042998],
                                    [174.2370479667, -39.1043865],
                                    [174.2230324667, -39.11499395],
                                    [174.2221168, -39.11534705],
                                    [174.2199784667, -39.1158339833],
                                    [174.2166180833, -39.1160069167],
                                ]
                            ]
                        ],
                        "type": "MultiPolygon",
                    },
                    "id": "U-::1443053",
                    "properties": {
                        "adjusted_nodes": 1238,
                        "date_adjusted": "2011-05-10T12:09:10Z",
                        "id": 1443053,
                        "survey_reference": None,
                    },
                    "type": "Feature",
                },
                {
                    "geometry": {
                        "coordinates": [
                            [
                                [
                                    [174.2166180833, -39.1160069167],
                                    [174.2105324333, -39.0628896333],
                                    [174.2187671333, -39.0444813667],
                                    [174.2336286, -39.0435761833],
                                    [174.2489834333, -39.0673477167],
                                    [174.2371150833, -39.1042998],
                                    [174.2370479667, -39.1043865],
                                    [174.2230324667, -39.11499395],
                                    [174.2221168, -39.11534705],
                                    [174.2199784667, -39.1158339833],
                                    [174.2166180833, -39.1160069167],
                                ]
                            ]
                        ],
                        "type": "MultiPolygon",
                    },
                    "id": "U+::1443053",
                    "properties": {
                        "adjusted_nodes": 1238,
                        "colour": None,
                        "date_adjusted": "2011-05-10T12:09:10Z",
                        "id": 1443053,
                        "surv_ref": "test",
                    },
                    "type": "Feature",
                },
                {
                    "geometry": {
                        "coordinates": [
                            [
                                [
                                    [174.7311576833, -36.79928385],
                                    [174.7304707167, -36.79640095],
                                    [174.7304722, -36.7963232833],
                                    [174.7312468333, -36.7955355667],
                                    [174.7317962167, -36.7951379833],
                                    [174.7318702333, -36.7950879667],
                                    [174.7318998167, -36.7950707167],
                                    [174.73205185, -36.7949820833],
                                    [174.7322039, -36.79489345],
                                    [174.7328121333, -36.7945389],
                                    [174.7331398833, -36.79434785],
                                    [174.7333079833, -36.7942498667],
                                    [174.7333417167, -36.7942316667],
                                    [174.7337021, -36.7941665],
                                    [174.7339906833, -36.7942629333],
                                    [174.73428805, -36.7944331167],
                                    [174.7365411333, -36.7964726167],
                                    [174.73656865, -36.7965525667],
                                    [174.7365538333, -36.796667],
                                    [174.7363353, -36.79687885],
                                    [174.7361800167, -36.7970018167],
                                    [174.7329695167, -36.79907145],
                                    [174.7326544833, -36.7992142],
                                    [174.7311576833, -36.79928385],
                                ]
                            ]
                        ],
                        "type": "MultiPolygon",
                    },
                    "id": "U-::1452332",
                    "properties": {
                        "adjusted_nodes": 558,
                        "date_adjusted": "2011-06-07T15:22:58Z",
                        "id": 1452332,
                        "survey_reference": None,
                    },
                    "type": "Feature",
                },
                {
                    "geometry": {
                        "coordinates": [
                            [
                                [
                                    [174.7311576833, -36.79928385],
                                    [174.7304707167, -36.79640095],
                                    [174.7304722, -36.7963232833],
                                    [174.7312468333, -36.7955355667],
                                    [174.7317962167, -36.7951379833],
                                    [174.7318702333, -36.7950879667],
                                    [174.7318998167, -36.7950707167],
                                    [174.73205185, -36.7949820833],
                                    [174.7322039, -36.79489345],
                                    [174.7328121333, -36.7945389],
                                    [174.7331398833, -36.79434785],
                                    [174.7333079833, -36.7942498667],
                                    [174.7333417167, -36.7942316667],
                                    [174.7337021, -36.7941665],
                                    [174.7339906833, -36.7942629333],
                                    [174.73428805, -36.7944331167],
                                    [174.7365411333, -36.7964726167],
                                    [174.73656865, -36.7965525667],
                                    [174.7365538333, -36.796667],
                                    [174.7363353, -36.79687885],
                                    [174.7361800167, -36.7970018167],
                                    [174.7329695167, -36.79907145],
                                    [174.7326544833, -36.7992142],
                                    [174.7311576833, -36.79928385],
                                ]
                            ]
                        ],
                        "type": "MultiPolygon",
                    },
                    "id": "U+::1452332",
                    "properties": {
                        "adjusted_nodes": 558,
                        "colour": "yellow",
                        "date_adjusted": "2011-06-07T15:22:58Z",
                        "id": 1452332,
                        "surv_ref": None,
                    },
                    "type": "Feature",
                },
                {
                    "geometry": {
                        "coordinates": [
                            [
                                [0.0, 0.0],
                                [0.0, 0.001],
                                [0.001, 0.001],
                                [0.001, 0.0],
                                [0.0, 0.0],
                            ]
                        ],
                        "type": "Polygon",
                    },
                    "id": "I::9999999",
                    "properties": {
                        "adjusted_nodes": 123,
                        "colour": None,
                        "date_adjusted": "2019-07-05T13:04:00+01:00",
                        "id": 9999999,
                        "surv_ref": "Null Island™ 🗺",
                    },
                    "type": "Feature",
                },
            ],
            "type": "FeatureCollection",
        }


def test_schema_diff_as_text(gen_uuid):
    old_schema = Schema(
        [
            ColumnSchema(gen_uuid(), "fid", "integer", 0, size=64),
            ColumnSchema(
                gen_uuid(),
                "geom",
                "geometry",
                None,
                geometryType="MULTIPOLYGON",
                geometryCRS="EPSG:2193",
            ),
            ColumnSchema(gen_uuid(), "building_id", "integer", None, size=32),
            ColumnSchema(gen_uuid(), "name", "text", None),
            ColumnSchema(gen_uuid(), "use", "text", None),
            ColumnSchema(gen_uuid(), "suburb_locality", "text", None),
            ColumnSchema(gen_uuid(), "town_city", "text", None),
            ColumnSchema(gen_uuid(), "territorial_authority", "text", None),
            ColumnSchema(gen_uuid(), "last_modified", "date", None),
        ]
    )
    new_schema = Schema(
        [
            ColumnSchema(gen_uuid(), "fid", "integer", 0, size=64),
            ColumnSchema(gen_uuid(), "building_id", "integer", None, size=64),
            ColumnSchema(gen_uuid(), "name", "text", None, size=40),
            ColumnSchema(gen_uuid(), "territorial_authority", "text", None),
            ColumnSchema(gen_uuid(), "use", "text", None),
            ColumnSchema(gen_uuid(), "colour", "integer", None, size=32),
            ColumnSchema(gen_uuid(), "town_city", "text", None),
            ColumnSchema(
                gen_uuid(),
                "geom",
                "geometry",
                None,
                geometryType="MULTIPOLYGON",
                geometryCRS="EPSG:2193",
            ),
            ColumnSchema(gen_uuid(), "last_modified", "date", None),
        ]
    )
    aligned_schema = old_schema.align_to_self(new_schema)

    output = schema_diff_as_text(old_schema, aligned_schema)

    assert click.unstyle(output).splitlines() == [
        "  [",
        "    {",
        '      "id": "b11ea716-6b85-f672-741f-8281aaa04bef",',
        '      "name": "fid",',
        '      "dataType": "integer",',
        '      "primaryKeyIndex": 0,',
        '      "size": 64',
        "    },",
        "-   {",
        '-     "id": "0d167b8b-294f-c2be-4747-bc947672d3a0",',
        '-     "name": "geom",',
        '-     "dataType": "geometry",',
        '-     "geometryType": "MULTIPOLYGON",',
        '-     "geometryCRS": "EPSG:2193"',
        "-   },",
        "    {",
        '      "id": "0f28f35f-89d8-2b93-40d7-30abe42c69ea",',
        '      "name": "building_id",',
        '      "dataType": "integer",',
        '-     "size": 32,',
        '+     "size": 64,',
        "    },",
        "    {",
        '      "id": "b5c69fa8-f48f-59bb-7aab-95225daf4774",',
        '      "name": "name",',
        '      "dataType": "text",',
        '+     "size": 40,',
        "    },",
        "+   {",
        '+     "id": "d087bf39-1c76-fdd9-1315-0e81c6bd360f",',
        '+     "name": "territorial_authority",',
        '+     "dataType": "text"',
        "+   },",
        "    {",
        '      "id": "9f1924ac-097a-fc0a-b168-a06e8db32af7",',
        '      "name": "use",',
        '      "dataType": "text"',
        "    },",
        "-   {",
        '-     "id": "1bcf7a4a-19e9-9752-6264-0fd1d387633b",',
        '-     "name": "suburb_locality",',
        '-     "dataType": "text"',
        "-   },",
        "+   {",
        '+     "id": "0f4e1e5b-9adb-edbe-6cbd-0ee0140448e6",',
        '+     "name": "colour",',
        '+     "dataType": "integer",',
        '+     "size": 32',
        "+   },",
        "    {",
        '      "id": "1777c850-baa2-6d52-dfcd-309f1741ff51",',
        '      "name": "town_city",',
        '      "dataType": "text"',
        "    },",
        "-   {",
        '-     "id": "d087bf39-1c76-fdd9-1315-0e81c6bd360f",',
        '-     "name": "territorial_authority",',
        '-     "dataType": "text"',
        "-   },",
        "+   {",
        '+     "id": "0d167b8b-294f-c2be-4747-bc947672d3a0",',
        '+     "name": "geom",',
        '+     "dataType": "geometry",',
        '+     "geometryType": "MULTIPOLYGON",',
        '+     "geometryCRS": "EPSG:2193"',
        "+   },",
        "    {",
        '      "id": "db82ba8c-c997-4bf1-87ef-b5108bdccde7",',
        '      "name": "last_modified",',
        '      "dataType": "date"',
        "    },",
        "  ]",
    ]
