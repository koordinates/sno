import json
import pytest

from sno.sqlalchemy import gpkg_engine
from sno.exceptions import SUCCESS, INVALID_OPERATION, NO_CONFLICT
from sno.merge_util import (
    MergeIndex,
    CommitWithReference,
    MERGE_HEAD,
    MERGE_BRANCH,
    MERGE_MSG,
    ALL_MERGE_FILES,
)
from sno.repo import SnoRepo, SnoRepoState


H = pytest.helpers.helpers()


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(
            H.POINTS,
            id="points",
        ),
        pytest.param(
            H.POLYGONS,
            id="polygons",
        ),
        pytest.param(H.TABLE, id="table"),
    ],
)
def test_merge_fastforward(data, data_working_copy, cli_runner, insert, request):
    with data_working_copy(data.ARCHIVE) as (repo_path, wc):
        repo = SnoRepo(repo_path)
        # new branch
        r = cli_runner.invoke(["checkout", "-b", "changes"])
        assert r.exit_code == 0, r
        assert repo.head.name == "refs/heads/changes"

        h = repo.head.target.hex

        # make some changes
        with gpkg_engine(wc).connect() as db:
            insert(db)
            insert(db)
            commit_id = insert(db)

        H.git_graph(request, "pre-merge")
        assert repo.head.target.hex == commit_id

        r = cli_runner.invoke(["checkout", "master"])
        assert r.exit_code == 0, r
        assert repo.head.target.hex != commit_id

        r = cli_runner.invoke(["merge", "--ff-only", "changes"])
        assert r.exit_code == 0, r

        H.git_graph(request, "post-merge")

        assert repo.head.name == "refs/heads/master"
        assert repo.head.target.hex == commit_id
        c = repo.head_commit
        assert len(c.parents) == 1
        assert c.parents[0].parents[0].parents[0].hex == h


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(
            H.POINTS,
            id="points",
        ),
        pytest.param(
            H.POLYGONS,
            id="polygons",
        ),
        pytest.param(H.TABLE, id="table"),
    ],
)
def test_merge_fastforward_noff(
    data,
    data_working_copy,
    cli_runner,
    insert,
    request,
    disable_editor,
):
    with data_working_copy(data.ARCHIVE) as (repo_path, wc):
        repo = SnoRepo(repo_path)
        # new branch
        r = cli_runner.invoke(["checkout", "-b", "changes"])
        assert r.exit_code == 0, r
        assert repo.head.name == "refs/heads/changes"

        h = repo.head.target.hex

        # make some changes
        with gpkg_engine(wc).connect() as db:
            insert(db)
            insert(db)
            commit_id = insert(db)

        H.git_graph(request, "pre-merge")
        assert repo.head.target.hex == commit_id

        r = cli_runner.invoke(["checkout", "master"])
        assert r.exit_code == 0, r
        assert repo.head.target.hex != commit_id

        # force creation of a merge commit
        r = cli_runner.invoke(["merge", "changes", "--no-ff", "-o", "json"])
        assert r.exit_code == 0, r

        H.git_graph(request, "post-merge")

        merge_commit_id = json.loads(r.stdout)["sno.merge/v1"]["commit"]

        assert repo.head.name == "refs/heads/master"
        assert repo.head.target.hex == merge_commit_id
        c = repo.head_commit
        assert len(c.parents) == 2
        assert c.parents[0].hex == h
        assert c.parents[1].hex == commit_id
        assert c.message == 'Merge branch "changes" into master'


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(
            H.POINTS,
            id="points",
        ),
        pytest.param(
            H.POLYGONS,
            id="polygons",
        ),
        pytest.param(H.TABLE, id="table"),
    ],
)
def test_merge_true(
    data,
    data_working_copy,
    cli_runner,
    insert,
    request,
    disable_editor,
):
    with data_working_copy(data.ARCHIVE) as (repo_path, wc_path):
        repo = SnoRepo(repo_path)
        wc = repo.working_copy
        # new branch
        r = cli_runner.invoke(["checkout", "-b", "changes"])
        assert r.exit_code == 0, r
        assert repo.head.name == "refs/heads/changes"

        h = repo.head.target.hex

        # make some changes
        with wc.session() as db:
            insert(db)
            insert(db)
            b_commit_id = insert(db)
            assert repo.head.target.hex == b_commit_id

        r = cli_runner.invoke(["checkout", "master"])
        assert r.exit_code == 0, r
        assert repo.head.target.hex != b_commit_id

        with wc.session() as db:
            m_commit_id = insert(db)
        H.git_graph(request, "pre-merge-master")

        # fastforward merge should fail
        r = cli_runner.invoke(["merge", "--ff-only", "changes"])
        assert r.exit_code == INVALID_OPERATION, r
        assert (
            "Can't resolve as a fast-forward merge and --ff-only specified" in r.stderr
        )

        r = cli_runner.invoke(["merge", "changes", "--ff", "-o", "json"])
        assert r.exit_code == 0, r
        H.git_graph(request, "post-merge")

        merge_commit_id = json.loads(r.stdout)["sno.merge/v1"]["commit"]

        assert repo.head.name == "refs/heads/master"
        assert repo.head.target.hex == merge_commit_id
        c = repo.head_commit
        assert len(c.parents) == 2
        assert c.parents[0].hex == m_commit_id
        assert c.parents[1].hex == b_commit_id
        assert c.parents[0].parents[0].hex == h
        assert c.message == 'Merge branch "changes" into master'

        # check the database state
        num_inserts = len(insert.inserted_fids)

        with wc.session() as db:
            rowcount = 0
            for pk in insert.inserted_fids:
                rowcount += db.execute(
                    f"SELECT COUNT(*) FROM {data.LAYER} WHERE {data.LAYER_PK} = :pk",
                    {"pk": pk},
                ).fetchone()[0]
            assert rowcount == num_inserts


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(
            H.POINTS,
            id="points",
        ),
        pytest.param(
            H.POLYGONS,
            id="polygons",
        ),
        pytest.param(H.TABLE, id="table"),
    ],
)
@pytest.mark.parametrize(
    "output_format",
    ["text", "json"],
)
@pytest.mark.parametrize(
    "dry_run",
    [pytest.param(False, id=""), pytest.param(True, id="dryrun")],
)
def test_merge_conflicts(
    data,
    output_format,
    dry_run,
    create_conflicts,
    cli_runner,
):
    with create_conflicts(data) as repo:
        ancestor = CommitWithReference.resolve(repo, "ancestor_branch")
        ours = CommitWithReference.resolve(repo, "ours_branch")
        theirs = CommitWithReference.resolve(repo, "theirs_branch")

        cmd = ["merge", "theirs_branch", f"--output-format={output_format}"]
        if dry_run:
            cmd += ["--dry-run"]

        r = cli_runner.invoke(cmd)
        assert r.exit_code == 0, r

        if output_format == "text":
            merging_state_message = (
                ["(Not actually merging due to --dry-run)", ""]
                if dry_run
                else [
                    'Repository is now in "merging" state.',
                    "View conflicts with `sno conflicts` and resolve them with `sno resolve`.",
                    "Once no conflicts remain, complete this merge with `sno merge --continue`.",
                    "Or use `sno merge --abort` to return to the previous state.",
                    "",
                ]
            )

            assert (
                r.stdout.split("\n")
                == [
                    'Merging branch "theirs_branch" into ours_branch',
                    "Conflicts found:",
                    "",
                    f"{data.LAYER}:",
                    f"    {data.LAYER}:feature: 4 conflicts",
                    "",
                ]
                + merging_state_message
            )

        else:
            jdict = json.loads(r.stdout)
            assert jdict == {
                "sno.merge/v1": {
                    "branch": "ours_branch",
                    "commit": ours.id.hex,
                    "merging": {
                        "ancestor": {
                            "commit": ancestor.id.hex,
                            "abbrevCommit": ancestor.short_id,
                        },
                        "ours": {
                            "branch": "ours_branch",
                            "commit": ours.id.hex,
                            "abbrevCommit": ours.short_id,
                        },
                        "theirs": {
                            "branch": "theirs_branch",
                            "commit": theirs.id.hex,
                            "abbrevCommit": theirs.short_id,
                        },
                    },
                    "dryRun": dry_run,
                    "message": 'Merge branch "theirs_branch" into ours_branch',
                    "conflicts": {data.LAYER: {"feature": 4}},
                    "state": "merging",
                },
            }

        if not dry_run:
            assert repo.read_gitdir_file(MERGE_HEAD).strip() == theirs.id.hex
            assert repo.read_gitdir_file(MERGE_BRANCH).strip() == "theirs_branch"
            assert (
                repo.read_gitdir_file(MERGE_MSG)
                == 'Merge branch "theirs_branch" into ours_branch\n'
            )

            merge_index = MergeIndex.read_from_repo(repo)
            assert len(merge_index.conflicts) == 4
            cli_runner.invoke(["merge", "--abort"])

        for filename in ALL_MERGE_FILES:
            assert not repo.gitdir_file(filename).exists()


def test_merge_state_lock(create_conflicts, cli_runner):
    with create_conflicts(H.POINTS) as repo:
        # Repo state: normal
        # sno checkout works, but sno conflicts and sno resolve do not.
        assert repo.state == SnoRepoState.NORMAL

        r = cli_runner.invoke(["checkout", "ours_branch"])
        assert r.exit_code == SUCCESS
        r = cli_runner.invoke(["conflicts"])
        assert r.exit_code == INVALID_OPERATION
        r = cli_runner.invoke(["resolve", "dummy_conflict", "--with=delete"])
        assert r.exit_code == INVALID_OPERATION

        r = cli_runner.invoke(["merge", "theirs_branch"])
        assert r.exit_code == SUCCESS

        # Repo state: merging
        assert repo.state == SnoRepoState.MERGING

        # sno checkout is locked, but sno conflicts and sno resolve work.
        r = cli_runner.invoke(["checkout", "ours_branch"])
        assert r.exit_code == INVALID_OPERATION
        r = cli_runner.invoke(["conflicts"])
        assert r.exit_code == SUCCESS
        r = cli_runner.invoke(["resolve", "dummy_conflict", "--with=delete"])
        assert r.exit_code == NO_CONFLICT  # "dummy_conflict" is not a real conflict
