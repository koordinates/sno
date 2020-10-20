import contextlib
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from enum import Enum

import click
import pygit2
from osgeo import gdal

from .base import WorkingCopy
from sno import crs_util, gpkg, gpkg_adapter
from sno.db_util import changes_rowcount
from sno.filter_util import UNFILTERED
from sno.geometry import Geometry, normalise_gpkg_geom
from sno.schema import Schema


L = logging.getLogger("sno.working_copy.gpkg")


class SQLCommand(Enum):
    INSERT = "INSERT"
    INSERT_OR_REPLACE = "INSERT OR REPLACE"


def placeholders(vals):
    """Returns '?,?,?,?...' - where the nunber of ? returned is len(vals)"""
    count = len(vals)
    assert count > 0
    return "?" + (",?" * (count - 1))


def sql_insert_dict(dbcur, sql_command, table_name, row_dict):
    """
    Inserts a row into a database table.
    sql_command should be a member of SQLCommand (INSERT or INSERT_OR_REPLACE)
    """
    keys, values = zip(*row_dict.items())
    sql = f"""
        {sql_command.value} INTO {table_name}
            ({','.join([gpkg.ident(k) for k in keys])})
        VALUES
            ({placeholders(keys)});
    """
    return dbcur.execute(sql, values)


class WorkingCopy_GPKG(WorkingCopy):
    def __init__(self, repo, path):
        self.repo = repo
        self.path = path

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
        return gpkg.ident(n)

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

        if hasattr(self, "_db"):
            # inner - reuse
            L.debug(f"session(bulk={bulk}): existing...")
            with self._db:
                yield self._db
            L.debug(f"session(bulk={bulk}): existing/done")
        else:
            L.debug(f"session(bulk={bulk}): new...")
            self._db = gpkg.db(
                self.full_path,
            )
            dbcur = self._db.cursor()

            if bulk:
                L.debug("Invoking bulk mode %s", bulk)
                orig_journal = dbcur.execute("PRAGMA journal_mode;").fetchone()[0]
                orig_locking = dbcur.execute("PRAGMA locking_mode;").fetchone()[0]

                dbcur.execute("PRAGMA synchronous = OFF;")
                dbcur.execute("PRAGMA cache_size = -1048576;")  # -KiB => 1GiB

                if bulk >= 2:
                    dbcur.execute("PRAGMA journal_mode = MEMORY;")
                    dbcur.execute("PRAGMA locking_mode = EXCLUSIVE;")

            try:
                with self._db:
                    yield self._db
            except Exception:
                raise
            finally:
                if bulk:
                    L.debug(
                        "Disabling bulk %s mode (Journal: %s; Locking: %s)",
                        bulk,
                        orig_journal,
                        orig_locking,
                    )
                    dbcur.execute("PRAGMA synchronous = ON;")
                    dbcur.execute("PRAGMA cache_size = -2000;")  # default

                    if bulk >= 2:
                        dbcur.execute(f"PRAGMA locking_mode = {orig_locking};")
                        dbcur.execute(
                            "SELECT name FROM sqlite_master LIMIT 1;"
                        )  # unlock
                        dbcur.execute(f"PRAGMA journal_mode = {orig_journal};")

                del dbcur
                self._db.close()
                del self._db
                L.debug(f"session(bulk={bulk}): new/done")

    def _get_columns(self, dataset):
        pk_field = None
        cols = {}
        for col in dataset.get_gpkg_meta_item("sqlite_table_info"):
            col_spec = f"{gpkg.ident(col['name'])} {col['type']}"
            if col["pk"]:
                col_spec += " PRIMARY KEY"
                pk_field = col["name"]
                # TODO: Should INTEGER PRIMARY KEYs ever be non-AUTOINCREMENT?
                # See https://github.com/koordinates/sno/issues/188
                if col["type"] == "INTEGER":
                    col_spec += " AUTOINCREMENT"
            if col["notnull"]:
                col_spec += " NOT NULL"
            cols[col["name"]] = col_spec

        return cols, pk_field

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
            dbcur = db.cursor()
            dbcur.execute(
                f"""
                SELECT count(*) FROM sqlite_master
                WHERE type='table' AND name IN ({self.STATE_TABLE}, {self.TRACKING_TABLE});
                """
            )
            return dbcur.fetchone()[0] == 2

    def has_data(self):
        """
        Returns true if the GPKG working copy seems to have user-created content already.
        """
        if not self.is_created():
            return False
        with self.session() as db:
            dbcur = db.cursor()
            dbcur.execute(
                f"""
                SELECT count(*) FROM sqlite_master
                WHERE type='table'
                    AND name NOT IN ({self.STATE_TABLE}, {self.TRACKING_TABLE})
                    AND NAME NOT LIKE 'gpkg%';
                """
            )
            return dbcur.fetchone()[0] > 0

    def create_and_initialise(self):
        # GDAL: Create GeoPackage
        # GDAL: Add metadata/etc
        gdal_driver = gdal.GetDriverByName("GPKG")
        gdal_ds = gdal_driver.Create(str(self.full_path), 0, 0, 0, gdal.GDT_Unknown)
        del gdal_ds

        with self.session() as db:
            dbcur = db.cursor()
            # Remove placeholder stuff GDAL creates
            dbcur.execute(
                "DELETE FROM gpkg_geometry_columns WHERE table_name='ogr_empty_table';"
            )
            dbcur.execute(
                "DELETE FROM gpkg_contents WHERE table_name='ogr_empty_table';"
            )
            dbcur.execute("DROP TABLE IF EXISTS ogr_empty_table;")

            # Create metadata tables
            dbcur.execute(
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
            dbcur.execute(
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

            dbcur.execute(
                f"""
                CREATE TABLE {self.STATE_TABLE} (
                    table_name TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NULL,
                    CONSTRAINT {self._sno_table(self.STATE_NAME, 'pk')} PRIMARY KEY (table_name, key)
                );
            """
            )

            dbcur.execute(
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
            dbcur = db.cursor()
            # Update GeoPackage core tables
            for o in gpkg_spatial_ref_sys:
                sql_insert_dict(
                    dbcur, SQLCommand.INSERT_OR_REPLACE, "gpkg_spatial_ref_sys", o
                )

            # our repo copy doesn't include all fields from gpkg_contents
            # but the default value for last_change (now), and NULL for {min_x,max_x,min_y,max_y}
            # should deal with the remaining fields
            sql_insert_dict(dbcur, SQLCommand.INSERT, "gpkg_contents", gpkg_contents)

            if gpkg_geometry_columns:
                sql_insert_dict(
                    dbcur,
                    SQLCommand.INSERT,
                    "gpkg_geometry_columns",
                    gpkg_geometry_columns,
                )

            gpkg_metadata = dataset.get_gpkg_meta_item("gpkg_metadata")
            gpkg_metadata_reference = dataset.get_gpkg_meta_item(
                "gpkg_metadata_reference"
            )
            if gpkg_metadata and gpkg_metadata_reference:
                self._write_meta_metadata(
                    table_name, gpkg_metadata, gpkg_metadata_reference, dbcur
                )

    def _write_meta_metadata(
        self, table_name, gpkg_metadata, gpkg_metadata_reference, dbcur
    ):
        """Populate gpkg_metadata and gpkg_metadata_reference tables."""
        # gpkg_metadata_reference.md_file_id is a foreign key -> gpkg_metadata.id,
        # have to make sure these IDs still match once we insert.
        metadata_id_map = {}
        for row in gpkg_metadata:
            params = dict(row.items())
            params.pop("id")

            sql_insert_dict(dbcur, SQLCommand.INSERT, "gpkg_metadata", params)
            metadata_id_map[row["id"]] = dbcur.getconnection().last_insert_rowid()

        for row in gpkg_metadata_reference:
            params = dict(row.items())
            params["md_file_id"] = metadata_id_map[row["md_file_id"]]
            params["md_parent_id"] = metadata_id_map.get(row["md_parent_id"], None)
            params["table_name"] = table_name

            sql_insert_dict(dbcur, SQLCommand.INSERT, "gpkg_metadata_reference", params)

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

    def delete_meta(self, dataset):
        table_name = dataset.table_name
        with self.session() as db:
            dbcur = db.cursor()
            self._delete_meta_metadata(table_name, dbcur)
            # FOREIGN KEY constraints are still active, so we delete in a particular order:
            dbcur.execute(
                """DELETE FROM gpkg_geometry_columns WHERE table_name = ?;""",
                (dataset.table_name,),
            )
            dbcur.execute(
                """DELETE FROM gpkg_contents WHERE table_name = ?;""",
                (dataset.table_name,),
            )

    def _delete_meta_metadata(self, table_name, dbcur):
        dbcur.execute(
            """SELECT md_file_id FROM gpkg_metadata_reference WHERE table_name = ?;""",
            (table_name,),
        )
        ids = [row[0] for row in dbcur]
        dbcur.execute(
            """DELETE FROM gpkg_metadata_reference WHERE table_name = ?;""",
            (table_name,),
        )
        if ids:
            dbcur.execute(
                f"""DELETE FROM gpkg_metadata WHERE id IN ({placeholders(ids)});""",
                ids,
            )

    def _create_spatial_index(self, dbcur, dataset):
        L = logging.getLogger(f"{self.__class__.__qualname__}.write_full")
        geom_col = dataset.geom_column_name

        # Create the GeoPackage Spatial Index
        t0 = time.monotonic()
        sql = f"SELECT CreateSpatialIndex({gpkg.param_str(dataset.table_name)}, {gpkg.param_str(geom_col)});"
        L.debug(
            "Creating spatial index for %s.%s: %s", dataset.table_name, geom_col, sql
        )
        dbcur.execute(sql)
        L.info("Created spatial index in %ss", time.monotonic() - t0)

    def _drop_spatial_index(self, dbcur, dataset):
        L = logging.getLogger(f"{self.__class__.__qualname__}.write_full")
        geom_col = dataset.geom_column_name

        # Delete the GeoPackage Spatial Index
        t0 = time.monotonic()
        sql = f"SELECT DisableSpatialIndex({gpkg.param_str(dataset.table_name)}, {gpkg.param_str(geom_col)});"
        L.debug(
            "Dropping spatial index for %s.%s: %s", dataset.table_name, geom_col, sql
        )
        dbcur.execute(sql)
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
        table = dataset.table_name

        with self.session() as db:
            dbcur = db.cursor()

            if dataset.has_geometry:
                geom_col = dataset.geom_column_name
                sql = f"""
                    UPDATE gpkg_contents
                    SET
                        min_x=(SELECT ST_MinX(Extent({gpkg.ident(geom_col)})) FROM {gpkg.ident(table)}),
                        min_y=(SELECT ST_MinY(Extent({gpkg.ident(geom_col)})) FROM {gpkg.ident(table)}),
                        max_x=(SELECT ST_MaxX(Extent({gpkg.ident(geom_col)})) FROM {gpkg.ident(table)}),
                        max_y=(SELECT ST_MaxY(Extent({gpkg.ident(geom_col)})) FROM {gpkg.ident(table)}),
                        last_change=?
                    WHERE
                        table_name=?;
                """
            else:
                sql = """
                    UPDATE gpkg_contents
                    SET
                        min_x=NULL,
                        min_y=NULL,
                        max_x=NULL,
                        max_y=NULL,
                        last_change=?
                    WHERE
                        table_name=?;
                """

            dbcur.execute(
                sql,
                (
                    change_time.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),  # GPKG Spec Req.15
                    table,
                ),
            )

            rc = changes_rowcount(dbcur)
            assert rc == 1, f"gpkg_contents update: expected 1Δ, got {rc}"

    def get_db_tree(self, table_name="*"):
        with self.session() as db:
            dbcur = db.cursor()
            dbcur.execute(
                f"""
                    SELECT value
                    FROM {self.STATE_TABLE}
                    WHERE table_name=? AND key=?;
                """,
                (table_name, "tree"),
            )
            row = dbcur.fetchone()
            if not row:
                # It's okay to not have anything in the tree table - it might just mean there are no commits yet.
                # It might also mean that the working copy is not yet initialised - see WorkingCopy.get
                return None

            wc_tree_id = row[0]
            return wc_tree_id

    def _create_triggers(self, dbcur, dataset):
        table = dataset.table_name
        pkf = gpkg.ident(gpkg.pk(dbcur.getconnection(), table))
        ts = gpkg.param_str(table)

        # sqlite doesn't let you do param substitutions in CREATE TRIGGER
        dbcur.execute(
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
        dbcur.execute(
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
        dbcur.execute(
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

    def _placeholders_with_setsrid(self, dataset):
        # We make sure every geometry has the SRID of the column that it belongs to.
        # This is different to in dataset storage, where we normalise each geometry and store it with an SRID of 0.
        result = ["?"] * len(dataset.schema.columns)
        for i, col in enumerate(dataset.schema):
            if col.data_type != "geometry":
                continue
            crs_name = col.extra_type_info.get("geometryCRS", None)
            if crs_name is None:
                continue
            crs_id = crs_util.get_identifier_int_from_dataset(dataset, crs_name)
            result[i] = f"SetSRID(?, {crs_id})"
        return ",".join(result)

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
            dbcur = db.cursor()

            for dataset in datasets:
                table = dataset.table_name

                self.write_meta(dataset)

                # Create the table
                cols, pk_field = self._get_columns(dataset)
                col_names = cols.keys()
                col_specs = cols.values()
                dbcur.execute(
                    f"""
                    CREATE TABLE {gpkg.ident(table)}
                    ({', '.join(col_specs)});
                """
                )

                L.info("Creating features...")
                sql_insert_features = f"""
                    INSERT INTO {gpkg.ident(table)}
                        ({','.join([gpkg.ident(k) for k in col_names])})
                    VALUES
                        ({self._placeholders_with_setsrid(dataset)});
                """
                feat_progress = 0
                t0 = time.monotonic()
                t0p = t0

                CHUNK_SIZE = 10000
                total_features = dataset.feature_count
                for row_dicts in self._chunk(dataset.features(), CHUNK_SIZE):
                    row_tuples = (row_dict.values() for row_dict in row_dicts)
                    dbcur.executemany(sql_insert_features, row_tuples)
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

                if dataset.has_geometry:
                    self._create_spatial_index(dbcur, dataset)

                self.update_gpkg_contents(dataset, change_time)
                self._create_triggers(dbcur, dataset)

            dbcur.execute(
                f"INSERT OR REPLACE INTO {self.STATE_TABLE} (table_name, key, value) VALUES (?, ?, ?);",
                ("*", "tree", target_tree_or_commit.peel(pygit2.Tree).hex),
            )

    def write_features(self, dbcur, dataset, pk_iter, *, ignore_missing=False):
        cols, pk_field = self._get_columns(dataset)
        col_names = cols.keys()

        sql_write_feature = f"""
            INSERT OR REPLACE INTO {gpkg.ident(dataset.table_name)}
                ({','.join([gpkg.ident(k) for k in col_names])})
            VALUES
                ({self._placeholders_with_setsrid(dataset)});
        """

        feat_count = 0
        CHUNK_SIZE = 10000
        for row_dicts in self._chunk(
            dataset.get_features(pk_iter, ignore_missing=ignore_missing),
            CHUNK_SIZE,
        ):
            row_tuples = (row_dict.values() for row_dict in row_dicts)
            dbcur.executemany(sql_write_feature, row_tuples)
            feat_count += changes_rowcount(dbcur)

        return feat_count

    def delete_features(self, dbcur, dataset, pk_iter):
        cols, pk_field = self._get_columns(dataset)

        sql_del_feature = f"""
            DELETE FROM {gpkg.ident(dataset.table_name)}
            WHERE {gpkg.ident(pk_field)}=?;
        """

        feat_count = 0
        CHUNK_SIZE = 10000
        for rows in self._chunk(zip(pk_iter), CHUNK_SIZE):
            dbcur.executemany(sql_del_feature, rows)
            feat_count += changes_rowcount(dbcur)

        return feat_count

    def drop_table(self, target_tree_or_commit, *datasets):
        with self.session() as db:
            dbcur = db.cursor()
            for dataset in datasets:
                table = dataset.table_name
                if dataset.has_geometry:
                    self._drop_spatial_index(dbcur, dataset)

                dbcur.execute(f"""DROP TABLE IF EXISTS {gpkg.ident(table)};""")
                self.delete_meta(dataset)

                dbcur.execute(
                    f"""DELETE FROM {self.TRACKING_TABLE} WHERE table_name=?;""",
                    (table,),
                )

    def _execute_diff_query(self, dbcur, dataset, feature_filter=None):
        feature_filter = feature_filter or UNFILTERED
        table = dataset.table_name
        pk_field = dataset.schema.pk_columns[0].name

        diff_sql = f"""
            SELECT
                {self.TRACKING_TABLE}.pk AS ".__track_pk",
                {gpkg.ident(table)}.*
            FROM {self.TRACKING_TABLE} LEFT OUTER JOIN {gpkg.ident(table)}
            ON ({self.TRACKING_TABLE}.pk = {gpkg.ident(table)}.{gpkg.ident(pk_field)})
            WHERE ({self.TRACKING_TABLE}.table_name = ?)
        """
        params = [table]
        if feature_filter is not UNFILTERED:
            diff_sql += (
                f"\nAND {self.TRACKING_TABLE}.pk IN ({placeholders(feature_filter)})"
            )
            params += [str(pk) for pk in feature_filter]
        dbcur.execute(diff_sql, params)

    def _execute_dirty_rows_query(self, dbcur, dataset):
        sql_changed = f"SELECT pk FROM {self.TRACKING_TABLE} " "WHERE table_name=?;"
        dbcur.execute(sql_changed, (dataset.table_name,))

    def reset_tracking_table(self, reset_filter=UNFILTERED):
        reset_filter = reset_filter or UNFILTERED

        with self.session() as db:
            dbcur = db.cursor()
            if reset_filter == UNFILTERED:
                dbcur.execute(f"DELETE FROM {self.TRACKING_TABLE};")
                return

            for dataset_path, dataset_filter in reset_filter.items():
                table = dataset_path.strip("/").replace("/", "__")
                if (
                    dataset_filter == UNFILTERED
                    or dataset_filter.get("feature") == UNFILTERED
                ):
                    dbcur.execute(
                        f"DELETE FROM {self.TRACKING_TABLE} WHERE table_name=?;",
                        (table,),
                    )
                    continue

                CHUNK_SIZE = 100
                pks = dataset_filter.get("feature", ())
                for pk_chunk in self._chunk(pks, CHUNK_SIZE):
                    dbcur.execute(
                        f"DELETE FROM {self.TRACKING_TABLE} WHERE table_name=? AND pk IN ({placeholders(pk_chunk)});",
                        (table, *pk_chunk),
                    )

    def _reset_tracking_table_for_dataset(self, dbcur, dataset):
        dbcur.execute(
            f"DELETE FROM {self.TRACKING_TABLE} WHERE table_name=?;",
            (dataset.table_name,),
        )
        return changes_rowcount(dbcur)

    def _update_state_table_tree_impl(self, dbcur, tree_id):
        dbcur.execute(
            f"UPDATE {self.STATE_TABLE} SET value=? WHERE table_name='*' AND key='tree';",
            (tree_id,),
        )
        return changes_rowcount(dbcur)

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

    def _apply_meta_title(self, dataset, src_value, dest_value, dbcur):
        # TODO - find a better way to roundtrip titles while keeping them unique
        table = dataset.table_name
        identifier = f"{table}: {dest_value}"
        dbcur.execute(
            """UPDATE gpkg_contents SET identifier = ? WHERE table_name = ?""",
            (identifier, table),
        )

    def _apply_meta_description(self, dataset, src_value, dest_value, dbcur):
        dbcur.execute(
            """UPDATE gpkg_contents SET description = ? WHERE table_name = ?""",
            (dest_value, dataset.table_name),
        )

    def _apply_meta_schema_json(self, dataset, src_value, dest_value, dbcur):
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
            dbcur.execute(
                f"""
                    ALTER TABLE {gpkg.ident(dataset.table_name)}
                    RENAME COLUMN {gpkg.ident(src_name)} TO {gpkg.ident(dest_name)}
                """
            )

    def _apply_meta_metadata_dataset_json(self, dataset, src_value, dest_value, dbcur):
        table = dataset.table_name
        self._delete_meta_metadata(table, dbcur)
        if dest_value:
            gpkg_metadata = gpkg_adapter.json_to_gpkg_metadata(dest_value, table)
            gpkg_metadata_reference = gpkg_adapter.json_to_gpkg_metadata(
                dest_value, table, reference=True
            )
            self._write_meta_metadata(
                table, gpkg_metadata, gpkg_metadata_reference, dbcur
            )

    def _update_table(
        self, base_ds, target_ds, dbcur, commit=None, track_changes_as_dirty=False
    ):
        super()._update_table(base_ds, target_ds, dbcur, commit, track_changes_as_dirty)
        self._update_gpkg_contents(target_ds, dbcur, commit)

    def _update_gpkg_contents(self, dataset, dbcur, commit=None):
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
            dbcur.execute(
                f"""
                WITH _BBOX AS (
                    SELECT
                        Min(MbrMinX({gpkg.ident(geom_col)})) AS min_x,
                        Min(MbrMinY({gpkg.ident(geom_col)})) AS min_y,
                        Max(MbrMaxX({gpkg.ident(geom_col)})) AS max_x,
                        Max(MbrMaxY({gpkg.ident(geom_col)})) AS max_y
                    FROM {gpkg.ident(table)}
                )
                UPDATE gpkg_contents
                SET
                    last_change=?,
                    min_x=(SELECT min_x FROM _BBOX),
                    min_y=(SELECT min_y FROM _BBOX),
                    max_x=(SELECT max_x FROM _BBOX),
                    max_y=(SELECT max_y FROM _BBOX)
                WHERE
                    table_name=?;
                """,
                (
                    gpkg_change_time,
                    table,
                ),
            )
        else:
            dbcur.execute(
                """UPDATE gpkg_contents SET last_change=? WHERE table_name=?;""",
                (gpkg_change_time, table),
            )

        rc = changes_rowcount(dbcur)
        assert rc == 1, f"gpkg_contents update: expected 1Δ, got {rc}"


class WorkingCopy_GPKG_1(WorkingCopy_GPKG):
    """
    GeoPackage Working Copy for v0.1-v0.4 repositories
    """

    SNO_TABLE_PREFIX = ".sno-"

    # The state table was called "meta" in GPKG_1 but we have too many things called meta.
    STATE_NAME = "meta"

    def _db_geom_to_gpkg_geom(self, g):
        # Its possible in GPKG to put arbitrary values in columns, regardless of type.
        # We don't try to convert them here - we let the commit validation step report this as an error.
        if not isinstance(g, bytes):
            return g
        # In V1 we don't normalise the geometries - we just roundtrip them as-is.
        return Geometry.of(g)

    def _placeholders_with_setsrid(self, dataset):
        # In V1 we just roundtrip geometries as-is, and we don't zero out the SRIDs to normalise them -
        # so we don't need to set the SRID to the true value when we write them to GPKG.
        return ",".join(["?"] * len(dataset.schema.columns))


class WorkingCopy_GPKG_2(WorkingCopy_GPKG):
    """
    GeoPackage Working Copy for v0.5+ repositories
    """

    # Using this prefix means OGR/QGIS doesn't list these tables as datasets
    SNO_TABLE_PREFIX = "gpkg_sno_"
