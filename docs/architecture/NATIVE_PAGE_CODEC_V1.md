# NumPy page codec v1

**Status:** accepted for item 13 in `0.0.2`

## Decision

Grafx keeps `PageCodecV1` as the default correctness oracle and adds the explicitly selected
`NumpyPageCodecV1` adapter through `DatabaseConfig(codec="numpy")`. The adapter is shipped in the
same pure-Python wheel, while NumPy remains optional under `okto-grafx[accel]`; no compiler or
platform-specific Grafx wheel is introduced.

Both adapters read and write page format version 1. The selector changes neither catalog nor page
bytes, creates no capability bit and needs no migration. Pure and NumPy handles may therefore read
and write the same database concurrently under the unchanged multiwriter/multireader protocol.

There is deliberately no `"auto"` selector. The default is `"pure"`, and an explicit `"numpy"`
request refuses with `GrafxConfigurationError` when NumPy is unavailable. This keeps measurement
honest and prevents a host from changing execution merely by installing an unrelated package.

## Bounded vectorized path

The accelerated adapter owns only the slot-directory loops already behind the per-database
`PageCodec` port:

- encode uses NumPy little-endian `u16` packing from 16 slots onward;
- decode uses vectorized bounds and overlap validation from 96 slots onward;
- smaller directories use the pure codec because array setup would cost more than the short loop;
- invalid directories are passed to the pure decoder, which remains the single authority for the
  public exception class, message and details;
- checksums continue through the process-wide CRC-32C implementation. Installing `[accel]` with
  the default `checksum="auto"` therefore combines the NumPy directory path with
  `google-crc32c`.

The thresholds are implementation constants, not durable parameters or SLOs. They can be tuned in
a later release without migration because either branch produces the same bytes and result.

## Safety and observability

The NumPy path is stateless and bounded by the configured page size. It performs no I/O, locking,
WAL work or caching. Encoding operates only on an exact built-in `Page`; subclasses retain the pure
path. Decode validates the checksum and complete geometry before constructing a `Page`. Seeded
mutants and explicit corruptions must produce the same success or typed refusal as the oracle.

`database.codec` reports `format_version`, `page_size`, the concrete codec implementation and the
currently effective CRC-32C implementation as `process_checksum_implementation`. The explicit name
matters: checksum selection is process-wide by design, while the page codec receipt is per database.

## Directional evidence

On the development Windows/Python 3.13 host with 8 KiB pages and native CRC already installed,
the focused micro-run observed these non-gating ratios:

| Operation | Directory | Pure | NumPy | Ratio |
|---|---:|---:|---:|---:|
| encode full page | 40 slots | 45.28 us | 23.57 us | 1.92x |
| encode full page | 64 slots | 89.54 us | 35.41 us | 2.53x |
| encode full page | 200 slots | 287.94 us | 139.18 us | 2.07x |
| decode full page | 128 slots | 80.32 us | 47.49 us | 1.69x |
| decode full page | 512 slots | 343.94 us | 160.97 us | 2.14x |
| decode full page | 1,000 slots | 764.38 us | 227.43 us | 3.36x |

These numbers demonstrate direction only. Page density, checksum provider and host determine the
end-to-end effect; pages below the decode threshold deliberately claim no decode gain.

A final recheck of the reviewed implementation on the same host, at 200 slots/8 KiB and over the
median of seven 500-operation samples, measured encode `164.85 -> 53.87 us` (`3.06x`) and decode
`150.48 -> 82.12 us` (`1.83x`). This is still a component microbenchmark: it does not claim the
same ratio for a complete transaction, whose WAL, barriers, coordination and index work are
unchanged.

## Excluded work

- A bespoke C/Rust extension and `abi3` wheel matrix are not part of this milestone. This host has
  no C/Rust toolchain and the repository has no compiled-wheel publishing channel.
- Tuple/value codecs and native record-header scanning remain separate semantic surfaces. They are
  not smuggled into a page-format adapter.
- A process-global scan-kernel installer is rejected for this slice because opening one database
  must not change another database's selected implementation. Any future scan kernel needs an
  explicit per-instance composition boundary and its own refusal-parity contract.
- Page format v2, compressed heap pages and changed WAL semantics are outside the item.

## When to use

Use `codec="numpy"` for index-heavy or dense-page workloads when `[accel]` is installed. Keep
`codec="pure"` for the zero-dependency deployment, as the reference during differential diagnosis,
or when predictable startup without optional packages is more important than the directory-loop
gain.
