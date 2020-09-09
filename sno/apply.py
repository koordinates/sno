import json
from datetime import datetime
from enum import Enum, auto

import click

from .git_util import author_signature
from .exceptions import (
    NO_TABLE,
    NO_WORKING_COPY,
    NotFound,
    NotYetImplemented,
    InvalidOperation,
)
from .diff_structs import RepoDiff, DeltaDiff, Delta
from .geometry import hex_wkb_to_gpkg_geom
from .git_util import gc
from .schema import Schema
from .structure import RepositoryStructure
from .timestamps import iso8601_utc_to_datetime, iso8601_tz_to_timedelta
from .working_copy import WorkingCopy


V1_NO_META_UPDATE = (
    "Sorry, patches that make meta changes are not supported until Datasets V2\n"
    "Use `sno upgrade`"
)
V1_NO_DATASET_CREATE_DELETE = (
    "Sorry, patches that create or delete datasets are not supported until Datasets V2\n"
    "Use `sno upgrade`"
)
NO_COMMIT_NO_DATASET_CREATE_DELETE = (
    "Sorry, patches that create or delete datasets cannot be applied with --no-commit"
)


class MetaChangeType(Enum):
    CREATE_DATASET = auto()
    DELETE_DATASET = auto()
    META_UPDATE = auto()


def _meta_change_type(ds_diff_dict):
    meta_diff = ds_diff_dict.get("meta", {})
    if not meta_diff:
        return None
    schema_diff = meta_diff.get("schema.json", {})
    if "+" in schema_diff and "-" not in schema_diff:
        return MetaChangeType.CREATE_DATASET
    elif "-" in schema_diff and "+" not in schema_diff:
        return MetaChangeType.DELETE_DATASET
    return MetaChangeType.META_UPDATE


def check_change_supported(repo_version, dataset, ds_path, meta_change_type, commit):
    desc = None
    if meta_change_type == MetaChangeType.CREATE_DATASET:
        desc = f"Patch creates dataset '{ds_path}'"
    elif meta_change_type == MetaChangeType.DELETE_DATASET:
        desc = f"Patch deletes dataset '{ds_path}'"
    else:
        desc = f"Patch contains meta changes for dataset '{ds_path}'"

    if repo_version < 2 and meta_change_type is not None:
        message = (
            V1_NO_META_UPDATE
            if meta_change_type == MetaChangeType.META_UPDATE
            else V1_NO_DATASET_CREATE_DELETE
        )
        raise NotYetImplemented(f"{desc}\n{message}")

    if dataset is None and meta_change_type != MetaChangeType.CREATE_DATASET:
        raise NotFound(
            f"Patch contains changes for dataset '{ds_path}' which is not in this repository",
            exit_code=NO_TABLE,
        )
    if dataset is not None and meta_change_type == MetaChangeType.CREATE_DATASET:
        raise InvalidOperation(
            f"Patch creates dataset '{ds_path}' which already exists in this repository"
        )
    if not commit and meta_change_type in (
        MetaChangeType.CREATE_DATASET,
        MetaChangeType.DELETE_DATASET,
    ):
        raise InvalidOperation(f"{desc}\n{NO_COMMIT_NO_DATASET_CREATE_DELETE}")


def unjson_feature(geom_column_name, f):
    if f is not None and geom_column_name is not None:
        f[geom_column_name] = hex_wkb_to_gpkg_geom(f[geom_column_name])
    return f


def apply_patch(*, repo, commit, patch_file, allow_empty, **kwargs):
    try:
        patch = json.load(patch_file)
        json_diff = patch["sno.diff/v1+hexwkb"]
    except (KeyError, json.JSONDecodeError) as e:
        raise click.FileError("Failed to parse JSON patch file") from e

    rs = RepositoryStructure(repo)
    wc = WorkingCopy.get(repo)
    if not commit and not wc:
        # TODO: might it be useful to apply without committing just to *check* if the patch applies?
        raise NotFound("--no-commit requires a working copy", exit_code=NO_WORKING_COPY)

    if wc:
        wc.check_not_dirty()

    repo_diff = RepoDiff()
    for ds_path, ds_diff_dict in json_diff.items():
        dataset = rs.get(ds_path)
        meta_change_type = _meta_change_type(ds_diff_dict)
        check_change_supported(rs.version, dataset, ds_path, meta_change_type, commit)

        meta_changes = ds_diff_dict.get("meta", {})

        if meta_changes:
            meta_diff = DeltaDiff(
                Delta(
                    (k, v["-"]) if "-" in v else None,
                    (k, v.get("+")) if "+" in v else None,
                )
                for (k, v) in meta_changes.items()
            )
            repo_diff.recursive_set([ds_path, "meta"], meta_diff)

        if dataset is not None:
            pk_name = dataset.primary_key
            geom_column_name = dataset.geom_column_name
        else:
            schema = Schema.from_column_dicts(meta_diff["schema.json"].new_value)
            pk_name = schema.pk_columns[0].name
            geom_columns = schema.geometry_columns
            geom_column_name = geom_columns[0].name if geom_columns else None

        feature_changes = ds_diff_dict.get("feature", [])

        def extract_key(feature):
            if feature is None:
                return None
            return feature[pk_name], feature

        def parse_delta(change):
            return Delta(
                extract_key(unjson_feature(geom_column_name, change.get("-"))),
                extract_key(unjson_feature(geom_column_name, change.get("+"))),
            )

        if feature_changes:
            feature_diff = DeltaDiff(
                (parse_delta(change) for change in feature_changes)
            )
            repo_diff.recursive_set([ds_path, "feature"], feature_diff)

    if commit:
        try:
            metadata = patch["sno.patch/v1"]
        except KeyError:
            # Not all diffs are patches. If we're given a raw diff, we can't commit it properly
            raise click.UsageError(
                "Patch contains no author information, and --no-commit was not supplied"
            )

        author_kwargs = {}
        for k, patch_kwarg in (
            ("time", "authorTime"),
            ("email", "authorEmail"),
            ("offset", "authorTimeOffset"),
            ("name", "authorName"),
        ):
            if patch_kwarg in metadata:
                author_kwargs[k] = metadata[patch_kwarg]

        if "time" in author_kwargs:
            author_kwargs["time"] = int(
                datetime.timestamp(iso8601_utc_to_datetime(author_kwargs["time"]))
            )
        if "offset" in author_kwargs:
            author_kwargs["offset"] = int(
                iso8601_tz_to_timedelta(author_kwargs["offset"]).total_seconds()
                / 60  # minutes
            )

        author = author_signature(repo, **author_kwargs)
        oid = rs.commit(
            repo_diff,
            metadata["message"],
            author=author,
            allow_empty=allow_empty,
        )
        click.echo(f"Commit {oid.hex}")

    else:
        oid = rs.create_tree_from_diff(repo_diff)

    if wc:
        # oid refers to either a commit or tree
        wc_target = repo.get(oid)
        click.echo(f"Updating {wc.path} ...")
        wc.reset(wc_target, track_changes_as_dirty=not commit)


@click.command()
@click.pass_context
@click.option(
    "--commit/--no-commit",
    "commit",
    default=True,
    help="Commit changes",
)
@click.option(
    "--allow-empty",
    is_flag=True,
    default=False,
    help=(
        "Usually recording a commit that has the exact same tree as its sole "
        "parent commit is a mistake, and the command prevents you from making "
        "such a commit. This option bypasses the safety"
    ),
)
@click.argument("patch_file", type=click.File("r", encoding="utf-8"))
def apply(ctx, **kwargs):
    """
    Applies and commits the given JSON patch (as created by `sno show -o json`)
    """
    apply_patch(repo=ctx.obj.repo, **kwargs)
    gc(ctx.obj.repo, "--auto", use_subprocess=False)
