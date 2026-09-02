"""The rotation case is the whole reason this module exists."""
import asyncio
import os

from relay.logtail import LogTail


def _drive(path, actions, expect, timeout=6.0):
    """Run the tail, perform `actions` against the file, collect `expect` lines."""
    got = []

    async def main():
        tail = LogTail(path, poll=0.05, from_start=True)

        async def collect():
            async for line in tail.lines():
                got.append(line)
                if len(got) >= expect:
                    return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.15)
        for act in actions:
            act()
            await asyncio.sleep(0.25)
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except asyncio.TimeoutError:
            task.cancel()
        tail.close()

    asyncio.run(main())
    return got


def test_reads_appended_lines(tmp_path):
    p = tmp_path / "latest.log"
    p.write_text("first\n")
    got = _drive(str(p), [lambda: p.open("a").write("second\n")], expect=2)
    assert got == ["first", "second"]


def test_survives_rotation_to_a_new_inode(tmp_path):
    """A restart replaces latest.log. A tail holding the old fd goes silent."""
    p = tmp_path / "latest.log"
    old = tmp_path / "2026-09-02-1.log"
    p.write_text("before restart\n")

    def rotate():
        os.rename(p, old)          # server gzips the old log aside
        p.write_text("after restart\n")   # and opens a fresh one

    got = _drive(str(p), [rotate], expect=2)
    assert "before restart" in got
    assert "after restart" in got, "tail followed the old inode and went silent"


def test_survives_the_file_briefly_disappearing(tmp_path):
    p = tmp_path / "latest.log"
    p.write_text("one\n")

    def gap():
        os.remove(p)

    def restore():
        p.write_text("two\n")

    got = _drive(str(p), [gap, restore], expect=2)
    assert "one" in got and "two" in got


def test_truncation_in_place_is_handled(tmp_path):
    p = tmp_path / "latest.log"
    p.write_text("long first line\n")

    def truncate():
        with p.open("w") as fh:
            fh.write("short\n")

    got = _drive(str(p), [truncate], expect=2)
    assert "short" in got


def test_missing_file_at_startup_is_not_fatal(tmp_path):
    p = tmp_path / "latest.log"

    def create():
        p.write_text("appeared\n")

    got = _drive(str(p), [create], expect=1)
    assert got == ["appeared"]
