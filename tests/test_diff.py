import functools
import json
import re

import html5lib
import pytest

import sno
from sno.diff_structs import Delta, DeltaDiff
from sno.sqlalchemy import gpkg_engine
from sno.geometry import hex_wkb_to_ogr
from sno.repo import SnoRepo


H = pytest.helpers.helpers()

DIFF_OUTPUT_FORMATS = ["text", "geojson", "json", "quiet", "html"]
SHOW_OUTPUT_FORMATS = ["text", "json"]


def _check_html_output(s):
    parser = html5lib.HTMLParser(strict=True, namespaceHTMLElements=False)
    # throw errors on invalid HTML
    document = parser.parse(s)
    # find the <script> element containing data
    el = document.find("./head/script[@id='sno-data']")
    # find the JSON
    m = re.match(r"\s*const DATA=(.*);\s*$", el.text, flags=re.DOTALL)
    # validate it
    return json.loads(m.group(1))


@pytest.mark.parametrize("output_format", DIFF_OUTPUT_FORMATS)
def test_diff_points(output_format, data_working_copy, cli_runner):
    """ diff the working copy against HEAD """
    with data_working_copy("points") as (repo, wc):
        # empty
        r = cli_runner.invoke(
            ["diff", f"--output-format={output_format}", "--output=-", "--exit-code"]
        )
        assert r.exit_code == 0, r.stderr

        # make some changes
        with gpkg_engine(wc).connect() as db:

            r = db.execute(H.POINTS.INSERT, H.POINTS.RECORD)
            assert r.rowcount == 1
            r = db.execute(f"UPDATE {H.POINTS.LAYER} SET fid=9998 WHERE fid=1;")
            assert r.rowcount == 1
            r = db.execute(
                f"UPDATE {H.POINTS.LAYER} SET name='test', t50_fid=NULL WHERE fid=2;"
            )
            assert r.rowcount == 1
            r = db.execute(f"DELETE FROM {H.POINTS.LAYER} WHERE fid=3;")
            assert r.rowcount == 1

        r = cli_runner.invoke(
            ["diff", f"--output-format={output_format}", "--output=-"]
        )
        if output_format == "quiet":
            assert r.exit_code == 1, r
            assert r.stdout == ""
        elif output_format == "text":
            assert r.exit_code == 0, r
            assert r.stdout.splitlines() == [
                "--- nz_pa_points_topo_150k:feature:1",
                "+++ nz_pa_points_topo_150k:feature:9998",
                "--- nz_pa_points_topo_150k:feature:2",
                "+++ nz_pa_points_topo_150k:feature:2",
                "-                                  t50_fid = 2426272",
                "+                                  t50_fid = ␀",
                "-                                     name = ␀",
                "+                                     name = test",
                "--- nz_pa_points_topo_150k:feature:3",
                "-                                     geom = POINT(...)",
                "-                                  t50_fid = 2426273",
                "-                               name_ascii = Tauwhare Pa",
                "-                               macronated = N",
                "-                                     name = Tauwhare Pa",
                "+++ nz_pa_points_topo_150k:feature:9999",
                "+                                     geom = POINT(...)",
                "+                                  t50_fid = 9999999",
                "+                               name_ascii = Te Motu-a-kore",
                "+                               macronated = N",
                "+                                     name = Te Motu-a-kore",
            ]
        elif output_format == "geojson":
            assert r.exit_code == 0, r
            odata = json.loads(r.stdout)
            assert len(odata["features"]) == 6
            assert odata == {
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
                        "id": "U+::9998",
                        "properties": {
                            "fid": 9998,
                            "macronated": "N",
                            "name": None,
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
                            "fid": 2,
                            "macronated": "N",
                            "name": "test",
                            "name_ascii": None,
                            "t50_fid": None,
                        },
                        "type": "Feature",
                    },
                    {
                        "geometry": {
                            "coordinates": [177.07125219628702, -37.97947548462757],
                            "type": "Point",
                        },
                        "id": "D::3",
                        "properties": {
                            "fid": 3,
                            "macronated": "N",
                            "name": "Tauwhare Pa",
                            "name_ascii": "Tauwhare Pa",
                            "t50_fid": 2426273,
                        },
                        "type": "Feature",
                    },
                    {
                        "geometry": {"coordinates": [0.0, 0.0], "type": "Point"},
                        "id": "I::9999",
                        "properties": {
                            "fid": 9999,
                            "macronated": "N",
                            "name": "Te Motu-a-kore",
                            "name_ascii": "Te Motu-a-kore",
                            "t50_fid": 9999999,
                        },
                        "type": "Feature",
                    },
                ],
                "type": "FeatureCollection",
            }

        elif output_format == "json":
            assert r.exit_code == 0, r
            odata = json.loads(r.stdout)
            assert (
                len(odata["sno.diff/v1+hexwkb"]["nz_pa_points_topo_150k"]["feature"])
                == 4
            )
            assert odata == {
                "sno.diff/v1+hexwkb": {
                    "nz_pa_points_topo_150k": {
                        "feature": [
                            {
                                "+": {
                                    "fid": 9998,
                                    "geom": "010100000097F3EF201223664087D715268E0043C0",
                                    "macronated": "N",
                                    "name": None,
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
                                    "fid": 2,
                                    "geom": "0101000000E702F16784226640ADE666D77CFE42C0",
                                    "macronated": "N",
                                    "name": "test",
                                    "name_ascii": None,
                                    "t50_fid": None,
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
                                "-": {
                                    "fid": 3,
                                    "geom": "0101000000459AAFB247226640C6DAE2735FFD42C0",
                                    "macronated": "N",
                                    "name": "Tauwhare Pa",
                                    "name_ascii": "Tauwhare Pa",
                                    "t50_fid": 2426273,
                                }
                            },
                            {
                                "+": {
                                    "fid": 9999,
                                    "geom": "010100000000000000000000000000000000000000",
                                    "macronated": "N",
                                    "name": "Te Motu-a-kore",
                                    "name_ascii": "Te Motu-a-kore",
                                    "t50_fid": 9999999,
                                }
                            },
                        ],
                    }
                }
            }

        elif output_format == "html":
            _check_html_output(r.stdout)


@pytest.mark.parametrize("output_format", DIFF_OUTPUT_FORMATS)
def test_diff_reprojection(output_format, data_working_copy, cli_runner):
    """ diff the working copy against HEAD """
    with data_working_copy("points") as (repo, wc):
        # make some changes
        with gpkg_engine(wc).connect() as db:
            r = db.execute(
                f"UPDATE {H.POINTS.LAYER} SET name='test', t50_fid=NULL WHERE fid=2;"
            )
            assert r.rowcount == 1

        r = cli_runner.invoke(
            [
                "diff",
                f"--output-format={output_format}",
                "--output=-",
                f"--crs=epsg:2193",
            ]
        )

        def _check_geojson(featurecollection):
            features = featurecollection["features"]
            assert len(features) == 2
            assert features[0]["geometry"]["coordinates"] == [
                1958227.0621957763,
                5787640.540304652,
            ]
            assert features[1]["geometry"]["coordinates"] == [
                1958227.0621957763,
                5787640.540304652,
            ]

        if output_format == "quiet":
            assert r.exit_code == 1, r.stderr
            assert r.stdout == ""
        elif output_format == "text":
            assert r.exit_code == 0, r.stderr
        elif output_format == "geojson":
            assert r.exit_code == 0, r.stderr
            featurecollection = json.loads(r.stdout)
            _check_geojson(featurecollection)
        elif output_format == "json":
            assert r.exit_code == 0, r.stderr
            odata = json.loads(r.stdout)
            features = odata["sno.diff/v1+hexwkb"]["nz_pa_points_topo_150k"]["feature"]
            assert len(features) == 1
            expected_wkt = "POINT (1958227.06219578 5787640.54030465)"
            assert (
                hex_wkb_to_ogr(features[0]["+"]["geom"]).ExportToWkt() == expected_wkt
            )
            assert (
                hex_wkb_to_ogr(features[0]["-"]["geom"]).ExportToWkt() == expected_wkt
            )

        elif output_format == "html":
            odata = _check_html_output(r.stdout)
            _check_geojson(odata["nz_pa_points_topo_150k"])


def test_show_crs_with_aspatial_dataset(data_archive, cli_runner):
    """
    --crs should be ignored when used with aspatial data
    """
    with data_archive("table"):
        r = cli_runner.invoke(
            [
                "show",
                f"--output-format=json",
                f"--crs=epsg:2193",
            ]
        )
        assert r.exit_code == 0, r.stderr


@pytest.mark.parametrize("output_format", DIFF_OUTPUT_FORMATS)
def test_diff_polygons(output_format, data_working_copy, cli_runner):
    """ diff the working copy against HEAD """
    with data_working_copy("polygons") as (repo, wc):
        # empty
        r = cli_runner.invoke(
            ["diff", f"--output-format={output_format}", "--output=-", "--exit-code"]
        )
        assert r.exit_code == 0, r.stderr

        # make some changes
        with gpkg_engine(wc).connect() as db:

            r = db.execute(H.POLYGONS.INSERT, H.POLYGONS.RECORD)
            assert r.rowcount == 1
            r = db.execute(f"UPDATE {H.POLYGONS.LAYER} SET id=9998 WHERE id=1424927;")
            assert r.rowcount == 1
            r = db.execute(
                f"UPDATE {H.POLYGONS.LAYER} SET survey_reference='test', date_adjusted='2019-01-01T00:00:00Z' WHERE id=1443053;"
            )
            assert r.rowcount == 1
            r = db.execute(f"DELETE FROM {H.POLYGONS.LAYER} WHERE id=1452332;")
            assert r.rowcount == 1

        r = cli_runner.invoke(
            ["diff", f"--output-format={output_format}", "--output=-"]
        )
        if output_format == "quiet":
            assert r.exit_code == 1, r
            assert r.stdout == ""
        elif output_format == "text":
            assert r.exit_code == 0, r
            assert r.stdout.splitlines() == [
                "--- nz_waca_adjustments:feature:1424927",
                "+++ nz_waca_adjustments:feature:9998",
                "--- nz_waca_adjustments:feature:1443053",
                "+++ nz_waca_adjustments:feature:1443053",
                "-                            date_adjusted = 2011-05-10T12:09:10Z",
                "+                            date_adjusted = 2019-01-01T00:00:00Z",
                "-                         survey_reference = ␀",
                "+                         survey_reference = test",
                "--- nz_waca_adjustments:feature:1452332",
                "-                                     geom = MULTIPOLYGON(...)",
                "-                            date_adjusted = 2011-06-07T15:22:58Z",
                "-                         survey_reference = ␀",
                "-                           adjusted_nodes = 558",
                "+++ nz_waca_adjustments:feature:9999999",
                "+                                     geom = MULTIPOLYGON(...)",
                "+                            date_adjusted = 2019-07-05T13:04:00+01:00",
                "+                         survey_reference = Null Island™ 🗺",
                "+                           adjusted_nodes = 123",
            ]

        elif output_format == "geojson":
            assert r.exit_code == 0, r
            odata = json.loads(r.stdout)
            assert len(odata["features"]) == 6
            assert odata == {
                "features": [
                    {
                        "geometry": {
                            "coordinates": [
                                [
                                    [
                                        [175.36501955, -37.8677371333],
                                        [175.3594248167, -37.8596774667],
                                        [175.3587766, -37.8587394],
                                        [175.3575281667, -37.8568299667],
                                        [175.3573468, -37.8564048833],
                                        [175.3503192167, -37.8384090167],
                                        [175.3516358, -37.8348565833],
                                        [175.3577393167, -37.8277652167],
                                        [175.3581963667, -37.8273121833],
                                        [175.3613082667, -37.8270649667],
                                        [175.3843470333, -37.84913405],
                                        [175.38430045, -37.8493045833],
                                        [175.3774678333, -37.8602782667],
                                        [175.3750135667, -37.8641522],
                                        [175.3739396667, -37.8658466833],
                                        [175.3726953667, -37.8674995333],
                                        [175.3725163333, -37.86759125],
                                        [175.36501955, -37.8677371333],
                                    ]
                                ]
                            ],
                            "type": "MultiPolygon",
                        },
                        "id": "U-::1424927",
                        "properties": {
                            "adjusted_nodes": 1122,
                            "date_adjusted": "2011-03-25T07:30:45Z",
                            "id": 1424927,
                            "survey_reference": None,
                        },
                        "type": "Feature",
                    },
                    {
                        "geometry": {
                            "coordinates": [
                                [
                                    [
                                        [175.36501955, -37.8677371333],
                                        [175.3594248167, -37.8596774667],
                                        [175.3587766, -37.8587394],
                                        [175.3575281667, -37.8568299667],
                                        [175.3573468, -37.8564048833],
                                        [175.3503192167, -37.8384090167],
                                        [175.3516358, -37.8348565833],
                                        [175.3577393167, -37.8277652167],
                                        [175.3581963667, -37.8273121833],
                                        [175.3613082667, -37.8270649667],
                                        [175.3843470333, -37.84913405],
                                        [175.38430045, -37.8493045833],
                                        [175.3774678333, -37.8602782667],
                                        [175.3750135667, -37.8641522],
                                        [175.3739396667, -37.8658466833],
                                        [175.3726953667, -37.8674995333],
                                        [175.3725163333, -37.86759125],
                                        [175.36501955, -37.8677371333],
                                    ]
                                ]
                            ],
                            "type": "MultiPolygon",
                        },
                        "id": "U+::9998",
                        "properties": {
                            "adjusted_nodes": 1122,
                            "date_adjusted": "2011-03-25T07:30:45Z",
                            "id": 9998,
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
                            "date_adjusted": "2019-01-01T00:00:00Z",
                            "id": 1443053,
                            "survey_reference": "test",
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
                        "id": "D::1452332",
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
                                        [0.0, 0.0],
                                        [0.0, 0.001],
                                        [0.001, 0.001],
                                        [0.001, 0.0],
                                        [0.0, 0.0],
                                    ]
                                ]
                            ],
                            "type": "MultiPolygon",
                        },
                        "id": "I::9999999",
                        "properties": {
                            "adjusted_nodes": 123,
                            "date_adjusted": "2019-07-05T13:04:00+01:00",
                            "id": 9999999,
                            "survey_reference": "Null Island™ 🗺",
                        },
                        "type": "Feature",
                    },
                ],
                "type": "FeatureCollection",
            }

        elif output_format == "json":
            assert r.exit_code == 0, r
            odata = json.loads(r.stdout)
            assert (
                len(odata["sno.diff/v1+hexwkb"]["nz_waca_adjustments"]["feature"]) == 4
            )
            assert odata == {
                "sno.diff/v1+hexwkb": {
                    "nz_waca_adjustments": {
                        "feature": [
                            {
                                "+": {
                                    "adjusted_nodes": 1122,
                                    "date_adjusted": "2011-03-25T07:30:45Z",
                                    "geom": "01060000000100000001030000000100000012000000D2B47A3DAEEB65402E86A80212EF42C01D23796880EB6540D54A46E909EE42C03E7210197BEB6540B164332CEBED42C003ECE8DE70EB6540C99AB69AACED42C0916A8E626FEB654040F4DAAC9EED42C0615CA5D035EB6540F2B295FC50EB42C04AA3B89940EB6540D90F9D94DCEA42C00937B99972EB6540163FEB35F4E942C0B9103A5876EB65408D6D995DE5E942C008A85AD68FEB654069D2CB43DDE942C0D24A26924CEC6540C455AF6CB0EC42C0D21275304CEC6540E6CE3803B6EC42C018EA6B3714EC6540D17726991DEE42C00D91731C00EC65401BE20E8A9CEE42C0EBE45150F7EB6540D10F6A10D4EE42C01C6BD51EEDEB6540CD6886390AEF42C0FB975FA7EBEB6540DB85E63A0DEF42C0D2B47A3DAEEB65402E86A80212EF42C0",
                                    "id": 9998,
                                    "survey_reference": None,
                                },
                                "-": {
                                    "adjusted_nodes": 1122,
                                    "date_adjusted": "2011-03-25T07:30:45Z",
                                    "geom": "01060000000100000001030000000100000012000000D2B47A3DAEEB65402E86A80212EF42C01D23796880EB6540D54A46E909EE42C03E7210197BEB6540B164332CEBED42C003ECE8DE70EB6540C99AB69AACED42C0916A8E626FEB654040F4DAAC9EED42C0615CA5D035EB6540F2B295FC50EB42C04AA3B89940EB6540D90F9D94DCEA42C00937B99972EB6540163FEB35F4E942C0B9103A5876EB65408D6D995DE5E942C008A85AD68FEB654069D2CB43DDE942C0D24A26924CEC6540C455AF6CB0EC42C0D21275304CEC6540E6CE3803B6EC42C018EA6B3714EC6540D17726991DEE42C00D91731C00EC65401BE20E8A9CEE42C0EBE45150F7EB6540D10F6A10D4EE42C01C6BD51EEDEB6540CD6886390AEF42C0FB975FA7EBEB6540DB85E63A0DEF42C0D2B47A3DAEEB65402E86A80212EF42C0",
                                    "id": 1424927,
                                    "survey_reference": None,
                                },
                            },
                            {
                                "+": {
                                    "adjusted_nodes": 1238,
                                    "date_adjusted": "2019-01-01T00:00:00Z",
                                    "geom": "0106000000010000000103000000010000000B000000DDEF0B89EEC665400CAB8C50D98E43C0AA7883AEBCC66540F6237BC40C8843C0D25EEE2300C7654002A1BF90B18543C0218DAFE279C76540391485E7938543C09EE81AACF7C76540E85798D99E8843C02E055F7296C765405BFD22B2598D43C0EA119EE595C765406BD26D895C8D43C087CDFB1423C76540723E2B1FB88E43C08DFCB0941BC7654054B82FB1C38E43C0A00948100AC76540FB04E1A5D38E43C0DDEF0B89EEC665400CAB8C50D98E43C0",
                                    "id": 1443053,
                                    "survey_reference": "test",
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
                                "-": {
                                    "adjusted_nodes": 558,
                                    "date_adjusted": "2011-06-07T15:22:58Z",
                                    "geom": "01060000000100000001030000000100000018000000C43FCCA465D7654049FCE5EE4E6642C031DD1F0460D765406D606177F06542C064343C0760D765408E68DDEBED6542C0774AC25F66D7654003E4041CD46542C00442E6DF6AD765405B0AD914C76542C00F9E1F7B6BD76540B7354771C56542C099152AB96BD76540ED1D93E0C46542C03E5700F86CD76540F85610F9C16542C01E90DF366ED76540FDC68D11BF6542C056546E3273D765402D735F73B36542C056C5C5E175D76540EFB2BA30AD6542C06AC54D4277D76540182AC9FAA96542C09C400C8977D7654048F61C62A96542C03590D37C7AD76540168A743FA76542C0F38A07DA7CD7654069796568AA6542C0FF12A7497FD76540FD8AFFFBAF6542C0D5F5B5BE91D765406A7190D0F26542C049E06AF891D76540BCC23B6FF56542C08B3858D991D76540B6662B2FF96542C07E0C0C0F90D76540E2CF4B20006642C03FF664C98ED7654020CAD027046642C020E67C7C74D765406A7528F9476642C052A1D0E771D76540D9BFA1A64C6642C0C43FCCA465D7654049FCE5EE4E6642C0",
                                    "id": 1452332,
                                    "survey_reference": None,
                                }
                            },
                            {
                                "+": {
                                    "adjusted_nodes": 123,
                                    "date_adjusted": "2019-07-05T13:04:00+01:00",
                                    "geom": "01060000000100000001030000000100000005000000000000000000000000000000000000000000000000000000FCA9F1D24D62503FFCA9F1D24D62503FFCA9F1D24D62503FFCA9F1D24D62503F000000000000000000000000000000000000000000000000",
                                    "id": 9999999,
                                    "survey_reference": "Null Island™ 🗺",
                                }
                            },
                        ],
                    }
                }
            }

        elif output_format == "html":
            _check_html_output(r.stdout)


@pytest.mark.parametrize("output_format", DIFF_OUTPUT_FORMATS)
def test_diff_table(output_format, data_working_copy, cli_runner):
    """ diff the working copy against HEAD """
    with data_working_copy("table") as (repo, wc):
        # empty
        r = cli_runner.invoke(
            ["diff", f"--output-format={output_format}", "--output=-", "--exit-code"]
        )
        assert r.exit_code == 0, r.stderr

        # make some changes
        with gpkg_engine(wc).connect() as db:

            r = db.execute(H.TABLE.INSERT, H.TABLE.RECORD)
            assert r.rowcount == 1
            r = db.execute(
                f'UPDATE {H.TABLE.LAYER} SET "OBJECTID"=9998 WHERE OBJECTID=1;'
            )
            assert r.rowcount == 1
            r = db.execute(
                f"UPDATE {H.TABLE.LAYER} SET name='test', POP2000=9867 WHERE OBJECTID=2;"
            )
            assert r.rowcount == 1
            r = db.execute(f'DELETE FROM {H.TABLE.LAYER} WHERE "OBJECTID"=3;')
            assert r.rowcount == 1

        r = cli_runner.invoke(
            ["diff", f"--output-format={output_format}", "--output=-"]
        )
        if output_format == "quiet":
            assert r.exit_code == 1, r
            assert r.stdout == ""
        elif output_format == "text":
            assert r.exit_code == 0, r
            assert r.stdout.splitlines() == [
                "--- countiestbl:feature:1",
                "+++ countiestbl:feature:9998",
                "--- countiestbl:feature:2",
                "+++ countiestbl:feature:2",
                "-                                     NAME = Ferry",
                "+                                     NAME = test",
                "-                                  POP2000 = 7199.0",
                "+                                  POP2000 = 9867.0",
                "--- countiestbl:feature:3",
                "-                                     NAME = Stevens",
                "-                               STATE_NAME = Washington",
                "-                               STATE_FIPS = 53",
                "-                                CNTY_FIPS = 065",
                "-                                     FIPS = 53065",
                "-                                     AREA = 2529.9794",
                "-                                  POP1990 = 30948.0",
                "-                                  POP2000 = 40652.0",
                "-                               POP90_SQMI = 12",
                "-                               Shape_Leng = 4.876296245235406",
                "-                               Shape_Area = 0.7954858988987561",
                "+++ countiestbl:feature:9999",
                "+                                     NAME = Lake of the Gruffalo",
                "+                               STATE_NAME = Minnesota",
                "+                               STATE_FIPS = 27",
                "+                                CNTY_FIPS = 077",
                "+                                     FIPS = 27077",
                "+                                     AREA = 1784.0634",
                "+                                  POP1990 = 4076.0",
                "+                                  POP2000 = 4651.0",
                "+                               POP90_SQMI = 2",
                "+                               Shape_Leng = 4.05545998243992",
                "+                               Shape_Area = 0.565449933741451",
            ]

        elif output_format == "geojson":
            assert r.exit_code == 0, r
            odata = json.loads(r.stdout)
            assert len(odata["features"]) == 6
            assert odata == {
                "features": [
                    {
                        "geometry": None,
                        "id": "U-::1",
                        "properties": {
                            "AREA": 1784.0634,
                            "CNTY_FIPS": "077",
                            "FIPS": "27077",
                            "NAME": "Lake of the Woods",
                            "OBJECTID": 1,
                            "POP1990": 4076.0,
                            "POP2000": 4651.0,
                            "POP90_SQMI": 2,
                            "STATE_FIPS": "27",
                            "STATE_NAME": "Minnesota",
                            "Shape_Area": 0.5654499337414509,
                            "Shape_Leng": 4.055459982439919,
                        },
                        "type": "Feature",
                    },
                    {
                        "geometry": None,
                        "id": "U+::9998",
                        "properties": {
                            "AREA": 1784.0634,
                            "CNTY_FIPS": "077",
                            "FIPS": "27077",
                            "NAME": "Lake of the Woods",
                            "OBJECTID": 9998,
                            "POP1990": 4076.0,
                            "POP2000": 4651.0,
                            "POP90_SQMI": 2,
                            "STATE_FIPS": "27",
                            "STATE_NAME": "Minnesota",
                            "Shape_Area": 0.5654499337414509,
                            "Shape_Leng": 4.055459982439919,
                        },
                        "type": "Feature",
                    },
                    {
                        "geometry": None,
                        "id": "U-::2",
                        "properties": {
                            "AREA": 2280.2319,
                            "CNTY_FIPS": "019",
                            "FIPS": "53019",
                            "NAME": "Ferry",
                            "OBJECTID": 2,
                            "POP1990": 6295.0,
                            "POP2000": 7199.0,
                            "POP90_SQMI": 3,
                            "STATE_FIPS": "53",
                            "STATE_NAME": "Washington",
                            "Shape_Area": 0.7180593026451161,
                            "Shape_Leng": 3.786160993863997,
                        },
                        "type": "Feature",
                    },
                    {
                        "geometry": None,
                        "id": "U+::2",
                        "properties": {
                            "AREA": 2280.2319,
                            "CNTY_FIPS": "019",
                            "FIPS": "53019",
                            "NAME": "test",
                            "OBJECTID": 2,
                            "POP1990": 6295.0,
                            "POP2000": 9867.0,
                            "POP90_SQMI": 3,
                            "STATE_FIPS": "53",
                            "STATE_NAME": "Washington",
                            "Shape_Area": 0.7180593026451161,
                            "Shape_Leng": 3.786160993863997,
                        },
                        "type": "Feature",
                    },
                    {
                        "geometry": None,
                        "id": "D::3",
                        "properties": {
                            "AREA": 2529.9794,
                            "CNTY_FIPS": "065",
                            "FIPS": "53065",
                            "NAME": "Stevens",
                            "OBJECTID": 3,
                            "POP1990": 30948.0,
                            "POP2000": 40652.0,
                            "POP90_SQMI": 12,
                            "STATE_FIPS": "53",
                            "STATE_NAME": "Washington",
                            "Shape_Area": 0.7954858988987561,
                            "Shape_Leng": 4.876296245235406,
                        },
                        "type": "Feature",
                    },
                    {
                        "geometry": None,
                        "id": "I::9999",
                        "properties": {
                            "AREA": 1784.0634,
                            "CNTY_FIPS": "077",
                            "FIPS": "27077",
                            "NAME": "Lake of the Gruffalo",
                            "OBJECTID": 9999,
                            "POP1990": 4076.0,
                            "POP2000": 4651.0,
                            "POP90_SQMI": 2,
                            "STATE_FIPS": "27",
                            "STATE_NAME": "Minnesota",
                            "Shape_Area": 0.565449933741451,
                            "Shape_Leng": 4.05545998243992,
                        },
                        "type": "Feature",
                    },
                ],
                "type": "FeatureCollection",
            }

        elif output_format == "json":
            assert r.exit_code == 0, r
            odata = json.loads(r.stdout)
            assert len(odata["sno.diff/v1+hexwkb"]["countiestbl"]["feature"]) == 4
            assert odata == {
                "sno.diff/v1+hexwkb": {
                    "countiestbl": {
                        "feature": [
                            {
                                "+": {
                                    "AREA": 1784.0634,
                                    "CNTY_FIPS": "077",
                                    "FIPS": "27077",
                                    "NAME": "Lake of the Woods",
                                    "OBJECTID": 9998,
                                    "POP1990": 4076.0,
                                    "POP2000": 4651.0,
                                    "POP90_SQMI": 2,
                                    "STATE_FIPS": "27",
                                    "STATE_NAME": "Minnesota",
                                    "Shape_Area": 0.5654499337414509,
                                    "Shape_Leng": 4.055459982439919,
                                },
                                "-": {
                                    "AREA": 1784.0634,
                                    "CNTY_FIPS": "077",
                                    "FIPS": "27077",
                                    "NAME": "Lake of the Woods",
                                    "OBJECTID": 1,
                                    "POP1990": 4076.0,
                                    "POP2000": 4651.0,
                                    "POP90_SQMI": 2,
                                    "STATE_FIPS": "27",
                                    "STATE_NAME": "Minnesota",
                                    "Shape_Area": 0.5654499337414509,
                                    "Shape_Leng": 4.055459982439919,
                                },
                            },
                            {
                                "+": {
                                    "AREA": 2280.2319,
                                    "CNTY_FIPS": "019",
                                    "FIPS": "53019",
                                    "NAME": "test",
                                    "OBJECTID": 2,
                                    "POP1990": 6295.0,
                                    "POP2000": 9867.0,
                                    "POP90_SQMI": 3,
                                    "STATE_FIPS": "53",
                                    "STATE_NAME": "Washington",
                                    "Shape_Area": 0.7180593026451161,
                                    "Shape_Leng": 3.786160993863997,
                                },
                                "-": {
                                    "AREA": 2280.2319,
                                    "CNTY_FIPS": "019",
                                    "FIPS": "53019",
                                    "NAME": "Ferry",
                                    "OBJECTID": 2,
                                    "POP1990": 6295.0,
                                    "POP2000": 7199.0,
                                    "POP90_SQMI": 3,
                                    "STATE_FIPS": "53",
                                    "STATE_NAME": "Washington",
                                    "Shape_Area": 0.7180593026451161,
                                    "Shape_Leng": 3.786160993863997,
                                },
                            },
                            {
                                "-": {
                                    "AREA": 2529.9794,
                                    "CNTY_FIPS": "065",
                                    "FIPS": "53065",
                                    "NAME": "Stevens",
                                    "OBJECTID": 3,
                                    "POP1990": 30948.0,
                                    "POP2000": 40652.0,
                                    "POP90_SQMI": 12,
                                    "STATE_FIPS": "53",
                                    "STATE_NAME": "Washington",
                                    "Shape_Area": 0.7954858988987561,
                                    "Shape_Leng": 4.876296245235406,
                                }
                            },
                            {
                                "+": {
                                    "AREA": 1784.0634,
                                    "CNTY_FIPS": "077",
                                    "FIPS": "27077",
                                    "NAME": "Lake of the Gruffalo",
                                    "OBJECTID": 9999,
                                    "POP1990": 4076.0,
                                    "POP2000": 4651.0,
                                    "POP90_SQMI": 2,
                                    "STATE_FIPS": "27",
                                    "STATE_NAME": "Minnesota",
                                    "Shape_Area": 0.565449933741451,
                                    "Shape_Leng": 4.05545998243992,
                                }
                            },
                        ],
                    }
                }
            }

        elif output_format == "html":
            _check_html_output(r.stdout)


@pytest.mark.parametrize(
    "head_sha,head1_sha",
    [
        pytest.param(H.POINTS.HEAD_SHA, H.POINTS.HEAD1_SHA, id="commit_hash"),
        pytest.param(H.POINTS.HEAD_TREE_SHA, H.POINTS.HEAD1_TREE_SHA, id="tree_hash"),
    ],
)
def test_diff_rev_noop(head_sha, head1_sha, data_archive_readonly, cli_runner):
    """diff between trees / commits - no-op"""

    NOOP_SPECS = (
        f"{head_sha[:6]}...{head_sha[:6]}",
        f"{head_sha}...{head_sha}",
        f"{head1_sha}...{head1_sha}",
        "HEAD^1...HEAD^1",
        f"{head_sha}...",
        f"...{head_sha}",
    )

    with data_archive_readonly("points"):
        for spec in NOOP_SPECS:
            print(f"noop: {spec}")
            r = cli_runner.invoke(["diff", "--exit-code", spec])
            assert r.exit_code == 0, r


@pytest.mark.parametrize(
    "head_sha,head1_sha",
    [
        pytest.param(H.POINTS.HEAD_SHA, H.POINTS.HEAD1_SHA, id="commit_hash"),
        pytest.param(H.POINTS.HEAD_TREE_SHA, H.POINTS.HEAD1_TREE_SHA, id="tree_hash"),
    ],
)
def test_diff_rev_rev(head_sha, head1_sha, data_archive_readonly, cli_runner):
    """diff between trees / commits - no-op"""

    F_SPECS = (
        f"{head1_sha}...{head_sha}",
        f"{head1_sha}...",
        "HEAD^1...HEAD",
    )

    R_SPECS = (
        f"{head_sha}...{head1_sha}",
        f"...{head1_sha}",
        "HEAD...HEAD^1",
    )

    CHANGE_IDS = {
        (1182, 1182),
        (1181, 1181),
        (1168, 1168),
        (1166, 1166),
        (1095, 1095),
    }

    with data_archive_readonly("points"):
        for spec in F_SPECS:
            print(f"fwd: {spec}")
            r = cli_runner.invoke(["diff", "--exit-code", "-o", "json", spec])
            assert r.exit_code == 1, r
            odata = json.loads(r.stdout)["sno.diff/v1+hexwkb"]
            assert len(odata[H.POINTS.LAYER]["feature"]) == 5

            change_ids = {
                (
                    f.get("-", {}).get(H.POINTS.LAYER_PK),
                    f.get("+", {}).get(H.POINTS.LAYER_PK),
                )
                for f in odata[H.POINTS.LAYER]["feature"]
            }
            assert change_ids == CHANGE_IDS
            # this commit _adds_ names
            change_names = {
                (f["-"]["name"], f["+"]["name"])
                for f in odata[H.POINTS.LAYER]["feature"]
            }
            assert not any(n[0] for n in change_names)
            assert all(n[1] for n in change_names)

        for spec in R_SPECS:
            print(f"rev: {spec}")
            r = cli_runner.invoke(["diff", "--exit-code", "-o", "json", spec])
            assert r.exit_code == 1, r
            odata = json.loads(r.stdout)["sno.diff/v1+hexwkb"]
            assert len(odata[H.POINTS.LAYER]["feature"]) == 5
            change_ids = {
                (
                    f.get("-", {}).get(H.POINTS.LAYER_PK),
                    f.get("+", {}).get(H.POINTS.LAYER_PK),
                )
                for f in odata[H.POINTS.LAYER]["feature"]
            }
            assert change_ids == CHANGE_IDS
            # so names are _removed_
            change_names = {
                (f["-"]["name"], f["+"]["name"])
                for f in odata[H.POINTS.LAYER]["feature"]
            }
            assert all(n[0] for n in change_names)
            assert not any(n[1] for n in change_names)


def test_diff_rev_wc(data_working_copy, cli_runner):
    """ diff the working copy against commits """
    # ID  R0  ->  R1  ->  WC
    # 1   a       a1      a
    # 2   b       b1      b1
    # 3   c       c       c1
    # 4   d       d1      d2
    # 5   e       e1      e*
    # 6   f       f*      f+
    # 7   g       g*      -
    # 8   -       h+      h1
    # 9   -       i+      i*
    # 10  -       j+      j
    # 11  -       -       k+
    # 12  l       l*      l1+

    # Legend:
    #     x     existing
    #     xN    edit
    #     x*    delete
    #     x+    insert
    #     -     not there

    # Columns: id,value

    R0 = "075b0cf1414d71a6edbcdb4f05da93e1083ccdc2"
    R1 = "c9d8c52ec9e8b1260aec153958954c880573e24a"  # HEAD

    with data_working_copy("editing") as (repo, wc):
        # empty HEAD -> no working copy changes
        # r = cli_runner.invoke(["diff", "--exit-code", f"HEAD"])
        # assert r.exit_code == 0, r

        # make the R1 -> WC changes
        with gpkg_engine(wc).connect() as db:

            EDITS = ((1, "a"), (3, "c1"), (4, "d2"), (8, "h1"))
            for pk, val in EDITS:
                r = db.execute("UPDATE editing SET value = ? WHERE id = ?;", (val, pk))
                assert r.rowcount == 1

            r = db.execute("DELETE FROM editing WHERE id IN (5, 9);")
            assert r.rowcount == 2

            r = db.execute(
                "INSERT INTO editing (id, value) VALUES (6, 'f'), (11, 'k'), (12, 'l1');"
            )
            assert r.rowcount == 3

        def _extract(diff_json):
            ds = {}
            for f in odata["editing"]["feature"]:
                old = f.get("-")
                new = f.get("+")
                pk = old["id"] if old else new["id"]
                v_old = old["value"] if old else None
                v_new = new["value"] if new else None
                ds[pk] = (v_old, v_new)
            return ds

        # changes from HEAD (R1 -> WC)
        r = cli_runner.invoke(["diff", "--exit-code", "-o", "json", R1])
        assert r.exit_code == 1, r
        odata = json.loads(r.stdout)["sno.diff/v1+hexwkb"]
        ddata = _extract(odata)
        assert ddata == {
            1: ("a1", "a"),
            3: ("c", "c1"),
            4: ("d1", "d2"),
            5: ("e1", None),
            6: (None, "f"),
            8: ("h", "h1"),
            9: ("i", None),
            11: (None, "k"),
            12: (None, "l1"),
        }

        # changes from HEAD^1 (R0 -> WC)
        r = cli_runner.invoke(["diff", "--exit-code", "-o", "json", R0])
        assert r.exit_code == 1, r
        odata = json.loads(r.stdout)["sno.diff/v1+hexwkb"]
        ddata = _extract(odata)
        assert ddata == {
            2: ("b", "b1"),
            3: ("c", "c1"),
            4: ("d", "d2"),
            5: ("e", None),
            7: ("g", None),
            8: (None, "h1"),
            10: (None, "j"),
            11: (None, "k"),
            12: ("l", "l1"),
        }


def test_diff_object_add_empty():
    null_diff = DeltaDiff()
    assert len(null_diff) == 0

    assert null_diff + null_diff is not null_diff
    assert null_diff + null_diff == null_diff

    diff = DeltaDiff(
        [
            Delta.insert((20, {"pk": 20})),
            Delta.update((10, {"pk": 10}), (11, {"pk": 11})),
            Delta.delete((30, {"pk": 30})),
        ],
    )
    assert diff + null_diff == diff
    assert null_diff + diff == diff


# ID  R0  ->  R1  ->  R2
# 1   a       a1      a
# 2   b       b1      b1
# 3   c       c       c1
# 4   d       d1      d2
# 5   e       e1      e*
# 6   f       f*      f+
# 7   g       g*      -
# 8   -       h+      h1
# 9   -       i+      i*
# 10  -       j+      j
# 11  -       -       k+
# 12  l       l*      l1+
DIFF_R1 = DeltaDiff(
    [
        Delta.update((1, {"pk": 1, "v": "a"}), (1, {"pk": 1, "v": "a1"})),
        Delta.update((2, {"pk": 2, "v": "b"}), (2, {"pk": 2, "v": "b1"})),
        # 3 no-op: c -> c
        Delta.update((4, {"pk": 4, "v": "d"}), (4, {"pk": 4, "v": "d1"})),
        Delta.update((5, {"pk": 5, "v": "e"}), (5, {"pk": 5, "v": "e1"})),
        Delta.delete((6, {"pk": 6, "v": "f"})),
        Delta.delete((7, {"pk": 7, "v": "g"})),
        Delta.insert((8, {"pk": 8, "v": "h"})),
        Delta.insert((9, {"pk": 9, "v": "i"})),
        Delta.insert((10, {"pk": 10, "v": "j"})),
        # 11 no-op: None -> None
        Delta.delete((12, {"pk": 12, "v": "l"})),
    ],
)

DIFF_R2 = DeltaDiff(
    [
        Delta.update((1, {"pk": 1, "v": "a1"}), (1, {"pk": 1, "v": "a"})),
        # 2 no-op: b1 -> b1
        Delta.update((3, {"pk": 3, "v": "c"}), (3, {"pk": 3, "v": "c1"})),
        Delta.update((4, {"pk": 4, "v": "d1"}), (4, {"pk": 4, "v": "d2"})),
        Delta.delete((5, {"pk": 5, "v": "e1"})),
        Delta.insert((6, {"pk": 6, "v": "f"})),
        # 7 no-op: None -> None
        Delta.update((8, {"pk": 8, "v": "h"}), (8, {"pk": 8, "v": "h1"})),
        Delta.delete((9, {"pk": 9, "v": "i"})),
        # 10 no-op: j -> j
        Delta.insert((11, {"pk": 11, "v": "k"})),
        Delta.insert((12, {"pk": 12, "v": "l1"})),
    ],
)

DIFF_R0_R2 = DeltaDiff(
    [
        # 1 no-op: a -> a
        Delta.update((2, {"pk": 2, "v": "b"}), (2, {"pk": 2, "v": "b1"})),
        Delta.update((3, {"pk": 3, "v": "c"}), (3, {"pk": 3, "v": "c1"})),
        Delta.update((4, {"pk": 4, "v": "d"}), (4, {"pk": 4, "v": "d2"})),
        Delta.delete((5, {"pk": 5, "v": "e"})),
        # 6 no-op: f -> f
        Delta.delete((7, {"pk": 7, "v": "g"})),
        Delta.insert((8, {"pk": 8, "v": "h1"})),
        # 9 no-op: None -> None
        Delta.insert((10, {"pk": 10, "v": "j"})),
        Delta.insert((11, {"pk": 11, "v": "k"})),
        Delta.update((12, {"pk": 12, "v": "l"}), (12, {"pk": 12, "v": "l1"})),
    ],
)


def test_diff_object_add():
    assert DIFF_R1 != DIFF_R0_R2
    assert DIFF_R2 != DIFF_R0_R2
    assert DIFF_R1 + DIFF_R2 == DIFF_R0_R2

    diff = DIFF_R1.copy()
    assert diff != DIFF_R0_R2
    diff += DIFF_R2
    assert diff == DIFF_R0_R2

    assert ~DIFF_R0_R2 == ~DIFF_R2 + ~DIFF_R1


def test_diff_object_add_reverse():
    """
    Check that ~(A + B) == (~B + ~A)
    """
    assert ~DIFF_R0_R2 == ~DIFF_R2 + ~DIFF_R1


def _count(generator):
    return len(list(generator))


def test_diff_object_eq_reverse():
    forward = DIFF_R0_R2
    reverse = ~forward
    forward_counts = forward.type_counts()
    reverse_counts = reverse.type_counts()
    assert forward_counts["inserts"] == reverse_counts["deletes"]
    assert forward_counts["deletes"] == reverse_counts["inserts"]
    assert forward_counts["updates"] == reverse_counts["updates"]
    assert list(forward.values()) == [Delta(v1, v0) for v0, v1 in reverse.values()]


def test_diff_3way(data_working_copy, cli_runner, insert, request):
    with data_working_copy("points") as (repo_path, wc):
        engine = gpkg_engine(wc)
        repo = SnoRepo(repo_path)

        # new branch
        r = cli_runner.invoke(["checkout", "-b", "changes"])
        assert r.exit_code == 0, r.stderr
        assert repo.head.name == "refs/heads/changes"

        # make some changes
        with engine.connect() as db:
            insert(db)
            insert(db)
            b_commit_id = insert(db)
        assert repo.head.target.hex == b_commit_id

        r = cli_runner.invoke(["checkout", "master"])
        assert r.exit_code == 0, r.stderr
        assert repo.head.target.hex != b_commit_id

        with engine.connect() as db:
            m_commit_id = insert(db)

        H.git_graph(request, "pre-merge-master")

        # Three dots diff should show both sets of changes.
        r = cli_runner.invoke(["diff", "-o", "json", f"{m_commit_id}...{b_commit_id}"])
        assert r.exit_code == 0, r.stderr
        features = json.loads(r.stdout)["sno.diff/v1+hexwkb"]["nz_pa_points_topo_150k"][
            "feature"
        ]
        assert len(features) == 4

        r = cli_runner.invoke(["diff", "-o", "json", f"{b_commit_id}...{m_commit_id}"])
        assert r.exit_code == 0, r.stderr
        features = json.loads(r.stdout)["sno.diff/v1+hexwkb"]["nz_pa_points_topo_150k"][
            "feature"
        ]
        assert len(features) == 4

        # Two dots diff should show only one set of changes - the changes on the target branch.
        r = cli_runner.invoke(["diff", "-o", "json", f"{m_commit_id}..{b_commit_id}"])
        assert r.exit_code == 0, r.stderr
        features = json.loads(r.stdout)["sno.diff/v1+hexwkb"]["nz_pa_points_topo_150k"][
            "feature"
        ]
        assert len(features) == 3

        r = cli_runner.invoke(["diff", "-o", "json", f"{b_commit_id}..{m_commit_id}"])
        assert r.exit_code == 0, r.stderr
        features = json.loads(r.stdout)["sno.diff/v1+hexwkb"]["nz_pa_points_topo_150k"][
            "feature"
        ]
        assert len(features) == 1


@pytest.mark.parametrize("output_format", SHOW_OUTPUT_FORMATS)
def test_show_points_HEAD(output_format, data_archive_readonly, cli_runner):
    """
    Show a patch; ref defaults to HEAD
    """
    with data_archive_readonly("points"):
        r = cli_runner.invoke(["show", f"--output-format={output_format}", "HEAD"])
        assert r.exit_code == 0, r.stderr

        if output_format == "text":
            commit_hash = r.stdout[7:47]
            assert r.stdout.splitlines() == [
                f"commit {commit_hash}",
                "Author: Robert Coup <robert@coup.net.nz>",
                "Date:   Thu Jun 20 15:28:33 2019 +0100",
                "",
                "    Improve naming on Coromandel East coast",
                "",
                "--- nz_pa_points_topo_150k:feature:1095",
                "+++ nz_pa_points_topo_150k:feature:1095",
                "-                               name_ascii = ␀",
                "+                               name_ascii = Harataunga (Rakairoa)",
                "",
                "-                               macronated = N",
                "+                               macronated = Y",
                "-                                     name = ␀",
                "+                                     name = Harataunga (Rākairoa)",
                "",
                "--- nz_pa_points_topo_150k:feature:1166",
                "+++ nz_pa_points_topo_150k:feature:1166",
                "-                               name_ascii = ␀",
                "+                               name_ascii = Oturu",
                "-                                     name = ␀",
                "+                                     name = Oturu",
                "--- nz_pa_points_topo_150k:feature:1168",
                "+++ nz_pa_points_topo_150k:feature:1168",
                "-                               name_ascii = ␀",
                "+                               name_ascii = Tairua",
                "-                                     name = ␀",
                "+                                     name = Tairua",
                "--- nz_pa_points_topo_150k:feature:1181",
                "+++ nz_pa_points_topo_150k:feature:1181",
                "-                               name_ascii = ␀",
                "+                               name_ascii = Ko Te Ra Matiti (Wharekaho)",
                "-                               macronated = N",
                "+                               macronated = Y",
                "-                                     name = ␀",
                "+                                     name = Ko Te Rā Matiti (Wharekaho)",
                "--- nz_pa_points_topo_150k:feature:1182",
                "+++ nz_pa_points_topo_150k:feature:1182",
                "-                               name_ascii = ␀",
                "+                               name_ascii = Ko Te Ra Matiti (Wharekaho)",
                "-                               macronated = N",
                "+                               macronated = Y",
                "-                                     name = ␀",
                "+                                     name = Ko Te Rā Matiti (Wharekaho)",
            ]

        elif output_format == "json":
            j = json.loads(r.stdout)
            # check the diff's present, but this test doesn't need to have hundreds of lines
            # to know exactly what it is (we have diff tests above)
            assert "sno.diff/v1+hexwkb" in j
            assert j["sno.show/v1"] == {
                "authorEmail": "robert@coup.net.nz",
                "authorName": "Robert Coup",
                "authorTime": "2019-06-20T14:28:33Z",
                "authorTimeOffset": "+01:00",
                "message": "Improve naming on Coromandel East coast",
            }


def test_diff_filtered(data_archive_readonly, cli_runner):
    with data_archive_readonly("points"):
        r = cli_runner.invoke(["diff", "HEAD^...", "nz_pa_points_topo_150k:1182"])
        assert r.exit_code == 0, r.stderr
        assert r.stdout.splitlines() == [
            "--- nz_pa_points_topo_150k:feature:1182",
            "+++ nz_pa_points_topo_150k:feature:1182",
            "-                               name_ascii = ␀",
            "+                               name_ascii = Ko Te Ra Matiti (Wharekaho)",
            "-                               macronated = N",
            "+                               macronated = Y",
            "-                                     name = ␀",
            "+                                     name = Ko Te Rā Matiti (Wharekaho)",
        ]


@pytest.mark.parametrize("output_format", SHOW_OUTPUT_FORMATS)
def test_show_polygons_initial(output_format, data_archive_readonly, cli_runner):
    """Make sure we can show the initial commit"""
    with data_archive_readonly("polygons"):
        r = cli_runner.invoke(["log"])
        assert r.exit_code == 0, r.stderr
        initial_commit = re.findall("commit ([0-9a-f]+)\n", r.stdout)[-1]

        r = cli_runner.invoke(
            ["show", f"--output-format={output_format}", initial_commit]
        )
        assert r.exit_code == 0, r.stderr

        if output_format == "text":
            lines = r.stdout.splitlines()

            assert lines[0:6] == [
                "commit a149557b7cec7a35c07a9bc404a5d53f6c5ad154",
                "Author: Robert Coup <robert@coup.net.nz>",
                "Date:   Mon Jul 22 12:05:39 2019 +0100",
                "",
                "    Import from nz-waca-adjustments.gpkg",
                "",
            ]

            assert "+++ nz_waca_adjustments:meta:title" in lines
            index = lines.index("+++ nz_waca_adjustments:meta:title")
            assert lines[index : index + 2] == [
                "+++ nz_waca_adjustments:meta:title",
                "+ NZ WACA Adjustments",
            ]

            assert "+++ nz_waca_adjustments:feature:1424927" in lines
            index = lines.index("+++ nz_waca_adjustments:feature:1424927")
            assert lines[index : index + 5] == [
                "+++ nz_waca_adjustments:feature:1424927",
                "+                                     geom = MULTIPOLYGON(...)",
                "+                            date_adjusted = 2011-03-25T07:30:45Z",
                "+                         survey_reference = ␀",
                "+                           adjusted_nodes = 1122",
            ]

        elif output_format == "json":
            j = json.loads(r.stdout)
            assert "sno.diff/v1+hexwkb" in j
            assert j["sno.show/v1"] == {
                "authorEmail": "robert@coup.net.nz",
                "authorName": "Robert Coup",
                "authorTime": "2019-07-22T11:05:39Z",
                "authorTimeOffset": "+01:00",
                "message": "Import from nz-waca-adjustments.gpkg",
            }


def test_show_json_format(data_archive_readonly, cli_runner):
    with data_archive_readonly("points"):
        r = cli_runner.invoke(["show", f"-o", "json", "--json-style=compact", "HEAD"])

        assert r.exit_code == 0, r.stderr
        # output is compact, no indentation
        assert '"sno.diff/v1+hexwkb": {"' in r.stdout


def test_show_json_coloured(data_archive_readonly, cli_runner, monkeypatch):
    always_output_colour = lambda x: True
    monkeypatch.setattr(sno.output_util, "can_output_colour", always_output_colour)

    with data_archive_readonly("points"):
        r = cli_runner.invoke(["show", f"-o", "json", "--json-style=pretty", "HEAD"])
        assert r.exit_code == 0, r.stderr
        # No asserts about colour codes - that would be system specific. Just a basic check:
        assert '"sno.diff/v1+hexwkb"' in r.stdout


def test_create_patch(data_archive_readonly, cli_runner):
    """
    Show a patch; ref defaults to HEAD
    """
    with data_archive_readonly("points"):
        r = cli_runner.invoke(["create-patch"])
        assert r.exit_code == 2, r.stderr
        r = cli_runner.invoke(["create-patch", "HEAD"])
        assert r.exit_code == 0, r.stderr

        j = json.loads(r.stdout)
        # check the diff's present, but this test doesn't need to have hundreds of lines
        # to know exactly what it is (we have diff tests above)
        assert "sno.diff/v1+hexwkb" in j
        assert j["sno.patch/v1"] == {
            "authorEmail": "robert@coup.net.nz",
            "authorName": "Robert Coup",
            "authorTime": "2019-06-20T14:28:33Z",
            "authorTimeOffset": "+01:00",
            "message": "Improve naming on Coromandel East coast",
        }


def test_show_shallow_clone(data_archive_readonly, cli_runner, tmp_path, chdir):
    # just checking you can 'show' the first commit of a shallow clone
    with data_archive_readonly("points") as original_path:
        clone_path = tmp_path / "shallow-clone"
        r = cli_runner.invoke(["clone", "--depth=1", original_path, clone_path])
        assert r.exit_code == 0, r.stderr

        with chdir(clone_path):
            r = cli_runner.invoke(["show"])
            assert r.exit_code == 0, r


def test_diff_streaming(data_archive_readonly):
    # Test that a diff can be created without reading every feature,
    # and that the features in that diff can be read one by one.
    with data_archive_readonly("points") as repo_path:
        repo = SnoRepo(repo_path)
        old = repo.datasets("HEAD^")[H.POINTS.LAYER]
        new = repo.datasets("HEAD")[H.POINTS.LAYER]

        def override_get_feature(self, *args, **kwargs):
            self.get_feature_calls += 1
            return self.__class__.get_feature(self, *args, **kwargs)

        old.get_feature_calls = 0
        new.get_feature_calls = 0

        old.get_feature = functools.partial(override_get_feature, old)
        new.get_feature = functools.partial(override_get_feature, new)

        feature_diff = old.diff(new)["feature"]

        expected_calls = 0
        assert old.get_feature_calls == expected_calls
        assert new.get_feature_calls == expected_calls

        for key, delta in sorted(feature_diff.items()):
            print(delta.old_value)
            print(delta.new_value)
            expected_calls += 1
            assert old.get_feature_calls == expected_calls
            assert new.get_feature_calls == expected_calls
