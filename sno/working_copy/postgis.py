import contextlib
import logging
import re
import time
from urllib.parse import urlsplit, urlunsplit

import click
import pygit2
import sqlalchemy
from sqlalchemy.orm import sessionmaker
from sqlalchemy.types import Text
import psycopg2


from .base import WorkingCopy
from . import postgis_adapter
from sno import crs_util
from sno.schema import Schema
from sno.sqlalchemy import postgis_engine, insert_command
from sqlalchemy.dialects.postgresql import insert


"""
* database needs to exist
* database needs to have postgis enabled
* database user needs to be able to:
    1. create 'sno' schema & tables
    2. create & alter tables in the default (or specified) schema
    3. create triggers
"""

L = logging.getLogger("sno.working_copy.postgis")


def insert_or_replace_command(table_name, col_names, schema=None, pk_col_names=None):
    stmt = insert(
        sqlalchemy.table(
            table_name,
            *[sqlalchemy.column(c) for c in col_names],
            schema=schema,
        )
    )
    update_dict = {c.name: c for c in stmt.excluded if c.name not in pk_col_names}
    return stmt.on_conflict_do_update(index_elements=pk_col_names, set_=update_dict)


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
        self.engine = postgis_engine(self.dburl)
        self.sessionmaker = sessionmaker(bind=self.engine)

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
        # TODO - use sqlalchemy not strings
        return f'"{self.schema}"."{table}"'

    @contextlib.contextmanager
    def session(self, bulk=0):
        """
        Context manager for GeoPackage DB sessions, yields a connection object inside a transaction

        Calling again yields the _same_ connection, the transaction/etc only happen in the outer one.
        """
        L = logging.getLogger(f"{self.__class__.__qualname__}.session")

        if hasattr(self, "_session"):
            # inner - reuse
            L.debug(f"session: existing...")
            yield self._session
            L.debug(f"session: existing/done")

        else:
            L.debug(f"session: new...")

            try:
                # TODO - use "with" for sqlalchemy transaction.
                self._session = self.sessionmaker()
                self._configure_output(self._session)
                # self._register_geometry_type(self._session)

                self._session.execute("BEGIN TRANSACTION;")
                yield self._session
                self._session.commit()
            except Exception:
                self._session.rollback()
                raise
            finally:
                self._session.close()
                del self._session
                L.debug(f"session(bulk={bulk}): new/done")

    def _configure_output(self, db):
        """Output timestamptz in UTC, and output interval as ISO 8601 duration."""

    def _register_geometry_type(self, db):
        # TODO
        """
        Register adapt_geometry_from_db for the type with OID: 'geometry'::regtype::oid
        - which could be different in different postgis databases, and might not even exist.
        """

    def is_created(self):
        """
        Returns true if the postgres schema referred to by this working copy exists and
        contains at least one table. If it exists but is empty, it is treated as uncreated.
        This is so the postgres schema can be created ahead of time before a repo is created
        or configured, without it triggering code that checks for corrupted working copies.
        Note that it might not be initialised as a working copy - see self.is_initialised.
        """
        with self.session() as db:
            r = db.execute(
                """
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_schema=:schema;
                """,
                {"schema": self.schema},
            )
            return r.scalar() > 0

    def is_initialised(self):
        """
        Returns true if the postgis working copy is initialised -
        the schema exists and has the necessary sno tables, _sno_state and _sno_track.
        """
        with self.session() as db:
            r = db.execute(
                """
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_schema=:schema AND table_name IN ('_sno_state', '_sno_track');
                """,
                {"schema": self.schema},
            )
            return r.scalar() == 2

    def has_data(self):
        """
        Returns true if the postgis working copy seems to have user-created content already.
        """
        with self.session() as db:
            r = db.execute(
                """
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_schema=:schema AND table_name NOT IN ('_sno_state', '_sno_track');
                """,
                {"schema": self.schema},
            )
            return r.scalar() > 0

    def create_and_initialise(self):
        with self.session() as db:
            db.execute(f"""CREATE SCHEMA IF NOT EXISTS "{self.schema}";""")

            metadata = sqlalchemy.MetaData(schema=self.schema)
            sqlalchemy.schema.Table(
                "_sno_state",
                metadata,
                sqlalchemy.schema.Column(
                    "table_name", Text, nullable=False, primary_key=True
                ),
                sqlalchemy.schema.Column("key", Text, nullable=False, primary_key=True),
                sqlalchemy.schema.Column("value", Text, nullable=True),
                schema=self.schema,
            )
            sqlalchemy.schema.Table(
                "_sno_track",
                metadata,
                sqlalchemy.schema.Column(
                    "table_name", Text, nullable=False, primary_key=True
                ),
                sqlalchemy.schema.Column("pk", Text, nullable=True, primary_key=True),
                schema=self.schema,
            )
            metadata.create_all(db.connection())

            db.execute(
                f"""
                CREATE OR REPLACE FUNCTION "{self.schema}"."_sno_track_trigger"() RETURNS TRIGGER AS $body$
                DECLARE
                    pk_field text := quote_ident(TG_ARGV[0]);
                    pk_old text;
                    pk_new text;
                BEGIN
                    IF (TG_OP = 'INSERT' OR TG_OP = 'UPDATE') THEN
                        EXECUTE 'SELECT $1.' || pk_field USING NEW INTO pk_new;

                        INSERT INTO {self.TRACKING_TABLE} (table_name,pk) VALUES
                        (TG_TABLE_NAME::TEXT, pk_new)
                        ON CONFLICT DO NOTHING;
                    END IF;
                    IF (TG_OP = 'UPDATE' OR TG_OP = 'DELETE') THEN
                        EXECUTE 'SELECT $1.' || pk_field USING OLD INTO pk_old;

                        INSERT INTO {self.TRACKING_TABLE} (table_name,pk) VALUES
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
            )

    def delete(self, keep_container_if_possible=False):
        """ Delete all tables in the schema"""
        # We don't use drop ... cascade since that could also delete things outside the schema.
        # Better to fail to delete the schema, than to delete things the user didn't want to delete.
        with self.session() as db:
            # Don't worry about constraints when dropping everything.
            db.execute("SET CONSTRAINTS ALL DEFERRED;")
            # Drop tables
            r = db.execute(
                "SELECT tablename FROM pg_tables where schemaname=:schema;",
                {"schema": self.schema},
            )
            tables = [t[0] for t in r]
            if tables:
                table_identifiers = ", ".join(
                    (self._table_identifier(t) for t in tables)
                )
                db.execute(f"DROP TABLE IF EXISTS {table_identifiers};")

            # Drop functions
            r = db.execute(
                "SELECT proname from pg_proc WHERE pronamespace = (:schema)::regnamespace;",
                {"schema": self.schema},
            )
            functions = [f[0] for f in r]
            if functions:
                function_identifiers = ", ".join(
                    (self._table_identifier(f) for f in functions)
                )
                db.execute(f"DROP FUNCTION IF EXISTS {function_identifiers};")

            # Drop schema, unless keep_container_if_possible=True
            if not keep_container_if_possible:
                db.execute(f"""DROP SCHEMA IF EXISTS "{self.schema}";""")

    def write_meta(self, dataset):
        with self.session():
            self.write_meta_title(dataset)
            self.write_meta_crs(dataset)

    def write_meta_title(self, dataset):
        """Write the dataset title as a comment on the table."""
        with self.session() as db:
            db.execute(
                f"""COMMENT ON TABLE "{self.schema}"."{dataset.table_name}" IS :comment""",
                {"comment": dataset.get_meta_item("title")},
            )

    def write_meta_crs(self, dataset):
        """Populate the public.spatial_ref_sys table with data from this dataset."""
        spatial_refs = postgis_adapter.generate_postgis_spatial_ref_sys(dataset)
        if not spatial_refs:
            return

        with self.session() as db:
            # We do not automatically overwrite a CRS if it seems likely to be one
            # of the Postgis builtin definitions - Postgis has lots of EPSG and ESRI
            # definitions built-in, plus the 900913 (GOOGLE) definition.
            # See POSTGIS_WC.md for help on working with CRS definitions in a Postgis WC.
            db.execute(
                """
                INSERT INTO public.spatial_ref_sys AS SRS (srid, auth_name, auth_srid, srtext, proj4text)
                VALUES (:srid, :auth_name, :auth_srid, :srtext, :proj4text)
                ON CONFLICT (srid) DO UPDATE
                    SET (auth_name, auth_srid, srtext, proj4text)
                    = (EXCLUDED.auth_name, EXCLUDED.auth_srid, EXCLUDED.srtext, EXCLUDED.proj4text)
                    WHERE SRS.auth_name NOT IN ('EPSG', 'ESRI') AND SRS.srid <> 900913;
                """,
                spatial_refs,
            )

    def delete_meta(self, dataset):
        """Delete any metadata that is only needed by this dataset."""
        pass  # There is no metadata except for the spatial_ref_sys table.

    def _create_spatial_index(self, dataset):
        L = logging.getLogger(f"{self.__class__.__qualname__}._create_spatial_index")

        geom_col = dataset.geom_column_name

        # Create the PostGIS Spatial Index
        L.debug("Creating spatial index for %s.%s", dataset.table_name, geom_col)
        t0 = time.monotonic()
        with self.session() as db:
            db.execute(
                f"""
                CREATE INDEX "{dataset.table_name}_idx_{geom_col}"
                ON "{self.schema}"."{dataset.table_name}" USING GIST ("{geom_col}");
                """
            )
        L.info("Created spatial index in %ss", time.monotonic() - t0)

    def _create_triggers(self, db, dataset):
        db.execute(
            f"""
            CREATE TRIGGER "sno_track" AFTER INSERT OR UPDATE OR DELETE ON "{self.schema}"."{dataset.table_name}"
            FOR EACH ROW EXECUTE PROCEDURE "{self.schema}"."_sno_track_trigger"(:pk_field)
            """,
            {"pk_field": dataset.primary_key},
        )

    @contextlib.contextmanager
    def _suspend_triggers(self, db, dataset):
        db.execute(
            f"""
            ALTER TABLE "{self.schema}"."{dataset.table_name}"
            DISABLE TRIGGER "sno_track";
            """
        )
        yield
        db.execute(
            f"""
            ALTER TABLE "{self.schema}"."{dataset.table_name}"
            ENABLE TRIGGER "sno_track";
            """
        )

    def get_db_tree(self, table_name="*"):
        with self.session() as db:
            try:
                r = db.execute(
                    f"""
                    SELECT value FROM {self.STATE_TABLE} WHERE table_name=:table_name AND key='tree';
                    """,
                    {"table_name": table_name},
                )
                return r.scalar()
            except psycopg2.errors.UndefinedTable:
                # It's okay to not have anything in the _sno_state table - it might just mean there are no commits yet.
                # It might also mean that the working copy is not yet initialised - see WorkingCopy.get
                return None

    def write_full(self, commit, *datasets, **kwargs):
        """
        Writes a full layer into a working-copy table

        Use for new working-copy checkouts.
        """
        L = logging.getLogger(f"{self.__class__.__qualname__}.write_full")

        with self.session(bulk=2) as db:

            db.execute(f"""CREATE SCHEMA IF NOT EXISTS "{self.schema}";""")

            for dataset in datasets:
                table = dataset.table_name

                # Create the table
                table_spec = postgis_adapter.v2_schema_to_postgis_spec(
                    dataset.schema, dataset
                )

                db.execute(
                    f"""CREATE TABLE IF NOT EXISTS "{self.schema}"."{table}" ({table_spec});"""
                )
                self.write_meta(dataset)

                L.info("Creating features...")
                sql = insert_command(
                    dataset.table_name, dataset.schema.column_names, schema=self.schema
                )
                feat_progress = 0
                t0 = time.monotonic()
                t0p = t0

                CHUNK_SIZE = 10000
                total_features = dataset.feature_count

                for row_dicts in self._chunk(
                    dataset.features_with_crs_ids(), CHUNK_SIZE
                ):
                    db.execute(sql, row_dicts)
                    feat_progress += len(row_dicts)

                    t0a = time.monotonic()
                    L.info(
                        "%.1f%% %d/%d features... @%.1fs (+%.1fs, ~%d F/s)",
                        feat_progress / total_features * 100,
                        feat_progress,
                        total_features,
                        t0a - t0,
                        t0a - t0p,
                        CHUNK_SIZE / (t0a - t0p or 0.001),
                    )
                    t0p = t0a

                t1 = time.monotonic()
                L.info(
                    "Added %d features to postgresql in %.1fs", feat_progress, t1 - t0
                )
                L.info(
                    "Overall rate: %d features/s", (feat_progress / (t1 - t0 or 0.001))
                )

                if dataset.has_geometry:
                    self._create_spatial_index(dataset)

                # Create triggers
                self._create_triggers(db, dataset)

            db.execute(
                f"""
                INSERT INTO {self.STATE_TABLE} (table_name, key, value) VALUES (:table_name, :key, :value)
                ON CONFLICT (table_name, key) DO UPDATE
                SET value=EXCLUDED.value;
                """,
                {
                    "table_name": "*",
                    "key": "tree",
                    "value": commit.peel(pygit2.Tree).hex,
                },
            )

    def write_features(self, db, dataset, pk_list, *, ignore_missing=False):
        pk_col_names = [c.name for c in dataset.schema.pk_columns]
        sql = insert_or_replace_command(
            dataset.table_name,
            dataset.schema.column_names,
            schema=self.schema,
            pk_col_names=pk_col_names,
        )
        feat_count = 0
        CHUNK_SIZE = 10000
        for row_dicts in self._chunk(
            dataset.get_features_with_crs_ids(pk_list, ignore_missing=ignore_missing),
            CHUNK_SIZE,
        ):
            r = db.execute(sql, row_dicts)
            feat_count += r.rowcount

        return feat_count

    def delete_features(self, db, dataset, pk_iter):
        pk_field = dataset.primary_key

        sql_del_feature = f"""DELETE FROM {self._table_identifier(dataset.table_name)} WHERE "{pk_field}"=:pk;"""

        feat_count = 0
        CHUNK_SIZE = 10000
        for pks in self._chunk(zip(pk_iter), CHUNK_SIZE):
            r = db.execute(sql_del_feature, [{"pk": pk} for pk in pks])
            feat_count += r.rowcount

        return feat_count

    def drop_table(self, target_tree_or_commit, *datasets):
        with self.session() as db:
            for dataset in datasets:
                table = dataset.table_name

                db.execute(f"DROP TABLE IF EXISTS {self._table_identifier(table)};")
                self.delete_meta(dataset)

                db.execute(
                    f"DELETE FROM {self.TRACKING_TABLE} WHERE table_name=:table;",
                    {"table": table},
                )

    def meta_items(self, dataset):
        with self.session() as db:
            r = db.execute(
                "SELECT obj_description((:table_identifier)::regclass, 'pg_class');",
                {"table_identifier": f"{self.schema}.{dataset.table_name}"},
            )
            title = r.scalar()
            yield "title", title

            table_info_sql = """
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
                WHERE C.table_schema=:table_schema AND C.table_name=:table_name
                ORDER BY C.ordinal_position;
            """
            r = db.execute(
                table_info_sql,
                {"table_schema": self.schema, "table_name": dataset.table_name},
            )
            pg_table_info = list(r)

            spatial_ref_sys_sql = """
                SELECT SRS.* FROM public.spatial_ref_sys SRS
                LEFT OUTER JOIN public.geometry_columns GC ON (GC.srid = SRS.srid)
                WHERE GC.f_table_schema=:table_schema AND GC.f_table_name=:table_name;
            """
            r = db.execute(
                spatial_ref_sys_sql,
                {"table_schema": self.schema, "table_name": dataset.table_name},
            )
            pg_spatial_ref_sys = list(r)

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
            f"COMMENT ON TABLE {self._table_identifier(dataset.table_name)} IS :comment",
            {"comment": dest_value},
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
            db.execute(
                f"""ALTER TABLE {self._table_identifier(table)} DROP COLUMN IF EXISTS "{src_name}";"""
            )

        for col_id in name_updates:
            src_name = src_schema[col_id].name
            dest_name = dest_schema[col_id].name
            db.execute(
                f"""ALTER TABLE {self._table_identifier(table)} RENAME COLUMN "{src_name}" TO "{dest_name}";"""
            )

        do_write_crs = False
        for col_id in type_updates:
            col = dest_schema[col_id]
            dest_type = postgis_adapter.v2_type_to_pg_type(col, dataset)

            if col.data_type == "geometry":
                crs_name = col.extra_type_info.get("geometryCRS")
                if crs_name is not None:
                    crs_id = crs_util.get_identifier_int_from_dataset(dataset, crs_name)
                    if crs_id is not None:
                        dest_type += (
                            f""" USING ST_SetSRID("{col.name}"::GEOMETRY, {crs_id})"""
                        )
                        do_write_crs = True

            db.execute(
                f"""ALTER TABLE {self._table_identifier(table)} ALTER COLUMN "col.name" TYPE {dest_type};"""
            )

        if do_write_crs:
            self.write_meta_crs(dataset)
