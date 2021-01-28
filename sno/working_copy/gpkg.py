import contextlib
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import click
import pygit2
from sqlalchemy.orm import sessionmaker
from osgeo import gdal


from .base import WorkingCopy
from sno import gpkg, gpkg_adapter
from sno.filter_util import UNFILTERED
from sno.geometry import normalise_gpkg_geom
from sno.schema import Schema
from sno.sqlalchemy import gpkg_engine, insert_command


L = logging.getLogger("sno.working_copy.gpkg")


def insert_or_replace_command(table_name, col_names):
    return insert_command(table_name, col_names).prefix_with("OR REPLACE")


class WorkingCopy_GPKG(WorkingCopy):
    # Using this prefix means OGR/QGIS doesn't list these tables as datasets
    SNO_TABLE_PREFIX = "gpkg_sno_"

    def __init__(self, repo, path):
        self.repo = repo
        self.path = path
        self.engine = gpkg_engine(self.full_path)
        self.sessionmaker = sessionmaker(bind=self.engine)

    @classmethod
    def check_valid_path(cls, path):
        if not str(path).endswith(".gpkg"):
            suggested_path = f"{os.path.splitext(str(path))[0]}.gpkg"
            raise click.UsageError(
                f"Invalid GPKG path - expected .gpkg suffix, eg {suggested_path}"
            )

    @property
    def full_path(self):
        """ Return a full absolute path to the working copy """
        return (self.repo.workdir_path / self.path).resolve()

    def _sno_table(self, name, suffix=""):
        n = f"{self.SNO_TABLE_PREFIX}{name}"
        if suffix:
            n += "_" + suffix
        return n

    @contextlib.contextmanager
    def session(self, bulk=0):
        """
        Context manager for GeoPackage DB sessions, yields a connection object inside a transaction

        Calling again yields the _same_ connection, the transaction/etc only happen in the outer one.

        @bulk controls bulk-loading operating mode:
            0: default, no bulk operations (normal)
            1: synchronous, larger cache (bulk changes)
            2: exclusive locking, memory journal (bulk load)
        """
        L = logging.getLogger(f"{self.__class__.__qualname__}.session")

        # TODO - look into bulk, locking_mode, journal_mode, synchronous, cache_size

        if hasattr(self, "_session"):
            # inner - reuse
            L.debug(f"session(bulk={bulk}): existing...")
            yield self._session
            L.debug(f"session(bulk={bulk}): existing/done")

        else:
            L.debug(f"session(bulk={bulk}): new...")

            try:
                self._session = self.sessionmaker()

                if bulk:
                    self._session.execute("PRAGMA synchronous = OFF;")
                    self._session.execute(
                        "PRAGMA cache_size = -1048576;"
                    )  # -KiB => 1GiB
                if bulk >= 2:
                    self._session.execute("PRAGMA journal_mode = MEMORY;")
                    self._session.execute("PRAGMA locking_mode = EXCLUSIVE;")

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

    def delete(self, keep_container_if_possible=False):
        """ Delete the working copy files """
        self.full_path.unlink()

        # for sqlite this might include wal/journal/etc files
        # app.gpkg -> app.gpkg-wal, app.gpkg-journal
        # https://www.sqlite.org/shortnames.html
        for f in Path(self.full_path).parent.glob(Path(self.path).name + "-*"):
            f.unlink()

    def is_created(self):
        """
        Returns true if the GPKG file referred to by this working copy exists.
        Note that it might not be initialised as a working copy - see self.is_initialised.
        """
        return self.full_path.is_file()

    def is_initialised(self):
        """
        Returns true if the GPKG working copy is initialised -
        the GPKG file exists and has the necessary gpkg_sno tables - state and tracking.
        """
        if not self.is_created():
            return False
        with self.session() as db:
            r = db.execute(
                f"""
                SELECT count(*) FROM sqlite_master
                WHERE type='table' AND name IN ('{self.STATE_TABLE}', '{self.TRACKING_TABLE}');
                """
            )
            return r.scalar() == 2

    def has_data(self):
        """
        Returns true if the GPKG working copy seems to have user-created content already.
        """
        if not self.is_created():
            return False
        with self.session() as db:
            r = db.execute(
                f"""
                SELECT count(*) FROM sqlite_master
                WHERE type='table'
                    AND name NOT IN ('{self.STATE_TABLE}', '{self.TRACKING_TABLE}')
                    AND NAME NOT LIKE 'gpkg%';
                """
            )
            return r.scalar() > 0

    def create_and_initialise(self):
        # GDAL: Create GeoPackage
        # GDAL: Add metadata/etc
        gdal_driver = gdal.GetDriverByName("GPKG")
        gdal_ds = gdal_driver.Create(str(self.full_path), 0, 0, 0, gdal.GDT_Unknown)
        del gdal_ds

        with self.session() as db:
            # Remove placeholder stuff GDAL creates
            db.execute(
                "DELETE FROM gpkg_geometry_columns WHERE table_name='ogr_empty_table';"
            )
            db.execute("DELETE FROM gpkg_contents WHERE table_name='ogr_empty_table';")
            db.execute("DROP TABLE IF EXISTS ogr_empty_table;")

            # Create metadata tables
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS gpkg_metadata (
                    id INTEGER CONSTRAINT m_pk PRIMARY KEY ASC NOT NULL,
                    md_scope TEXT NOT NULL DEFAULT 'dataset',
                    md_standard_uri TEXT NOT NULL,
                    mime_type TEXT NOT NULL DEFAULT 'text/xml',
                    metadata TEXT NOT NULL DEFAULT ''
                );
            """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS gpkg_metadata_reference (
                    reference_scope TEXT NOT NULL,
                    table_name TEXT,
                    column_name TEXT,
                    row_id_value INTEGER,
                    timestamp DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    md_file_id INTEGER NOT NULL,
                    md_parent_id INTEGER,
                    CONSTRAINT crmr_mfi_fk FOREIGN KEY (md_file_id) REFERENCES gpkg_metadata(id),
                    CONSTRAINT crmr_mpi_fk FOREIGN KEY (md_parent_id) REFERENCES gpkg_metadata(id)
                );
            """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS gpkg_extensions (
                    table_name TEXT,
                    column_name TEXT,
                    extension_name TEXT NOT NULL,
                    definition TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    CONSTRAINT ge_tce UNIQUE (table_name, column_name, extension_name)
                );
                """
            )

            db.execute(
                f"""
                CREATE TABLE {self.STATE_TABLE} (
                    table_name TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NULL,
                    CONSTRAINT {self._sno_table(self.STATE_NAME, 'pk')} PRIMARY KEY (table_name, key)
                );
            """
            )

            db.execute(
                f"""
                CREATE TABLE {self.TRACKING_TABLE} (
                    table_name TEXT NOT NULL,
                    pk TEXT NULL,
                    CONSTRAINT {self._sno_table(self.TRACKING_NAME, 'pk')} PRIMARY KEY (table_name, pk)
                );
            """
            )

    def write_meta(self, dataset):
        """
        Populate the following tables with data from this dataset:
        gpkg_contents, gpkg_geometry_columns, gpkg_spatial_ref_sys, gpkg_metadata, gpkg_metadata_reference
        """
        table_name = dataset.table_name
        gpkg_contents = dataset.get_gpkg_meta_item("gpkg_contents")
        gpkg_contents["table_name"] = table_name

        # FIXME: find a better way to roundtrip identifiers
        identifier_prefix = f"{dataset.table_name}: "
        if not gpkg_contents["identifier"].startswith(identifier_prefix):
            gpkg_contents["identifier"] = (
                identifier_prefix + gpkg_contents["identifier"]
            )

        gpkg_geometry_columns = dataset.get_gpkg_meta_item("gpkg_geometry_columns")
        gpkg_spatial_ref_sys = dataset.get_gpkg_meta_item("gpkg_spatial_ref_sys")

        with self.session() as db:
            # Update GeoPackage core tables
            if gpkg_spatial_ref_sys:
                gsrs = gpkg_spatial_ref_sys
                db.execute(
                    insert_or_replace_command("gpkg_spatial_ref_sys", gsrs[0].keys()),
                    gpkg_spatial_ref_sys,
                )

            # our repo copy doesn't include all fields from gpkg_contents
            # but the default value for last_change (now), and NULL for {min_x,max_x,min_y,max_y}
            # should deal with the remaining fields
            db.execute(
                insert_command("gpkg_contents", gpkg_contents.keys()), gpkg_contents
            )

            if gpkg_geometry_columns:
                ggc = gpkg_geometry_columns
                db.execute(insert_command("gpkg_geometry_columns", ggc.keys()), ggc)

            gpkg_metadata = dataset.get_gpkg_meta_item("gpkg_metadata")
            gpkg_metadata_reference = dataset.get_gpkg_meta_item(
                "gpkg_metadata_reference"
            )
            if gpkg_metadata and gpkg_metadata_reference:
                self._write_meta_metadata(
                    table_name, gpkg_metadata, gpkg_metadata_reference, db
                )

    def _write_meta_metadata(
        self, table_name, gpkg_metadata, gpkg_metadata_reference, db
    ):
        """Populate gpkg_metadata and gpkg_metadata_reference tables."""
        # gpkg_metadata_reference.md_file_id is a foreign key -> gpkg_metadata.id,
        # have to make sure these IDs still match once we insert.
        metadata_id_map = {}
        for row in gpkg_metadata:
            params = dict(row.items())
            params.pop("id")

            r = db.execute(insert_command("gpkg_metadata", params.keys()), params)
            metadata_id_map[row["id"]] = r.lastrowid

        for row in gpkg_metadata_reference:
            params = dict(row.items())
            params["md_file_id"] = metadata_id_map[row["md_file_id"]]
            params["md_parent_id"] = metadata_id_map.get(row["md_parent_id"], None)
            params["table_name"] = table_name

            r = db.execute(
                insert_command("gpkg_metadata_reference", params.keys()), params
            )

    def meta_items(self, dataset):
        """
        Extract all the metadata of this GPKG and convert to dataset V2 format.
        Note that the extracted schema will not be aligned to any existing schema
        - the generated column IDs are stable, but do not necessarily match the ones in the dataset.
        Calling Schema.align_* is required to find how the columns matches the existing schema.
        """
        with self.session() as db:
            gpkg_meta_items_obj = gpkg.get_gpkg_meta_items_obj(db, dataset.table_name)

        gpkg_name = os.path.basename(self.path)

        # Column IDs are generated deterministically from the column contents and the current state.
        # That way, they don't vary at random if the same command is run twice in a row, but
        # they will vary as the repo state changes so that we don't accidentally generate the same ID twice
        # for two unrelated columns.
        id_salt = f"{gpkg_name} {dataset.table_name} {self.get_db_tree()}"

        yield from gpkg_adapter.all_v2_meta_items(gpkg_meta_items_obj, id_salt=id_salt)

    # Some types are approximated as text in GPKG - see super()._remove_hidden_meta_diffs
    _APPROXIMATED_TYPES = gpkg_adapter.APPROXIMATED_TYPES

    def _remove_hidden_meta_diffs(self, dataset, ds_meta_items, wc_meta_items):
        # Fix up anything we may have done to the primary key before calling super()
        if (
            dataset.has_geometry
            and "schema.json" in ds_meta_items
            and "schema.json" in wc_meta_items
        ):
            self._restore_approximated_primary_key(
                ds_meta_items["schema.json"], wc_meta_items["schema.json"]
            )

        super()._remove_hidden_meta_diffs(dataset, ds_meta_items, wc_meta_items)

    def _restore_approximated_primary_key(self, ds_schema, wc_schema):
        """
        GPKG requires that there is a primary key of type INTEGER (int64) in geospatial tables.
        If the dataset had a primary key of another type, then this will have been approximated.
        If the PK remains this same type when roundtripped, we remove this diff and treat it as the same.
        """
        ds_pk = self._find_pk(ds_schema)
        wc_pk = self._find_pk(wc_schema)
        if not ds_pk or not wc_pk:
            return

        if wc_pk["dataType"] != "integer":
            # This isn't a compliant GPKG that we would create, maybe the user edited it.
            # Keep the diff as it is.
            return

        if ds_pk["dataType"] == "integer":
            # Dataset PK type of int8, int16, int32 was approximated as int64.
            # Restore it to its original size
            if wc_pk.get("size") != ds_pk.get("size"):
                wc_pk["size"] = ds_pk.get("size")
        else:
            # Dataset PK has non-integer PK type, which would be approximated by demoting it to a non-PK
            # and adding another column of type INTEGER PK that is not found in the dataset.
            # If the working copy still has this structure, restore the original PK as a PK.
            demoted_pk = self._find_by_name(wc_schema, ds_pk["name"])
            if demoted_pk and demoted_pk["dataType"] == ds_pk["dataType"]:
                # Remove auto-generated PK column
                wc_schema.remove(wc_pk)
                # Restore demoted-PK as PK again
                demoted_pk["primaryKeyIndex"] = 0

    def _find_pk(self, schema_cols):
        return next((c for c in schema_cols if c.get("primaryKeyIndex") == 0), None)

    def _find_by_name(self, schema_cols, name):
        return next((c for c in schema_cols if c["name"] == name), None)

    def delete_meta(self, dataset):
        table_name = dataset.table_name
        with self.session() as db:
            self._delete_meta_metadata(table_name, db)
            # FOREIGN KEY constraints are still active, so we delete in a particular order:
            db.execute(
                """DELETE FROM gpkg_geometry_columns WHERE table_name = :table_name;""",
                {"table_name": dataset.table_name},
            )
            db.execute(
                """DELETE FROM gpkg_contents WHERE table_name = :table_name;""",
                {"table_name": dataset.table_name},
            )

    def _delete_meta_metadata(self, table_name, db):
        r = db.execute(
            """SELECT md_file_id FROM gpkg_metadata_reference WHERE table_name = :table_name;""",
            {"table_name": table_name},
        )
        ids = [row[0] for row in r]
        db.execute(
            """DELETE FROM gpkg_metadata_reference WHERE table_name = :table_name;""",
            {"table_name": table_name},
        )
        if ids:
            db.execute(
                """DELETE FROM gpkg_metadata WHERE id = :id;""",
                [{"id": i} for i in ids],
            )

    def _create_spatial_index(self, db, dataset):
        L = logging.getLogger(f"{self.__class__.__qualname__}._create_spatial_index")
        geom_col = dataset.geom_column_name

        # Create the GeoPackage Spatial Index
        t0 = time.monotonic()
        L.debug("Creating spatial index for %s.%s", dataset.table_name, geom_col)

        db.execute(
            "SELECT gpkgAddSpatialIndex(:table, :geom);",
            {"table": dataset.table_name, "geom": geom_col},
        )

        L.info("Created spatial index in %ss", time.monotonic() - t0)

    def _drop_spatial_index(self, dbcur, dataset):
        L = logging.getLogger(f"{self.__class__.__qualname__}._drop_spatial_index")
        geom_col = dataset.geom_column_name

        # Delete the GeoPackage Spatial Index
        t0 = time.monotonic()
        L.debug("Dropping spatial index for %s.%s", dataset.table_name, geom_col)

        rtree_table = f"rtree_{dataset.table_name}_{geom_col}"
        dbcur.execute(f"DROP TABLE {gpkg.ident(rtree_table)};")
        dbcur.execute(
            f"DELETE FROM gpkg_extensions WHERE (table_name, column_name, extension_name) = (:table_name, :column_name, 'gpkg_rtree_index')",
            {"table_name": dataset.table_name, "column_name": geom_col},
        )

        L.info("Dropped spatial index in %ss", time.monotonic() - t0)

    def _drop_triggers(self, dbcur, dataset):
        table = dataset.table_name
        dbcur.execute(f"DROP TRIGGER {self._sno_table(table, 'ins')}")
        dbcur.execute(f"DROP TRIGGER {self._sno_table(table, 'upd')}")
        dbcur.execute(f"DROP TRIGGER {self._sno_table(table, 'del')}")

    @contextlib.contextmanager
    def _suspend_triggers(self, dbcur, dataset):
        self._drop_triggers(dbcur, dataset)
        try:
            yield
        finally:
            self._create_triggers(dbcur, dataset)

    def update_gpkg_contents(self, dataset, change_time):
        table_name = dataset.table_name

        with self.session() as db:
            if dataset.has_geometry:
                geom_col = dataset.geom_column_name
                sql = f"""
                    UPDATE gpkg_contents
                    SET
                        min_x=(SELECT ST_MinX(Extent({gpkg.ident(geom_col)})) FROM {gpkg.ident(table_name)}),
                        min_y=(SELECT ST_MinY(Extent({gpkg.ident(geom_col)})) FROM {gpkg.ident(table_name)}),
                        max_x=(SELECT ST_MaxX(Extent({gpkg.ident(geom_col)})) FROM {gpkg.ident(table_name)}),
                        max_y=(SELECT ST_MaxY(Extent({gpkg.ident(geom_col)})) FROM {gpkg.ident(table_name)}),
                        last_change=:last_change
                    WHERE
                        table_name=:table_name
                """
            else:
                sql = """
                    UPDATE gpkg_contents
                    SET min_x=NULL, min_y=NULL, max_x=NULL, max_y=NULL,
                        last_change=:last_change
                    WHERE
                        table_name=:table_name
                """

            rc = db.execute(
                sql,
                {
                    "last_change": change_time.strftime(
                        "%Y-%m-%dT%H:%M:%S.%fZ"
                    ),  # GPKG Spec Req.15
                    "table_name": table_name,
                },
            ).rowcount
            assert rc == 1, f"gpkg_contents update: expected 1Δ, got {rc}"

    def get_db_tree(self, table_name="*"):
        with self.session() as db:
            r = db.execute(
                f"""
                    SELECT value
                    FROM {self.STATE_TABLE}
                    WHERE table_name=:table_name AND key='tree';
                """,
                {"table_name": table_name},
            )
            row = r.fetchone()
            if not row:
                # It's okay to not have anything in the tree table - it might just mean there are no commits yet.
                # It might also mean that the working copy is not yet initialised - see WorkingCopy.get
                return None

            wc_tree_id = row[0]
            return wc_tree_id

    def _create_triggers(self, db, dataset):
        table = dataset.table_name
        pkf = gpkg.ident(dataset.primary_key)
        ts = gpkg.param_str(table)

        # sqlite doesn't let you do param substitutions in CREATE TRIGGER
        db.execute(
            f"""
            CREATE TRIGGER {self._sno_table(table, 'ins')}
               AFTER INSERT
               ON {gpkg.ident(table)}
            BEGIN
                INSERT OR REPLACE INTO {self.TRACKING_TABLE}
                    (table_name, pk)
                VALUES ({ts}, NEW.{pkf});
            END;
        """
        )
        db.execute(
            f"""
            CREATE TRIGGER {self._sno_table(table, 'upd')}
               AFTER UPDATE
               ON {gpkg.ident(table)}
            BEGIN
                INSERT OR REPLACE INTO {self.TRACKING_TABLE}
                    (table_name, pk)
                VALUES
                    ({ts}, NEW.{pkf}),
                    ({ts}, OLD.{pkf});
            END;
        """
        )
        db.execute(
            f"""
            CREATE TRIGGER {self._sno_table(table, 'del')}
               AFTER DELETE
               ON {gpkg.ident(table)}
            BEGIN
                INSERT OR REPLACE INTO {self.TRACKING_TABLE}
                    (table_name, pk)
                VALUES
                    ({ts}, OLD.{pkf});
            END;
        """
        )

    def _db_geom_to_gpkg_geom(self, g):
        # Its possible in GPKG to put arbitrary values in columns, regardless of type.
        # We don't try to convert them here - we let the commit validation step report this as an error.
        if not isinstance(g, bytes):
            return g
        # We normalise geometries to avoid spurious diffs - diffs where nothing
        # of any consequence has changed (eg, only endianness has changed).
        # This includes setting the SRID to zero for each geometry so that we don't store a separate SRID per geometry,
        # but only one per column at most.
        return normalise_gpkg_geom(g)

    def write_full(self, target_tree_or_commit, *datasets, safe=True):
        """
        Writes a full layer into a working-copy table

        Use for new working-copy checkouts.
        """
        commit = (
            target_tree_or_commit
            if isinstance(target_tree_or_commit, pygit2.Commit)
            else None
        )
        if commit:
            change_time = datetime.utcfromtimestamp(commit.commit_time)
        else:
            change_time = datetime.utcnow()

        L = logging.getLogger(f"{self.__class__.__qualname__}.write_full")
        with self.session(bulk=(0 if safe else 2)) as db:
            for dataset in datasets:
                table_name = dataset.table_name

                self.write_meta(dataset)

                # Create the table
                table_spec = gpkg_adapter.v2_schema_to_sqlite_spec(dataset)

                # GPKG requires an integer primary key for spatial tables, so we add it in if needed:
                if dataset.has_geometry and "PRIMARY KEY" not in table_spec:
                    table_spec = (
                        '".sno-auto-pk" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,'
                        + table_spec
                    )

                db.execute(f"""CREATE TABLE {gpkg.ident(table_name)} ({table_spec});""")

                if dataset.has_geometry:
                    self._create_spatial_index(db, dataset)

                L.info("Creating features...")

                sql = insert_command(dataset.table_name, dataset.schema.column_names)
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
                L.info("Added %d features to GPKG in %.1fs", feat_progress, t1 - t0)
                L.info(
                    "Overall rate: %d features/s", (feat_progress / (t1 - t0 or 0.001))
                )

                self.update_gpkg_contents(dataset, change_time)
                self._create_triggers(db, dataset)

            row = {
                "table_name": "*",
                "key": "tree",
                "value": target_tree_or_commit.peel(pygit2.Tree).hex,
            }
            db.execute(insert_or_replace_command(self.STATE_TABLE, row.keys()), row)

    def write_features(self, db, dataset, pk_list, *, ignore_missing=False):
        sql = insert_or_replace_command(dataset.table_name, dataset.schema.column_names)
        feat_count = 0
        CHUNK_SIZE = 10000
        for row_dicts in self._chunk(
            dataset.get_features_with_crs_ids(pk_list, ignore_missing=ignore_missing),
            CHUNK_SIZE,
        ):
            r = db.execute(sql, row_dicts)
            feat_count += r.rowcount

        return feat_count

    def delete_features(self, db, dataset, pk_list):
        if not pk_list:
            return 0

        CHUNK_SIZE = 10000
        for pks in self._chunk(pk_list, CHUNK_SIZE):
            r = db.execute(
                f"""
                    DELETE FROM {gpkg.ident(dataset.table_name)}
                    WHERE {gpkg.ident(dataset.primary_key)}=:pk;
                """,
                [{"pk": pk} for pk in pks],
            )

        return r.rowcount

    def drop_table(self, target_tree_or_commit, *datasets):
        with self.session() as db:
            for dataset in datasets:
                table_name = dataset.table_name
                if dataset.has_geometry:
                    self._drop_spatial_index(db, dataset)

                db.execute(f"""DROP TABLE IF EXISTS {gpkg.ident(table_name)};""")
                self.delete_meta(dataset)

                db.execute(
                    f"""DELETE FROM {self.TRACKING_TABLE} WHERE table_name = :table_name;""",
                    {"table_name": table_name},
                )

    def _execute_dirty_pks_query(self, db, dataset):
        return db.execute(
            f"""SELECT pk FROM {self.TRACKING_TABLE} WHERE table_name = :table_name;""",
            {"table_name": dataset.table_name},
        )

    def _execute_dirty_rows_query(
        self, db, dataset, feature_filter=None, meta_diff=None
    ):
        feature_filter = feature_filter or UNFILTERED
        table = dataset.table_name
        if (
            meta_diff
            and "schema.json" in meta_diff
            and meta_diff["schema.json"].new_value
        ):
            schema = Schema.from_column_dicts(meta_diff["schema.json"].new_value)
        else:
            schema = dataset.schema

        pk_field = schema.pk_columns[0].name
        col_names = ",".join([f"TAB.{gpkg.ident(col.name)}" for col in schema])

        diff_sql = f"""
            SELECT
                TRA.pk AS ".__track_pk",
                {col_names}
            FROM {self.TRACKING_TABLE} TRA LEFT OUTER JOIN {gpkg.ident(table)} TAB
            ON (TRA.pk = TAB.{gpkg.ident(pk_field)})
            WHERE (TRA.table_name = :table_name)
        """
        params = {"table_name": table}

        if feature_filter is not UNFILTERED:
            diff_sql += " AND TRA.pk = :pk"
            params = [{"table_name": table, "pk": str(pk)} for pk in feature_filter]
        return db.execute(diff_sql, params)

    def reset_tracking_table(self, reset_filter=UNFILTERED):
        reset_filter = reset_filter or UNFILTERED

        with self.session() as db:
            if reset_filter == UNFILTERED:
                db.execute(f"DELETE FROM {self.TRACKING_TABLE};")
                return

            for dataset_path, dataset_filter in reset_filter.items():
                table = dataset_path.strip("/").replace("/", "__")
                if (
                    dataset_filter == UNFILTERED
                    or dataset_filter.get("feature") == UNFILTERED
                ):
                    db.execute(
                        f"DELETE FROM {self.TRACKING_TABLE} WHERE table_name=:table_name;",
                        {"table_name": table},
                    )
                    continue

                pks = dataset_filter.get("feature", ())
                db.execute(
                    f"DELETE FROM {self.TRACKING_TABLE} WHERE table_name=:table_name AND pk=:pk;",
                    [{"table_name": table, "pk": str(pk)} for pk in pks],
                )

    def _reset_tracking_table_for_dataset(self, db, dataset):
        r = db.execute(
            f"DELETE FROM {self.TRACKING_TABLE} WHERE table_name=:table_name;",
            {"table_name": dataset.table_name},
        )
        return r.rowcount

    def _update_state_table_tree_impl(self, db, tree_id):
        r = db.execute(
            f"UPDATE {self.STATE_TABLE} SET value=:value WHERE table_name='*' AND key='tree';",
            {"value": tree_id},
        )
        return r.rowcount

    def _is_meta_update_supported(self, dataset_version, meta_diff):
        """
        Returns True if the given meta-diff is supported *without* dropping and rewriting the table.
        (Any meta change is supported - even in datasets v1 - if we drop and rewrite the table,
        but of course it is less efficient).
        meta_diff - DeltaDiff object containing the meta changes.
        """
        if not meta_diff:
            return True

        if dataset_version < 2:
            # Dataset1 doesn't support meta changes at all - except by rewriting the entire table.
            return False

        if "schema.json" not in meta_diff:
            return True

        schema_delta = meta_diff["schema.json"]
        if not schema_delta.old_value or not schema_delta.new_value:
            return False

        old_schema = Schema.from_column_dicts(schema_delta.old_value)
        new_schema = Schema.from_column_dicts(schema_delta.new_value)
        dt = old_schema.diff_type_counts(new_schema)
        # We do support name_updates, but we don't support any other type of schema update
        # - except by rewriting the entire table.
        dt.pop("name_updates")
        return sum(dt.values()) == 0

    def _apply_meta_title(self, dataset, src_value, dest_value, db):
        # TODO - find a better way to roundtrip titles while keeping them unique
        table_name = dataset.table_name
        identifier = f"{table_name}: {dest_value}"
        db.execute(
            """UPDATE gpkg_contents SET identifier = :identifier WHERE table_name = :table_name""",
            {"identifier": identifier, "table_name": table_name},
        )

    def _apply_meta_description(self, dataset, src_value, dest_value, db):
        db.execute(
            """UPDATE gpkg_contents SET description = :description WHERE table_name = :table_name""",
            {"description": dest_value, "table_name": dataset.table_name},
        )

    def _apply_meta_schema_json(self, dataset, src_value, dest_value, db):
        src_schema = Schema.from_column_dicts(src_value)
        dest_schema = Schema.from_column_dicts(dest_value)

        diff_types = src_schema.diff_types(dest_schema)
        name_updates = diff_types.pop("name_updates")
        if any(dt for dt in diff_types.values()):
            raise RuntimeError(
                f"This schema change not supported by update - should be drop + rewrite_full: {diff_types}"
            )

        for col_id in name_updates:
            src_name = src_schema[col_id].name
            dest_name = dest_schema[col_id].name
            db.execute(
                f"""
                    ALTER TABLE {gpkg.ident(dataset.table_name)}
                    RENAME COLUMN {gpkg.ident(src_name)} TO {gpkg.ident(dest_name)}
                """
            )

    def _apply_meta_metadata_dataset_json(self, dataset, src_value, dest_value, db):
        table = dataset.table_name
        self._delete_meta_metadata(table, db)
        if dest_value:
            gpkg_metadata = gpkg_adapter.json_to_gpkg_metadata(dest_value, table)
            gpkg_metadata_reference = gpkg_adapter.json_to_gpkg_metadata(
                dest_value, table, reference=True
            )
            self._write_meta_metadata(table, gpkg_metadata, gpkg_metadata_reference, db)

    def _update_table(
        self, base_ds, target_ds, db, commit=None, track_changes_as_dirty=False
    ):
        super()._update_table(base_ds, target_ds, db, commit, track_changes_as_dirty)
        self._update_gpkg_contents(target_ds, db, commit)

    def _update_gpkg_contents(self, dataset, db, commit=None):
        """
        Update the metadata for the given table in gpkg_contents to have the new bounding-box / last-updated timestamp.
        """
        if commit:
            change_time = datetime.utcfromtimestamp(commit.commit_time)
        else:
            change_time = datetime.utcnow()
        # GPKG Spec Req. 15:
        gpkg_change_time = change_time.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        table = dataset.table_name
        geom_col = dataset.geom_column_name
        if geom_col is not None:
            # FIXME: Why doesn't Extent(geom) work here as an aggregate?
            r = db.execute(
                f"""
                WITH _E AS (SELECT extent({gpkg.ident(geom_col)}) AS extent FROM {gpkg.ident(table)})
                SELECT ST_MinX(extent), ST_MinY(extent), ST_MaxX(extent), ST_MaxY(extent) FROM _E
                """
            )
            min_x, min_y, max_x, max_y = r.fetchone()
            rc = db.execute(
                """
                    UPDATE gpkg_contents
                    SET (last_change, min_x, min_y, max_x, max_y) = (:last_change, :min_x, :min_y, :max_x, :max_y)
                    WHERE table_name=:table_name;
                """,
                {
                    "last_change": gpkg_change_time,
                    "min_x": min_x,
                    "min_y": min_y,
                    "max_x": max_x,
                    "max_y": max_y,
                    "table_name": table,
                },
            ).rowcount
        else:
            rc = db.execute(
                """UPDATE gpkg_contents SET last_change=:last_change WHERE table_name=:table_name;""",
                {"last_change": gpkg_change_time, "table_name": table},
            ).rowcount
        assert rc == 1, f"gpkg_contents update: expected 1Δ, got {rc}"
