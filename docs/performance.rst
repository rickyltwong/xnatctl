Performance
===========

This page records what the transfer paths actually cost, so that a change
which makes them slower or hungrier is visible in review rather than
discovered in production.

Reproduce it with::

   uv run python scripts/bench_transfer.py                 # client cost
   uv run python scripts/bench_transfer.py --latency-ms 25 # LAN-like

The script runs each case in its own subprocess against a local fake XNAT and
reports wall time, throughput, and peak RSS.


How to read these numbers
-------------------------

The server is a loopback ``ThreadingHTTPServer`` handing back bytes prepared
in advance. That makes the numbers a measure of **xnatctl's own cost** --
thread pool, HTTP stack, archiving, extraction -- and comparable between runs
on the same machine.

They are **not** a prediction of throughput against a real XNAT. A real server
does database work, and a real link has bandwidth limits neither of which
appear here. Use the relative differences, not the absolute rates.

``--latency-ms`` adds server think time per response. It is the knob that
makes the difference between the transfer strategies visible, because their
request counts differ by three orders of magnitude.


Baseline
--------

Recorded 2026-08-06 on xnatctl 0.2.11.

**Machine:** Intel Xeon E5-2650 v4 @ 2.20GHz, **4 cores**, 23 GiB RAM, XFS,
Linux 5.14 (RHEL 9), CPython 3.11.14. The four-core count matters: it is why
worker counts above 4 stop helping on anything CPU-bound.

**Workload:** 1024 files x 32 KiB = 32 MiB, spread over 16 scans for download.

At 0 ms latency -- pure client cost:

.. list-table::
   :header-rows: 1
   :widths: 26 12 14 14 18 16

   * - Path
     - Workers
     - Seconds
     - Files/s
     - Peak RSS
     - RSS growth
   * - ``session download``
     - 1
     - 0.92
     - 1117
     - 65 MiB
     - 16 MiB
   * - ``session download``
     - 4
     - 1.07
     - 960
     - 76 MiB
     - 27 MiB
   * - ``session download``
     - 8
     - 1.15
     - 888
     - 85 MiB
     - 34 MiB
   * - ``session download``
     - 16
     - 1.15
     - 888
     - 129 MiB
     - 80 MiB
   * - ``session upload`` (tar)
     - 1
     - 0.50
     - 2067
     - 52 MiB
     - 6 MiB
   * - ``session upload`` (tar)
     - 4
     - 0.53
     - 1950
     - 56 MiB
     - 10 MiB
   * - ``session upload`` (tar)
     - 8
     - 0.62
     - 1655
     - 66 MiB
     - 21 MiB
   * - ``session upload`` (zip)
     - 4
     - 0.67
     - 1521
     - 56 MiB
     - 10 MiB
   * - ``session upload`` (gradual)
     - 1
     - 1.95
     - 526
     - 52 MiB
     - 6 MiB
   * - ``session upload`` (gradual)
     - 8
     - 2.05
     - 500
     - 59 MiB
     - 13 MiB

At 25 ms latency -- what a LAN-attached XNAT looks like:

.. list-table::
   :header-rows: 1
   :widths: 26 12 14 14 18 16

   * - Path
     - Workers
     - Seconds
     - Files/s
     - Peak RSS
     - RSS growth
   * - ``session download``
     - 1
     - 1.46
     - 699
     - 67 MiB
     - 17 MiB
   * - ``session download``
     - 4
     - 1.14
     - 900
     - 80 MiB
     - 30 MiB
   * - ``session download``
     - 16
     - 1.20
     - 851
     - 122 MiB
     - 72 MiB
   * - ``session upload`` (tar)
     - 4
     - 0.62
     - 1651
     - 58 MiB
     - 12 MiB
   * - ``session upload`` (gradual)
     - 1
     - 28.19
     - 36
     - 52 MiB
     - 6 MiB
   * - ``session upload`` (gradual)
     - 4
     - 7.32
     - 140
     - 55 MiB
     - 9 MiB
   * - ``session upload`` (gradual)
     - 8
     - 3.92
     - 261
     - 59 MiB
     - 13 MiB


What the numbers say
--------------------

**Neither path buffers its payload.** Moving 32 MiB grows RSS by 6 MiB
(upload) to 16 MiB (download) at one worker. A separate 128 MiB run -- 256
files x 512 KiB, closer to real DICOM slice sizes -- grew RSS by 11 MiB
uploading and 28 MiB downloading, and peaked at 111 MiB even at 16 workers.
The streaming claims made about both paths hold under load.

**Memory scales with workers, not with data.** Download costs roughly 4 MiB of
peak RSS per worker; the uploads about 2 MiB. Sixteen download workers is the
difference between a 65 MiB process and a 129 MiB one. On a memory-limited
box, ``--workers`` is the dial that matters, and the payload size is not.

**The upload mode dominates everything else.** At 25 ms latency, the batched
default moves 1651 files/s and the gradual handler 140 -- a factor of twelve.
The batched path sends one archive per worker, so its wall time barely notices
latency; the gradual path sends one request per file and pays the round trip
1024 times. Reach for ``--mode gradual`` when a session must be uploaded
file-by-file, not for speed. ``tar`` also beats ``zip`` by about 28 percent on
archive creation, which is why ``tar`` is the default.

**More workers only help where there are requests to overlap.** Gradual scales
close to linearly (36 to 140 to 261 files/s across 1, 4, and 8 workers).
Download and batched upload issue too few requests for that: download tops out
at 4 workers on this 4-core machine and then flattens, because it is bound by
ZIP extraction rather than by the network. The default of 4 is the right one
here; raising it past the core count spends memory for nothing.


Known limits of this harness
----------------------------

- **A loopback server has no bandwidth limit.** Real transfers of hundreds of
  megabytes are usually link-bound, and nothing here models that.
- **The fake server does no work.** A real XNAT parses DICOM, writes to a
  database, and updates a catalog on every import. Absolute upload rates
  against it will be far lower.
- **The download path builds a fresh** ``httpx.Client`` **per scan**, not per
  thread, so connection reuse across scans does not happen. On loopback that
  costs almost nothing. Over TLS on a real link, each one pays a fresh
  handshake -- this measurement cannot show that cost, and it is worth
  revisiting on real hardware before assuming it is negligible.
- **One machine, one run each.** These are not averaged over repetitions and
  carry no error bars. Treat a difference under about 10 percent as noise.

There is deliberately no automated performance gate. A flaky perf test that
fails on a busy CI runner teaches people to ignore failures, which costs more
than the regressions it would catch. Run the script when touching the transfer
paths and compare against the table above.
