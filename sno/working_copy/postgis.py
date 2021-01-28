import contextlib
import logging
import re
import time
from urllib.parse import urlsplit, urlunsplit

import click
import pygit2
import psycopg2
from psycopg2.extensions import new_type, register_adapter, register_type, Binary
from psycopg2.extras import DictCursor
from psycopg2.sql import Identifier, SQL


from .base import WorkingCopy
from . import postgis_adapter
from sno import crs_util
from sno.geometry import Geometry
from sno.exceptions import NotFound, NO_WORKING_COPY
from sno.filter_util import UNFILTERED
from sno.schema import Schema


"""
* database needs to exist
* database needs to have postgis enabled
* database user needs to be able to:
    1. create 'sno' schema & tables
    2. create & alter tables in the default (or specified) schema
    3. create triggers
"""

L = logging.getLogger("sno.working_copy.postgis")


def _adapt_geometry_to_db(g):
    return Binary(g.to_ewkb())


register_adapter(Geometry, _adapt_geometry_to_db)


def adapt_geometry_from_db(g, dbcur):
    return Geometry.from_hex_ewkb(g)


def adapt_timestamp_from_db(t, dbcur):
    # TODO - revisit timezones.
    if t is None:
        return t
    # Output timestamps in the same variant of ISO 8601 required by GPKG.
    return str(t).replace(" ", "T").replace("+00", "Z")


def adapt_to_string(v, dbcur):
    return str(v) if v is not None else None


# See https://github.com/psycopg/psycopg2/blob/master/psycopg/typecast_builtins.c
TIMESTAMP_OID = 1114
TIMESTAMP = new_type((TIMESTAMP_OID,), "TIMESTAMP", adapt_timestamp_from_db)
psycopg2.extensions.register_type(TIMESTAMP)

TIMESTAMPTZ_OID = 1184
TIMESTAMPTZ = new_type((TIMESTAMPTZ_OID,), "TIMESTAMPTZ", adapt_timestamp_from_db)
psycopg2.extensions.register_type(TIMESTAMPTZ)


# We mostly want data out of the DB as strings, just as happens in GPKG.
# Then we can serialise it using MessagePack.
# TODO - maybe move adapt-to-string logic to MessagePack?
ADAPT_TO_STR_TYPES = {
    1082: "DATE",
    1083: "TIME",
    1266: "TIME",
    704: "INTERVAL",
    1186: "INTERVAL",
    1700: "DECIMAL",
}

for oid in ADAPT_TO_STR_TYPES:
    t = new_type((oid,), ADAPT_TO_STR_TYPES[oid], adapt_to_string)
    psycopg2.extensions.register_type(t)


class WorkingCopy_Postgis(WorkingCopy):
    def __init__(self, repo, uri):
        """
        uri: connection string of the form postgresql://[user[:password]@][netloc][:port][/dbname/schema][?param1=value1&...]
        """
        self.L = logging.getLogger(self.__class__.__qualname__)

        self.repo = repo
        self.uri = uri
        self.path = uri

        url = urlsplit(uri)

        if url.scheme != "postgresql":
            raise ValueError("Expecting postgresql://")

        url_path = url.path
        path_parts = url_path[1:].split("/", 3) if url_path else []
        if len(path_parts) != 2:
            raise ValueError("Expecting postgresql://[HOST]/DBNAME/SCHEMA")
        url_path = f"/{path_parts[0]}"
        self.schema = path_parts[1]

        url_query = url.query
        if "fallback_application_name" not in url_query:
            url_query = "&".join(
                filter(None, [url_query, "fallback_application_name=sno"])
            )

        # rebuild DB URL suitable for libpq
        self.dburl = urlunsplit([url.scheme, url.netloc, url_path, url_query, ""])

    @classmethod
    def check_valid_uri(cls, uri, workdir_path):
        url = urlsplit(uri)

        if url.scheme != "postgresql":
            raise click.UsageError(
                "Invalid postgres URI - Expecting URI in form: postgresql://[HOST]/DBNAME/SCHEMA"
            )

        url_path = url.path
        path_parts = url_path[1:].split("/", 3) if url_path else []

        suggestion_message = ""
        if len(path_parts) == 1 and workdir_path is not None:
            suggested_path = f"/{path_parts[0]}/{cls.default_schema(workdir_path)}"
            suggested_uri = urlunsplit(
                [url.scheme, url.netloc, suggested_path, url.query, ""]
            )
            suggestion_message = f"\nFor example: {suggested_uri}"

        if len(path_parts) != 2:
            raise click.UsageError(
                "Invalid postgres URI - postgis working copy requires both dbname and schema:\n"
                "Expecting URI in form: postgresql://[HOST]/DBNAME/SCHEMA"
                + suggestion_message
            )

    @classmethod
    def default_schema(cls, workdir_path):
        stem = workdir_path.stem
        schema = re.sub("[^a-z0-9]+", "_", stem.lower()) + "_sno"
        if schema[0].isdigit():
            schema = "_" + schema
        return schema

    def __str__(self):
        p = urlsplit(self.uri)
        if p.password is not None:
            nl = p.hostname
            if p.username is not None:
                nl = f"{p.username}@{nl}"
            if p.port is not None:
                nl += f":{p.port}"

            p._replace(netloc=nl)
        return p.geturl()

    def _sno_table(self, sno_table):
        return self._table_identifier(f"_sno_{sno_table}")

    def _table_identifier(self, table):
        return Identifier(self.schema, table)

    @contextlib.contextmanager
    def session(self, bulk=0):
        """
        Context manager for PostgreSQL DB sessions, yields a connection object inside a transaction

        Calling again yields the _same_ connection, the transaction/etc only happen in the outer one.
        """
        L = logging.getLogger(f"{self.__class__.__qualname__}.session")

        if hasattr(self, "_db"):
            # inner - reuse
            L.debug("session: existing...")
            yield self._db
            L.debug("session: existing/done")
        else:
            L.debug("session: new...")
            try:
                self._db = psycopg2.connect(self.dburl, cursor_factory=DictCursor)
            except Exception as e:
                raise NotFound(str(e), exit_code=NO_WORKING_COPY)
            self._configure_output(self._db)
            self._register_geometry_type(self._db)

            try:
                yield self._db
            except:  # noqa
                self._db.rollback()
                raise
            else:
                self._db.commit()
            finally:
                self._db.close()
                del self._db
                L.debug("session: new/done")

    def _configure_output(self, db):
        """Output timestamptz in UTC, and output interval as ISO 8601 duration."""
        dbcur = db.cursor()
        dbcur.execute("SET timezone='UTC';")
        dbcur.execute("SET intervalstyle='iso_8601';")

    def _register_geometry_type(self, db):
        """
        Register adapt_geometry_from_db for the type with OID: 'geometry'::regtype::oid
        - which could be different in different postgis databases, and might not even exist.
        """
        dbcur = db.cursor()
        dbcur.execute("SELECT oid FROM pg_type WHERE typname='geometry';")
        r = dbcur.fetchone()
        if r:
            geometry_oid = r[0]
            geometry = new_type((geometry_oid,), "GEOMETRY", adapt_geometry_from_db)
            register_type(geometry, db)

    def is_created(self):
        """
        Returns true if the postgres schema referred to by this working copy exists and
        contains at least one table. If it exists but is empty, it is treated as uncreated.
        This is so the postgres schema can be created ahead of time before a repo is created
        or configured, without it triggering code that checks for corrupted working copies.
        Note that it might not be initialised as a working copy - see self.is_initialised.
        """
        with self.session() as db:
            dbcur = db.cursor()
            dbcur.execute(
                SQL(
                    """
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_schema=%s;
                    """
                ),
                (self.schema,),
            )
            return dbcur.fetchone()[0] > 0

    def is_initialised(self):
        """
        Returns true if the postgis working copy is initialised -
        the schema exists and has the necessary sno tables, _sno_state and _sno_track.
        """
        with self.session() as db:
            dbcur = db.cursor()
            dbcur.execute(
                SQL(
                    """
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_schema=%s AND table_name IN ('_sno_state', '_sno_track');
                    """
                ),
                (self.schema,),
            )
            return dbcur.fetchone()[0] == 2

    def has_data(self):
        """
        Returns true if the postgis working copy seems to have user-created content already.
        """
        with self.session() as db:
            dbcur = db.cursor()
            dbcur.execute(
                SQL(
                    """
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_schema=%s AND table_name NOT IN ('_sno_state', '_sno_track');
                    """
                ),
                (self.schema,),
            )
            return dbcur.fetchone()[0] > 0

    def create_and_initialise(self):
        with self.session() as db:
            dbcur = db.cursor()
            dbcur.execute(
                SQL("CREATE SCHEMA IF NOT EXISTS {}").format(Identifier(self.schema))
            )
            dbcur.execute(
                SQL(
                    """
                CREATE TABLE IF NOT EXISTS {} (
                    table_name TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NULL,
                    PRIMARY KEY (table_name, key)
                );
                """
                ).format(self.STATE_TABLE)
            )
            dbcur.execute(
                SQL(
                    """
                CREATE TABLE IF NOT EXISTS {} (
                    table_name TEXT NOT NULL,
                    pk TEXT NULL,
                    PRIMARY KEY (table_name, pk)
                );
                """
                ).format(self.TRACKING_TABLE)
            )
            dbcur.execute(
                SQL(
                    """
                CREATE OR REPLACE FUNCTION {func}() RETURNS TRIGGER AS $body$
                DECLARE
                    pk_field text := quote_ident(TG_ARGV[0]);
                    pk_old text;
                    pk_new text;
                BEGIN
                    IF (TG_OP = 'INSERT' OR TG_OP = 'UPDATE') THEN
                        EXECUTE 'SELECT $1.' || pk_field USING NEW INTO pk_new;

                        INSERT INTO {tracking_table} (table_name,pk) VALUES
                        (TG_TABLE_NAME::TEXT, pk_new)
                        ON CONFLICT DO NOTHING;
                    END IF;
                    IF (TG_OP = 'UPDATE' OR TG_OP = 'DELETE') THEN
                        EXECUTE 'SELECT $1.' || pk_field USING OLD INTO pk_old;

                        INSERT INTO {tracking_table} (table_name,pk) VALUES
                        (TG_TABLE_NAME::TEXT, pk_old)
                        ON CONFLICT DO NOTHING;

                        IF (TG_OP = 'DELETE') THEN
                            RETURN OLD;
                        END IF;
                    END IF;
                    RETURN NEW;
                END;
                $body$
                LANGUAGE plpgsql
                SECURITY DEFINER
            """
                ).format(
                    func=Identifier(self.schema, "_sno_track_trigger"),
                    tracking_table=self.TRACKING_TABLE,
                )
            )

    def delete(self, keep_container_if_possible=False):
        """ Delete all tables in the schema"""
        # We don't use drop ... cascade since that could also delete things outside the schema.
        # Better to fail to delete the schema, than to delete things the user didn't want to delete.
        with self.session() as db:
            dbcur = db.cursor()
            # Don't worry about constraints when dropping everything.
            dbcur.execute("SET CONSTRAINTS ALL DEFERRED;")
            # Drop tables
            dbcur.execute(
                SQL("SELECT tablename FROM pg_tables where schemaname=%s;"),
                (self.schema,),
            )
            tables = [t[0] for t in dbcur]
            if tables:
                dbcur.execute(
                    SQL("DROP TABLE IF EXISTS {};").format(
                        SQL(", ").join(self._table_identifier(t) for t in tables)
                    )
                )
            # Drop functions
            dbcur.execute(
                SQL(
                    "SELECT proname from pg_proc WHERE pronamespace = %s::regnamespace;"
                ),
                (self.schema,),
            )
            functions = [f[0] for f in dbcur]
            if functions:
                dbcur.execute(
                    SQL("DROP FUNCTION IF EXISTS {};").format(
                        SQL(", ").join(self._table_identifier(f) for f in functions)
                    )
                )
            # Drop schema, unless keep_container_if_possible=True
            if not keep_container_if_possible:
                dbcur.execute(
                    SQL("DROP SCHEMA IF EXISTS {};").format(Identifier(self.schema))
                )

    def write_meta(self, dataset):
        with self.session():
            self.write_meta_title(dataset)
            self.write_meta_crs(dataset)

    def write_meta_title(self, dataset):
        """Write the dataset title as a comment on the table."""
        with self.session() as db:
            dbcur = db.cursor()
            dbcur.execute(
                SQL("COMMENT ON TABLE {} IS %s").format(
                    self._table_identifier(dataset.table_name)
                ),
                (dataset.get_meta_item("title"),),
            )

    def write_meta_crs(self, dataset):
        """Populate the public.spatial_ref_sys table with data from this dataset."""
        spatial_refs = postgis_adapter.generate_postgis_spatial_ref_sys(dataset)
        if not spatial_refs:
            return

        with self.session() as db:
            dbcur = db.cursor()
            for row in spatial_refs:
                # We do not automatically overwrite a CRS if it seems likely to be one
                # of the Postgis builtin definitions - Postgis has lots of EPSG and ESRI
                # definitions built-in, plus the 900913 (GOOGLE) definition.
                # See POSTGIS_WC.md for help on working with CRS definitions in a Postgis WC.
                dbcur.execute(
                    SQL(
                        """
                        INSERT INTO public.spatial_ref_sys AS SRS ({}) VALUES ({})
                        ON CONFLICT (srid) DO UPDATE
                            SET (auth_name, auth_srid, srtext, proj4text)
                            = (EXCLUDED.auth_name, EXCLUDED.auth_srid, EXCLUDED.srtext, EXCLUDED.proj4text)
                            WHERE SRS.auth_name NOT IN ('EPSG', 'ESRI') AND SRS.srid <> 900913;
                        """
                    ).format(
                        SQL(",").join([Identifier(k) for k in row]),
                        SQL(",").join([SQL("%s")] * len(row)),
                    ),
                    tuple(row.values()),
                )

    def delete_meta(self, dataset):
        """Delete any metadata that is only needed by this dataset."""
        pass  # There is no metadata except for the spatial_ref_sys table.

    def _create_spatial_index(self, dataset):
        L = logging.getLogger(f"{self.__class__.__qualname__}._create_spatial_index")

        geom_col = dataset.geom_column_name

        # Create the PostGIS Spatial Index
        sql = SQL("CREATE INDEX {} ON {} USING GIST ({})").format(
            Identifier(f"{dataset.table_name}_idx_{geom_col}"),
            self._table_identifier(dataset.table_name),
            Identifier(geom_col),
        )
        L.debug(
            "Creating spatial index for %s.%s: %s", dataset.table_name, geom_col, sql
        )
        t0 = time.monotonic()
        with self.session() as db:
            db.cursor().execute(sql)
        L.info("Created spatial index in %ss", time.monotonic() - t0)

    def _create_triggers(self, db, dataset):
        pk_field = dataset.primary_key
        db.cursor().execute(
            SQL(
                """
            CREATE TRIGGER {} AFTER INSERT OR UPDATE OR DELETE ON {}
            FOR EACH ROW EXECUTE PROCEDURE {}(%s)
            """
            ).format(
                Identifier("sno_track"),
                self._table_identifier(dataset.table_name),
                Identifier(self.schema, "_sno_track_trigger"),
            ),
            (pk_field,),
        )

    @contextlib.contextmanager
    def _suspend_triggers(self, db, dataset):
        table = dataset.table_name
        db.cursor().execute(
            SQL("ALTER TABLE {} DISABLE TRIGGER sno_track").format(
                self._table_identifier(table),
            )
        )

        try:
            yield
        finally:
            db.cursor().execute(
                SQL("ALTER TABLE {} ENABLE TRIGGER sno_track").format(
                    self._table_identifier(table),
                )
            )

    def get_db_tree(self, table_name="*"):
        with self.session() as db:
            dbcur = db.cursor()
            try:
                dbcur.execute(
                    SQL("SELECT value FROM {} WHERE table_name=%s AND key=%s").format(
                        self.STATE_TABLE
                    ),
                    (table_name, "tree"),
                )
                row = dbcur.fetchone()
            except psycopg2.errors.UndefinedTable:
                row = None
            if not row:
                # It's okay to not have anything in the tree table - it might just mean there are no commits yet.
                # It might also mean that the working copy is not yet initialised - see WorkingCopy.get
                return None
            wc_tree_id = row[0]
            return wc_tree_id

    def write_full(self, commit, *datasets, **kwargs):
        """
        Writes a full layer into a working-copy table

        Use for new working-copy checkouts.
        """
        L = logging.getLogger(f"{self.__class__.__qualname__}.write_full")

        with self.session(bulk=2) as db:
            dbcur = db.cursor()

            dbcur.execute(
                SQL("CREATE SCHEMA IF NOT EXISTS {};").format(Identifier(self.schema))
            )

            for dataset in datasets:
                table = dataset.table_name

                # Create the table
                table_spec = postgis_adapter.v2_schema_to_postgis_spec(
                    dataset.schema, dataset
                )
                col_names = [col.name for col in dataset.schema]

                dbcur.execute(
                    SQL("CREATE TABLE IF NOT EXISTS {} ({});").format(
                        self._table_identifier(table), table_spec
                    )
                )
                self.write_meta(dataset)

                L.info("Creating features...")
                sql_insert_features = SQL(
                    """
                    INSERT INTO {} ({}) VALUES ({});
                """
                ).format(
                    self._table_identifier(table),
                    SQL(",").join([Identifier(c) for c in col_names]),
                    SQL(",").join([SQL("%s")] * len(col_names)),
                )

                feat_count = 0
                t0 = time.monotonic()
                t0p = t0

                CHUNK_SIZE = 10000
                for row_dicts in self._chunk(
                    dataset.features_with_crs_ids(), CHUNK_SIZE
                ):
                    row_tuples = (tuple(row_dict.values()) for row_dict in row_dicts)
                    dbcur.executemany(sql_insert_features, row_tuples)
                    feat_count += dbcur.rowcount

                    nc = feat_count / CHUNK_SIZE
                    if nc % 5 == 0 or not nc.is_integer():
                        t0a = time.monotonic()
                        L.info(
                            "%s features... @%.1fs (+%.1fs, ~%d F/s)",
                            feat_count,
                            t0a - t0,
                            t0a - t0p,
                            (CHUNK_SIZE * 5) / (t0a - t0p),
                        )
                        t0p = t0a

                t1 = time.monotonic()
                L.info("Added %d features to GPKG in %.1fs", feat_count, t1 - t0)
                L.info("Overall rate: %d features/s", (feat_count / (t1 - t0)))

                if dataset.has_geometry:
                    self._create_spatial_index(dataset)

                # Create triggers
                self._create_triggers(db, dataset)

            dbcur.execute(
                SQL(
                    """
                    INSERT INTO {} (table_name, key, value) VALUES (%s, %s, %s)
                    ON CONFLICT (table_name, key) DO UPDATE
                    SET value=EXCLUDED.value;
                """
                ).format(self.STATE_TABLE),
                ("*", "tree", commit.peel(pygit2.Tree).hex),
            )

    def write_features(self, db, dataset, pk_iter, *, ignore_missing=False):
        dbcur = db.cursor()
        pk_field = dataset.primary_key
        col_names = [col.name for col in dataset.schema]

        sql_write_feature = SQL(
            """
            INSERT INTO {table} ({cols}) VALUES ({placeholders})
            ON CONFLICT ({pk}) DO UPDATE
            SET
        """
        ).format(
            table=self._table_identifier(dataset.table_name),
            cols=SQL(",").join([Identifier(k) for k in col_names]),
            pk=Identifier(pk_field),
            placeholders=SQL(",").join([SQL("%s")] * len(col_names)),
        )
        upd_clause = []
        for k in col_names:
            upd_clause.append(SQL("{c}=EXCLUDED.{c}").format(c=Identifier(k)))
        sql_write_feature += SQL(", ").join(upd_clause)

        feat_count = 0
        CHUNK_SIZE = 10000
        for row_dicts in self._chunk(
            dataset.get_features_with_crs_ids(pk_iter, ignore_missing=ignore_missing),
            CHUNK_SIZE,
        ):
            row_tuples = (tuple(row_dict.values()) for row_dict in row_dicts)
            dbcur.executemany(sql_write_feature, row_tuples)
            feat_count += dbcur.rowcount

        return feat_count

    def delete_features(self, db, dataset, pk_iter):
        dbcur = db.cursor()
        pk_field = dataset.primary_key

        sql_del_feature = SQL("DELETE FROM {} WHERE {}=%s;").format(
            self._table_identifier(dataset.table_name), Identifier(pk_field)
        )

        feat_count = 0
        CHUNK_SIZE = 10000
        for rows in self._chunk(zip(pk_iter), CHUNK_SIZE):
            dbcur.executemany(sql_del_feature, rows)
            feat_count += dbcur.rowcount

        return feat_count

    def drop_table(self, target_tree_or_commit, *datasets):
        with self.session() as db:
            dbcur = db.cursor()
            for dataset in datasets:
                table = dataset.table_name

                dbcur.execute(
                    SQL("DROP TABLE IF EXISTS {};").format(
                        self._table_identifier(table)
                    )
                )
                self.delete_meta(dataset)

                dbcur.execute(
                    SQL("DELETE FROM {} WHERE table_name=%s;").format(
                        self.TRACKING_TABLE
                    ),
                    (table,),
                )

    def meta_items(self, dataset):
        with self.session() as db:
            dbcur = db.cursor()
            dbcur.execute(
                SQL("SELECT obj_description(%s::regclass, 'pg_class');"),
                (f"{self.schema}.{dataset.table_name}",),
            )
            title = dbcur.fetchone()[0]
            yield "title", title

            table_info_sql = SQL(
                """
                SELECT
                    C.column_name, C.ordinal_position, C.data_type, C.udt_name,
                    C.character_maximum_length, C.numeric_precision, C.numeric_scale,
                    KCU.ordinal_position AS pk_ordinal_position,
                    upper(postgis_typmod_type(A.atttypmod)) AS geometry_type,
                    postgis_typmod_srid(A.atttypmod) AS geometry_srid
                FROM information_schema.columns C
                LEFT OUTER JOIN information_schema.key_column_usage KCU
                ON (KCU.table_schema = C.table_schema)
                AND (KCU.table_name = C.table_name)
                AND (KCU.column_name = C.column_name)
                LEFT OUTER JOIN pg_attribute A
                ON (A.attname = C.column_name)
                AND (A.attrelid = (C.table_schema || '.' || C.table_name)::regclass::oid)
                WHERE C.table_schema=%s AND C.table_name=%s
                ORDER BY C.ordinal_position;
            """
            )
            dbcur.execute(table_info_sql, (self.schema, dataset.table_name))
            pg_table_info = list(dbcur)

            spatial_ref_sys_sql = SQL(
                """
                SELECT SRS.* FROM public.spatial_ref_sys SRS
                LEFT OUTER JOIN public.geometry_columns GC ON (GC.srid = SRS.srid)
                WHERE GC.f_table_schema=%s AND GC.f_table_name=%s;
            """
            )
            dbcur.execute(spatial_ref_sys_sql, (self.schema, dataset.table_name))
            pg_spatial_ref_sys = list(dbcur)

            id_salt = f"{self.schema} {dataset.table_name} {self.get_db_tree()}"
            schema = postgis_adapter.postgis_to_v2_schema(
                pg_table_info, pg_spatial_ref_sys, id_salt
            )
            yield "schema.json", schema.to_column_dicts()

            for crs_info in pg_spatial_ref_sys:
                wkt = crs_info["srtext"]
                id_str = crs_util.get_identifier_str(wkt)
                yield f"crs/{id_str}.wkt", crs_util.normalise_wkt(wkt)

    # Postgis has nowhere obvious to put this metadata.
    _UNSUPPORTED_META_ITEMS = ("description", "metadata/dataset.json")

    # Postgis approximates an int8 as an int16 - see super()._remove_hidden_meta_diffs
    _APPROXIMATED_TYPES = postgis_adapter.APPROXIMATED_TYPES

    def _remove_hidden_meta_diffs(self, dataset, ds_meta_items, wc_meta_items):
        super()._remove_hidden_meta_diffs(dataset, ds_meta_items, wc_meta_items)

        # Nowhere to put these in postgis WC
        for key in self._UNSUPPORTED_META_ITEMS:
            if key in ds_meta_items:
                del ds_meta_items[key]

        for key in ds_meta_items.keys() & wc_meta_items.keys():
            if not key.startswith("crs/"):
                continue
            old_is_standard = crs_util.has_standard_authority(ds_meta_items[key])
            new_is_standard = crs_util.has_standard_authority(wc_meta_items[key])
            if old_is_standard and new_is_standard:
                # The WC and the dataset have different definitions of a standard eg EPSG:2193.
                # We hide this diff because - hopefully - they are both EPSG:2193 (which never changes)
                # but have unimportant minor differences, and we don't want to update the Postgis builtin version
                # with the dataset version, or update the dataset version from the Postgis builtin.
                del ds_meta_items[key]
                del wc_meta_items[key]
            # If either definition is custom, we keep the diff, since it could be important.

    def _db_geom_to_gpkg_geom(self, g):
        # This is already handled by register_type
        return g

    def _execute_dirty_pks_query(self, db, dataset):
        sql_changed = SQL("SELECT pk FROM {} WHERE table_name=%s;").format(
            self.TRACKING_TABLE
        )
        dbcur = db.cursor()
        dbcur.execute(sql_changed, (dataset.table_name,))
        return dbcur.fetchall()

    def _execute_dirty_rows_query(
        self, db, dataset, feature_filter=None, meta_diff=None
    ):
        feature_filter = feature_filter or UNFILTERED
        table = dataset.table_name
        pk_field = dataset.schema.pk_columns[0].name

        diff_sql = SQL(
            """
            SELECT
                {tracking_table}.pk AS ".__track_pk",
                {table}.*
            FROM {tracking_table} LEFT OUTER JOIN {table}
            ON ({tracking_table}.pk = {table}.{pk_field}::text)
            WHERE ({tracking_table}.table_name = %s)
            """
        ).format(
            tracking_table=self.TRACKING_TABLE,
            table=self._table_identifier(table),
            pk_field=Identifier(pk_field),
        )
        params = [table]

        if feature_filter is not UNFILTERED:
            diff_sql += SQL("\nAND {}.pk IN %s").format(self.TRACKING_TABLE)
            params.append(tuple([str(pk) for pk in feature_filter]))

        dbcur = db.cursor()
        dbcur.execute(diff_sql, params)
        return dbcur.fetchall()

    def reset_tracking_table(self, reset_filter=UNFILTERED):
        reset_filter = reset_filter or UNFILTERED

        with self.session() as db:
            dbcur = db.cursor()
            if reset_filter == UNFILTERED:
                dbcur.execute(SQL("DELETE FROM {};").format(self.TRACKING_TABLE))
                return

            for dataset_path, dataset_filter in reset_filter.items():
                table = dataset_path.strip("/").replace("/", "__")
                if (
                    dataset_filter == UNFILTERED
                    or dataset_filter.get("feature") == UNFILTERED
                ):
                    dbcur.execute(
                        SQL("DELETE FROM {} WHERE table_name=%s;").format(
                            self.TRACKING_TABLE
                        ),
                        (table,),
                    )
                    continue

                CHUNK_SIZE = 100
                pks = dataset_filter.get("feature", ())
                for pk_chunk in self._chunk(pks, CHUNK_SIZE):
                    dbcur.execute(
                        SQL("DELETE FROM {} WHERE table_name=%s AND pk IN %s;").format(
                            self.TRACKING_TABLE
                        ),
                        (table, pk_chunk),
                    )

    def _reset_tracking_table_for_dataset(self, db, dataset):
        dbcur = db.cursor()
        dbcur.execute(
            SQL("DELETE FROM {} WHERE table_name=%s;").format(self.TRACKING_TABLE),
            (dataset.table_name,),
        )
        return dbcur.rowcount

    def _update_state_table_tree_impl(self, db, tree_id):
        dbcur = db.cursor()
        dbcur.execute(
            SQL("UPDATE {} SET value=%s WHERE table_name='*' AND key='tree';").format(
                self.STATE_TABLE
            ),
            (tree_id,),
        )
        return dbcur.rowcount

    def _is_meta_update_supported(self, dataset_version, meta_diff):
        """
        Returns True if the given meta-diff is supported *without* dropping and rewriting the table.
        (Any meta change is supported if we drop and rewrite the table, but of course it is less efficient).
        meta_diff - DeltaDiff object containing the meta changes.
        """
        if not meta_diff:
            return True

        if "schema.json" not in meta_diff:
            return True

        schema_delta = meta_diff["schema.json"]
        if not schema_delta.old_value or not schema_delta.new_value:
            return False

        old_schema = Schema.from_column_dicts(schema_delta.old_value)
        new_schema = Schema.from_column_dicts(schema_delta.new_value)
        dt = old_schema.diff_type_counts(new_schema)

        # We support deletes, name_updates, and type_updates -
        # but we don't support any other type of schema update except by rewriting the entire table.
        dt.pop("deletes")
        dt.pop("name_updates")
        dt.pop("type_updates")
        return sum(dt.values()) == 0

    def _apply_meta_title(self, dataset, src_value, dest_value, db):
        db.cursor().execute(
            SQL("COMMENT ON TABLE {} IS %s").format(
                self._table_identifier(dataset.table_name)
            ),
            (dest_value,),
        )

    def _apply_meta_description(self, dataset, src_value, dest_value, db):
        pass  # This is a no-op for postgis

    def _apply_meta_metadata_dataset_json(self, dataset, src_value, dest_value, db):
        pass  # This is a no-op for postgis

    def _apply_meta_schema_json(self, dataset, src_value, dest_value, db):
        src_schema = Schema.from_column_dicts(src_value)
        dest_schema = Schema.from_column_dicts(dest_value)

        diff_types = src_schema.diff_types(dest_schema)

        deletes = diff_types.pop("deletes")
        name_updates = diff_types.pop("name_updates")
        type_updates = diff_types.pop("type_updates")

        if any(dt for dt in diff_types.values()):
            raise RuntimeError(
                f"This schema change not supported by update - should be drop + rewrite_full: {diff_types}"
            )

        table = dataset.table_name
        for col_id in deletes:
            src_name = src_schema[col_id].name
            db.cursor().execute(
                SQL("ALTER TABLE {} DROP COLUMN IF EXISTS {};").format(
                    self._table_identifier(table), Identifier(src_name)
                )
            )

        for col_id in name_updates:
            src_name = src_schema[col_id].name
            dest_name = dest_schema[col_id].name
            db.cursor().execute(
                SQL("ALTER TABLE {} RENAME COLUMN {} TO {};").format(
                    self._table_identifier(table),
                    Identifier(src_name),
                    Identifier(dest_name),
                ),
            )

        do_write_crs = False
        for col_id in type_updates:
            col = dest_schema[col_id]
            dest_type = SQL(postgis_adapter.v2_type_to_pg_type(col, dataset))

            if col.data_type == "geometry":
                crs_name = col.extra_type_info.get("geometryCRS")
                if crs_name is not None:
                    crs_id = crs_util.get_identifier_int_from_dataset(dataset, crs_name)
                    if crs_id is not None:
                        dest_type = SQL("{} USING ST_SetSRID({}::GEOMETRY, {})").format(
                            dest_type, Identifier(col.name), SQL(str(crs_id))
                        )
                        do_write_crs = True

            db.cursor().execute(
                SQL("ALTER TABLE {} ALTER COLUMN {} TYPE {};").format(
                    self._table_identifier(table), Identifier(col.name), dest_type
                )
            )

        if do_write_crs:
            self.write_meta_crs(dataset)
