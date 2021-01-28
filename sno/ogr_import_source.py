import functools
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit


import click
from osgeo import gdal, ogr

from . import crs_util, gpkg, gpkg_adapter
from .exceptions import (
    InvalidOperation,
    NotFound,
    NotYetImplemented,
    NO_IMPORT_SOURCE,
    NO_TABLE,
)
from .geometry import Geometry
from .import_source import ImportSource
from .ogr_util import get_type_value_adapter
from .output_util import dump_json_output, get_input_mode, InputMode
from .schema import Schema, ColumnSchema
from .sqlalchemy import gpkg_engine
from .utils import ungenerator


# This defines what formats are allowed, as well as mapping
# sno prefixes onto an OGR format shortname.
FORMAT_TO_OGR_MAP = {
    "GPKG": "GPKG",
    "SHP": "ESRI Shapefile",
    # https://github.com/koordinates/sno/issues/86
    # 'TAB': 'MapInfo File',
    "PG": "PostgreSQL",
}
# The set of format prefixes where a local path is expected
# (as opposed to a URL / something else)
LOCAL_PATH_FORMATS = set(FORMAT_TO_OGR_MAP.keys()) - {"PG"}


class OgrImportSource(ImportSource):
    """
    Imports from an OGR source, currently from a whitelist of formats.
    """

    # NOTE: We don't support *List fields (eg IntegerList).
    OGR_TYPE_TO_V2_SCHEMA_TYPE = {
        "Integer": ("integer", 32),
        "Integer64": ("integer", 64),
        "Real": ("float", 64),
        "String": "text",
        "Binary": "blob",
        "Date": "date",
        "DateTime": "timestamp",
        "Time": "time",
    }
    OGR_SUBTYPE_TO_V2_SCHEMA_TYPE = {
        ogr.OFSTBoolean: "boolean",
        ogr.OFSTInt16: ("integer", 16),
        ogr.OFSTFloat32: ("float", 32),
    }

    DEFAULT_GEOMETRY_COLUMN_NAME = "geom"

    @classmethod
    def _all_subclasses(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from sub._all_subclasses()

    @classmethod
    def adapt_source_for_ogr(cls, source):
        # Accept Path objects
        ogr_source = str(source)
        # Optionally, accept driver-prefixed paths like 'GPKG:'
        allowed_formats = sorted(FORMAT_TO_OGR_MAP.keys())
        m = re.match(
            rf'^(OGR|{"|".join(FORMAT_TO_OGR_MAP.keys())}):(.+)$', ogr_source, re.I
        )
        prefix = None
        if m:
            prefix, ogr_source = m.groups()
            prefix = prefix.upper()
            if prefix == "OGR":
                # Don't specify a driver; let OGR just do whatever it can do.
                # We don't 'support' this, but it will probably work fine for some datasources.
                allowed_formats = None
            else:
                allowed_formats = [prefix]

                if prefix in LOCAL_PATH_FORMATS:
                    # resolve GPKG:~/foo.gpkg and GPKG:~me/foo.gpkg
                    # usually this is handled by the shell, but the GPKG: prefix prevents that
                    ogr_source = os.path.expanduser(ogr_source)

                if prefix in ("CSV", "PG"):
                    # OGR actually handles these prefixes itself...
                    ogr_source = f"{prefix}:{ogr_source}"
            if prefix in LOCAL_PATH_FORMATS:
                if not os.path.exists(ogr_source):
                    raise NotFound(
                        f"Couldn't find {ogr_source!r}", exit_code=NO_IMPORT_SOURCE
                    )
        else:
            # see if any subclasses have a handler for this.
            for subclass in cls._all_subclasses():
                if "handle_source_string" in subclass.__dict__:
                    retval = subclass.handle_source_string(ogr_source)
                    if retval is not None:
                        ogr_source, allowed_formats = retval
                        break

        return ogr_source, allowed_formats

    @classmethod
    def _ogr_open(cls, ogr_source, **open_kwargs):
        return gdal.OpenEx(
            ogr_source,
            gdal.OF_VECTOR | gdal.OF_VERBOSE_ERROR | gdal.OF_READONLY,
            **open_kwargs,
        )

    @classmethod
    def open(cls, source, table=None, primary_key=None):
        ogr_source, allowed_formats = cls.adapt_source_for_ogr(source)
        if allowed_formats is None:
            # let OGR use any driver it's been compiled with.
            open_kwargs = {}
        else:
            open_kwargs = {
                "allowed_drivers": [FORMAT_TO_OGR_MAP[x] for x in allowed_formats]
            }
        try:
            ds = cls._ogr_open(ogr_source, **open_kwargs)
        except RuntimeError as e:
            raise NotFound(
                f"{ogr_source!r} doesn't appear to be valid "
                f"(tried formats: {','.join(allowed_formats) if allowed_formats else '(all)'})",
                exit_code=NO_IMPORT_SOURCE,
            ) from e

        try:
            klass = globals()[
                f"{ds.GetDriver().ShortName.replace(' ', '')}ImportSource"
            ]
        except KeyError:
            klass = cls
        else:
            # Reopen ds to give subclasses a chance to specify open options.
            ds = klass._ogr_open(ogr_source, **open_kwargs)

        return klass(
            ds, table, source=source, ogr_source=ogr_source, primary_key=primary_key
        )

    @classmethod
    def quote_ident_part(cls, part):
        """
        SQL92 conformant identifier quoting, for use with OGR-dialect SQL
        (and most other dialects)
        """
        part = part.replace('"', '""')
        return f'"{part}"'

    @classmethod
    def quote_ident(cls, *parts):
        """
        Quotes an identifier with double-quotes for use in SQL queries.

            >>> quote_ident('mytable')
            '"mytable"'
        """
        if not parts:
            raise ValueError("at least one part required")
        return ".".join([cls.quote_ident_part(p) for p in parts])

    def __init__(
        self,
        ogr_ds,
        table=None,
        *,
        source,
        ogr_source,
        primary_key=None,
        **meta_overrides,
    ):
        self.ds = ogr_ds
        self.driver = self.ds.GetDriver()
        self.table = table
        self.source = source
        self.ogr_source = ogr_source
        self._primary_key = self._check_primary_key_option(primary_key)
        self._meta_overrides = {
            k: v for k, v in meta_overrides.items() if v is not None
        }

    def default_dest_path(self):
        return self.table

    def import_source_desc(self):
        return f"Import from {self.source_name}:{self.table} to {self.dest_path}/"

    def aggregate_import_source_desc(self, import_sources):
        if len(import_sources) == 1:
            return next(iter(import_sources)).import_source_desc()

        desc = f"Import {len(import_sources)} datasets from {self.source_name}:"
        for source in import_sources:
            if source.dest_path == source.table:
                desc += f"\n * {source.table}/"
            else:
                desc += f"\n * {source.dest_path} (from {source.table})"
        return desc

    @property
    def source_name(self):
        return Path(self.source).name

    def clone_for_table(self, table, primary_key=None, **meta_overrides):
        meta_overrides = {**self._meta_overrides, **meta_overrides}
        return self.__class__(
            self.ds,
            table=table,
            source=self.source,
            ogr_source=self.ogr_source,
            primary_key=primary_key or self._primary_key,
            **meta_overrides,
        )

    @property
    @functools.lru_cache(maxsize=1)
    def ogrlayer(self):
        return self.ds.GetLayerByName(self.table)

    def get_tables(self):
        """
        Returns a dict of OGRLayer objects keyed by layer name
        """
        layers = {}
        for i in range(self.ds.GetLayerCount()):
            layer = self.ds.GetLayerByIndex(i)
            layers[layer.GetName()] = layer
        return layers

    def print_table_list(self, do_json=False):
        names = {}
        for table_name, ogrlayer in self.get_tables().items():
            try:
                pretty_name = ogrlayer.GetMetadata_Dict()["IDENTIFIER"]
            except KeyError:
                pretty_name = table_name
            names[table_name] = pretty_name
        if do_json:
            dump_json_output({"sno.tables/v1": names}, sys.stdout)
        else:
            click.secho(f"Tables found:", bold=True)
            for table_name, pretty_name in names.items():
                click.echo(f"  {table_name} - {pretty_name}")
        return names

    def prompt_for_table(self, prompt):
        table_list = list(self.get_tables().keys())

        if len(table_list) == 1:
            return table_list[0]
        else:
            self.print_table_list()
            if get_input_mode() == InputMode.NO_INPUT:
                raise NotFound("No table specified", exit_code=NO_TABLE)
            t_choices = click.Choice(choices=table_list)
            t_default = table_list[0] if len(table_list) == 1 else None
            return click.prompt(
                f"\n{prompt}",
                type=t_choices,
                show_choices=False,
                default=t_default,
            )

    def __str__(self):
        s = str(self.source)
        if self.table:
            s += f":{self.table}"
        return s

    def check_table(self, table_name):
        if table_name not in self.get_tables():
            raise NotFound(
                f"Table '{table_name}' not found",
                exit_code=NO_TABLE,
            )

    def __enter__(self):
        self.check_table(self.table)

        if self.ds.TestCapability(ogr.ODsCTransactions):
            self.ds.StartTransaction()
        return self

    def __exit__(self, *exc):
        if self.ds.TestCapability(ogr.ODsCTransactions):
            self.ds.RollbackTransaction()

    @property
    @functools.lru_cache(maxsize=1)
    def feature_count(self):
        return self.ogrlayer.GetFeatureCount(force=False)

    @property
    @functools.lru_cache(maxsize=1)
    def primary_key(self):
        # NOTE: for many OGR drivers, FID column is always 'FID'.
        # For some drivers (databases), OGR will instead use the primary key
        # of the given table, BUT only if it is an integer.
        # For tables with non-integer PKS, ogrlayer.GetFIDColumn() returns ''.
        # In that case, we would have no choice but to get the PK name outside of OGR.
        # For that reason we don't use ogrlayer.GetFIDColumn() here,
        # and instead we have to implement custom PK behaviour in driver-specific subclasses.
        return self._primary_key or "FID"

    @property
    def layer_defn(self):
        return self.ogrlayer.GetLayerDefn()

    @property
    @functools.lru_cache(maxsize=1)
    def has_geometry(self):
        return bool(self.layer_defn.GetGeomFieldCount())

    def _check_primary_key_option(self, primary_key_name):
        if primary_key_name is None:
            return None
        if primary_key_name:
            ld = self.layer_defn

            if primary_key_name == self.ogrlayer.GetFIDColumn():
                # OGR automatically turns 'ogc_fid' column in postgres into an FID,
                # and removes it from the list of fields below.
                return primary_key_name

            for i in range(ld.GetFieldCount()):
                field = ld.GetFieldDefn(i)
                if primary_key_name == field.GetName():
                    return primary_key_name
        raise InvalidOperation(
            f"'{primary_key_name}' was not found in the dataset",
            param_hint="--primary-key",
        )

    def _get_primary_key_value(self, ogr_feature, name):
        return ogr_feature.GetFID()

    @property
    @functools.lru_cache(maxsize=1)
    def field_adapter_map(self):
        return {col.name: get_type_value_adapter(col.data_type) for col in self.schema}

    @ungenerator(dict)
    def _ogr_feature_to_sno_feature(self, ogr_feature):
        for name, adapter in self.field_adapter_map.items():
            if name == self.primary_key:
                value = self._get_primary_key_value(ogr_feature, name)
            elif name in self.geometry_column_names:
                value = ogr_feature.GetGeometryRef()
            else:
                value = ogr_feature.GetField(name)
            yield name, adapter(value)

    def _iter_ogr_features(self):
        l = self.ogrlayer
        l.ResetReading()
        f = l.GetNextFeature()
        while f is not None:
            yield f
            f = l.GetNextFeature()
        # end of iter
        l.ResetReading()

    def features(self):
        for ogr_feature in self._iter_ogr_features():
            yield self._ogr_feature_to_sno_feature(ogr_feature)

    @functools.lru_cache()
    def get_meta_item(self, name):
        if name in self._meta_overrides:
            return self._meta_overrides[name]
        ogr_metadata = self.ogrlayer.GetMetadata()
        if name == "title":
            return ogr_metadata.get("IDENTIFIER") or ""
        elif name == "description":
            return ogr_metadata.get("DESCRIPTION") or ""
        elif name == "schema.json":
            return self.schema.to_column_dicts()
        elif name == "metadata/dataset.json":
            return self.get_v2_metadata_json()
        elif name.startswith("crs/"):
            return self.get_crs_definition(name)
        raise KeyError(f"No meta item found with name: {name}")

    def crs_definitions(self):
        ld = self.layer_defn
        for i in range(ld.GetGeomFieldCount()):
            spatial_ref = ld.GetGeomFieldDefn(i).GetSpatialRef()
            if spatial_ref:
                yield (
                    crs_util.get_identifier_str(spatial_ref),
                    crs_util.normalise_wkt(spatial_ref.ExportToWkt()),
                )

    @property
    def pk_column_schema(self):
        if not self.primary_key:
            return None

        ld = self.ogrlayer.GetLayerDefn()

        ogr_pk_index = ld.GetFieldIndex(self.primary_key)
        if ogr_pk_index != -1:
            return self._field_to_v2_column_schema(ld.GetFieldDefn(ogr_pk_index))
        else:
            # FID field, isn't an OGR field
            return ColumnSchema(
                ColumnSchema.new_id(), self.primary_key, "integer", 0, size=64
            )

    @property
    @functools.lru_cache(maxsize=1)
    def geometry_column_names(self):
        ld = self.layer_defn
        # Some OGR drivers don't support named geometry fields; the dataset either has a geometry or doesn't.
        # In situations where there _is_ a field, it doesn't necessarily have a name.
        # So here we pick `DEFAULT_GEOMETRY_COLUMN_NAME` ("geom") as the default name.
        # Where there are multiple geom fields, they have names.
        return [
            ld.GetGeomFieldDefn(i).GetName() or self.DEFAULT_GEOMETRY_COLUMN_NAME
            for i in range(ld.GetGeomFieldCount())
        ]

    @property
    def geometry_columns_schema(self):
        ld = self.layer_defn
        return [
            self._geom_field_to_v2_column_schema(ld.GetGeomFieldDefn(i))
            for i in range(ld.GetGeomFieldCount())
        ]

    @property
    def regular_columns_schema(self):
        ld = self.layer_defn
        return [
            self._field_to_v2_column_schema(ld.GetFieldDefn(i))
            for i in range(ld.GetFieldCount())
            if ld.GetFieldDefn(i).GetName() != self.primary_key
        ]

    def _should_import_as_numeric(self, ogr_type, ogr_width, ogr_precision):
        if ogr_type not in ("Real", "Integer", "Integer64"):
            return False
        # We import numeric real/integer as numeric if they have a
        # nonzero width specified.
        # Unfortunately, that means these three all collide:
        #    NUMERIC (unqualified) --> ogr.Real(0.0) --> double
        #    FLOAT --> ogr.Real(0.0) --> double
        #    DOUBLE PRECISION --> ogr.Real(0.0) --> double
        # Fixing this collision might be a good reason to move away from OGR
        # for import processing in the near future.
        #
        # Note that the shapefile importsource overrides this since OGR *always* reports
        # a nonzero width for floats/ints in shapefiles.
        return ogr_width != 0

    def _field_to_v2_column_schema(self, fd):
        ogr_type = fd.GetTypeName()
        ogr_width = fd.GetWidth()
        ogr_precision = fd.GetPrecision()
        ogr_subtype = fd.GetSubType()
        if (not ogr_subtype) and self._should_import_as_numeric(
            ogr_type, ogr_width, ogr_precision
        ):
            data_type = "numeric"
            # Note 2: Rather confusingly, OGR's concepts of 'width' and 'precision'
            # correspond to 'precision' and 'scale' in most other systems, respectively:
            extra_type_info = {
                # total number of decimal digits
                "precision": ogr_width,
                # total number of decimal digits to the right of the decimal point
                "scale": ogr_precision,
            }
        else:
            if ogr_subtype == ogr.OFSTNone:
                data_type_info = self.OGR_TYPE_TO_V2_SCHEMA_TYPE.get(ogr_type)
                if data_type_info is None:
                    raise NotYetImplemented(
                        f"Unsupported column type for import: OGR type={ogr_type}"
                    )
            else:
                data_type_info = self.OGR_SUBTYPE_TO_V2_SCHEMA_TYPE.get(ogr_subtype)
                if data_type_info is None:
                    raise NotYetImplemented(
                        f"Unsupported column type for import: OGR subtype={ogr_subtype}"
                    )

            if isinstance(data_type_info, tuple):
                data_type, size = data_type_info
                extra_type_info = {"size": size}
            elif isinstance(data_type_info, str):
                data_type = data_type_info
                extra_type_info = {}

            if data_type in ("text", "blob") and ogr_width:
                extra_type_info["length"] = ogr_width

        name = fd.GetName()
        pk_index = 0 if name == self.primary_key else None
        return ColumnSchema(
            ColumnSchema.new_id(), name, data_type, pk_index, **extra_type_info
        )

    def _geom_field_to_v2_column_schema(self, geom_fd):
        name = geom_fd.GetName() or self.DEFAULT_GEOMETRY_COLUMN_NAME
        v2_geom_type = self._get_v2_geometry_type(geom_fd)
        extra_type_info = {"geometryType": v2_geom_type}

        # TODO: Support tables with different CRSs in different columns.
        crs_definitions = list(self.crs_definitions())
        if crs_definitions:
            extra_type_info["geometryCRS"] = crs_definitions[0][0]

        return ColumnSchema(
            ColumnSchema.new_id(), name, "geometry", None, **extra_type_info
        )

    def _get_v2_geometry_type(self, geom_fd):
        ogr_geom_type = geom_fd.GetType()
        z = "Z" if ogr.GT_HasZ(ogr_geom_type) else ""
        m = "M" if ogr.GT_HasM(ogr_geom_type) else ""

        ogr_geom_type = ogr.GT_Flatten(ogr_geom_type)
        if ogr_geom_type == ogr.wkbUnknown:
            v2_type = "GEOMETRY"
        else:
            # 'Line String' --> 'LineString' --> 'LINESTRING'
            v2_type = ogr.GeometryTypeToName(ogr_geom_type).replace(" ", "").upper()

        return f"{v2_type} {z}{m}".strip()

    def _init_schema(self):
        pk_col = self.pk_column_schema
        pk_cols = [pk_col] if pk_col else []
        columns = pk_cols + self.geometry_columns_schema + self.regular_columns_schema
        return Schema(columns)

    _KNOWN_METADATA_URIS = {
        "GDALMultiDomainMetadata": "http://gdal.org",
    }

    def get_v2_metadata_json(self):
        xml_metadata = self._meta_overrides.get("xml_metadata")
        if not xml_metadata:
            return None

        from xml.dom.minidom import parseString

        doc = parseString(xml_metadata)
        element = doc.documentElement
        if element.tagName in self._KNOWN_METADATA_URIS:
            uri = self._KNOWN_METADATA_URIS[element.tagName]
        else:
            uri = element.getAttribute("xmlns") or element.namespaceURI or "(unknown)"

        return {uri: {"text/xml": xml_metadata}}


class ESRIShapefileImportSource(OgrImportSource):
    def _should_import_as_numeric(self, ogr_type, ogr_width, ogr_precision):
        if not super()._should_import_as_numeric(ogr_type, ogr_width, ogr_precision):
            return False

        # Generally speaking, we import Real/Integer with nonzero 'width'
        # as fixed-width NUMERIC.
        # However, OGR *always* reports a nonzero 'width' for real/integer
        # in shapefiles. They have specific widths.
        # If we find fields with those specific widths, we can assume they're
        # actually ints/doubles, not NUMERIC
        if (
            # double or float
            (ogr_type == "Real" and ogr_width == 24 and ogr_precision == 15)
            # smallint or integer. normally integer is 9 but some can be 10
            or (ogr_type == "Integer" and ogr_width in (5, 9, 10))
            # integer64. normally width is 18 but can be up to 20
            or (ogr_type == "Integer64" and ogr_width in (18, 19, 20))
        ):
            return False
        return True


class GPKGImportSource(OgrImportSource):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.table:
            with self.engine.connect() as db:
                self._gpkg_primary_key = gpkg.pk(db, self.table)
                self.gpkg_meta_items_obj = gpkg.get_gpkg_meta_items_obj(db, self.table)

    @classmethod
    def quote_ident_part(cls, part):
        """
        SQLite-conformant identifier quoting
        """
        return gpkg.ident(part)

    @property
    @functools.lru_cache(maxsize=1)
    def engine(self):
        return gpkg_engine(self.ogr_source)

    @ungenerator(dict)
    def _gpkg_feature_to_sno_feature(self, gpkg_feature):
        for key, value in gpkg_feature.items():
            if key in self.geometry_column_names:
                yield (key, Geometry.of(value))
            else:
                yield key, value

    @property
    def primary_key(self):
        return self._primary_key or self._gpkg_primary_key

    def features(self):
        """
        Overrides the super implementation for performance reasons
        (it turns out that OGR feature iterators for GPKG are quite slow!)
        """
        with self.engine.connect() as db:
            r = db.execute(f"SELECT * FROM {self.quote_ident(self.table)};")
            for gpkg_feature in r:
                yield self._gpkg_feature_to_sno_feature(gpkg_feature)

    def get_v2_metadata_json(self):
        if (
            "xml_metadata" in self._meta_overrides
            or "metadata/dataset.json" in self._meta_overrides
        ):
            return super().get_v2_metadata_json()

        return gpkg_adapter.generate_v2_meta_item(
            self.gpkg_meta_items_obj, "metadata/dataset.json"
        )

    def crs_definitions(self):
        yield from gpkg_adapter.all_v2_crs_definitions(self.gpkg_meta_items_obj)


class PostgreSQLImportSource(OgrImportSource):
    @classmethod
    def postgres_url_to_ogr_conn_str(cls, url):
        """
        Takes a URL ('postgresql://..')
        and turns it into a key/value connection string, prefixed by 'PG:' for OGR.

        libpq actually handles URIs fine, but OGR doesn't :(
        So to import via OGR we have to convert them.

        https://www.postgresql.org/docs/current/libpq-connect.html#LIBPQ-CONNSTRING

        ^ These docs say these URLs can contain multiple hostnames or ports,
        but we don't handle that.
        """

        url = urlsplit(url)
        scheme = url.scheme.lower()
        if scheme not in ("postgres", "postgresql"):
            raise ValueError("Bad scheme")

        # Start with everything from the querystring.
        params = dict(parse_qsl(url.query))

        # Each of these fields can come from the main part of the URL,
        # OR can come from the querystring.
        # If both are specified, the querystring has precedence.
        # So in 'postgresql://host1/?host=host2', the resultant host is 'host2'
        if url.username:
            params.setdefault("user", url.username)
        if url.password:
            params.setdefault("password", url.password)
        if url.hostname:
            params.setdefault("host", unquote(url.hostname))
        if url.port:
            params.setdefault("port", url.port)
        dbname = (url.path or "/")[1:]
        if dbname:
            params.setdefault("dbname", dbname)

        conn_str = " ".join(sorted(f"{k}={v}" for (k, v) in params.items()))
        return f"PG:{conn_str}"

    @classmethod
    def handle_source_string(cls, source):
        if "://" not in source:
            return None
        try:
            return cls.postgres_url_to_ogr_conn_str(source), ["PG"]
        except ValueError:
            return None

    @classmethod
    def _ogr_open(cls, ogr_source, **open_kwargs):
        open_options = open_kwargs.setdefault("open_options", [])
        # don't only list tables listed in geometry_columns
        open_options.append("LIST_ALL_TABLES=YES")
        return super()._ogr_open(ogr_source, **open_kwargs)

    def psycopg2_conn(self):
        import psycopg2

        conn_str = self.source
        if conn_str.startswith("OGR:"):
            conn_str = conn_str[4:]
        if conn_str.startswith("PG:"):
            conn_str = conn_str[3:]
        # this will either be a URL or a key=value conn str
        return psycopg2.connect(conn_str)

    def _get_primary_key_value(self, ogr_feature, name):
        try:
            return ogr_feature.GetField(name)
        except KeyError:
            # OGR uses integer PKs as the 'FID', but then *doesn't*
            # expose them as fields.
            # In that case we have to call GetFID()
            return ogr_feature.GetFID()

    @property
    @functools.lru_cache(maxsize=1)
    def primary_key(self):
        if self._primary_key:
            return self._primary_key
        conn = self.psycopg2_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.attname
                FROM   pg_index i
                JOIN   pg_attribute a ON a.attrelid = i.indrelid
                                     AND a.attnum = ANY(i.indkey)
                WHERE  i.indrelid = %s::regclass
                AND    i.indisprimary;
                """,
                [self.table],
            )
            rows = cur.fetchall()
            num_pks = len(rows)
            if num_pks == 0:
                return None
            # TODO: handle multi-column PKs. Ignoring for now.
            assert num_pks == 1
            return rows[0][0]
