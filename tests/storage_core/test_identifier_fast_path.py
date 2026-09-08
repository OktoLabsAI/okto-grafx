"""Cold catalog admission keeps exactly the original ASCII identifier language."""

from itertools import product
import sys

from okto_grafx.domain.model.schema import MAX_IDENTIFIER_LENGTH, is_identifier


def original(name):
    return (isinstance(name, str) and bool(name) and len(name) <= MAX_IDENTIFIER_LENGTH
            and name.isascii() and (name[0].isalpha() or name[0] == "_")
            and all(char.isalnum() or char == "_" for char in name))


def test_all_ascii_pairs_and_boundary_lengths_keep_the_original_grammar():
    for first, second in product(map(chr, range(128)), repeat=2):
        name = first + second
        assert is_identifier(name) == original(name), repr(name)
    for length in (0, 1, MAX_IDENTIFIER_LENGTH, MAX_IDENTIFIER_LENGTH + 1):
        for char in ("a", "_", "9", "é", "\x00", "\n"):
            name = char * length
            assert is_identifier(name) == original(name)
    for code in range(128, 0x10000):
        assert not is_identifier("a" + chr(code))


def test_subclass_does_not_use_the_new_predicate_or_drop_old_hooks():
    class Named(str):
        def isidentifier(self):
            raise AssertionError("subclass entered the exact-string fast path")
        def isascii(self):
            return False
    assert not is_identifier(Named("valid"))
    for value in (None, 1, True, b"valid", [], {}):
        assert not is_identifier(value)


def test_exact_string_does_not_dispatch_a_python_frame_per_character():
    frames = []
    filename = is_identifier.__code__.co_filename
    def observe(frame, event, argument):
        if event == "call" and frame.f_code.co_filename == filename:
            frames.append(frame.f_code.co_name)
    previous = sys.getprofile()
    try:
        sys.setprofile(observe)
        assert is_identifier("x" * MAX_IDENTIFIER_LENGTH)
    finally:
        sys.setprofile(previous)
    assert frames == ["is_identifier"]
