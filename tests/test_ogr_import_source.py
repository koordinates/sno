import os
import pytest

from sno import structure
from sno.ogr_import_source import PostgreSQLImportSource
from sno.sno_repo import SnoRepo


def test_postgres_url_parsing():
    func = PostgreSQLImportSource.postgres_url_to_ogr_conn_str
    with pytest.raises(ValueError):
        func("https://example.com/")

    # this is valid (AFAICT), means connect on the default domain socket
    # to the default database with the current posix user
    assert func("postgres://") == "PG:"
    assert func("postgresql://") == "PG:"
    assert func("postgres:///") == "PG:"

    assert func("postgres://myhost/") == "PG:host=myhost"
    assert func("postgres:///?host=myhost") == "PG:host=myhost"
    # querystring params take precedence
    assert func("postgres://otherhost/?host=myhost") == "PG:host=myhost"

    assert func("postgres://myhost:1234/") == "PG:host=myhost port=1234"
    assert func("postgres://myhost/?port=1234") == "PG:host=myhost port=1234"
    assert (
        func("postgres://myhost/dbname?port=1234")
        == "PG:dbname=dbname host=myhost port=1234"
    )
    # everything, including extra options
    assert (
        func("postgres://u:p@h:1234/d?client_encoding=utf16")
        == "PG:client_encoding=utf16 dbname=d host=h password=p port=1234 user=u"
    )

    # domain socket hostnames starting with a '/' are handled, but
    # must be url-quoted if they're part of the URL
    assert (
        func("postgres://%2Fvar%2Flib%2Fpostgresql/d")
        == "PG:dbname=d host=/var/lib/postgresql"
    )
    # but url-quoting seems to be optional when they're in the querystring
    assert (
        func("postgres:///?host=/var/lib/postgresql") == "PG:host=/var/lib/postgresql"
    )
    assert (
        func("postgres:///?host=%2Fvar%2Flib%2Fpostgresql")
        == "PG:host=/var/lib/postgresql"
    )


def test_import_various_field_types(tmp_path, postgis_db, cli_runner):
    # Using postgres here because it has the best type preservation
    with postgis_db.cursor() as c:
        c.execute(
            """
                DROP TABLE IF EXISTS typoes;
                CREATE TABLE typoes (
                    bigant BIGINT PRIMARY KEY,
                    tumeric20_0 NUMERIC(20,0),
                    tumeric5_5 NUMERIC(5,5),
                    flote FLOAT,
                    dubble DOUBLE PRECISION,
                    smallant SMALLINT,
                    tumeric99_0 numeric(99,0),
                    tumeric4_0 numeric(4,0),
                    tumeric numeric,
                    techs varchar,
                    techs10 varchar(100)
                );
                """
        )

    r = cli_runner.invoke(["init", str(tmp_path)])
    assert r.exit_code == 0, r.stderr
    r = cli_runner.invoke(
        ["-C", str(tmp_path), "import", os.environ["SNO_POSTGRES_URL"], "typoes"],
    )

    assert r.exit_code == 0, r.stderr
    repo = SnoRepo(tmp_path)
    dataset = structure.RepositoryStructure(repo)["typoes"]

    cols = {}
    for col in dataset.schema.to_column_dicts():
        col = col.copy()
        col.pop("id")
        cols[col.pop("name")] = col

    assert cols == {
        "bigant": {"dataType": "integer", "primaryKeyIndex": 0, "size": 64},
        "dubble": {"dataType": "float", "size": 64},
        "smallant": {"dataType": "integer", "size": 16},
        "tumeric20_0": {"dataType": "numeric", "precision": 20, "scale": 0},
        "tumeric4_0": {"dataType": "numeric", "precision": 4, "scale": 0},
        "tumeric5_5": {"dataType": "numeric", "precision": 5, "scale": 5},
        "tumeric99_0": {"dataType": "numeric", "precision": 99, "scale": 0},
        "techs": {"dataType": "text"},
        "techs10": {"dataType": "text", "length": 100},
        # these two are regrettable but currently unavoidable;
        # ogr treats both floats and unqualified numerics as Real(0.0),
        # so they're indistinguishable from doubles.
        "tumeric": {"dataType": "float", "size": 64},
        "flote": {"dataType": "float", "size": 64},
    }
