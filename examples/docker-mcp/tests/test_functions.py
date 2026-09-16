"""Direct handler and normalization tests; protocol round trips are tested separately."""

from __future__ import annotations

import pytest
from aiodocker.exceptions import DockerError
from fake_docker import stamp
from fake_exchange import FakeExchange, FakeSession

from docker_mcp import tools
from docker_mcp.consent import Policy, only_reads
from docker_mcp.errors import Conflict, NotFound, NotRunning, Timeout, Trouble, Unreachable, clear
from docker_mcp.models import (
    ContainerFilter,
    ContainerRef,
    CreateRequest,
    EventFilter,
    ExecRequest,
    ImageFilter,
    ImageRef,
    LogRequest,
    NameFilter,
    Nothing,
    PruneRequest,
    PullRequest,
    ReadRequest,
    RemoveRequest,
    StopRequest,
    WaitRequest,
    WriteRequest,
)

# Each reshaping helper lives with the one tool that needs it, which is where
# a reader of that tool will look for it.
from docker_mcp.tools.container import detail
from docker_mcp.tools.containers import summary
from docker_mcp.tools.images import image_of
from docker_mcp.tools.logs import as_epoch

pytestmark = pytest.mark.asyncio

DEFAULT = Policy()

#: Force confirmation for actions exempt under the default policy.
CHANGES = Policy("changes")

NEVER = Policy("never")


async def test_a_list_entry_becomes_a_short_row():
    row = summary(
        {
            "Id": "abcdef0123456789",
            "Names": ["/cache"],
            "Image": "redis:7",
            "State": "running",
            "Status": "Up 3 hours",
            "Ports": [{"IP": "0.0.0.0", "PublicPort": 6379, "PrivatePort": 6379, "Type": "tcp"}],
        }
    )
    assert row.id == "abcdef012345"
    assert row.name == "cache"
    assert row.ports == ["0.0.0.0:6379->6379/tcp"]


async def test_a_port_with_no_public_side_is_still_reported():
    row = summary({"Id": "x", "Ports": [{"PrivatePort": 5432, "Type": "tcp"}]})
    assert row.ports == ["5432/tcp"]


async def test_an_exit_code_is_reported_only_once_it_has_stopped():
    running = detail(
        {"Id": "x", "Name": "/a", "State": {"Status": "running", "ExitCode": 0}, "Config": {}}
    )
    stopped = detail(
        {"Id": "x", "Name": "/a", "State": {"Status": "exited", "ExitCode": 0}, "Config": {}}
    )
    assert running.exit_code is None
    assert stopped.exit_code == 0


async def test_environment_values_never_leave_the_reshaping():
    reshaped = detail(
        {
            "Id": "x",
            "Name": "/a",
            "State": {"Status": "running"},
            "Config": {"Env": ["TOKEN=secret", "PATH=/usr/bin"]},
        }
    )
    assert reshaped.environment == ["PATH", "TOKEN"]
    assert "secret" not in reshaped.model_dump_json()


async def test_an_untagged_image_reports_no_tags():
    reshaped = image_of(
        {"Id": "sha256:abcdef0123456789", "RepoTags": ["<none>:<none>"], "Size": 1_500_000}
    )
    assert reshaped.tags == []
    assert reshaped.id == "abcdef012345"
    assert reshaped.size_mb == 1.5


async def test_listing_filters_by_name(client):
    listed = await tools.containers(ContainerFilter(all=True, name="work"), client)
    assert [item.name for item in listed.containers] == ["worker"]
    assert listed.total == 1


async def test_the_limit_bounds_what_is_returned_but_not_what_matched(client):
    listed = await tools.containers(ContainerFilter(all=True, limit=1), client)
    assert listed.shown == 1
    assert listed.total == 2


async def test_a_name_that_matches_nothing_is_said_plainly(client):
    with pytest.raises(NotFound, match="no container matches"):
        await tools.container(ContainerRef(container="absent"), client)


async def test_a_partial_name_that_matches_several_is_refused(client, fake):
    """Partial-name resolution is server-side; ambiguous matches must not choose a container."""
    for suffix in ("1", "2"):
        identity = f"aaaaaaaaaaaa{suffix}"
        fake.containers[identity] = dict(
            fake.containers["1111111111112222222222"], Id=identity, Names=[f"/web-{suffix}"]
        )
    with pytest.raises(NotFound, match="matches several"):
        await tools.container(ContainerRef(container="web"), client)

    # One of them by its exact name still resolves.
    found = await tools.container(ContainerRef(container="web-1"), client)
    assert found.name == "web-1"


async def test_logs_are_read_from_the_end(client):
    written = await tools.logs(LogRequest(container="worker", lines=1), client)
    assert written.lines == ["2026-09-13T10:00:02.000000000Z err ValueError: no such queue"]
    assert written.truncated is True, "the line before it was left out and nothing said so"


async def test_a_whole_log_is_not_reported_as_truncated(client):
    written = await tools.logs(LogRequest(container="worker", lines=100), client)
    assert written.shown == 2
    assert written.truncated is False


async def test_one_stream_still_returns_what_was_asked_for(client, fake):
    """The daemon counts `tail` over both streams and filters afterwards, so
    asking it for six lines of stderr returns about three. Asking for more and
    trimming is the only lever the API gives."""
    fake.logs["1111111111112222222222"] = [
        (stamp(second), "stdout" if second % 2 else "stderr", f"line {second}")
        for second in range(1, 21)
    ]
    written = await tools.logs(LogRequest(container="cache", stream="stderr", lines=6), client)
    assert written.shown == 6
    assert all("line" in line for line in written.lines)


async def test_images_are_filtered_by_reference(client):
    listed = await tools.images(ImageFilter(reference="redis*"), client)
    assert [item.tags for item in listed.images] == [["redis:7"]]


async def test_info_counts_what_is_running(client):
    reported = await tools.info(Nothing(), client)
    assert (reported.containers_running, reported.containers_total) == (1, 2)


async def test_stopping_reports_the_state_it_reached(client, fake):
    done = await tools.stop(StopRequest(container="cache"), client, FakeExchange())
    assert done.state == "exited"
    assert fake.stopped == ["1111111111112222222222"]


async def test_removing_asks_before_it_removes(client, fake):
    ex = FakeExchange(answers={"confirm": {"confirmed": True}})
    result = await tools.remove(RemoveRequest(container="worker"), client, ex, DEFAULT)
    assert "Remove container worker?" in ex.asked[0]
    assert result == "removed worker"
    assert fake.removed == ["3333333333334444444444"]


async def test_a_refusal_leaves_it_alone(client, fake):
    ex = FakeExchange(action="decline")
    result = await tools.remove(RemoveRequest(container="worker"), client, ex, DEFAULT)
    assert result == "not removed: decline"
    assert fake.removed == []


async def test_an_accepted_form_that_says_no_leaves_it_alone(client, fake):
    ex = FakeExchange(answers={"confirm": {"confirmed": False}})
    result = await tools.remove(RemoveRequest(container="worker"), client, ex, DEFAULT)
    assert result.startswith("not removed")
    assert fake.removed == []


async def test_a_command_that_was_refused_never_runs(client, fake):
    ex = FakeExchange(action="cancel")
    result = await tools.exec(
        ExecRequest(container="cache", command=["rm", "-rf", "/"]), client, ex, DEFAULT
    )
    assert result.exit_code == -1
    assert result.stderr == "not run: cancel"
    assert fake.executed == []


async def test_pulling_reports_every_layer(client, fake):
    ex = FakeExchange()
    result = await tools.pull(PullRequest(image="redis:7"), client, ex)
    assert result == "pulled redis:7 (3 layers)"
    assert fake.pulled == ["redis:7"]
    assert [level for level, _ in ex.logged] == ["info", "info"]
    assert ex.progress_reports, "a pull that says nothing looks like a hang"


async def test_an_image_time_is_readable_rather_than_an_epoch():
    reshaped = image_of({"Id": "sha256:abc", "RepoTags": [], "Created": 1776383604})
    assert reshaped.created.startswith("2026-")


async def test_a_container_time_is_left_as_it_came():
    reshaped = detail(
        {
            "Id": "x",
            "Name": "/a",
            "Created": "2026-09-13T20:00:46.554757481Z",
            "State": {"Status": "running"},
            "Config": {},
        }
    )
    assert reshaped.created == "2026-09-13T20:00:46.554757481Z"


async def test_a_time_that_never_happened_is_absent_rather_than_zero():
    """Docker uses 0001-01-01 to mean never."""
    reshaped = detail(
        {
            "Id": "x",
            "Name": "/a",
            "State": {
                "Status": "running",
                "StartedAt": "2026-09-13T20:00:47Z",
                "FinishedAt": "0001-01-01T00:00:00Z",
            },
            "Config": {},
        }
    )
    assert reshaped.started == "2026-09-13T20:00:47Z"
    assert reshaped.finished is None


async def test_a_command_keeps_what_was_one_argument():
    """Embedded spaces must preserve argument boundaries."""
    reshaped = detail(
        {
            "Id": "x",
            "Name": "/a",
            "State": {"Status": "running"},
            "Config": {"Cmd": ["sh", "-c", "echo started; sleep 300"]},
        }
    )
    assert reshaped.command == "sh -c 'echo started; sleep 300'"


async def test_an_entrypoint_is_part_of_the_command():
    reshaped = detail(
        {
            "Id": "x",
            "Name": "/a",
            "State": {"Status": "running"},
            "Config": {"Entrypoint": ["/bin/tini", "--"], "Cmd": ["nginx"]},
        }
    )
    assert reshaped.command == "/bin/tini -- nginx"


async def test_the_detail_does_not_repeat_the_state_under_another_name():
    """Docker inspect has no counterpart to the list's human-readable status."""
    reshaped = detail({"Id": "x", "Name": "/a", "State": {"Status": "running"}, "Config": {}})
    assert not hasattr(reshaped, "status")


async def test_a_reading_command_is_told_apart_from_a_writing_one():
    """Allowlisted commands with side-effect arguments still require confirmation."""
    assert only_reads(["ls", "-la", "/"])
    assert only_reads(["/bin/cat", "/etc/hosts"])
    assert only_reads(["find", "/app", "-name", "*.log"])
    assert not only_reads(["sh", "-c", "echo hi"])
    assert not only_reads(["find", "/app", "-delete"])
    assert not only_reads(["rm", "-rf", "/"])
    assert not only_reads([])


async def test_a_reading_command_runs_without_asking(client, fake):
    ex = FakeExchange(action="decline")
    result = await tools.exec(
        ExecRequest(container="cache", command=["ls", "/"]), client, ex, DEFAULT
    )
    assert ex.asked == [], "a command that changes nothing must not interrupt anybody"
    assert result.stdout == "bin\nboot\netc\n"
    assert result.exit_code == 0


async def test_a_permission_given_once_is_not_asked_for_twice(client, fake):
    session = FakeSession()
    ex = FakeExchange(answers={"confirm": {"confirmed": True, "remember": "this"}}, session=session)
    await tools.write(
        WriteRequest(container="cache", path="/tmp/a", content="one"), client, ex, CHANGES
    )
    await tools.write(
        WriteRequest(container="cache", path="/tmp/b", content="two"), client, ex, CHANGES
    )
    assert len(ex.asked) == 1
    assert session.values["consent"] == ["write:cache"]
    assert [path for _, path, _ in fake.written] == ["/tmp/a", "/tmp/b"]


async def test_a_permission_for_anything_covers_another_container(client, fake):
    session = FakeSession()
    ex = FakeExchange(answers={"confirm": {"confirmed": True, "remember": "any"}}, session=session)
    await tools.write(
        WriteRequest(container="cache", path="/tmp/a", content="one"), client, ex, CHANGES
    )
    await tools.write(
        WriteRequest(container="worker", path="/tmp/b", content="two"), client, ex, CHANGES
    )
    assert len(ex.asked) == 1
    assert session.values["consent"] == ["write:*"]


async def test_an_answer_that_says_once_is_asked_again(client, fake):
    session = FakeSession()
    ex = FakeExchange(answers={"confirm": {"confirmed": True}}, session=session)
    await tools.write(
        WriteRequest(container="cache", path="/tmp/a", content="one"), client, ex, CHANGES
    )
    await tools.write(
        WriteRequest(container="cache", path="/tmp/b", content="two"), client, ex, CHANGES
    )
    assert len(ex.asked) == 2
    assert "consent" not in session.values


async def test_a_permission_is_not_kept_where_there_is_no_session(client, fake):
    """Do not retain permissions without a caller to associate them with."""
    ex = FakeExchange(answers={"confirm": {"confirmed": True, "remember": "any"}})
    await tools.write(
        WriteRequest(container="cache", path="/tmp/a", content="one"), client, ex, CHANGES
    )
    await tools.write(
        WriteRequest(container="cache", path="/tmp/b", content="two"), client, ex, CHANGES
    )
    assert len(ex.asked) == 2


async def test_the_two_streams_are_reported_apart(client, fake):
    fake.exec_stdout = b"out\n"
    fake.exec_stderr = b"err\n"
    fake.exec_exit = 7
    result = await tools.exec(
        ExecRequest(container="cache", command=["ls"]), client, FakeExchange(), DEFAULT
    )
    assert (result.stdout, result.stderr, result.exit_code) == ("out\n", "err\n", 7)


async def test_a_command_needs_a_running_container(client, fake):
    """Translate Docker's 409 into an actionable stopped-container error."""
    with pytest.raises(Conflict, match="Start it first"):
        await tools.exec(
            ExecRequest(container="worker", command=["ls"]), client, FakeExchange(), DEFAULT
        )


async def test_a_command_that_outlasts_its_time_says_so(client, fake):
    fake.exec_delay = 1.5
    result = await tools.exec(
        ExecRequest(container="cache", command=["ls"], seconds=1), client, FakeExchange(), DEFAULT
    )
    assert result.timed_out is True
    assert result.exit_code == -1


async def test_a_command_is_given_its_directory_and_its_user(client, fake):
    await tools.exec(
        ExecRequest(container="cache", command=["ls"], workdir="/app", user="redis"),
        client,
        FakeExchange(),
        DEFAULT,
    )
    assert fake.executed[0]["WorkingDir"] == "/app"
    assert fake.executed[0]["User"] == "redis"


async def test_a_file_is_read_out_of_a_container(client):
    found = await tools.read(ReadRequest(container="cache", path="/etc/redis.conf"), client)
    assert found.text == "maxmemory 256mb\n"
    assert found.binary is False


async def test_a_file_is_read_out_of_a_stopped_container(client):
    """The archive API works on stopped containers, unlike exec."""
    found = await tools.read(ReadRequest(container="worker", path="/app/settings.ini"), client)
    assert "name = missing" in found.text


async def test_a_file_that_is_not_there_is_said_plainly(client):
    with pytest.raises(NotFound, match="/nowhere was not found"):
        await tools.read(ReadRequest(container="cache", path="/nowhere"), client)


async def test_a_directory_is_refused_rather_than_guessed_at(client, fake):
    """The daemon answers a directory with an archive of everything inside it.
    Reading the first file out of that returns one file's contents under the
    name of the directory, which is a wrong answer that looks like a right
    one."""
    fake.files["1111111111112222222222"]["/etc/hosts"] = b"127.0.0.1 localhost\n"
    with pytest.raises(Conflict, match="/etc is a directory"):
        await tools.read(ReadRequest(container="cache", path="/etc"), client)


async def test_a_full_container_id_never_reaches_the_caller():
    """The daemon names the container in sixty-four hex characters nobody
    typed. Its own output uses twelve."""
    long = "3156cbe0b5f620dbd8b3f136194b9d414aecf44e1d0272786ff61e671f2792df"
    with pytest.raises(Trouble) as raised:
        async with clear("reading the file"):
            raise DockerError(500, f"Could not find /x in container {long}")
    assert long not in str(raised.value)
    assert "3156cbe0b5f6" in str(raised.value)


async def test_writing_asks_and_then_writes(client, fake):
    ex = FakeExchange(answers={"confirm": {"confirmed": True}})
    written = await tools.write(
        WriteRequest(container="cache", path="/etc/redis.conf", content="maxmemory 1gb\n"),
        client,
        ex,
        CHANGES,
    )
    assert "Write 14 bytes to /etc/redis.conf" in ex.asked[0]
    assert written.size_bytes == 14
    assert fake.files["1111111111112222222222"]["/etc/redis.conf"] == b"maxmemory 1gb\n"
    assert "restart it" in (written.note or "")


async def test_a_refused_write_changes_nothing(client, fake):
    ex = FakeExchange(action="decline")
    with pytest.raises(Conflict, match="not written"):
        await tools.write(
            WriteRequest(container="cache", path="/etc/redis.conf", content="x"),
            client,
            ex,
            CHANGES,
        )
    assert fake.written == []


async def test_a_cursor_is_turned_into_what_the_daemon_reads():
    """Convert RFC 3339 to Unix seconds without losing subsecond precision."""
    assert as_epoch("2026-09-13T20:30:42.897161158Z").endswith(".897161158")
    assert as_epoch("1757795442.5") == "1757795442.5"
    assert "." not in as_epoch("2026-09-13T20:30:42Z")


async def test_a_cursor_reads_only_what_is_new(client):
    first = await tools.logs(LogRequest(container="cache"), client)
    assert first.cursor == "2026-09-13T10:00:02.000000000Z"
    again = await tools.logs(LogRequest(container="cache", since=first.cursor), client)
    assert again.lines == [], "the line the cursor came from must not arrive twice"


async def test_one_stream_alone_is_not_tagged(client):
    written = await tools.logs(LogRequest(container="worker", stream="stderr"), client)
    assert written.lines == ["2026-09-13T10:00:02.000000000Z ValueError: no such queue"]


async def test_a_noisy_log_is_narrowed_rather_than_widened(client):
    written = await tools.logs(LogRequest(container="cache", contains="DISK"), client)
    assert [line.split(" ", 2)[-1] for line in written.lines] == ["saving to disk"]


async def test_both_streams_are_merged_in_the_order_they_were_written(client):
    written = await tools.logs(LogRequest(container="worker"), client)
    assert [line.split(" ")[1] for line in written.lines] == ["out", "err"]


async def test_an_image_reports_what_still_holds_it(client):
    listed = await tools.images(ImageFilter(), client)
    held = {tuple(item.tags): item.used_by for item in listed.images}
    assert held[("redis:7",)] == ["cache"]
    assert held[("postgres:16",)] == []


async def test_only_the_images_nothing_uses_are_worth_reclaiming(client):
    listed = await tools.images(ImageFilter(unused=True), client)
    assert [item.tags for item in listed.images] == [["postgres:16"]]
    assert listed.reclaimable_mb == 420.0


async def test_a_volume_reports_what_mounts_it(client):
    listed = await tools.volumes(NameFilter(), client)
    mounted = {item.name: item.used_by for item in listed.volumes}
    assert mounted == {"cache-data": ["cache"], "orphan-data": []}


async def test_a_network_reports_its_subnet_and_what_is_attached(client):
    """The daemon does not say what is attached: `GET /networks` leaves
    `Containers` out, and only inspecting one network reports it. So the
    answer is built from the containers, each of which names its networks."""
    listed = await tools.networks(NameFilter(), client)
    attached = {item.name: item.containers for item in listed.networks}
    assert attached == {"bridge": ["cache"], "shop_default": ["worker"]}
    assert [item.subnets for item in listed.networks if item.name == "bridge"] == [
        ["172.17.0.0/16"]
    ]


async def test_events_say_what_happened_before_now(client):
    listed = await tools.events(EventFilter(), client)
    assert [(item.action, item.subject) for item in listed.events] == [
        ("die", "worker"),
        ("start", "cache"),
    ]
    assert listed.events[0].exit_code == 1


async def test_events_are_narrowed_to_one_container(client):
    listed = await tools.events(EventFilter(container="worker"), client)
    assert [item.subject for item in listed.events] == ["worker"]


async def test_counters_become_a_rate_rather_than_a_total(client):
    """Rates require two cumulative counter samples."""
    reported = await tools.stats(ContainerRef(container="cache"), client)
    assert reported.cpu_percent == 40.0
    assert (reported.memory_mb, reported.memory_percent) == (52.0, 20.0)
    assert reported.pids == 7


async def test_the_daemon_says_which_daemon_it_is(client):
    reported = await tools.info(Nothing(), client)
    assert reported.endpoint.startswith("http://127.0.0.1")


async def test_a_compose_service_is_named_as_one(client):
    found = await tools.container(ContainerRef(container="cache"), client)
    assert found.compose == "shop/cache"
    assert found.restart_policy == "unless-stopped"


async def test_creating_builds_what_the_daemon_takes(client, fake):
    ex = FakeExchange(answers={"confirm": {"confirmed": True}})
    made = await tools.create(
        CreateRequest(
            image="redis:7",
            name="fresh",
            ports=["8080:80"],
            volumes=["/data:/var/lib/redis:ro"],
            environment={"MODE": "test"},
            restart="always",
        ),
        client,
        ex,
        NEVER,
    )
    config = fake.created[0]["config"]
    assert config["ExposedPorts"] == {"80/tcp": {}}
    assert config["HostConfig"]["PortBindings"]["80/tcp"] == [{"HostIp": "", "HostPort": "8080"}]
    assert config["HostConfig"]["Binds"] == ["/data:/var/lib/redis:ro"]
    assert config["HostConfig"]["RestartPolicy"] == {"Name": "always"}
    assert config["Env"] == ["MODE=test"]
    assert made.state == "running"


async def test_creating_names_what_reaches_the_host(client, fake):
    ex = FakeExchange(answers={"confirm": {"confirmed": True}})
    await tools.create(
        CreateRequest(image="redis:7", ports=["8080:80"], volumes=["/data:/data"]),
        client,
        ex,
        CHANGES,
    )
    assert "publishing 8080:80" in ex.asked[0]
    assert "mounting /data:/data" in ex.asked[0]


async def test_a_port_nobody_can_read_is_refused_by_name(client, fake):
    ex = FakeExchange(answers={"confirm": {"confirmed": True}})
    with pytest.raises(ValueError, match="cannot read port"):
        await tools.create(CreateRequest(image="redis:7", ports=["eighty"]), client, ex, NEVER)


async def test_an_image_that_is_here_is_not_downloaded_again(client, fake):
    ex = FakeExchange(answers={"confirm": {"confirmed": True}})
    await tools.create(CreateRequest(image="redis:7"), client, ex, NEVER)
    assert fake.pulled == []


async def test_an_image_that_is_missing_is_downloaded_first(client, fake):
    ex = FakeExchange(answers={"confirm": {"confirmed": True}})
    await tools.create(CreateRequest(image="nginx:1.27"), client, ex, NEVER)
    assert fake.pulled == ["nginx:1.27"]


async def test_removing_an_image_asks_first(client, fake):
    ex = FakeExchange(answers={"confirm": {"confirmed": True}})
    result = await tools.remove_image(ImageRef(image="postgres:16"), client, ex, DEFAULT)
    assert "Remove image postgres:16?" in ex.asked[0]
    assert "removed postgres:16" in result
    assert fake.removed_images == ["postgres:16"]


async def test_pruning_says_what_went_and_what_it_freed(client, fake):
    ex = FakeExchange(answers={"confirm": {"confirmed": True}})
    reclaimed = await tools.prune(PruneRequest(what="volumes"), client, ex, DEFAULT)
    assert reclaimed.removed == ["orphan-data"]
    assert reclaimed.reclaimed_mb == 12.0
    assert fake.pruned == ["volumes"]


async def test_a_refused_prune_removes_nothing(client, fake):
    ex = FakeExchange(action="decline")
    with pytest.raises(Conflict, match="nothing removed"):
        await tools.prune(PruneRequest(what="images"), client, ex, DEFAULT)
    assert fake.pruned == []


async def test_a_container_that_starts_and_dies_is_not_reported_as_started():
    """Docker start returns before the process settles; inspect the later state and exit code."""
    done = tools.outcome("web", "start", {"Status": "exited", "ExitCode": 3}, "running")
    assert done.exit_code == 3
    assert "did not stay running" in (done.note or "")
    assert "Read `logs`" in (done.note or "")


async def test_a_container_that_runs_but_is_unhealthy_says_so():
    done = tools.outcome(
        "web", "start", {"Status": "running", "Health": {"Status": "unhealthy"}}, "running"
    )
    assert done.note == "it runs, but its own health check says it is unhealthy"


async def test_a_container_that_did_what_was_asked_says_nothing_extra():
    done = tools.outcome("web", "start", {"Status": "running"}, "running")
    assert done.note is None


async def test_waiting_gives_up_rather_than_holding_the_call_open(client):
    with pytest.raises(Timeout, match="still exited"):
        await tools.wait(WaitRequest(container="worker", state="running", seconds=1), client)


async def test_a_stopped_container_is_told_what_to_do_about_it():
    with pytest.raises(NotRunning, match="Start it first"):
        async with clear("running a command", subject="cache"):
            raise DockerError(409, "container 3156cbe0b5f6 is not running")


async def test_a_name_already_taken_is_a_conflict_rather_than_a_number():
    with pytest.raises(Conflict, match="name is already in use"):
        async with clear("creating the container", subject="cache"):
            raise DockerError(409, "Conflict. The name is already in use by cache.")


async def test_a_daemon_that_cannot_be_reached_says_which_one():
    with pytest.raises(Unreachable, match="Check that it runs"):
        async with clear("listing containers"):
            raise OSError("Cannot connect to host")


async def test_no_status_number_reaches_the_caller():
    """Expose actionable errors without raw HTTP status or full container ids."""
    with pytest.raises(Trouble) as raised:
        async with clear("starting", subject="cache"):
            raise DockerError(500, "driver failed programming external connectivity")
    assert "[500]" not in str(raised.value)
    assert "starting cache failed" in str(raised.value)


async def test_the_default_does_not_ask_before_creating_something(client, fake):
    ex = FakeExchange(action="decline")
    made = await tools.create(CreateRequest(image="redis:7", name="fresh"), client, ex, DEFAULT)
    assert ex.asked == []
    assert made.name == "fresh"


async def test_the_default_does_not_ask_before_writing_a_file(client, fake):
    ex = FakeExchange(action="decline")
    await tools.write(
        WriteRequest(container="cache", path="/tmp/a", content="x"), client, ex, DEFAULT
    )
    assert ex.asked == []
    assert [path for _, path, _ in fake.written] == ["/tmp/a"]


async def test_the_default_still_asks_before_what_cannot_be_undone(client, fake):
    ex = FakeExchange(answers={"confirm": {"confirmed": True}})
    await tools.remove(RemoveRequest(container="worker"), client, ex, DEFAULT)
    await tools.remove_image(ImageRef(image="postgres:16"), client, ex, DEFAULT)
    await tools.prune(PruneRequest(what="images"), client, ex, DEFAULT)
    assert len(ex.asked) == 3


async def test_a_host_that_asks_its_own_permission_can_turn_this_one_off(client, fake):
    ex = FakeExchange(action="decline")
    result = await tools.remove(RemoveRequest(container="worker"), client, ex, NEVER)
    assert ex.asked == []
    assert result == "removed worker"


async def test_a_deployment_may_ask_before_anything_that_changes_anything(client, fake):
    ex = FakeExchange(answers={"confirm": {"confirmed": True}})
    await tools.create(CreateRequest(image="redis:7"), client, ex, CHANGES)
    assert len(ex.asked) == 1


async def test_a_policy_nobody_can_read_is_refused_at_the_command_line():
    with pytest.raises(ValueError, match="confirm must be one of"):
        Policy("sometimes")
