"""Independent wire vectors and pre-admission refusal for the temporal format."""

import random
import struct

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxSchemaVersionMismatch
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.temporal_codec import (
    TemporalValueTag, TEMPORAL_VALUES_CAPABILITY_BIT, MAX_TEMPORAL_VALUE_BYTES,
    encode_temporal_value, decode_temporal_value,
)
from okto_grafx.domain.model.temporal_values import (
    DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue,
    TemporalInstant, MIN_YEAR, MAX_YEAR,
)
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.schema import ColumnDef, SchemaType, TableDef, encode_tuple, decode_tuple
from okto_grafx.domain.model.value import ValueType, encode_value, decode_value
from okto_grafx.domain.page import crc32c
from okto_grafx.domain.recovery.decision import committed_replay
from okto_grafx.engine.commit_redo import CommitRedo

from .test_catalog_replay_images import staged, records

_VECTORS = [
    (DateValue(1970),b"\x0c"+struct.pack("<q",0)),
    (LocalTimeValue(7),b"\x0d"+struct.pack("<Q",7)),
    (TimeValue(LocalTimeValue(9),-75),b"\x0e"+struct.pack("<Qi",9,-75)),
    (LocalDateTimeValue(DateValue(1969,12,31),LocalTimeValue(86399999999999)),b"\x0f"+struct.pack("<qQ",-1,86399999999999)),
    (DateTimeValue.from_epoch_parts(0,1,offset_seconds=3600,zone="Future/Zone"),b"\x10"+struct.pack("<qIiB",0,1,3600,11)+b"Future/Zone"),
    (DurationValue(-13,3,-1,999999999),b"\x11"+struct.pack("<qqqI",-13,3,-1,999999999)),
]


@pytest.mark.parametrize("value,raw",_VECTORS)
def test_independent_golden_vectors_and_cursor(value,raw):
    assert encode_temporal_value(value) == raw
    assert decode_temporal_value(raw) == (value,len(raw))
    assert decode_temporal_value(b"prefix"+raw+b"suffix",6) == (value,len(raw)+6)
    for end in range(len(raw)):
        with pytest.raises(GrafxCorruptionDetected):
            decode_temporal_value(raw[:end])


def test_concatenated_frames_do_not_consume_siblings():
    raw = b"".join(frame for _,frame in _VECTORS)
    offset = 0
    for expected,_ in _VECTORS:
        actual,offset = decode_temporal_value(raw,offset)
        assert actual == expected
    assert offset == len(raw)


@pytest.mark.parametrize("raw", [
    b"\x0c"+struct.pack("<q",(1<<63)-1),
    b"\x0d"+struct.pack("<Q",86400000000000),
    b"\x0e"+struct.pack("<Qi",0,64801),
    b"\x0e"+struct.pack("<Qi",0,-64801),
    b"\x0f"+struct.pack("<qQ",0,86400000000000),
    b"\x10"+struct.pack("<qIiB",0,1_000_000_000,0,0),
    b"\x10"+struct.pack("<qIiB",0,0,64801,0),
    b"\x10"+struct.pack("<qIiB",(1<<63)-1,0,0,0),
    b"\x10"+struct.pack("<qIiB",0,0,0,1)+b"\xff",
    b"\x10"+struct.pack("<qIiB",0,0,0,3)+b"../",
    b"\x10"+struct.pack("<qIiB",0,0,0,1)+b"\x00",
    b"\x11"+struct.pack("<qqqI",0,0,0,1_000_000_000),
    b"\x11"+struct.pack("<qqqI",0,0,0,0xffffffff),
])
def test_invalid_or_noncanonical_payloads_refuse(raw):
    with pytest.raises(GrafxCorruptionDetected):
        decode_temporal_value(raw)


@pytest.mark.parametrize("tag",[0,1,10,11,18,255])
def test_non_temporal_tags_refuse(tag):
    with pytest.raises(GrafxCorruptionDetected):
        decode_temporal_value(bytes((tag,))+bytes(100))


@pytest.mark.parametrize("raw,offset",[(b"",0),(b"\x0c",-1),(b"\x0c",1),(b"\x0c",True),
                                       (bytearray(10),0),(memoryview(bytes(10)),0),(None,0)])
def test_invalid_buffers_and_cursors_refuse(raw,offset):
    with pytest.raises(GrafxCorruptionDetected):
        decode_temporal_value(raw,offset)


@pytest.mark.parametrize("value",[None,1,1.0,"2000-01-01",TemporalInstant(0)])
def test_no_host_or_clock_value_encoding(value):
    with pytest.raises(SchemaMismatchError):
        encode_temporal_value(value)


def test_defensive_encoding_refuses_forged_native_components_without_callbacks():
    class Host:
        def __index__(self):
            raise AssertionError("No host index conversion")
    value = LocalTimeValue(0)
    object.__setattr__(value,"nanoseconds",Host())
    with pytest.raises(SchemaMismatchError):
        encode_temporal_value(value)
    zoned = DateTimeValue(LocalDateTimeValue(DateValue(2000),LocalTimeValue(0)),0)
    object.__setattr__(zoned,"local",Host())
    with pytest.raises(SchemaMismatchError):
        encode_temporal_value(zoned)


def test_maximum_zone_budget_and_full_native_boundaries():
    assert MAX_TEMPORAL_VALUE_BYTES == 273
    value = DateTimeValue.from_epoch_parts(0,zone="Z"*255)
    assert len(encode_temporal_value(value)) == 273
    for value in (DateValue(MIN_YEAR),DateValue(MAX_YEAR,12,31),
                  DateTimeValue(LocalDateTimeValue(DateValue(MIN_YEAR),LocalTimeValue(0)),64800),
                  DateTimeValue(LocalDateTimeValue(DateValue(MAX_YEAR,12,31),LocalTimeValue(86399999999999)),-64800),
                  DurationValue(-(1<<63),-(1<<63),-(1<<63)),
                  DurationValue((1<<63)-1,(1<<63)-1,(1<<63)-1,999999999)):
        raw = encode_temporal_value(value)
        assert decode_temporal_value(raw) == (value,len(raw))


def test_deterministic_roundtrips_and_mutated_frames_are_canonical_or_refused():
    rng = random.Random(0xC0DEC)
    for _ in range(1500):
        date = DateValue(rng.randrange(MIN_YEAR,MAX_YEAR),rng.randrange(1,13),rng.randrange(1,29))
        time = LocalTimeValue(rng.randrange(86400000000000))
        offset = rng.randrange(-64800,64801)
        local = LocalDateTimeValue(date,time)
        duration = DurationValue(rng.randrange(-(1<<63),(1<<63)),rng.randrange(-(1<<63),(1<<63)),
                                 rng.randrange(-(1<<63),(1<<63)),rng.randrange(1_000_000_000))
        for value in (date,time,TimeValue(time,offset),local,DateTimeValue(local,offset,"Future/Zone"),duration):
            frame = encode_temporal_value(value)
            assert decode_temporal_value(frame) == (value,len(frame))
            mutated = bytearray(frame)
            index = rng.randrange(len(mutated))
            mutated[index] ^= 1 << rng.randrange(8)
            try:
                decoded,following = decode_temporal_value(bytes(mutated))
            except GrafxCorruptionDetected:
                continue
            assert encode_temporal_value(decoded) == bytes(mutated[:following])


def test_temporal_tags_roundtrip_through_general_and_any_tuple_codecs():
    assert [int(tag) for tag in TemporalValueTag] == list(range(12,18))
    assert [int(tag) for tag in ValueType] == list(range(19))
    table = TableDef(1,"N","node",(ColumnDef("value",SchemaType.ANY),))
    for value,raw in _VECTORS:
        for offered in (value,[value],{"temporal":value}):
            expected = (value,) if isinstance(offered, list) else offered
            assert decode_value(encode_value(offered))[0] == expected
            assert decode_tuple(table, encode_tuple(table,(offered,))) == (expected,)
        assert decode_value(raw) == (value, len(raw))


def _future_catalog_frame():
    raw = bytearray(Catalog().upgrade_index_catalog().serialize()[:-4])
    bits = struct.unpack_from("<Q",raw,28)[0]
    struct.pack_into("<Q",raw,28,bits|TEMPORAL_VALUES_CAPABILITY_BIT)
    return bytes(raw)+struct.pack("<I",crc32c(bytes(raw)))


@pytest.mark.parametrize("door",["preflight","apply"])
def test_legacy_temporal_capability_reader_refuses_before_wal_page_application(door,monkeypatch):
    from okto_grafx.domain.model import catalog as catalog_module
    # Emulate a reader without temporal-capability support, independently of the
    # new writer's metadata support. Its catalog preflight must precede effects.
    monkeypatch.setattr(catalog_module,"_KNOWN_CAPABILITY_BITS",
                        catalog_module._KNOWN_CAPABILITY_BITS & ~TEMPORAL_VALUES_CAPABILITY_BIT)
    raw = _future_catalog_frame()
    with pytest.raises(GrafxSchemaVersionMismatch):
        Catalog.deserialize(raw)
    class FutureCatalog(Catalog):
        def serialize(self):
            return raw
    pool,store,images = staged(FutureCatalog().upgrade_index_catalog(),1000)
    before,writes = store.read_from_pages(),list(pool.storage.write_calls)
    def forbidden(*args,**kwargs):
        raise AssertionError("No data or catalog apply may precede capability admission")
    monkeypatch.setattr("okto_grafx.engine.commit_redo.apply_page_image",forbidden)
    with pytest.raises(GrafxSchemaVersionMismatch):
        getattr(CommitRedo(pool),door)(committed_replay(records(images,1000)))
    assert store.read_from_pages() == before
    assert pool.storage.write_calls == writes
