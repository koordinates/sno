import pytest
from sno.sqlalchemy import gpkg_engine


H = pytest.helpers.helpers()


def test_fsck(data_working_copy, cli_runner):
    with data_working_copy("points") as (repo, wc):
        engine = gpkg_engine(wc)

        r = cli_runner.invoke(["fsck"])
        assert r.exit_code == 0, r.stdout

        # introduce a feature mismatch
        # assert H.row_count(db, H.POINTS.LAYER) == H.POINTS.ROWCOUNT
        # assert H.row_count(db, "gpkg_sno_track") == 0

        with engine.connect() as db:
            db.execute(f"UPDATE {H.POINTS.LAYER} SET name='fred' WHERE fid=1;")
            db.execute("""DELETE FROM "gpkg_sno_track" WHERE pk='1';""")

        r = cli_runner.invoke(["fsck"])
        assert r.exit_code == 1, r

        r = cli_runner.invoke(["fsck", "--reset-dataset=nz_pa_points_topo_150k"])
        assert r.exit_code == 0, r

        # assert H.row_count(db, H.POINTS.LAYER) == H.POINTS.ROWCOUNT
        # assert H.row_count(db, "gpkg_sno_track") == 0

        r = cli_runner.invoke(["fsck"])
        assert r.exit_code == 0, r
