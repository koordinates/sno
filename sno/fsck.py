import os
import subprocess

import click

from . import gpkg
from .exceptions import NotFound, NO_WORKING_COPY
from .geometry import normalise_gpkg_geom


def _fsck_reset(repo, working_copy, dataset_paths):
    commit = repo.head_commit
    datasets = [repo.datasets()[p] for p in dataset_paths]

    working_copy.drop_table(commit, *datasets)
    working_copy.write_full(commit, *datasets)


@click.command(context_settings=dict(ignore_unknown_options=True))
@click.pass_context
@click.option(
    "--reset-dataset",
    "reset_datasets",
    multiple=True,
    help="Reset the working copy for this dataset path",
)
@click.argument("fsck_args", nargs=-1, type=click.UNPROCESSED)
def fsck(ctx, reset_datasets, fsck_args):
    """ Verifies the connectivity and validity of the objects in the database """
    repo = ctx.obj.repo

    click.echo("Checking repository integrity...")
    r = subprocess.call(["git", "-C", repo.path, "fsck"] + list(fsck_args))
    if r:
        click.Abort()

    # now check our stuff:
    # 1. working copy
    if "sno.workingcopy.path" not in repo.config:
        click.echo("No working-copy configured")
        return

    working_copy_path = repo.config["sno.workingcopy.path"]
    working_copy = repo.working_copy
    if not working_copy:
        raise NotFound(
            click.style(f"Working copy missing: {working_copy_path}", fg="red"),
            exit_code=NO_WORKING_COPY,
        )

    click.secho(f"✔︎ Working copy: {working_copy_path}", fg="green")

    if reset_datasets:
        click.secho(
            f"Resetting working copy for {', '.join(reset_datasets)} ...", bold=True
        )
        return _fsck_reset(repo, working_copy, reset_datasets)

    with working_copy.session() as db:
        tree = repo.head_tree

        # compare repo tree id to what's in the DB
        try:
            oid = working_copy.assert_db_tree_match(repo.head_tree)
            click.secho(
                f"✔︎ Working Copy tree id matches repository: {oid}", fg="green"
            )
        except working_copy.Mismatch as e:
            # try and find the tree we _do_ have
            click.secho(f"✘ Repository tree is: {tree.id}", fg="red")
            click.secho(f"✘ Working Copy tree is: {e.working_copy_tree_id}", fg="red")
            click.echo("This might be fixable via `checkout --force`")
            raise click.Abort()

        has_err = False
        for dataset in repo.datasets():
            click.secho(
                f"\nDataset: '{dataset.path}/' (table: '{dataset.table_name}')",
                bold=True,
            )
            table = dataset.table_name

            pk = gpkg.pk(db, table)
            click.echo(f'Primary key field for table: "{pk}"')
            if pk != dataset.primary_key:
                has_err = True
                click.secho(
                    f"✘ Primary Key mismatch between repo ({dataset.primary_key}) & working-copy table ({pk})",
                    fg="red",
                )

            r = db.execute(f"SELECT COUNT(*) FROM {gpkg.ident(table)};")
            wc_count = r.scalar()
            click.echo(f"{wc_count} features in {table}")
            ds_count = dataset.feature_count
            if wc_count != ds_count:
                has_err = True
                click.secho(
                    f"✘ Feature Count mismatch between repo ({ds_count}) & working-copy table ({wc_count})",
                    fg="red",
                )

            r = db.execute(
                f"SELECT COUNT(*) FROM {working_copy.TRACKING_TABLE} WHERE table_name=:table_name;",
                {"table_name": table},
            )
            track_count = r.scalar()
            click.echo(f"{track_count} rows marked as changed in working-copy")

            wc_diff = working_copy.diff_db_to_tree(dataset)
            wc_diff.prune()

            if wc_diff:
                click.secho(
                    f"! Working copy appears dirty according to the index",
                    fg="yellow",
                )

            if "meta" in wc_diff:
                meta_diff = wc_diff["meta"]
                click.secho(f"{dataset.path}:meta: ({len(meta_diff)})", fg="yellow")

                for path in meta_diff.keys():
                    click.echo(f"{dataset.path}:meta:{path}")

            if "feature" in wc_diff:
                feature_diff = wc_diff["feature"]
                click.secho(
                    f"{dataset.path}:feature: ({len(feature_diff)})", fg="yellow"
                )
                nul = "␀"

                # has feature changes
                # Note that pygit has its own names and letters for these operations - Add, Delete, Modify, Rename.
                # But, we call them insert, update and delete elsewhere in sno - so we should be consistent here.
                for delta in feature_diff.values():
                    if delta.type == "insert":
                        click.echo(f" I   {nul:>10} → {delta.key}")
                    elif delta.type == "delete":
                        click.echo(f" D   {delta.key:>10} → {nul}")
                    else:
                        is_rename = delta.old_key != delta.new_key
                        is_update = delta.old_value != delta.new_value

                        if is_rename and is_update:
                            click.echo(f" R+U {delta.old_key:>10} → {delta.new_key}")
                        elif is_rename:
                            click.echo(f" R   {delta.old_key:>10} → {delta.new_key}")
                        elif is_update:
                            click.echo(f" U   {delta.key:>10} → {nul}")

            # can't proceed with content comparison for dirty working copies
            if wc_diff:
                click.echo("Can't do any further checks")
                return

            if not has_err:
                click.echo("Checking features...")
                feature_err_count = 0
                geom_col = dataset.geom_column_name
                for feature, blob in dataset.features_plus_blobs():
                    h_verify = os.path.basename(dataset.encode_1pk_to_path(feature[pk]))
                    if blob.name != h_verify:
                        has_err = True
                        click.secho(
                            f"✘ Hash mismatch for feature '{feature[pk]}': repo says {blob.name} but should be {h_verify}",
                            fg="red",
                        )

                    f = db.execute(
                        f"SELECT * FROM {gpkg.ident(table)} WHERE {gpkg.ident(pk)}=:pk;",
                        {"pk": feature[pk]},
                    )
                    db_obj = dict(f.fetchone())
                    if db_obj is not None and geom_col is not None:
                        db_obj[geom_col] = normalise_gpkg_geom(db_obj[geom_col])
                    if db_obj != feature:
                        s_old = set(feature.items())
                        s_new = set(db_obj.items())
                        diff_add = dict(s_new - s_old)
                        diff_del = dict(s_old - s_new)
                        all_keys = sorted(set(diff_del.keys()) | set(diff_add.keys()))

                        has_err = True
                        click.secho(
                            f"✘ Mismatch between repository and working-copy for feature {pk}={feature[pk]}: fields: {', '.join(all_keys)}",
                            fg="red",
                        )

                        feature_err_count += 1
                        if feature_err_count == 100:
                            click.secho(
                                "! More than 100 errors, stopping for now.", fg="yellow"
                            )
                            break

        if has_err:
            raise click.Abort()

    click.secho("✔︎ Everything looks good", fg="green")
