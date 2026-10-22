# Stops: what an answer looks like when a gate says stop

Three short examples, one per gate. Each ends in a plain-words answer to the user; no card
is applied and no report block is filled. They are illustrative: the numbers are consistent
but were not produced by a fixture. A stop writes no report, oracle test or `.devkit` file
and runs no A/A test; one labelled timing for scale is enough. A local fix made after the
stop (Example C) is ordinary work, measured the usual way.

## Example C: stops at Gate 1, then does the ordinary fix

"Our import script is slow": 45 s for 3,000 records, "I was thinking a hash map". `time`
reports real 45.2 s, user 1.9 s, sys 0.4 s. Profile: 94% in `socket.recv`. Verdict: the
process is waiting on the network (one HTTP call per record). Not algorithmic; no card is
opened, no template is filled. The answer to the user, in full:

> The loop is not the problem. Of the 45.2 s, the CPU is busy for 2.3 s (user 1.9 + sys 0.4);
> the other 43 s the process is waiting for the network, and 94% of the profile is in
> `socket.recv`. A hash map or any other change to the loop can only touch the 2.3 s. The
> 3,000 records cost one HTTP call each, about 15 ms a call. Two fixes, in order: the API
> accepts 100 ids per request, so batching turns 3,000 calls into 30; if batching is not
> available, run the calls with bounded concurrency (8 to 16 in flight). Either should land
> under 10 s.

You asked for the import to be faster, and batching is a local change that is easy to undo,
so the answer continues with the fix itself, without a card or a report block. Ten
interleaved runs of each arm against the real API would be about 3,000 x 10 = 30,000 calls
for the old loop alone, so the repeated runs use a recorded client instead
(`measurement.md` section 9): one real run of each version records the responses and their
latencies, and every timed run replays them with the same per-request delay.

> I switched the loop to the batch call (100 ids per request, 30 requests). To time it
> without hammering the API, I recorded one real run of each version and replayed the
> recorded responses with their real latencies: 10 interleaved runs each, outputs compared
> run by run. Median 45.2 s before, 3.4 s after (IQR 0.6 s and 0.3 s), 13x faster (95% CI
> 11x..15x); the records written are identical. The one live run of the batch version took
> 3.6 s. Not verified: behaviour when the API rate-limits a batch.

When the fix needs something outside the code (a vendor API change, paid capacity, a change
other people must approve), the answer stops at the verdict and the two fixes instead.

## Example D: stops at Gate 2

"Should I use Aho-Corasick or a trie here? 40 patterns, about 2,000 lines, runs nightly.
Want it optimal." The code is `for line in lines: for p in patterns: if p in line`. Step 0:
the files are present, so the current script is timed once: 4 ms for the scan, about 20 ms
wall including interpreter startup (single run, for scale). Gate 2: 40 x 2,000 = 80k C-level
substring checks now, 800k at 10x, about 40 ms from the 4 ms timing, against a 1 s budget for
a nightly batch. The measured rate is about 2e7 checks a second (50 ns each, because each
check scans a whole line); `bound ops --runtime c-builtin` assumes 2e8 elements a second and
prints 4 ms at 10x, ten times under the measurement, so the timing is the number used. Stop. The symptom index names
`cards-multi-pattern.md`; its precondition line is read for the threshold and nothing is
applied. The answer to the user, in full:

> Neither. Keep the loop. It does 40 x 2,000 = 80,000 substring checks, each in C, and the
> scan took 4 ms when I timed it (one run, enough for scale). At ten times the input, 800,000
> checks, it is about 40 ms, against a job that runs once a night. Aho-Corasick starts to pay
> at about 50 or more literal patterns over text far longer than the patterns themselves,
> and in Python only through a compiled library (`pyahocorasick`), which is a new dependency;
> a hand-written version would be slower than `in` at this size. A trie on its own does not
> do substring search at all; it is the structure Aho-Corasick is built from. The point where
> a change would matter is about 2e7 checks a run, a second at the speed I measured: 10,000
> patterns over these 2,000 lines, or 40 patterns over 500,000 lines. Below that, "optimal" is the code you have. If you want the
> evidence on your logs, I can time the matching on a real nightly file; the verdict will
> hold.

## Example E: stops at Gate 3

"Rewrite the tokenizer in Rust; the report job takes 9 minutes." The profile of one run
gives the tokenizer 65 s of the 540 s: a 12% share, at production size.
`dev.py bound amdahl --p 0.12 --s 100` prints 1.13x end to end and a cap of 1.14x for any
speedup of that share, under 1 + 2 x this job's 8% run-to-run spread in its CI history
(1.16). Stop. The answer to the user, in full:

> A faster tokenizer cannot move the 9 minutes much. It is 65 s of the 540 s (12%), so even
> an infinitely fast one saves at most 65 s: 9 minutes becomes about 7.9, a 1.14x gain, and
> a 100x faster rewrite gives 1.13x. The other 475 s are where the time is: 310 s in
> `render_tables` and 120 s reading the input files. I would look at `render_tables` first;
> I have not profiled inside it yet.

The Gate 3 answer gives the profiled share and the size it was profiled at, the cap and the
end-to-end gain for the hoped speedup, and where the rest of the time goes.
