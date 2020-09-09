# sno changelog

Please note that compatibility for 0.x releases (software or repositories) isn't guaranteed. Sno is evolving quickly and things will change. However, we aim to provide the means to upgrade existing repositories between 0.x versions and to 1.0.

_When adding new entries to the changelog, please include issue/PR numbers wherever possible._

## 0.6.0 (UNRELEASED)

 * `apply` and `import` no longer create empty commits unless you specify `--allow-empty` [#243](https://github.com/koordinates/sno/issues/243), [#245](https://github.com/koordinates/sno/issues/245)
 * Patches that create or delete datasets are now supported in Datasets V2 [#239](https://github.com/koordinates/sno/issues/239)
 * `apply`, `commit` and `merge` commands now optimise repositories after committing, to avoid poor repo performance. [#250](https://github.com/koordinates/sno/issues/250)

## 0.5.0

sno v0.5 introduces a new repo layout, which is the default, dubbed 'Datasets V2'

Existing commands are backward compatible with V1 datasets, however some new functionality is only supported in repositories upgraded to the new layout.


### Datasets V2

 * Entire repositories can be upgraded from V1 to V2 using `sno upgrade EXISTING_REPO NEW_REPO`.
 * V2 should support everything V1 supports
 * All new repositories use the new layout by default. To opt out, use the `--repo-version=1` flag for `sno init`
 * A future release will drop support for v1 repositories

#### New features for V2 repositories only

 * Most schema changes now work
     - this includes column adds, drops, renames and reordering.
     - Notably, changing the primary key field of a dataset are not yet supported.
 * Meta changes are now supported (title, description and XML metadata for each dataset)
 * `import` now has a `--replace-existing` flag to replace existing dataset(s).

#### Missing functionality in Datasets V2

 * String primary keys and tables without primary keys are not yet supported. [#212](https://github.com/koordinates/sno/issues/212)
 * Changing the primary key column is not yet supported. [#238](https://github.com/koordinates/sno/issues/238)
 * Patches which create or delete datasets are not supported. [#239](https://github.com/koordinates/sno/issues/239)
 * Schema changes might not be correctly interpreted if too many changes are made at once (eg adding a new column with the same name as a deleted column - sno may incorrectly assume it is the same column).
    - It is safest to commit schema changes to any existing columns, then commit schema changes adding any new columns, then commit any feature changes.

### Breaking changes in this release

 * New structure to `sno diff` output:
    - Text output: Features are now labelled as `<dataset>:feature:<primary_key>`, consistent with meta items that are labelled as `<dataset>:meta:<meta_item_name>`
    - JSON output also uses "feature" and "meta" as keys for the different types of changes, instead of "featureChanges" and "metaChanges".
 * `sno show -o json` header key changed to `sno.show/v1`, which is not an applyable patch. Use `sno create-patch` to create a patch.
 * `sno upgrade` now only takes two arguments: `sno upgrade EXISTING_REPO NEW_REPO`. No other arguments are required or accepted, exactly how to upgrade the repository is detected automatically.

### Other changes in this release

 * Added `sno create-patch <refish>` - creates a JSON patch file, which can be applied using `sno apply` [#210](https://github.com/koordinates/sno/issues/210)
 * Added `sno data ls` - shows a list of datasets in the sno repository [#203](https://github.com/koordinates/sno/issues/203)
 * `sno help [command]` is a synonym for `sno [subcommand] --help` [#221](https://github.com/koordinates/sno/issues/221)
 * `sno clone` now support shallow clones (`--depth N`) to avoid cloning a repo's entire history [#174](https://github.com/koordinates/sno/issues/174)
 * `sno log` now supports JSON output with `--output-format json` [#170](https://github.com/koordinates/sno/issues/170)
 * `sno meta get` now prints text items as text (not encoded as JSON) [#211](https://github.com/koordinates/sno/issues/211)
 * `sno meta get` without arguments now outputs multiple datasets [#217](https://github.com/koordinates/sno/issues/217)
 * `sno diff` and `sno show` now accept a `--crs` parameter to reproject output [#213](https://github.com/koordinates/sno/issues/213)
 * Streaming diffs: less time until first change is shown when diffing large changes. [#156](https://github.com/koordinates/sno/issues/156)
 * Working copies are now created automatically. [#192](https://github.com/koordinates/sno/issues/192)
 * Commands which are misspelled now suggest the correct spelling [#199](https://github.com/koordinates/sno/issues/199)
 * Bugfix: operations that should immediately fail due to dirty working copy no longer partially succeed. [#181](https://github.com/koordinates/sno/issues/181)
 * Bugfix: some column datatype conversion issues during import and checkout.
 * Linux: Add openssh client dependency into rpm & deb packages. [#121](https://github.com/koordinates/sno/issues/121)
 * Windows: Fix missing PROJ data files in packages. [#235](https://github.com/koordinates/sno/issues/235)

## 0.4.1

### Packaging fix:

* packaging: Fix issue with broken git component paths in packages on macOS and Linux ([#143](https://github.com/koordinates/sno/issues/143))
* packaging: Exclude dev dependency in macOS package

### Minor features / fixes:

* Added a `sno meta get` command for viewing dataset metadata ([#136](https://github.com/koordinates/sno/issues/136))
* `merge`, `commit`, `init`, `import` commands can now take commit messages as files with `--message=@filename.txt`. This replaces the `sno commit -F` option ([#138](https://github.com/koordinates/sno/issues/138))
* `import`: Added `--table-info` option to set dataset metadata, when it can't be autodetected from the source database ([#139](https://github.com/koordinates/sno/issues/139))
* `pull`, `push`, `fetch`, `clone` commands now show progress - disabled with `--quiet` ([#144](https://github.com/koordinates/sno/issues/144))
* `import` now works while on an empty branch ([#149](https://github.com/koordinates/sno/issues/149))

## 0.4.0

### Major changes in this release

* Basic conflict resolution:
    - `sno merge` now puts the repo in a merging state when there are conflicts ([#80](https://github.com/koordinates/sno/issues/80))
    - Added `sno conflicts` to list conflicts ([#84](https://github.com/koordinates/sno/issues/84))
    - Added `sno resolve` ([#101](https://github.com/koordinates/sno/issues/101))
    - Added `sno merge --continue`  ([#94](https://github.com/koordinates/sno/issues/94))
* Major improvements to `sno import` and `sno init --import`:
    - Can now import from postgres databases ([#90](https://github.com/koordinates/sno/issues/90))
    - Multiple tables can be imported at once ([#118](https://github.com/koordinates/sno/issues/118))
* Added `sno show`: shows a commit. With `-o json` generates a patch ([#48](https://github.com/koordinates/sno/issues/48))
* Added `sno apply` to apply the patches generated by `sno show -o json` ([#61](https://github.com/koordinates/sno/issues/61))

### Minor features / fixes:

* add a changelog (here!)
* `sno import` enhancements (in addition to major changes above):
    - GPKG database paths no longer need `GPKG:` prefix
    - now takes table names as separate arguments
    - Added `--primary-key=FIELD` to override primary key field name
    - Added `--message` to customize the commit message
    - `--list` no longer requires a repository
* `sno init --import` enhancements:
    - imports are much faster ([#55](https://github.com/koordinates/sno/issues/55))
    - now imports _all_ tables from database, doesn't allow table to be specified
* Many JSON output improvements:
    - JSON output is specified with `-o json` instead of `--json` ([#98](https://github.com/koordinates/sno/issues/98))
    - Added syntax highlighting to JSON output when viewed in a terminal ([#54](https://github.com/koordinates/sno/issues/54))
    - `sno diff` JSON output layout has changed - features are now flat objects instead of GeoJSON objects. This is much more compact ([#71](https://github.com/koordinates/sno/issues/71))
    - Added JSON output option for most commands
    - Added `--json-style` option to several commands to control JSON formatting ([#70](https://github.com/koordinates/sno/issues/70))
* `sno diff`:
    - `a..b` now refers to the same changes as `sno log a..b` ([#116](https://github.com/koordinates/sno/issues/116))
    - can now diff against tree objects, particularly the empty tree ([#53](https://github.com/koordinates/sno/issues/53))
    - can now view some subset of the changes by supplying filter args, ie `[dataset[:pk]]`
* `sno commit`:
    - can now commit some subset of the changes by supplying filter args, ie `[dataset[:pk]]` ([#69](https://github.com/koordinates/sno/issues/69))
* removed `import-gpkg` command; use `import` instead ([#85](https://github.com/koordinates/sno/issues/85))
* Error messages now go to stderr instead of stdout ([#57](https://github.com/koordinates/sno/issues/57))
* Error conditions now use exit codes to indicate different types of errors ([#46](https://github.com/koordinates/sno/issues/46))

## 0.3.1

* Sno is now available on Windows 🎉 (Windows 8.1+ / Server 2016+ (64-bit))
* Updates to continuous integration — installers/archives are now built and tested with every commit for every platform.
* For macOS users, a homebrew "tap" is now available: `brew cask install koordinates/sno/sno`
* Several bug fixes

## 0.3.0

### Major changes in this release

* License: sno is now publicly available under the GPL open source license.
* Sno now has a website at [**sno.earth**](https://sno.earth)
* Standalone builds and packaging for Linux and macOS. Windows won't be far behind
* Refactoring to support alternative database working copies
* Dependency upgrades (GDAL; Git; Pygit2; Proj; Libgit2; Sqlite; and others)
* Several bug fixes

### Upgrading

If you were running a preview release, remove it before installing the new release:

```console
$ brew uninstall sno
$ brew untap koordinates/sno
```

### Repository Hosting

We have an initial preview available of our Sno repository hosting. This allows you & your team to push and pull Sno repositories. Please contact support@koordinates.com with your Github username and we can get you set up. There is no cost for this service.

### Compatibility


Repositories created with Sno v0.2 are compatible with v0.3. For assistance upgrading any v0.1 repositories, please read our [upgrade guide](https://github.com/koordinates/sno/wiki/Upgrading).


## 0.2.0

### Major changes in this release
* First and foremost, the name — we're now called Sno!
* A new repository structure/layout, which has better performance and a smaller on-disk size
* Data imports are now orders of magnitude faster
* Support for multiple datasets in a single Sno repository
* Support for non-spatial datasets
* Increased test coverage including end-to-end tests
* Improved macOS Homebrew packaging with CI testing.
* Prototype support for spatial-indexing and a sno query command for spatial lookups.
* Diffs across branches/commits, and a GeoJSON diff format.
* Numerous bug fixes
