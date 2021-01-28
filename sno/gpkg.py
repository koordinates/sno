import collections

from sno import gpkg_adapter


def ident(identifier):
    """ Sqlite identifier replacement """
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def param_str(value):
    """
    Sqlite parameter string replacement.

    Generally don't use this. Needed for creating triggers/etc though.
    """
    if value is None:
        return "NULL"
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def get_gpkg_meta_items_obj(db, layer):
    """
    Returns an object that supports .gpkg_meta_items and .get_gpkg_meta_item, for a single layer -
    to be used with gpkg_adapter
    """

    class GpkgTableMetaItems:
        """Collection of gpkg_meta_items for a particular table."""

        def __init__(self, db, layer):
            self.db_repr = repr(db)
            self.layer_repr = repr(layer)
            self._gpkg_meta_items = dict(get_gpkg_meta_items(db, layer))

        def gpkg_meta_items(self):
            yield from self._gpkg_meta_items.items()

        def get_gpkg_meta_item(self, name):
            try:
                return self._gpkg_meta_items[name]
            except KeyError:
                if name in gpkg_adapter.GPKG_META_ITEM_NAMES:
                    return None
                raise

        def __repr__(self):
            return f"{self.__class__.__name__}({self.db_repr}, {self.layer_repr})"

        __str__ = __repr__

    return GpkgTableMetaItems(db, layer)


def get_gpkg_meta_items(db, table_name, keys=None):
    """
    Returns metadata from the gpkg_* tables about this GPKG.
    Keep this in sync with OgrImportSource.gpkg_meta_items for other datasource types.
    """

    QUERIES = {
        "gpkg_contents": (
            # we ignore dynamic fields (last-change, min_x, min_y, max_x, max_y)
            """
            SELECT table_name, data_type, identifier, description, srs_id
            FROM gpkg_contents WHERE table_name=:table_name;
            """,
            dict,
        ),
        "gpkg_geometry_columns": (
            """
            SELECT table_name, column_name, geometry_type_name, srs_id, z, m
            FROM gpkg_geometry_columns WHERE table_name=:table_name;
            """,
            dict,
        ),
        "sqlite_table_info": (f"PRAGMA table_info({ident(table_name)});", list),
        "gpkg_metadata_reference": (
            """
            SELECT MR.*
            FROM gpkg_metadata_reference MR
                INNER JOIN gpkg_metadata M ON (MR.md_file_id = M.id)
            WHERE
                MR.table_name=:table_name
                AND MR.column_name IS NULL
                AND MR.row_id_value IS NULL;
            """,
            list,
        ),
        "gpkg_metadata": (
            """
            SELECT M.*
            FROM gpkg_metadata_reference MR
                INNER JOIN gpkg_metadata M ON (MR.md_file_id = M.id)
            WHERE
                MR.table_name=:table_name
                AND MR.column_name IS NULL
                AND MR.row_id_value IS NULL;
            """,
            list,
        ),
        "gpkg_spatial_ref_sys": (
            """
            SELECT DISTINCT SRS.*
            FROM gpkg_spatial_ref_sys SRS
                LEFT OUTER JOIN gpkg_contents C ON (C.srs_id = SRS.srs_id)
                LEFT OUTER JOIN gpkg_geometry_columns G ON (G.srs_id = SRS.srs_id)
            WHERE
                (C.table_name=:table_name OR G.table_name=:table_name)
            """,
            list,
        ),
    }
    try:
        for key, (sql, rtype) in QUERIES.items():
            if keys is not None and key not in keys:
                continue
            # check table exists, the metadata ones may not
            if not key.startswith("sqlite_"):
                r = db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=:name;",
                    {"name": key},
                )
                if not r.fetchone():
                    continue

            r = db.execute(sql, {"table_name": table_name})
            value = [collections.OrderedDict(sorted(zip(row.keys(), row))) for row in r]
            if rtype is dict:
                value = value[0] if len(value) else None
            yield (key, value)
    except Exception:
        print(f"Error building meta/{key}")
        raise


def pk(db, table):
    """ Find the primary key for a GeoPackage table """

    # Requirement 150:
    # A feature table or view SHALL have a column that uniquely identifies the
    # row. For a feature table, the column SHOULD be a primary key. If there
    # is no primary key column, the first column SHALL be of type INTEGER and
    # SHALL contain unique values for each row.

    q = db.execute(f"PRAGMA table_info({ident(table)});")
    fields = []
    for field in q:
        if field["pk"]:
            return field["name"]
        fields.append(field)

    if fields[0]["type"] == "INTEGER":
        return fields[0]["name"]
    else:
        raise ValueError("No valid GeoPackage primary key field found")


def geom_cols(db, table):
    q = db.execute(
        """
            SELECT column_name
            FROM gpkg_geometry_columns
            WHERE table_name=:table_name
            ORDER BY column_name;
        """,
        {"table_name": table},
    )
    return tuple(r[0] for r in q)
