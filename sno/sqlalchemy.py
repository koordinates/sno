import sqlalchemy
from pysqlite3 import dbapi2 as sqlite
import psycopg2
from psycopg2.extensions import Binary, new_type, register_adapter, register_type


from sno import spatialite_path
from sno.geometry import Geometry


def insert_command(table_name, col_names, schema=None):
    return sqlalchemy.table(
        table_name,
        *[sqlalchemy.column(c) for c in col_names],
        schema=schema,
    ).insert()


def gpkg_engine(path):
    def _on_connect(pysqlite_conn, connection_record):
        pysqlite_conn.isolation_level = None
        pysqlite_conn.enable_load_extension(True)
        pysqlite_conn.load_extension(spatialite_path)
        pysqlite_conn.enable_load_extension(False)
        dbcur = pysqlite_conn.cursor()
        dbcur.execute("SELECT EnableGpkgMode();")
        dbcur.execute("PRAGMA foreign_keys = ON;")
        dbcur.execute("PRAGMA journal_mode = TRUNCATE;")  # faster

    engine = sqlalchemy.create_engine(f"sqlite:///{path}", module=sqlite)
    sqlalchemy.event.listen(engine, "connect", _on_connect)
    return engine


# PostGIS set-up - timestamps:


def _adapt_timestamp_from_pg(t, db):
    if t is None:
        return t
    # Output timestamps in the same variant of ISO 8601 required by GPKG.
    return str(t).replace(" ", "T").replace("+00", "Z")


# See https://github.com/psycopg/psycopg2/blob/master/psycopg/typecast_builtins.c
TIMESTAMP_OID = 1114
TIMESTAMP = new_type((TIMESTAMP_OID,), "TIMESTAMP", _adapt_timestamp_from_pg)
psycopg2.extensions.register_type(TIMESTAMP)

TIMESTAMPTZ_OID = 1184
TIMESTAMPTZ = new_type((TIMESTAMPTZ_OID,), "TIMESTAMPTZ", _adapt_timestamp_from_pg)
psycopg2.extensions.register_type(TIMESTAMPTZ)


# PostGIS set-up - strings:
# We mostly want data out of the DB as strings, just as happens in GPKG.
# Then we can serialise it using MessagePack.


def _adapt_to_string(v, db):
    return str(v) if v is not None else None


ADAPT_TO_STR_TYPES = {
    1082: "DATE",
    1083: "TIME",
    1266: "TIME",
    704: "INTERVAL",
    1186: "INTERVAL",
    1700: "DECIMAL",
}

for oid in ADAPT_TO_STR_TYPES:
    t = new_type((oid,), ADAPT_TO_STR_TYPES[oid], _adapt_to_string)
    psycopg2.extensions.register_type(t)


# PostGIS set-up - geometry:


def _adapt_geometry_to_pg(g):
    return Binary(g.to_ewkb())


def _adapt_geometry_from_pg(g, db):
    return Geometry.from_hex_ewkb(g)


# We can register_adapter, but we still have to register_type below:
register_adapter(Geometry, _adapt_geometry_to_pg)


def postgis_engine(pgurl):
    def _on_connect(psycopg2_conn, connection_record):
        dbcur = psycopg2_conn.cursor()
        dbcur.execute("SET timezone='UTC';")
        dbcur.execute("SET intervalstyle='iso_8601';")
        dbcur.execute("SELECT oid FROM pg_type WHERE typname='geometry';")
        # Unlike the other set-up, this must be done with an actual connection open -
        # otherwise we don't know the geometry_oid (or if the geometry type exists at all).
        r = dbcur.fetchone()
        if r:
            geometry_type = new_type((r[0],), "GEOMETRY", _adapt_geometry_from_pg)
            register_type(geometry_type, psycopg2_conn)

    engine = sqlalchemy.create_engine(pgurl, module=psycopg2)
    sqlalchemy.event.listen(engine, "connect", _on_connect)

    return engine
