"""Cleanup uses explicit exception flow and drains every remaining iterator."""

import pytest

from okto_grafx.engine.query_engine import _iterator_cleanup


class Stream:
    def __init__(self, seen, name, fail=False):
        self.seen, self.name, self.fail = seen, name, fail

    def close(self):
        self.seen.append(self.name)
        if self.fail:
            raise RuntimeError(self.name)


def test_all_streams_close_even_when_first_cleanup_fails():
    seen = []
    streams = (Stream(seen, "first", True), Stream(seen, "second", True), Stream(seen, "last"))
    with pytest.raises(RuntimeError, match="first") as failure:
        with _iterator_cleanup(lambda: streams):
            pass
    assert seen == ["first", "second", "last"]
    assert any("second" in note for note in failure.value.__notes__)


@pytest.mark.parametrize("primary", [ValueError("query"), GeneratorExit(), KeyboardInterrupt()])
def test_original_failure_and_process_control_preserved(primary):
    seen = []
    with pytest.raises(type(primary)) as failure:
        with _iterator_cleanup(lambda: (Stream(seen, "close", True), Stream(seen, "next"))):
            raise primary
    assert failure.value is primary
    assert seen == ["close", "next"]
    assert "close" in primary.__notes__[0]


def test_inventory_is_evaluated_at_exit_not_entry():
    seen = []
    stack = []
    with _iterator_cleanup(lambda: reversed(stack)):
        stack.append(Stream(seen, "outer"))
        stack.append(Stream(seen, "inner"))
    assert seen == ["inner", "outer"]
