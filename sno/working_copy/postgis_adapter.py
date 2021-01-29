from osgeo.osr import SpatialReference
from psycopg2.sql import Identifier, SQL


from sno import crs_util
from sno.schema import Schema, ColumnSchema


V2_TYPE_TO_PG_TYPE = {
    "boolean": "boolean",
    "blob": "bytea",
    "date": "date",
    "float": {0: "real", 32: "real", 64: "double precision"},
    "geometry": "geometry",
    "integer": {
        0: "integer",
        8: "smallint",  # Approximated as smallint (int16)
        16: "smallint",
        32: "integer",
        64: "bigint",
    },
    "interval": "interval",
    "numeric": "numeric",
    "text": "text",
    "time": "time",
    "timestamp": "timestamptz",
    # TODO - time and timestamp come in two flavours, with and without timezones.
    # Code for preserving these flavours in datasets and working copies needs more work.
}

PG_TYPE_TO_V2_TYPE = {
    "boolean": "boolean",
    "smallint": ("integer", 16),
    "integer": ("integer", 32),
    "bigint": ("integer", 64),
    "real": ("float", 32),
    "double precision": ("float", 64),
    "bytea": "blob",
    "character varying": "text",
    "date": "date",
    "geometry": "geometry",
    "interval": "interval",
    "numeric": "numeric",
    "text": "text",
    "time": "time",
    "timetz": "time",
    "timestamp": "timestamp",
    "timestamptz": "timestamp",
    "varchar": "text",
}

# Types that can't be roundtrip perfectly in Postgis, and what they end up as.
APPROXIMATED_TYPES = {"integer": {8: ("integer", 16)}}


def v2_schema_to_postgis_spec(schema, v2_obj):
    """
    Generate the SQL CREATE TABLE spec from a V2 object eg:
    'fid INTEGER, geom GEOMETRY(POINT,2136), desc VARCHAR(128), PRIMARY KEY(fid)'
    """
    result = [f'"{col.name}" {v2_type_to_pg_type(col, v2_obj)}' for col in schema]

    if schema.pk_columns:
        pk_col_names = ", ".join((f'"{col.name}"' for col in schema.pk_columns))
        result.append(f"PRIMARY KEY({pk_col_names})")

    return ", ".join(result)


def v2_type_to_pg_type(column_schema, v2_obj):
    """Convert a v2 schema type to a postgis type."""

    v2_type = column_schema.data_type
    extra_type_info = column_schema.extra_type_info

    pg_type_info = V2_TYPE_TO_PG_TYPE.get(v2_type)
    if pg_type_info is None:
        raise ValueError(f"Unrecognised data type: {v2_type}")

    if isinstance(pg_type_info, dict):
        return pg_type_info.get(extra_type_info.get("size", 0))

    pg_type = pg_type_info
    if pg_type == "geometry":
        geometry_type = extra_type_info.get("geometryType")
        crs_name = extra_type_info.get("geometryCRS")
        crs_id = None
        if crs_name is not None:
            crs_id = crs_util.get_identifier_int_from_dataset(v2_obj, crs_name)
        return _v2_geometry_type_to_pg_type(geometry_type, crs_id)

    if pg_type == "text":
        length = extra_type_info.get("length", None)
        return f"varchar({length})" if length is not None else "text"

    if pg_type == "numeric":
        precision = extra_type_info.get("precision", None)
        scale = extra_type_info.get("scale", None)
        if precision is not None and scale is not None:
            return f"numeric({precision},{scale})"
        elif precision is not None:
            return f"numeric({precision})"
        else:
            return "numeric"

    return pg_type


def _v2_geometry_type_to_pg_type(geometry_type, crs_id):
    if geometry_type is not None:
        geometry_type = geometry_type.replace(" ", "")

    if geometry_type is not None and crs_id is not None:
        return f"geometry({geometry_type},{crs_id})"
    elif geometry_type is not None:
        return f"geometry({geometry_type})"
    else:
        return "geometry"


def postgis_to_v2_schema(pg_table_info, pg_spatial_ref_sys, id_salt):
    """Generate a V2 schema from the given postgis metadata tables."""
    return Schema(
        [
            _postgis_to_column_schema(col, pg_spatial_ref_sys, id_salt)
            for col in pg_table_info
        ]
    )


def _postgis_to_column_schema(pg_col_info, pg_spatial_ref_sys, id_salt):
    """
    Given the postgis column info for a particular column, and some extra context in
    case it is a geometry column, converts it to a ColumnSchema. The extra context will
    only be used if the given pg_col_info is the geometry column.
    Parameters:
    pg_col_info - info about a single column from pg_table_info.
    pg_spatial_ref_sys - rows of the "spatial_ref_sys" table that are referenced by this dataset.
    id_salt - the UUIDs of the generated ColumnSchema are deterministic and depend on
    the name and type of the column, and on this salt.
    """
    name = pg_col_info["column_name"]
    pk_index = pg_col_info["pk_ordinal_position"]
    if pk_index is not None:
        pk_index -= 1
    data_type, extra_type_info = _pg_type_to_v2_type(pg_col_info, pg_spatial_ref_sys)

    col_id = ColumnSchema.deterministic_id(name, data_type, id_salt)
    return ColumnSchema(col_id, name, data_type, pk_index, **extra_type_info)


def _pg_type_to_v2_type(pg_col_info, pg_spatial_ref_sys):
    v2_type_info = PG_TYPE_TO_V2_TYPE.get(pg_col_info["data_type"])
    if v2_type_info is None:
        v2_type_info = PG_TYPE_TO_V2_TYPE.get(pg_col_info["udt_name"])

    if isinstance(v2_type_info, tuple):
        v2_type = v2_type_info[0]
        extra_type_info = {"size": v2_type_info[1]}
    else:
        v2_type = v2_type_info
        extra_type_info = {}

    if v2_type == "geometry":
        return _pg_type_to_v2_geometry_type(pg_col_info, pg_spatial_ref_sys)

    if v2_type == "text":
        length = pg_col_info["character_maximum_length"] or None
        if length is not None:
            extra_type_info["length"] = length

    if v2_type == "numeric":
        extra_type_info["precision"] = pg_col_info["numeric_precision"] or None
        extra_type_info["scale"] = pg_col_info["numeric_scale"] or None

    return v2_type, extra_type_info


def _pg_type_to_v2_geometry_type(pg_col_info, pg_spatial_ref_sys):
    """
    col_name - the name of the column.
    pg_spatial_ref_sys - rows of the "spatial_ref_sys" table that are referenced by this dataset.
    """
    geometry_type = pg_col_info["geometry_type"].upper()
    # Look for Z, M, or ZM suffix
    geometry_type, m = _pop_suffix(geometry_type, "M")
    geometry_type, z = _pop_suffix(geometry_type, "Z")
    geometry_type = f"{geometry_type} {z}{m}".strip()

    geometry_crs = None
    crs_id = pg_col_info["geometry_srid"]
    if crs_id:
        crs_info = next((r for r in pg_spatial_ref_sys if r["srid"] == crs_id), None)
        if crs_info:
            geometry_crs = crs_util.get_identifier_str(crs_info["srtext"])

    return "geometry", {"geometryType": geometry_type, "geometryCRS": geometry_crs}


def _pop_suffix(geometry_type, suffix):
    """
    Returns (geometry-type-without-suffix, suffix) if geometry-type ends with suffix.
    Otherwise just returns (geometry-type, "")
    """
    if geometry_type.endswith(suffix):
        return geometry_type[:-1], suffix
    else:
        return geometry_type, ""


def generate_postgis_spatial_ref_sys(v2_obj):
    """
    Generates the contents of the spatial_ref_sys table from the v2 object.
    The result is a list containing a dict per table row.
    Each dict has the format {column-name: value}.
    """
    # TODO - this is not yet written anywhere.
    result = []
    for crs_name, definition in v2_obj.crs_definitions():
        spatial_ref = SpatialReference(definition)
        auth_name = spatial_ref.GetAuthorityName(None) or "NONE"
        crs_id = crs_util.get_identifier_int(spatial_ref)
        result.append(
            {
                "srid": crs_id,
                "auth_name": auth_name,
                "auth_srid": crs_id,
                "srtext": definition,
                "proj4text": spatial_ref.ExportToProj4(),
            }
        )
    return result


def _dimension_count(geometry_type):
    # Look for Z, M, or ZM suffix
    geometry_type, m = _pop_suffix(geometry_type, "M")
    geometry_type, z = _pop_suffix(geometry_type, "Z")
    return len(f"XY{z}{m}")
