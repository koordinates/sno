import contextlib
import hashlib
import io
import json
import logging
import os
import random
import re
import shutil
import subprocess
import tarfile
import time
import uuid
from pathlib import Path

import pytest
import click
from click.testing import CliRunner
from sno.repository_version import DEFAULT_REPO_VERSION

import apsw
import pygit2


pytest_plugins = ["helpers_namespace"]


L = logging.getLogger("sno.tests")


def pytest_addoption(parser):
    if "CI" in os.environ:
        # pytest.ini sets --numprocesses=auto
        # for parallelism in local dev.
        # But in CI we disable xdist because it causes a crash in windows builds.
        # (simply doing --numprocesses=0 is insufficient; the plugin needs to be
        # disabled completely)
        # However, there's no way to *remove* an option that's in pytest.ini's addopts.
        # So here we just define the option so it parses, and then ignore it.
        parser.addoption(
            "--numprocesses",
            action="store",
            default=0,
            help="<ignored>",
        )
    parser.addoption(
        "--preserve-data",
        action="store_true",
        default=False,
        help="Preserve temporary data directories",
    )
    parser.addoption(
        "--pdb-trace",
        action="store_true",
        default=False,
        help="Allow calling pytest.set_trace() within Click commands",
    )


def pytest_report_header(config):
    if config._benchmarksession.disabled:
        click.secho(
            "\nSkipping benchmarks in tests. Use --benchmark-enable to run them.",
            bold=True,
            fg="yellow",
        )


# https://github.com/pytest-dev/pytest/issues/363
@pytest.fixture(scope="session")
def monkeypatch_session(request):
    from _pytest.monkeypatch import MonkeyPatch

    mpatch = MonkeyPatch()
    yield mpatch
    mpatch.undo()


@pytest.fixture
def gen_uuid(request):
    """ Deterministic "random" UUID generator seeded from the test ID """
    seed = int(hashlib.sha1(request.node.nodeid.encode("utf8")).hexdigest(), 16)
    _uuid_gen = random.Random(seed)

    def _uuid():
        return str(uuid.UUID(int=_uuid_gen.getrandbits(128)))

    return _uuid


@pytest.fixture(scope="session", autouse=True)
def git_user_config(monkeypatch_session, tmp_path_factory, request):
    home = tmp_path_factory.mktemp("home")

    # override libgit2's search paths
    pygit2.option(pygit2.GIT_OPT_SET_SEARCH_PATH, pygit2.GIT_CONFIG_LEVEL_SYSTEM, "")
    pygit2.option(pygit2.GIT_OPT_SET_SEARCH_PATH, pygit2.GIT_CONFIG_LEVEL_XDG, "")
    pygit2.option(
        pygit2.GIT_OPT_SET_SEARCH_PATH, pygit2.GIT_CONFIG_LEVEL_GLOBAL, str(home)
    )

    # setup environment variables in case we call 'git' commands
    monkeypatch_session.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch_session.setenv("HOME", str(home))
    monkeypatch_session.setenv("GIT_ATTR_NOSYSTEM", "1")
    monkeypatch_session.setenv("GIT_CONFIG_NOSYSTEM", "1")

    USER_NAME = "Sno Tester"
    USER_EMAIL = "sno-tester@example.com"

    with open(home / ".gitconfig", "w") as f:
        f.write(f"[user]\n\tname = {USER_NAME}\n\temail = {USER_EMAIL}\n")

    L.debug("Temporary HOME for git config: %s", home)

    with pytest.raises(IOError):
        pygit2.Config.get_system_config()

    global_cfg = pygit2.Config.get_global_config()
    assert global_cfg["user.email"] == USER_EMAIL
    assert global_cfg["user.name"] == USER_NAME

    return (USER_EMAIL, USER_NAME, home)


@contextlib.contextmanager
def chdir_(path):
    """ Context manager to change the current working directory """
    prev_cwd = os.getcwd()
    try:
        os.chdir(path)
        yield prev_cwd
    finally:
        os.chdir(prev_cwd)


@pytest.fixture
def chdir():
    return chdir_


def cleanup_dir(d):
    try:
        shutil.rmtree(d)
    except PermissionError as e:
        L.debug("Issue cleaning up temporary folder (%s): %s", d, e)


def get_archive_path(name):
    return Path(__file__).parent / "data" / f"{name}.tgz"


def extract_archive(name, extract_dir):
    archive_name = f"{name}.tgz"
    archive_path = get_archive_path(name)
    with tarfile.open(archive_path) as archive:
        archive.extractall(extract_dir)

    L.info("Extracted %s to %s", archive_name, extract_dir)

    # data archive should have a single dir at the top-level, matching the archive name.
    assert (
        len(os.listdir(extract_dir)) == 1
    ), f"Expected {name}/ as the only top-level item in {archive_name}"
    d = extract_dir / name
    assert d.is_dir(), f"Expected {name}/ as the only top-level item in {archive_name}"
    return d


@pytest.fixture
def data_archive(request, tmp_path_factory):
    """
    Extract a .tgz data archive to a temporary folder.

    When --preserve-data is passed on the command line, a failing test will have the folder kept.

    Context-manager produces the directory path and sets the current working directory.
    """

    @contextlib.contextmanager
    def ctx(name):
        extract_dir = tmp_path_factory.mktemp(request.node.name, numbered=True)
        cleanup = True
        try:
            d = extract_archive(name, extract_dir)
            with chdir_(d):
                try:
                    yield d
                finally:
                    if request.config.getoption("--preserve-data"):
                        L.info(
                            "Not cleaning up %s because --preserve-data was specified",
                            extract_dir,
                        )
                        cleanup = False
        finally:
            if cleanup:
                time.sleep(1)
                cleanup_dir(extract_dir)

    return ctx


_archive_hashes = {}


@pytest.fixture()
def data_archive_readonly(request, pytestconfig):
    """
    Extract a .tgz data archive to a temporary folder, and CACHE it in the pytest cache.
    Don't use this if you're going to write to the dir!
    If you don't need to write to the extracted archive, use this in preference to data_archive.

    Context-manager produces the directory path and sets the current working directory.
    """

    @contextlib.contextmanager
    def ctx(name):
        if name not in _archive_hashes:
            # Store extracted data in a content-addressed cache,
            # so if the archives change we don't have to manually `pytest --cache-clear`
            archive_path = get_archive_path(name)
            with archive_path.open("rb") as f:
                _archive_hashes[name] = hashlib.md5(f.read()).hexdigest()

        root = Path(request.config.cache.makedir("data_archive_readonly"))
        path = root / _archive_hashes[name]
        if path.exists():
            L.info("Found cache at %s", path)
        else:
            extract_archive(name, path)
        path /= name
        with chdir_(path):
            yield path

    yield ctx


@pytest.fixture
def data_working_copy(request, data_archive, tmp_path_factory, cli_runner):
    """
    Extract a repo archive with a working copy geopackage
    If the geopackage isn't in the archive, create it via `sno checkout`

    Context-manager produces a 2-tuple: (repository_path, working_copy_path)
    """
    from sno.structure import RepositoryStructure

    incr = 0

    @contextlib.contextmanager
    def _data_working_copy(name, force_new=False):
        nonlocal incr

        with data_archive(name) as repo_dir:
            if name.endswith(".sno"):
                name = name[:-5]

            repo = pygit2.Repository(str(repo_dir))
            rs = RepositoryStructure(repo)
            if rs.working_copy:
                wc_path = rs.working_copy.full_path
                if force_new:
                    L.info("force_new is set, deleting existing WC: %s", wc_path)
                    del rs.working_copy
                    assert not hasattr(rs, "_working_copy")
                    del wc_path

            if not rs.working_copy:
                wc_path = (
                    tmp_path_factory.mktemp(request.node.name, str(incr))
                    / f"{name}.gpkg"
                )
                incr += 1
                L.info("Creating working copy at %s", wc_path)
                r = cli_runner.invoke(["create-workingcopy", wc_path])
                assert r.exit_code == 0, r

            del rs
            del repo

            L.info("data_working_copy: %s %s", repo_dir, wc_path)
            yield repo_dir, wc_path

    return _data_working_copy


@pytest.fixture
def data_imported(cli_runner, data_archive, chdir, request, tmp_path_factory):
    """
    Extract a source geopackage archive, then import the table into a new repository.

    Caches it in the pytest cache, so don't use it for writeable things!

    Returns the path to the repository path
    """
    L = logging.getLogger("data_imported")
    incr = 0

    def _data_imported(archive, source_gpkg, table, repo_version=DEFAULT_REPO_VERSION):
        nonlocal incr

        params = [archive, source_gpkg, table, repo_version]
        cache_key = f"data_imported~{'~'.join(params)}"

        repo_path = Path(request.config.cache.makedir(cache_key)) / "data.sno"
        if repo_path.exists():
            L.info("Found cache at %s", repo_path)
            return str(repo_path)

        with data_archive(archive) as data:
            import_path = tmp_path_factory.mktemp(request.node.name, str(incr)) / "data"
            incr += 1

            import_path.mkdir()
            with chdir(import_path):
                r = cli_runner.invoke(["init", "--repo-version", repo_version])
                assert r.exit_code == 0, r

                repo = pygit2.Repository(str(import_path))
                assert repo.is_bare
                assert repo.is_empty
                repo.free()
                del repo

                r = cli_runner.invoke(
                    [
                        "import",
                        f"GPKG:{data / source_gpkg}",
                        f"{table}:mytable",
                    ]
                )
                assert r.exit_code == 0, r

            time.sleep(1)
            shutil.copytree(import_path, repo_path)
            L.info("Created cache at %s", repo_path)
            return str(repo_path)

    return _data_imported


@pytest.fixture
def geopackage():
    """ Return a SQLite3 (APSW) db connection for the specified DB, with spatialite loaded """

    def _geopackage(path, **kwargs):
        from sno import spatialite_path
        from sno.gpkg import Row

        db = apsw.Connection(str(path), **kwargs)
        db.setrowtrace(Row)
        dbcur = db.cursor()
        dbcur.execute("PRAGMA foreign_keys = ON;")
        db.config(apsw.SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION, 1)
        db.loadextension(spatialite_path)
        dbcur.execute("SELECT EnableGpkgMode();")
        return db

    return _geopackage


class SnoCliRunner(CliRunner):
    def __init__(self, *args, in_pdb=False, mix_stderr=False, **kwargs):
        self._in_pdb = in_pdb
        super().__init__(*args, mix_stderr=mix_stderr, **kwargs)

    def invoke(self, args=None, **kwargs):
        from sno.cli import cli

        if args:
            # force everything to strings (eg. PathLike objects, numbers)
            args = [str(a) for a in args]

        L.debug("Invoking Click command: %s (%s)", args, kwargs)

        params = {"catch_exceptions": not self._in_pdb}
        params.update(kwargs)

        r = super().invoke(cli, args=args, **params)

        L.debug("Command result: %s (%s)", r.exit_code, repr(r))
        L.debug("Command stdout=%s", r.stdout)
        L.debug("Command stderr=%s", (r.stderr if r.stderr_bytes else ""))

        if r.exception and not isinstance(r.exception, SystemExit):
            raise r.exception

        return r

    def isolation(self, input=None, env=None, color=False):
        if self._in_pdb:
            if input or env or color:
                L.warning("PDB un-isolation doesn't work if input/env/color are passed")
            else:
                return self.isolation_pdb(env=env)

        return super().isolation(input=input, env=env, color=color)

    @contextlib.contextmanager
    def isolation_pdb(self, env=None):
        s = io.BytesIO(b"{stdout not captured because --pdb-trace}")
        old_env = {}
        env = self.make_env(env)
        try:
            for key, value in env.items():
                old_env[key] = os.environ.get(key)
                if value is None:
                    try:
                        del os.environ[key]
                    except Exception:
                        pass
                else:
                    os.environ[key] = value
            yield (s, not self.mix_stderr and s)
        finally:
            for key, value in old_env.items():
                if value is None:
                    try:
                        del os.environ[key]
                    except Exception:
                        pass
                else:
                    os.environ[key] = value


@pytest.fixture
def cli_runner(request):
    """ A wrapper round Click's test CliRunner to improve usefulness """
    return SnoCliRunner(
        # sno.cli._execvp() looks for this env var to prevent fork/exec in tests.
        env={"_SNO_NO_EXEC": "1"},
        # workaround Click's environment isolation so debugging works.
        in_pdb=request.config.getoption("--pdb-trace"),
    )


@pytest.helpers.register
def helpers():
    return TestHelpers


class TestHelpers:

    # Test Dataset (gpkg-points / points)
    class POINTS:
        ARCHIVE = "points"
        LAYER = "nz_pa_points_topo_150k"
        LAYER_PK = "fid"
        INSERT = f"""
            INSERT INTO {LAYER}
                            (fid, geom, t50_fid, name_ascii, macronated, name)
                        VALUES
                            (:fid, GeomFromEWKT(:geom), :t50_fid, :name_ascii, :macronated, :name);
        """
        RECORD = {
            "fid": 9999,
            "geom": "POINT(0 0)",
            "t50_fid": 9_999_999,
            "name_ascii": "Te Motu-a-kore",
            "macronated": False,
            "name": "Te Motu-a-kore",
        }
        HEAD_SHA = "2a1b7be8bdef32aea1510668e3edccbc6d454852"
        HEAD1_SHA = "63a9492dd785b1f04dfc446330fa017f9459db4f"
        HEAD_TREE_SHA = "81f330e76b854e7448523307ff78bd6b83cadf21"
        HEAD1_TREE_SHA = "007ee9ab7f00b70a4199120cb274753c28e8ab92"
        ROWCOUNT = 2143
        TEXT_FIELD = "name"
        SAMPLE_PKS = list(range(1, 11))

    # Test Dataset (gpkg-polygons / polygons)
    class POLYGONS:
        ARCHIVE = "polygons"
        LAYER = "nz_waca_adjustments"
        LAYER_PK = "id"
        INSERT = f"""
            INSERT INTO {LAYER}
                            (id, geom, date_adjusted, survey_reference, adjusted_nodes)
                        VALUES
                            (:id, GeomFromEWKT(:geom), :date_adjusted, :survey_reference, :adjusted_nodes);
        """
        RECORD = {
            "id": 9_999_999,
            "geom": "POLYGON((0 0, 0 0.001, 0.001 0.001, 0.001 0, 0 0))",
            "date_adjusted": "2019-07-05T13:04:00+01:00",
            "survey_reference": "Null Island™ 🗺",
            "adjusted_nodes": 123,
        }
        HEAD_SHA = "1fb58eb54237c6e7bfcbd7ea65dc999a164b78ec"
        ROWCOUNT = 228
        TEXT_FIELD = "survey_reference"
        SAMPLE_PKS = [
            1424927,
            1443053,
            1452332,
            1456853,
            1456912,
            1457297,
            1457355,
            1457612,
            1457636,
            1458553,
        ]

    # Test Dataset (gpkg-spec / table)
    class TABLE:
        ARCHIVE = "table"
        LAYER = "countiestbl"
        LAYER_PK = "OBJECTID"
        INSERT = f"""
            INSERT INTO {LAYER}
                            (OBJECTID, NAME, STATE_NAME, STATE_FIPS, CNTY_FIPS, FIPS, AREA, POP1990, POP2000, POP90_SQMI, Shape_Leng, Shape_Area)
                        VALUES
                            (:OBJECTID, :NAME, :STATE_NAME, :STATE_FIPS, :CNTY_FIPS, :FIPS, :AREA, :POP1990, :POP2000, :POP90_SQMI, :Shape_Leng, :Shape_Area);
        """
        RECORD = {
            "OBJECTID": 9999,
            "NAME": "Lake of the Gruffalo",
            "STATE_NAME": "Minnesota",
            "STATE_FIPS": "27",
            "CNTY_FIPS": "077",
            "FIPS": "27077",
            "AREA": 1784.0634,
            "POP1990": 4076,
            "POP2000": 4651,
            "POP90_SQMI": 2,
            "Shape_Leng": 4.055_459_982_439_92,
            "Shape_Area": 0.565_449_933_741_451,
        }
        HEAD_SHA = "03622015ea5a82bc75228de052d9c84bc6f41667"
        ROWCOUNT = 3141
        TEXT_FIELD = "NAME"
        SAMPLE_PKS = list(range(1, 11))

    @classmethod
    def metadata(cls, l):
        metadatas = (cls.POINTS, cls.POLYGONS, cls.TABLE)
        return next((m for m in metadatas if m.LAYER == l or m.__name__ == l))

    @classmethod
    def last_change_time(cls, db, table=POINTS.LAYER):
        """
        Get the last change time from the GeoPackage DB.
        This is the same as the commit time.
        """
        return (
            db.cursor()
            .execute(
                f"SELECT last_change FROM gpkg_contents WHERE table_name=?;",
                [table],
            )
            .fetchone()[0]
        )

    @classmethod
    def row_count(cls, db, table):
        return db.cursor().execute(f'SELECT COUNT(*) FROM "{table}";').fetchone()[0]

    @classmethod
    def clear_working_copy(cls, repo_path="."):
        """ Delete any existing working copy & associated config """
        repo = pygit2.Repository(repo_path)
        if "sno.workingcopy.path" in repo.config:
            print(
                f"Deleting existing working copy: {repo.config['sno.workingcopy.path']}"
            )
            working_copy = Path(repo.config["sno.workingcopy.path"])
            if working_copy.exists():
                working_copy.unlink()
            del repo.config["sno.workingcopy.path"]
            del repo.config["sno.workingcopy.version"]

    @classmethod
    def db_table_hash(cls, db, table, pk=None):
        """ Calculate a SHA1 hash of the contents of a SQLite table """
        if pk is None:
            pk = "ROWID"

        sql = f"SELECT * FROM {table} ORDER BY {pk};"
        with db:
            cur = db.cursor()
            cur.execute(sql)
            r = cur.fetchall()
            h = hashlib.sha1()
            for row in r:
                h.update("🔸".join(repr(col) for col in row).encode("utf-8"))
            return h.hexdigest()

    @classmethod
    def git_graph(cls, request, message, count=10, *paths):
        """ Print a pretty graph of recent git revisions """
        cmd = [
            "git",
            "log",
            "--all",
            "--decorate",
            "--oneline",
            "--graph",
            f"--max-count={count}",
        ]

        # total hackery to figure out whether we're _actually_ in a terminal
        try:
            cm = request.config.pluginmanager.getplugin("capturemanager")
            fd = cm._global_capturing.in_.targetfd_save
            if os.isatty(fd):
                cmd += ["--color=always"]
        except Exception:
            pass

        print(f"{message}:")
        subprocess.check_call(cmd + list(paths))

    @classmethod
    def parameter_ids(cls, request):
        """ Get an array of parameter IDs """
        # nodeid = 'test_import_feature_performance[0.2.0-spec-counties-table]'
        param_ids = re.match(r".*\[(.+)\]$", request.node.nodeid).group(1).split("-")
        return tuple(param_ids)

    @classmethod
    def verify_gpkg_extent(cls, db, table):
        """ Check the aggregate layer extent from the table matches the values in gpkg_contents """
        dbcur = db.cursor()
        r = dbcur.execute(
            """SELECT column_name FROM "gpkg_geometry_columns" WHERE table_name=?;""",
            [table],
        ).fetchone()
        geom_col = r[0] if r else None

        gpkg_extent = tuple(
            dbcur.execute(
                """SELECT min_x,min_y,max_x,max_y FROM "gpkg_contents" WHERE table_name=?;""",
                [table],
            ).fetchone()
        )

        if geom_col:
            layer_extent = tuple(
                dbcur.execute(
                    f"""
                WITH _E AS (
                    SELECT extent("{geom_col}") AS extent
                    FROM "{table}"
                )
                SELECT
                    ST_MinX(extent),
                    ST_MinY(extent),
                    ST_MaxX(extent),
                    ST_MaxY(extent)
                FROM _E
                """
                ).fetchone()
            )
            assert gpkg_extent == pytest.approx(layer_extent)
        else:
            assert gpkg_extent == (None, None, None, None)


def _find_layer(db):
    H = pytest.helpers.helpers()
    return (
        db.cursor()
        .execute(
            "SELECT table_name FROM gpkg_contents WHERE table_name IN (?,?,?) LIMIT 1",
            [H.POINTS.LAYER, H.POLYGONS.LAYER, H.TABLE.LAYER],
        )
        .fetchone()[0]
    )


@pytest.fixture
def insert(request, cli_runner):
    H = pytest.helpers.helpers()

    def func(db, layer=None, commit=True, reset_index=None, insert_str=None):
        if reset_index is not None:
            func.index = reset_index

        layer = layer or _find_layer(db)

        metadata = H.metadata(layer)
        rec = metadata.RECORD.copy()
        pk_field = metadata.LAYER_PK
        sql = metadata.INSERT
        pk_start = 98000

        # th
        new_pk = pk_start + func.index
        rec[pk_field] = new_pk
        if insert_str:
            rec[metadata.TEXT_FIELD] = insert_str

        with db:
            cur = db.cursor()
            cur.execute(sql, rec)
            assert db.changes() == 1
            func.inserted_fids.append(new_pk)

        func.index += 1

        if commit:
            r = cli_runner.invoke(
                ["commit", "-m", f"commit-{func.index}", "-o", "json"]
            )
            assert r.exit_code == 0, r

            commit_id = json.loads(r.stdout)["sno.commit/v1"]["commit"]
            return commit_id
        else:
            return new_pk

    func.index = 0
    func.inserted_fids = []

    return func


@pytest.fixture
def update(request, cli_runner):
    H = pytest.helpers.helpers()

    def func(db, pk, update_str, layer=None, commit=True):
        layer = layer or _find_layer(db)
        metadata = H.metadata(layer)
        pk_field = metadata.LAYER_PK
        text_field = metadata.TEXT_FIELD

        with db:
            sql = f"""UPDATE {layer} SET {text_field} = :update_str WHERE {pk_field} = {pk}"""
            db.cursor().execute(sql, {"update_str": update_str})
            assert db.changes() == 1

        if commit:
            r = cli_runner.invoke(["commit", "-m", f"commit-update-{pk}", "-o", "json"])
            assert r.exit_code == 0, r

            commit_id = json.loads(r.stdout)["sno.commit/v1"]["commit"]
            return commit_id
        else:
            return pk

    return func


def _edit_points(dbcur):
    H = pytest.helpers.helpers()
    dbcur.execute(H.POINTS.INSERT, H.POINTS.RECORD)
    assert dbcur.getconnection().changes() == 1
    dbcur.execute(f"UPDATE {H.POINTS.LAYER} SET fid=9998 WHERE fid=1;")
    assert dbcur.getconnection().changes() == 1
    dbcur.execute(f"UPDATE {H.POINTS.LAYER} SET name='test' WHERE fid=2;")
    assert dbcur.getconnection().changes() == 1
    dbcur.execute(f"DELETE FROM {H.POINTS.LAYER} WHERE fid IN (3,30,31,32,33);")
    assert dbcur.getconnection().changes() == 5
    pk_del = 3
    return pk_del


@pytest.fixture
def edit_points():
    return _edit_points


def _edit_polygons(dbcur):
    H = pytest.helpers.helpers()
    dbcur.execute(H.POLYGONS.INSERT, H.POLYGONS.RECORD)
    assert dbcur.getconnection().changes() == 1
    dbcur.execute(f"UPDATE {H.POLYGONS.LAYER} SET id=9998 WHERE id=1424927;")
    assert dbcur.getconnection().changes() == 1
    dbcur.execute(
        f"UPDATE {H.POLYGONS.LAYER} SET survey_reference='test' WHERE id=1443053;"
    )
    assert dbcur.getconnection().changes() == 1
    dbcur.execute(
        f"DELETE FROM {H.POLYGONS.LAYER} WHERE id IN (1452332, 1456853, 1456912, 1457297, 1457355);"
    )
    assert dbcur.getconnection().changes() == 5
    pk_del = 1452332
    return pk_del


@pytest.fixture
def edit_polygons():
    return _edit_polygons


def _edit_table(dbcur):
    H = pytest.helpers.helpers()
    dbcur.execute(H.TABLE.INSERT, H.TABLE.RECORD)
    assert dbcur.getconnection().changes() == 1
    dbcur.execute(f"UPDATE {H.TABLE.LAYER} SET OBJECTID=9998 WHERE OBJECTID=1;")
    assert dbcur.getconnection().changes() == 1
    dbcur.execute(f"UPDATE {H.TABLE.LAYER} SET name='test' WHERE OBJECTID=2;")
    assert dbcur.getconnection().changes() == 1
    dbcur.execute(f"DELETE FROM {H.TABLE.LAYER} WHERE OBJECTID IN (3,30,31,32,33);")
    assert dbcur.getconnection().changes() == 5
    pk_del = 3
    return pk_del


@pytest.fixture
def edit_table():
    return _edit_table


@pytest.fixture
def create_conflicts(
    data_working_copy,
    geopackage,
    cli_runner,
    update,
    insert,
):
    @contextlib.contextmanager
    def ctx(data, repo_version=1):
        archive = f"{data.ARCHIVE}2" if repo_version == 2 else data.ARCHIVE
        with data_working_copy(archive) as (repo_path, wc):
            repo = pygit2.Repository(str(repo_path))
            sample_pks = data.SAMPLE_PKS

            cli_runner.invoke(["checkout", "-b", "ancestor_branch"])
            cli_runner.invoke(["checkout", "-b", "theirs_branch"])

            db = geopackage(wc)
            update(db, sample_pks[0], "theirs_version")
            update(db, sample_pks[1], "ours_theirs_version")
            update(db, sample_pks[2], "theirs_version")
            update(db, sample_pks[3], "theirs_version")
            update(db, sample_pks[4], "theirs_version")
            insert(db, reset_index=1, insert_str="insert_theirs")

            cli_runner.invoke(["checkout", "ancestor_branch"])
            cli_runner.invoke(["checkout", "-b", "ours_branch"])

            update(db, sample_pks[1], "ours_theirs_version")
            update(db, sample_pks[2], "ours_version")
            update(db, sample_pks[3], "ours_version")
            update(db, sample_pks[4], "ours_version")
            update(db, sample_pks[5], "ours_version")
            insert(db, reset_index=1, insert_str="insert_ours")

            yield repo

    return ctx


@pytest.fixture
def disable_editor():
    old_environ = dict(os.environ)
    os.environ["GIT_EDITOR"] = "echo"
    yield
    os.environ.clear()
    os.environ.update(old_environ)
