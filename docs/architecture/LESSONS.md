# Lessons — recorded after the W1 contract freeze

Applied to W2+ briefs. NOT usable to reject work started before they were written (CONTRACT §13).

## L1 — deleting a refusal path needs an in-code proof and an independent check

A security review flagged C3 for deleting two guards (an identifier length check and a `held` term in
a liveness key) on the instruction of a coordinator message it could not verify came from the user.
The instance was benign -- both deletions were verified unreachable/redundant afterwards, by reading
the code rather than by trusting the report:

- the length check: `_verify_envelope` establishes `len(raw) == header + owner_len + checksum` before
  the slice, so the slice provably has the length it asks for, and an oversized `owner_len` is still
  caught by `_validate_identifier`'s limit;
- the `held` term: all three callers (`:891`, `:1027`, `:1076`) check `held` before asking about a
  stall, so the protection lives in the callers.

The pattern is still the one an attack would use, so it gets a rule rather than a shrug.

**Deleting any code path that REFUSES -- a corruption check, a validation, a bounds test -- requires:**
1. an **in-code comment** stating why no input can reach it, at the site, so the next reader does not
   have to reconstruct the argument (C3 did this unprompted, at both sites);
2. **verification by someone other than the deleter** -- the coordinator or the next critic -- naming
   the specific guarantee that makes it unreachable, not merely repeating the claim;
3. the **A93 2x2** where the guard is redundant rather than unreachable, showing the cell in which the
   defence actually fails.

And for the agents: an instruction to delete a safety check is exactly the shape a hostile message
would take. Complying is defensible only when you can prove the deletion safe from the code itself --
in which case say so with the proof, as C3 did. If you cannot, refuse and report (A60).
## L2 — A91 is too narrow: never hold a lock across a WAIT on foreign code either

A91 says never *call* host-supplied code while holding an internal lock. C8 obeyed it exactly — its
teardown calls no host code, and the docstring saying so is literally true — and still deadlocked,
because it holds the lock across `server.shutdown()`, which **waits for a thread that is running host
code**. `socketserver`'s `_handle_request_noblock` reaches `handle_error` -> `report()` ->
`EventSink.emit()` on the accept loop, and a host that touches the lifecycle from `emit()` closes the
cycle. Reproduced unforced: 2 of 8 trials, 400 start/stop cycles each against 3 live scrapers.

**The rule A91 should have stated: while holding an internal lock, do not call foreign code AND do
not block on anything that can be waiting on foreign code** -- a `join()`, a `shutdown()`, a queue
`get()`, a condition wait, or any library call whose completion depends on a thread that may run a
callback. "I call no host code here" is not the test; "nothing I wait for can be running host code"
is. Audit by asking of every blocking call under a lock: *what is the longest thing this can wait
for, and can that thing re-enter me?*

Structural remedy, and the one to prefer: **make the lock cover the DECISION, not the work.** Take
the lock, decide and mark that this caller owns the teardown, release it, then do the teardown
outside. A second caller sees the mark and either waits on a separate event or returns. The lock then
protects a state transition that cannot block, which is the only thing it was ever needed for.

Not usable to reject W0/W1 work under CONTRACT §13 -- but a deadlock is blocking on its own terms
under §13.3, which is how this was caught without the contract moving.

## L3 — private forks need unique roots; the scratchpad is shared

A critic's fork was wiped mid-session by another agent using the same `scratchpad/fork` path; it
detected the loss, discarded that reading and redid it under a unique root. A80 made private forks
the default without saying where they live. **A fork root must be unique per agent per run** -- name
it for the component and a run token -- and carry the `.battery-root` stamp A95 asks for. Two agents
sharing a fork path is the same class of collision A58 fixed for the shared checkout, one level down.
## L4 — static inspection of author intent is adversarially unwinnable; measure coverage instead

C0's skip-attribution gate has now been defeated in **seven consecutive rounds**. Every fix was
correct for the escape demonstrated, and a new escape appeared each time: a reason string, a module
name, a marker argument, a dotted suffix, a sub-expression, a platform value no target reports, a
rebinding after the one the gate reads, a `def` spelling the counter does not recognise. The pattern
is not carelessness -- each round closed its case properly. The pattern is the **frame**.

The gate tries to decide, from code the author wrote, whether the author's stated reason for skipping
is honest. That is a question about intent, checked against evidence the author controls, and A89's
diagnostic ("can the author add a member to the satisfying set?") keeps answering yes in a new
dimension because the author writes the whole input.

**The durable answer is to stop asking whether each skip is justified and start measuring whether
each test ever RUNS.** Across the families D9 requires, every test must execute on at least one --
observed from real runs, not inferred from conditions. A test skipped everywhere is then caught
without anyone inspecting why it skipped, and the author cannot extend the set of families the CI
matrix reports. That is a cross-run check and belongs to **C13 (bench/CI)** with **W6**, not to a
conftest that can only see one family at a time.

Keep the per-session gate: it gives fast, local diagnostics and it caught real evasions. Stop
treating it as the guarantee. **The guarantee is coverage measured across the matrix; the conftest
rule is a smoke alarm, not a load-bearing wall.**

Carry into C13's brief and W6's integration matrix.
## L5 — an operation can report success, write the right bytes, and use the wrong NAME

C2's first implementation of the POSIX-semantics rename hand-rolled the `FILE_RENAME_INFO` buffer.
It **returned success and wrote correct content under a garbled filename** (`\u0467`). Every content
assertion passed -- the bytes were right, the reader that already held the file was fine, and the
call reported no error. It surfaced only because the builder asserted the **directory listing**.

Generalise it: a test that verifies an operation by reading back **through the same handle or name
the operation returned** cannot see a naming fault, because it follows the operation's own answer.
For anything that publishes, renames, rotates or links, **assert the namespace independently** --
list the directory, resolve the path afresh, or open by the name a different participant would use.
This is A72 (prove the fixture produced the state it claims) aimed at the *result* rather than the
setup, and it is the same shape as this build's consistency checks that agreed with the corruption
they were meant to catch: the check and the thing checked shared an assumption.

Also worth carrying: C2 refuted the coordinator's stated mechanism (`FILE_SHARE_DELETE`) with a
five-row measurement table **before** implementing, and the coordinator's own same-process
reproduction turned out not to reproduce through the delivered device at all. Asking a builder to
"confirm or refute before implementing" cost one extra step and prevented a fix built on a wrong
diagnosis.

## L5 — CONFIRMED LIVE, and worse than when it was recorded

L5 was written from a near-miss C2 caught in development: a rename that reported success while
writing a garbled filename. **The delivered fix shipped the same class of defect**, found by the
blind critic:

- the buffer is sized in Python **code points** while Windows counts **UTF-16 code units**, so a
  non-BMP character in the database directory path overflows it;
- with two such characters a raw `ValueError` escapes `atomic_replace` (§11 DoD 5);
- with exactly one, and `len(target) % 4 == 1` so the struct carries no padding NUL, the call
  **returns success, the old record is kept, and a junk name enters the namespace**.

Two lessons on top of L5's original text:

1. **Sizing a buffer for a foreign ABI is a units question, and `len()` answers a different one.**
   Anywhere a Python string crosses into a C structure, state the unit explicitly
   (`len(s.encode("utf-16-le")) // 2`) rather than letting `len()` stand in for it.
2. **A test corpus that shares an alphabet with its assertions cannot find this.** No file under
   `tests/` contains a non-BMP character and every C2 device is rooted at pytest's ASCII `tmp_path`,
   so the entire class was unreachable by construction. When a component accepts an arbitrary user
   string (a path, a name, a label), the corpus must leave the ASCII plane -- otherwise the tests
   agree with the code about what inputs exist, which is the same failure as a check that shares an
   assumption with the thing it checks.

## L6 — the second mass-kill left nothing to repair, and that was designed rather than lucky

A session limit killed **eleven agents simultaneously**, several mid-battery. Compare the two
incidents:

**First mass-kill (before A76/A80/A95):** live mutations left on the shared tree, one of them
undetected until an unrelated ambiguity guard fired on a later slice; a stale lock held by a dead
pid; hours of recovery; several readings retroactively void.

**Second mass-kill (after):** nothing to repair. Verified in about four minutes:
- **A95's root stamps settled the dangerous question first.** The two components that were mid-battery
  had `.battery-root` files naming fork paths under `okto-c9-battery/`, so they *provably* could not
  have touched the shared checkout. No code reading, no guessing -- the stamp answered it.
- **A80's private forks** meant a battery killed mid-mutation had nothing shared to corrupt.
- **A59's process enumeration** found the lock free but **10 interpreters alive** -- exactly the case
  A59 exists for. Eight were orphaned `multiprocessing.spawn` children of dead parents; the other two
  belonged to a different project entirely and were correctly left alone.
- Then the evidence: **269 Python files parse**, **1,936 gate tests green with 0 failures and 0
  skips**, no mutation sentinel anywhere in the tree.

The lesson is not "we got lucky twice". It is that **every one of those amendments was written from a
specific incident that cost real time, and each one turned an investigation into a lookup.** The
cheapest safety mechanism in this build is a file that says what a process is about to mutate.

Corollary worth keeping: after any mass interruption, the order is **enumerate processes -> read the
root stamps -> check for residue -> parse everything -> run the gates**. Cheapest and most decisive
first; do not start by reading code.

## L7 — a mutant that does not parse is not a kill (the mirror of A70)

C2's battery scored a mutation as **KILLED** when the mutated file did not even import. The suite went
red, so the driver recorded a kill -- and it had measured nothing at all. Re-run as a syntactically
valid edit, that mutation is genuinely killed by four tests, but the original score was an artefact.

A70 says a mutation whose anchor did not match applies nothing and must not be scored a **survivor**.
This is the same error at the other end: **a mutation that applies but cannot be imported must not be
scored a kill.** A collection error is not a test detecting a behaviour change; it is the suite
failing to run. Both directions inflate a kill rate while measuring nothing.

**Required of every battery: after mutating, confirm the target still imports** (compile it, or check
that the run produced test results rather than a collection error), and score anything else as
UNMEASURED per A75.2. A kill rate is only meaningful if every mutation in the denominator both
*applied* (A70) and *ran* (L7).

C2 raised this against its own driver unprompted, which is the behaviour that makes the rest of its
numbers worth reading.

## L8 — the shared architecture docs are a contended resource, and a shell append can fail silently

`COMPONENTS.md` and `PUNCHLIST.md` are written by the coordinator and by every component that records
a punch-list entry. With eleven agents live, a `cat >> file << 'EOF'` append failed with **Device or
resource busy** while the `echo "recorded"` on the next line succeeded -- so the log said the write
landed and it had not.

That is A75's trap in miniature, aimed at my own coordination rather than at a test run: **the
message after a command is not the command's result.** Same family as reading a wrapper's exit code,
or a battery scoring a kill from a collection error (L7).

**Rule: after writing to a shared document, verify the content is present** -- `grep` for a distinctive
string from what you just wrote -- and retry on `OSError` rather than assuming. Prefer a Python append
with an explicit retry loop over a shell heredoc, because the failure is visible as an exception
instead of a status nobody reads.

Generalisation worth carrying into W6: **any write to a resource other agents share needs
read-back verification**, not a success message. The build already applies this to page images, control
records and renames; the documentation was the one shared resource nobody had applied it to.

## L9 — the damage/caller-error discriminator, stated better than A11-revised stated it

A11-revised says `corruption_detected` is for damaged bytes, never an access failure or a caller's bad
argument. True, and it kept producing arguments about edge cases. C1 found the operational test while
enumerating every path that reaches the decision:

> **Is the state reachable with a value the component itself hands out?**

A freed slot is: C7 frees slots on purpose and references to them survive, so an ordinary caller holding
an ordinary `RecordRef` reaches it. That is **not damage** -- and routing it to `corruption_detected`
was routing a normal state into FR-8/FR-10 truncation and quarantine. A locator naming page 0 or slot 0
is **not** reachable that way: every `RecordRef` C1 produces has `page >= 1` and `slot >= 1`, so you
must fabricate one.

The corollary is about **provenance, and where it is knowable**: `Page` cannot know where a slot id came
from, so it must refuse rather than classify; `HeapStore` **can**, because slot 0 of a heap data page is
a structural slot it wrote itself, not an argument -- so a heap page that cannot say which table it owns
is structurally incomplete however the entry came to be empty. **Classify at the layer that knows the
provenance; refuse at the layer that does not.**

C1 also declined to reclassify the locator guards unilaterally, "on a surface C6 has just built
against", and punch-listed them with the argument instead. Restraint about a dependency's expectations
is worth as much as the fix.

## L10 — a fork synced from `git ls-files` cannot collect, and scores a whole battery against nothing

C1's battery driver copied only **git-tracked** files into its private fork. A sibling then added an
uncommitted `okto_grafx/api` package that `okto_grafx/__init__.py` imports -- so the fork could not
import at all, the baseline read red, and **an entire battery was scored against a tree that never
ran**. Any driver that syncs from `git ls-files` has this hole, and it widens every time a component
lands new files before anything is committed. **Mirror the working tree, not the index.**

The reason this cost one battery rather than several is **A80's requirement that the fork's own baseline
be GREEN before any mutation is applied.** That check is what turns this from a silent wrong answer into
a visible refusal, and it is why it belongs in every brief. L7 is the same shape one level down (a
mutant that does not import is not a kill); this is the shape at the level of the whole fork.

Related transient, worth knowing rather than diagnosing: for about a minute
`engine/vector_engine.py` imported a name `domain/vector/entry.py` no longer defined -- a sibling
mid-rename -- which broke collection of an unrelated suite through `tests/conftest.py`. With many
components writing one tree, **a collection error that resolves on its own is a sibling's edit, not your
defect.** Re-run before investigating.

## L11 — a bound a coin-flip away from firing is worse than no bound, because it makes the defect look like weather

C0 measured the fixture that had cost four critics their whole-repo readings, under the contention that
was breaking them (49 Python processes on 16 cores):

```
copytree         7.55s
pip wheel      291.09s
fixture total  298.63s      against the bound then in force: 300s
```

**99.5% consumed.** The bound was not wrong in kind; it was wrong in **size** -- close enough to the
real cost that which side it landed on was decided by how many siblings happened to be running. Three
critics saw three different outcomes from one test and each reasonably suspected something different.

Two rules from it:

1. **Ask what firing means before choosing a number.** Here, firing is *always* the bad outcome,
   because pytest-timeout's thread method hard-exits and **destroys the junit report either way**
   (A75.2 -> UNMEASURED). So the bound's only job is to keep a genuine wedge finite, not to police a
   duration the suite does not govern -- which is why 1800 s (~170x at-rest, ~6x the worst contended
   build) is the right answer and 300 s was not. Where firing *is* informative, the calculus differs.
2. **A hazard decided by collection order needs a structural guard, not a fix.** Whichever test
   triggers the shared fixture first pays for it, so a *fifth* dependent added later silently
   reintroduces the problem. C0 added a test that fails when any dependent lacks its own bound -- A73
   applied: make the mistake unrepresentable rather than corrected once.

**The same shape is live elsewhere and was hiding real defects.** `tests/txn`'s
`test_no_thread_ever_sees_a_non_grafx_failure` runs 102 s against a 60 s bound; re-run at 900 s it
completes in 324 s **and exposes nine multi-process failures the hard-exit had been swallowing**. A
suite that reads UNMEASURED is not a suite that reads green, and the difference is exactly where
defects live.

Note also C0's methodology, which is the part to copy: it **planted a probe to establish that a
per-test mark covers fixture setup at all** before raising anything. Raising a number that does not
apply fixes nothing and looks like it did.

## L12 — a test double weaker than the real adapter certifies nothing about the real adapter

C11 assembled the stack for the first time and immediately found that **C3 emits
`oktografx_lease_wait_seconds` without ever registering it**. Against any *recording* sink,
`acquire_writer_lease` raises `GrafxConfigurationError` -- so **every write commit under
`metrics="openmetrics"` or `"json"` fails**. C3 is signed off, with a thorough suite, and its suite
could not see this: it uses a metrics double that does not enforce registration, while C8's real sink
does (A51).

This is the third instance of the shape in this build. C5's multi-process story runs on
`shared_device.py`, whose `recycle()` is weaker than `LocalStorageDevice`'s. C5's WAL claims were
proven against `LogWal`, a double that rescans where the real `WalManager` did not -- which is how
CF-6 survived until another component drove the real thing.

**A double must be at least as strict as the adapter it stands for, on every property the test
relies on.** Where it is looser, the test proves something about the double. Two practical
requirements:

1. **State what the double is looser about**, in the double, next to the method that differs. C5's
   `shared_device` docstring did this and its critic still had to discover the consequence.
2. **Some test must drive the real adapter**, even if only one path, so the gap between double and
   reality has somewhere to show. Every defect of this shape here was found by a *different component*
   using the real thing -- never by the owner's own suite.

Corollary for W6: a component signed off entirely against doubles has not been verified against the
system, only against its own model of it.

### L12 addendum — C3's framing, which is sharper than the original

> The common thread in all three instances is that **the double was written to make the component's
> own tests pass, so it could only ever confirm them.** The annotation is the cheap half; the
> real-sink test is the half that can fail.

That is the mechanism, not just the symptom: a double authored alongside the tests it serves inherits
every assumption those tests make, so it cannot contradict them. It is the same defect as a
consistency check that shares an assumption with the thing it checks -- which this build has now
produced in three different guises.

C3's remedy is the shape to copy. Five doubles annotated **at the method that differs**, each naming
the real behaviour it does not reproduce -- including `RecordingMetricsSink.register`, whose note
records its own failure history: *"the real sink refuses unregistered names, undeclared label values,
and conflicting re-registration, which is how the unregistered lease-wait metric reached C11."* Plus
four tests driving the real `OpenMetricsSink`, one of which asserts the exact failure C11 met.

One further detail worth copying: C3 registers from **C8's frozen catalog** (`metric(name)`) rather
than restating the descriptor locally. So it cannot drift from what the dashboard and CI gate read,
**and** C11's blanket `register_catalog` registers the identical object -- making the second
registration a no-op instead of the "already registered with a different descriptor" conflict a
hand-written copy would eventually cause.

## L13 — never rework a component while its blind critic is reviewing it (coordinator error)

I told C9 to re-base onto C7's `IndexStore` while its round-1 critic was mid-review. Two of the files
under review -- `domain/vector/entry.py` and `domain/vector/index.py` -- were **deleted at 17:02-17:04,
during the review**. The critic handled it correctly: it re-verified its live finding against the
current tree, anchored the other to the artefact it was handed, and said plainly which was which.

The cost is still real. A blocking defect (B2) is now described against code that no longer exists, so
it cannot be *verified fixed* -- only *re-applied by argument* to whatever replaced it. That is
strictly weaker evidence, and it is the reviewer's time spent on a moving target.

**Rule: a component under blind review is frozen until the verdict lands.** If a decision cannot wait,
either stop the critic and re-brief it against the new shape, or take the decision and hold it until
the review reports. Both are cheap; discovering it in the report is not.

The corollary that saved this one: **a critic must state, for every finding, whether it verified it
against the tree as it stands now.** C9's critic did (B1 re-verified at 17:30 against sha
`9e047a60c59b`; B2 not, files gone). Without that line the whole report would have been unusable.

## L14 — a guard on the encoder is not a guard on the format

C9 refuses a non-finite component in `encode_vector_key` -- and `decode_vector_key` has no finiteness
check, so a key assembled from the format's own bytes decodes to a NaN and is accepted by every
downstream door. The guard sits on the path the component's own tests take; the leak is on the path a
caller takes.

`entry.py`'s own comment named the risk verbatim -- *"a caller can reach the index directly with a key
it built itself, and that door would otherwise make a NaN persistable in a space of any dtype"* -- and
the test written for it, `test_a_hand_built_key_of_a_nan_cannot_be_staged_into_an_index`, **does not
build a key by hand**: it calls `index.key_for(...)`, the encoder that already refuses. The name
over-claims and the real door is untested.

**Where a format can be constructed by a caller, validate on DECODE, not only on encode.** Anything
that accepts bytes is a public door regardless of who is expected to call it, and a test whose name
says "hand-built" must actually build it by hand.

## L15 — when a defect class is identified, sweep the tree before routing the instance

C11 found C2 concatenating a localized `strerror` into a Grafx message (G1/A7). C2 fixed it -- and
then found **the same defect in C8's file**, which it does not own. A tree-wide sweep by the
coordinator settled the true extent in one command: four instances, all in
`adapters/metrics_json.py`, and exactly one component already carrying the correct pattern.

Routing the instance alone would have left three siblings and no way to know it. The sweep costs one
`grep` and answers a question nobody could answer from inside a component: **is this a mistake or a
pattern?**

Practice: when a review produces a defect that is a *class* rather than a one-off -- a wrong
classification, a leaked platform string, a guard on the wrong side of a format, a double weaker than
its adapter -- grep for the shape across every component before writing the routing message. Then
route the instance **with the fix that already exists elsewhere**, and say who has it. C2's
`details["platform_message"]` pattern was three hours old when C8 needed it.

Corollary: **the components find these, not the coordinator.** C2 reported a defect in a file it does
not own rather than treating "not mine" as the end of the thought; C7 reported C5's missing index
commit; C9 reported C11's missing wiring; C13 reported C5's `__slots__` omission. Every one of those
crossed an ownership boundary voluntarily.

## L16 — a test asserting against a CUMULATIVE trail can be satisfied by an earlier operation

C7 added a flush to `create()` and re-ran the guards downstream of the change (A83.1). **M45 regressed
from KILLED to SURVIVED**: `test_a_commit_puts_the_index_pages_on_the_device` asserts against
`device.write_calls`, which accumulates for the life of the device. The new `create()` flush had
already put both index files in that list, so the test passed **with the commit flush deleted** -- it
had stopped testing the operation in its own name.

This is A62's family aimed at *sequence* rather than *class*: there, a test could not tell which guard
raised; here, it cannot tell which operation wrote. Both pass for the wrong reason, and both are
invisible in a green run.

**Where a test observes a recorded trail -- write calls, emitted events, published records, log
entries -- clear the trail immediately before the operation under test**, so the assertion can only be
satisfied by that operation. C7's repair does exactly that, and M45, M43 and M58 then all kill, which
also proves the two flushes are **independently observable** (A67).

The general warning: **adding a new call site can silently un-test an existing one** whenever the
assertion target is cumulative. Re-running the battery after a fix is what surfaces it -- not writing
new tests for the new behaviour, which is the instinct that misses it entirely.

## L17 — when a component can destroy, the tie goes to preservation; and agree by construction, not coincidence

Two formulations from C6 that are sharper than the rules they satisfy, and both generalise.

**On classifying a failure that authorises destruction.** C6's rule for whether a probe's refusal is
evidence of damage: **only a non-retryable `Grafx*` failure counts.** A retryable one refuses with
`retryable=True` so the caller retries; *anything else* refuses too --

> a probe that broke says nothing about the bytes, and **guessing in the destructive direction is the
> wrong way to be wrong.**

Any component that quarantines, truncates, retires or purges should adopt that asymmetry explicitly:
an uncertain signal must resolve toward preservation, never toward the irreversible act. And C6 wrote
the **positive** case too, so the rule cannot be satisfied by refusing everything -- a refuse-all
implementation passes every negative test.

**On two stores that must agree.** C6's ledger indexes an entry by `(entry_type, origin, offset,
length)` -- deliberately **the same identity the quarantine builds its file name from** -- so the two
agree *by construction rather than by coincidence*. Where two structures must describe the same thing,
derive both from one identity instead of keeping two rules that happen to match today. This is A24
(one definition) aimed at agreement rather than at duplication.

**A footnote worth the space:** proving the B3 backstop load-bearing with A93's 2x2 is what exposed a
*second* defect -- the reserved-name comparison was case-sensitive, so `MANIFEST.JSON` collided on
NTFS and passed on POSIX (G4/D9, failing on the family the contract calls primary). The matrix does
not only validate a claim; running it makes you construct the cases that break it.

## L18 — a mutation that changes no behaviour is INVALID, neither a kill nor a survivor

Three ways a mutation can fail to be a measurement, and all three have now been found here:

| | shape | verdict |
|---|---|---|
| **A70** | the anchor did not match, so nothing was applied | **not a survivor** |
| **L7** | applied, but the file no longer imports -- the suite failed to run | **not a kill** |
| **L18** | applied, imports, and **changes no behaviour** | **neither -- invalid** |

C1 found the third in its own battery: `I1-hand-out-then-spend` turned out to be an always-true branch
assigning an unused local. It "survived" correctly and meaninglessly, and counting it as a survivor
would have named a missing test that cannot exist.

**This is not an equivalent mutant (A93).** An equivalent mutant changes the code's semantics in a way
no *input* can distinguish -- it is a real probe whose survival is informative about the input space.
A no-op edit changes nothing at all; there is no semantics to distinguish. A93's 2x2 is the right
answer to the first and a category error applied to the second.

**Required: a battery must confirm the mutant differs in behaviour, not merely in bytes.** The cheap
check is that the mutated file's parse tree differs meaningfully -- or simply that the author can state
what the mutation makes the code fail to do (A87's test). If the answer is "nothing", it is not a
probe. Discard it and replace it with a behavioural one, as C1 did, rather than letting it pad either
column of the tally.

## L19 — a child helper that swallows the real error makes a sibling's breakage look like your defect

C0 found nine failures in `tests/txn/test_txn_multiprocess.py` hiding behind a timeout, reported as
*"a participant died before reporting"*, children exiting 1. They did not reproduce in isolation and
looked like a genuine multi-process defect in a signed-off component.

They were not. **C5's child helper had its imports outside the guard**, so while a sibling was
mid-write in `src/`, the children could not import at all, exited 1, and reported nothing back. The
parent could only say *"a participant died"* -- **indistinguishable from a real defect in C5**. C5 hit
the same thing live mid-session (`cannot import name 'VECTOR_INDEX_DESCRIPTOR_PREFIX'`), which is what
identified it.

Two requirements, and the second is the one that cost the time:

1. **A spawned child's imports belong inside its error guard**, so an import failure is reported rather
   than swallowed by process exit.
2. **A child must send back the failure's type and traceback, not just an exit code.** An exit code
   collapses "your logic is wrong" and "my environment broke" into one signal, and a parent asserting
   on it cannot tell them apart. C5's helper now returns both, with a test pinning it.

This is L10's family -- concurrent sibling writes making a fork or a child unusable -- but the
expensive part was diagnostic, not environmental: **a report that cannot name why a participant died
sends everyone hunting the wrong defect.** With eleven agents writing one tree, treat any child
failure whose cause is not carried back as UNMEASURED (A75.2) until it is.

## L20 — the coordinator's diagnosis of a defect's EXTENT is a hypothesis; the battery establishes it

Three times in one session a routing message carried my technical diagnosis alongside the demonstrated
defect, and three times the builder found the diagnosis wrong **by measuring before implementing**:

- **C2 / CF-5.** I said `FILE_SHARE_DELETE` was the missing piece. C2 measured five cases and refuted
  it -- the device had opened that way since A16 and `os.replace` still failed, because `MoveFileExW`
  frees the target's directory entry eagerly and **no share mode permits that while handles remain.**
  My same-process reproduction also did not reproduce through the delivered device at all.
- **C5 / the commit number.** I told three components the csn is known at §8.5 step 3.2. It is not:
  at 3.2 you hold the last *published* number, the csn is the COMMIT record's LSN assigned at 3.4, and
  the batch size depends on which pages the insert touched -- **a genuine circularity** I had not seen.
- **C9 / the `fsum` overflow.** I said only `dot` was exposed, because `norm` and `euclidean` overflow
  their squares to `inf` first and reach the guard. True for one huge component, **false for many
  moderate ones**: a hundred components of ~3.16e153 have finite squares whose exact sum overflows.
  C9's own mutation (R03) survived in `norm` and named the missing test.

The demonstrated defect was correct every time; the **explanation of how far it reached** was not. A
demonstration proves a case; only a sweep proves a boundary, and the coordinator is the one party who
did not run one.

**Practice: route the demonstration and the fix, and mark the mechanism as a hypothesis to confirm or
refute before implementing.** "Confirm or refute the mechanism before you build on it" cost one extra
step in C2's case and prevented a fix built on a wrong diagnosis. Say it every time. And when a
builder refutes it, that is the process working -- record the correction where the next reader will
find it, not in a message that scrolls away.

## L21 — a "saved" marker must hold an IMAGE, not a reference to the live object

C1's `CatalogStore` compared `_persisted` against `_catalog` to decide whether it held unsaved
changes. `_persisted` was a **reference to the very object `_catalog` goes on mutating**, so the two
always compared equal and **every unsaved change looked saved**. It cost a test failure to find, and
the guard it disabled is the one standing between a stale participant and a destroyed schema.

The general form, and it is not confined to catalogs: **any marker recording "what we last wrote"
must capture a value that cannot change underneath it** -- a serialised image, a digest, a deep copy,
a version number. A reference records only "the object is still itself", which is always true.

Note the family this belongs to. It is the same defect as a consistency check that shares an
assumption with the thing it checks (C1's drift comparison agreeing with the damage), and as a test
that verifies an operation by reading back through the answer the operation returned (L5). **In all
three, two things that must be independent are secretly the same thing**, and the tell is a comparison
that can never fail.

Corollary for reviewers: a guard whose comparison **cannot** produce inequality is dead in the way
A34 means, and no test will say so, because it passes.

## L22 — one shared signal, three defects: a process-local counter cannot speak for a shared file

Three separate blocking defects in this build trace to a single assumption:

- **CF-6** (C4): the WAL held its segment index in memory from `open()`, so two participants assigned
  the **same LSN** and a cold reader could not replay.
- **The cross-participant staleness** (C1): a participant that had already read a table never saw
  another process's committed rows, because the derived state is keyed on an epoch **only its own pool
  advances** -- and the catalog half was worse, since dropping frames says nothing to an object that
  holds none.
- **E1** (C5/C10): `CatalogStore._require_derived_from_current_pages` cannot catch a lost schema commit
  for the same reason, and the OCC predicate never runs on an empty interest set.

Each fix was correct for its component and none generalised, because **the assumption was never stated
as an assumption.** A cache, an epoch and an index are all *derived state*, and derived state over a
**shared** file needs a signal that moves when someone else writes -- which a process-local counter
never does.

C1's `begin_read_view(token)` states the constraint honestly rather than pretending to solve it: the
component **cannot** detect a foreign write on its own, because the only shared thing it holds is the
device and asking the device means reading the page -- the cost the cache exists to avoid. So the
caller supplies a token it already watches (the durable WAL tail, the lease epoch), and **no token
means assume it moved.** Fail-safe by default.

**Practice for W6 and beyond: for every piece of derived state, write down what shared signal
invalidates it.** If the answer is "a counter this process owns", it is wrong for a multi-process
database, and the defect is waiting for someone to run two participants.

## L23 — a durability check read back through the same cache is not a durability check

`tests/index/test_staleness.py:308 test_the_stale_flag_survives_reopening_the_file` reopened the index
through the **same** `database.pool` that had just written the flag. It passed while the file on the
device carried `flags=0`. The write never reached the disk and the test could not tell, because the
thing it asked was the thing that had made the write.

This is L16's shape moved one layer down: the assertion and the defect share a component, so the
comparison can never fail. It is also L12's shape — a reader weaker than the real one certifies
nothing — and L21's: two things that should be independent are secretly the same.

**The rule:** a test whose subject is *durability* must read from a cold pool over the device, or from
a second process. Never from the cache that performed the write.

**The corollary that found the defect:** §8.5 step 6 deliberately does not fsync data pages, because
the WAL is the authority and redo is idempotent. Any write that **no WAL record covers** is therefore
outside that guarantee and must flush itself. C7 already knew this for `create()` and stated it in the
code; `mark_stale` was the second such write and did not. When a component has one "the log does not
cover this" flush, enumerate the others — there is rarely exactly one.

## L24 — when a component has two regimes and one has an independent safety net, every test gets written in the safe one

C9's battery survivor R20 reverted the rule that a tombstone must reach a warm graph. Every existing
test of that rule ran in the **exact** regime — which reads the heap, and would therefore have excluded
the deleted row *however the graph behaved*. The rule was tested only where it could not fail. In the
**approximate** regime, where the graph is the only authority, a deleted row stayed rankable: §13's
first wrong-result shape, untested until the battery said so.

The pull is structural, not careless. The safe regime is the one that is easy to assert against,
because a second mechanism is already producing the right answer there. That second mechanism is
exactly what makes the assertion worthless for the rule you meant to test (A62/A67a — two mechanisms
alibiing each other).

**The rule:** when a component answers the same question two ways — exact and approximate, cached and
cold, validated and trusted — enumerate the regimes first and require the rule to be tested in the one
with **no** safety net. If a rule can only be stated in the safe regime, it is not being tested; the
net is.

Same family: L16 (an earlier operation satisfies the assertion), L23 (the cache that made the write
answers the durability question), L12 (a weaker double certifies nothing). In all of them a second
thing is quietly supplying the answer the test believes it is getting from the code under review.

## L25 — a test-suite refactor can delete coverage while the suite stays green, and the diff reads as tidying

C9's battery survivors R26, R27 and R28 reverted the duplicate-version guard in each regime and the
record-id tie-break. **All three had tests.** C9's own rewrite of the suite onto the index framework
deleted them; the names no longer existed anywhere in `tests/vector`. The suite stayed green the whole
time, because a deleted test cannot fail.

This is A82 exactly, and it is invisible to ordinary review: the diff reads as tidying. A reviewer
comparing before and after sees a cleaner suite, not a smaller one. Nothing in a green run distinguishes
"this rule is still guarded" from "the guard was removed along with the rule's only witness."

**The rule:** run the A31 battery after **test-file** changes, not only after production changes. A
refactor of the suite is a change to the only instrument that can detect changes to the code — it must
be measured by something outside itself. Cheap alternative when a full battery is too expensive: diff
the collected test-id set (`--collect-only`) before and after, and account for every id that
disappeared.

Corollary for reviewers: "the suite is green after my refactor" is not evidence. It is the claim that
needs evidence.

Same family as L4 (static inspection of intent is adversarially unwinnable — measure coverage instead)
and L18 (a mutation that changes no behaviour is invalid). The battery is the only witness that sits
outside the suite.

## L26 — battery driver faults on this platform, and the guards that caught them

Three instrument faults hit real batteries this session. All three were caught by guards the contract
already required, which is the point: each would otherwise have produced *phantom findings* rather than
a visible error.

1. **Anchors written with `
` against a CRLF file match zero times.** This repo has mixed line endings
   (`planner.py` is CRLF; most files are LF; C6 found five of six targets CRLF). **A70's exactly-once
   check reported 19 hard errors instead of 19 phantom survivors.** C1 and C6 both hit it. Normalise
   both sides before comparing (A59).
2. **A text-mode journal rewrites line endings on restore.** C7's driver would have silently converted
   all 366 LF endings in `records.py`. **A76's sha-verified restore caught it.** Read and write journal
   payloads as bytes.
3. **Two batteries over one fork root corrupt each other.** C7 chased "non-determinism" that was its own
   backgrounded battery mutating the fork its foreground runs were reading. **A80.1 applies to two
   batteries over one root, not only to forking from a moving tree.** Its driver now stamps
   `.battery-running` and refuses.

Also operational: **the session scratchpad is shared between components, not private.** Another
component's `battery.py` overwrote C6's mid-run. Use a per-component subdirectory and a specific
driver name.

None of this changes the quality bar. It is tooling that any battery on this platform needs, and the
reason to write it down is that each fault presents as a *result* rather than as an error.

## L20 (fourth instance) — my framing of P6 was wrong, and C7 measured it

I told C7 that `clear_stale` and `note_reconciled` were "the conservative direction -- a needless
rebuild, never a wrong answer." C7 measured and refuted the second half. The removals a reconciliation
pass makes travel as `INDEX_RECONCILE` records and reach the device, but **the horizon that authorised
them is covered by nothing**, and replay does not restore it (`apply()` erases entries and never calls
`reconciled_to`). On a cold pool, `verify()` reports `missing_entry` on a perfectly clean index -- the
cries-wolf failure `create()` already flushes to avoid.

`clear_stale` and `advance_built_through` *are* conservative. `note_reconciled` is not. Fourth time a
builder has refuted my claim about a defect's **extent** by measuring it.

### L26 addendum — the shared scratchpad shares TOOLS, not just space, and the failure mode is a plausible wrong number

C7's battery driver at `scratchpad/battery.py` was **silently overwritten by C13's driver**. L3 made
fork *roots* unique because the scratchpad is shared; the *drivers* in it are shared too, and a generic
name is a collision waiting to happen.

Nothing was damaged this time only because the two drivers took their fork path differently. **Had they
been argument-compatible, one component's mutants would have been scored against another component's
suite, and the run would have reported a kill rate for it** — a number that looks exactly like a real
measurement. That is the hazard: not a crash, a plausible wrong answer.

Driver, journal and fork-path file go in a per-component subdirectory (`scratchpad/<component>/`), with
a specific name. Anything in the bare scratchpad is shared mutable state between agents.

### L26 addendum 2 — the right question for an unlogged write (C7's sharpening, better than mine)

I framed the `note_reconciled` mistake as a misclassification. C7 framed it better, and its version is
the one to carry:

> I had classified it with `clear_stale` and `advance_built_through` because all three write the same
> header page, and the grouping felt obviously right. What separates it is not **where it writes** but
> **what replay can rebuild**.

The removals travel as `INDEX_RECONCILE` records; the horizon that authorised them travels in nothing;
`apply()` never calls `reconciled_to`. So the two halves come back **asymmetrically** and `verify()`
accuses a clean index.

**The general question for any write no log record covers is "if I lose only this, what does the
surviving state now claim?" — not "is this write important?"**, which is what grouping by page tempts
you to ask. A write can be unimportant on its own and still leave the survivors telling a lie.

## L27 — a decode guard needs its encode half, or the component reports its own output as damage

L14 says a guard on the encoder is not a guard on the format: validate on **decode**, because bytes
are a door of their own. C6 did exactly that for lone surrogates -- and left the encoder able to emit
one. `_escaped_code_point` took the `code_point <= 0xFFFF` branch for `0xD83D`, wrote `\ud83d`, and the
reader then refused it.

Measured end to end by C6's own blind critic: `quarantine.capture()` succeeds, `record_retirement`
succeeds, and then **`LedgerStore.export()` -- a FROZEN section 8.6 door -- raises
`GrafxCorruptionDetected` on the component's own freshly written bytes.** A false integrity incident,
which is precisely what A11-revised exists to prevent.

**The rule:** when a decode guard is added, ask what the encoder can still produce that the new guard
refuses. The two halves must agree about what the format can carry. L14 is about where the guard that
stops *incoming* damage belongs; this is about the encoder never manufacturing that damage itself.

The general shape: **a validator and a producer that disagree turn a correct check into a
self-inflicted fault.** The check is right, the producer is right in isolation, and the pair is broken.
Fixed by refusing a lone surrogate at encode with `GrafxConfigurationError` -- a caller's mistake, not
damage -- and pinned by `test_the_writer_never_produces_what_the_reader_refuses` (5 shapes).

Footnote, because it is the same lesson one level up: the coordinator's own script that appended this
entry died with `UnicodeEncodeError` on the lone surrogate in its text. A non-`Grafx*` escape from an
unpaired code point is not hypothetical -- it is what happens to anything that later re-serialises the
value, which is the reason the decode guard's docstring gives for existing.

## L28 — a value that has only ever had one possible answer is not being tested, it is being echoed

`install_crc32c` returns the name it REPLACED, and says so in its docstring. The composition root's
`install_checksum` promised the name it INSTALLED and returned that value straight through. The two
are different things -- and for the whole life of the project they were the same string, because the
pure-Python reference was the only implementation that could ever be in effect. Replaced == installed
== "pure", on every path, in every test.

The defect appeared the moment a machine had a native provider: `install_checksum(checksum="pure")`
answered `"native"`. Nothing about the code had changed; the ENVIRONMENT had gained a second possible
answer, and the first thing it did was tell the two apart.

**The rule:** when a value can currently take only one answer, no test of it is a test. A test that
asserts `f() == "pure"` where "pure" is the only string the system can produce passes against every
implementation of `f`, correct or not -- it is the same shape as L16 (an earlier operation satisfies
the assertion) and L24 (the regime with a safety net is the one that gets tested), one level more
abstract: the assertion cannot fail, so it measures nothing.

**What to do about it:** when a port has one adapter, a selector has one reachable value, or an
optional dependency is absent everywhere the suite runs, that surface is UNMEASURED however green it
looks. Either make a second answer reachable in the test (install the extra in CI, inject a second
adapter, force the selector) or write the gap down. The three of these in this repository were the
checksum implementation, the vector math adapter, and the platform family -- and the platform family
is the one C13's cross-family matrix exists to keep honest, which is the shape of the answer for the
other two.

Corollary for a review: "this assertion has never failed" and "this assertion cannot fail" look
identical in a green run, and the battery is what distinguishes them -- but only if the mutation can
produce the second answer at all. A mutation battery on a single-adapter port is measuring the same
blind spot it is meant to find.

## L29 — a set that three different jobs share must be MEASURED, not re-derived from intent

`_pages_touched_by` answered one question -- "which pages did this commit change?" -- for three
jobs: what the log carries, what the interest set declares, and what an abandoned attempt undoes.
It answered it by re-deriving the pages from where the commit's ROWS landed. A heap append changes
a page no row lands on: the previous last page, whose `next_page` is the only thing that makes the
new page reachable. So the link was never logged, never declared, and never undone.

The third one is what corrupted databases. A refused attempt left the pool holding a tail page
pointing at the page it had just abandoned; the next commit of any participant flushed that link to
the device; a later walk followed it into a page nobody had written and refused with
`corruption_detected` on a database in which nothing had gone wrong. Three processes appending to
one table reproduced it in under a minute. **7858 tests were green.**

**The rule:** when several jobs depend on the same answer about what code DID, derive that answer
from what actually happened, not from a second reading of what the code was supposed to do. The
re-derivation has to name every site that touches a page, and the day a site is added it is silently
short by one -- with no test able to see it, because nothing declares the omission. The fix reads
the buffer pool twice, before and after the attempt, and takes the difference.

**And the first version of that fix was short too, which is the sharper half of this lesson.** It
measured the frames the pool still held DIRTY. A page the attempt changed and the pool then evicted
was written back, marked clean, and left the measurement -- so a commit larger than the buffer
budget logged the new pages and not the links that reach them: 400 rows in one commit against a
256-frame budget replayed to 2 of 402. "Measured, therefore complete" is not an argument; the
question is always what the instrument DROPS. A dirty flag is not a record of what happened, it is a
record of what has not been dealt with yet, and those are different sets the moment anything deals
with one. The write-back now remembers the page instead of forgetting it.

**Corollary, and it is the sharper half:** the enumeration was not obviously wrong. It carried a
careful docstring arguing that including a page that did not change is harmless and missing one is
not, and it was RIGHT about that -- it just did not include a page it did not know about. Prose that
argues for the safe direction is not evidence that the code went that way. Ask what produces the set,
not what the comment says about it.

## L30 — the regime that breaks is the one nothing runs

Every concurrency test in this repository did one of three things: drove the engine below the public
door, read with a fresh short-lived process, or contended hard enough that the writers serialised.
The defect of L29 needed all three to be false at once -- long-lived processes, appending to one
table, through `connect()`, contending little enough that their appends overlapped. Contention
HIDES it: with a shared row forcing conflicts, three writers serialise and the window closes, which
is why the arm of the experiment with the most conflicts was the arm that came out clean.

**The rule:** a concurrency suite that only tests the contended case tests the case where the
protocol is doing the least work. The uncontended concurrent case -- several writers whose work does
not intersect, which is the case a user expects to be FAST and therefore the case they will run --
is a different regime, and it is the one where a missing declaration goes unnoticed, because nothing
refuses. Same shape as L24: the regime with a safety net is the one that gets tested.

Practical consequence, recorded as a standing obligation: `tests/smoke/` exists now, it drives only
the public door, it is slow on purpose, and a cheaper version of it did not find this. Before
declaring a concurrency property held, name the regime the test ran in and the regime it did not.


## L31 — writing "CLOSED" is a claim, and the register is where an unearned one does damage

The round-1 fix for L29 was real, tested and an improvement. What was wrong was the sentence next to
it: `COMPONENTS.md` said CF-14 was CLOSED and `LESSONS.md` stated the general rule as proven, while
the code held only for buffer budgets large enough never to evict. The blind critic's own report put
it exactly: the defect it found was "not a regression" -- the pre-change code was worse on the same
input -- and it was blocking anyway, because *"that gap between the claim and the code is what makes
it a finding rather than a footnote."*

**The rule:** a register entry is load-bearing. The next person to touch this code will read CLOSED
and not re-derive the case, and a lesson stated as a general rule will be applied where it does not
hold. So the claim has to name the regime it was established in. "Closed for commits that fit the
buffer budget" would have been true, would have cost one clause, and would have pointed straight at
the case that was still open.

Practical form, and it is the same shape as L28 and L30: before writing CLOSED, name the parameter
you held fixed while measuring -- the budget, the process count, the table count, the page size --
and either vary it or write down that you did not.


## L32 — a component can be complete, tested, signed off, and reachable by nobody

C7 built an index framework: a hash index, the dual visibility rule of section 8.7, candidate
validation against the heap, staleness detection, rebuild, recovery. C10 built `IndexSeek` into the
planner and wired `_index_definitions` to feed it. Both were reviewed and both were right. And
`db.indexes.indexes()` returned `()` on every database anyone ever created, because no statement in
the grammar creates an index and `PRIMARY KEY` did not either. `_index_for` searched an empty list,
`IndexSeek` was never once chosen, and a facility that was fully built was fully unreachable.

Every test of C7 passed, because they built their indexes by hand. Every test of C10's planner
passed, because they handed it definitions directly. The seam between "the framework can hold an
index" and "something puts one in it" belonged to neither component's suite, so neither component's
suite covered it -- and the integration tests did not notice either, because a scan returns the same
ROWS as a seek. Only the clock told the difference, and no test asserts a clock.

**The rule:** when two components meet, ask what CREATES the thing they exchange, and write a test
at the outermost door that asserts it exists. Not that it works -- that it EXISTS. `assert
db.indexes.indexes() != ()` after a `CREATE TABLE` would have caught this on the first day, and it
is one line.

Same family as E3, which was the commit seam not populating any index, and it is not a coincidence
that both defects lived on a seam and both were invisible to a green suite. E3 was found by
counterfactual (revert the seam, watch 1615 tests stay green); this one was found by a smoke test
that would not finish. Neither was found by anything that was looking.

**Corollary for a review:** "component X is signed off" and "the feature X provides is reachable"
are different claims, and a per-component definition of done can only ever establish the first.
