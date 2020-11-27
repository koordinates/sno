import json
import io

import click

from .apply import apply_patch
from .cli_util import StringFromFile, add_help_subcommand
from .exceptions import InvalidOperation
from .output_util import (
    dump_json_output,
    format_json_for_output,
    format_wkt_for_output,
    write_with_indent,
    resolve_output_path,
    wrap_text_to_terminal,
)
from .structure import RepositoryStructure

# Changing these items would generally break the repo;
# we disallow that.
READONLY_ITEMS = {
    "primary_key",
    "sqlite_table_info",
    "fields",
}


@add_help_subcommand
@click.group()
@click.pass_context
def meta(ctx, **kwargs):
    """
    Read and update meta values for a dataset.
    """


@meta.command(name="get")
@click.option(
    "--output-format",
    "-o",
    type=click.Choice(["text", "json"]),
    default="text",
)
@click.option(
    "--json-style",
    type=click.Choice(["extracompact", "compact", "pretty"]),
    default="pretty",
    help="How to format the JSON output. Only used with -o json",
)
@click.option("--ref", default="HEAD")
@click.argument("dataset", required=False)
@click.argument("keys", required=False, nargs=-1)
@click.pass_context
def meta_get(ctx, output_format, json_style, ref, dataset, keys):
    """
    Prints the value of meta keys.

    Optionally, output can be filtered to a dataset and a particular key.
    """
    rs = RepositoryStructure.lookup(ctx.obj.repo, ref)

    if dataset:
        try:
            datasets = [rs[dataset]]
        except KeyError:
            raise click.UsageError(f"No such dataset: {dataset}")
    else:
        datasets = rs

    fp = resolve_output_path("-")

    all_items = {}
    for ds in datasets:
        if keys:
            all_items[ds.path] = get_meta_items(ds, keys)
        else:
            all_items[ds.path] = dict(ds.meta_items())

    if output_format == "text":
        for ds_path, items in all_items.items():
            click.secho(ds_path, bold=True)
            for key, value in items.items():
                click.secho(f"    {key}", bold=True)
                value_indent = "        "
                if key.endswith(".json") or not isinstance(value, str):
                    value = format_json_for_output(value, fp, json_style=json_style)
                    write_with_indent(fp, value, indent=value_indent)
                elif key.endswith(".wkt"):
                    value = format_wkt_for_output(value, fp)
                    write_with_indent(fp, value, indent=value_indent)
                else:
                    fp.write(wrap_text_to_terminal(value, indent=value_indent))
    else:
        dump_json_output(all_items, fp, json_style=json_style)


def get_meta_items(ds, keys):
    items = {}
    for key in keys:
        for func in (ds.get_meta_item, ds.get_gpkg_meta_item):
            try:
                items[key] = func(key)
                break
            except KeyError:
                continue

    missing_keys = list(keys - items.keys())
    if missing_keys:
        raise click.UsageError(
            f"Couldn't find items: {', '.join(sorted(missing_keys))}"
        )
    return items


class KeyValueType(click.ParamType):
    name = "key=value"

    def convert(self, value, param, ctx):
        value = tuple(value.split("=", 1))
        if len(value) != 2:
            self.fail(f"{value} should be of the form KEY=VALUE", param, ctx)

        key, value = value
        if not key:
            self.fail(f"Key should not be empty", param, ctx)

        return key, value


@meta.command(name="set")
@click.option(
    "--message",
    "-m",
    help="Use the given message as the commit message",
    type=StringFromFile(encoding="utf-8"),
)
@click.argument("dataset")
@click.argument(
    "items",
    type=KeyValueType(),
    required=True,
    nargs=-1,
    metavar="KEY=VALUE [KEY=VALUE...]",
)
@click.pass_context
def meta_set(ctx, message, dataset, items):
    """
    Sets multiple meta items for a dataset, and creates a commit.
    """
    rs = RepositoryStructure(ctx.obj.repo)

    if rs.version < 2:
        raise InvalidOperation(
            "This repo doesn't support meta changes, use `sno upgrade`"
        )

    if message is None:
        message = f"Update metadata for {dataset}"

    def _parse(key, value):
        if key.endswith(".json"):
            try:
                return json.loads(value)
            except json.decoder.JSONDecodeError as e:
                raise click.BadParameter(f"{key} is not valid JSON:\n{e}")
        else:
            return value

    patch = {
        "sno.diff/v1+hexwkb": {
            dataset: {
                "meta": {key: {"+": _parse(key, value)} for (key, value) in items}
            }
        },
        "sno.patch/v1": {"message": message},
    }
    patch_file = io.StringIO()
    json.dump(patch, patch_file)
    patch_file.seek(0)
    apply_patch(
        repo=ctx.obj.repo,
        do_commit=True,
        patch_file=patch_file,
        allow_empty=False,
        allow_missing_old_values=True,
    )
