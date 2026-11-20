# Public activity log bulk saves

The reference host's workflow commit calls `StoreActivityExecutionLogSink.PersistExecutionLogsAsync`, which maps dirty activities and calls `IActivityExecutionStore.SaveManyAsync`. This probe resolves that public interface from the reference host after startup and submits preconstructed activity records. It times the storage portion of the checkpoint. [Caller](../../main/src/modules/Flamme.Workflows.Runtime/Services/StoreActivityExecutionLogSink.cs) · [Public store implementation and serialization callback](../../main/src/modules/Flamme.Persistence.EFCore/Modules/Runtime/ActivityExecutionLogStore.cs)

All three configurations use Release/net10.0 builds, a scoped DI store, three configured tenants pointing to the same local database, and the default tenant for the call. The runner starts one isolated `postgres:16` container, then runs the reference hosts sequentially in main/PR/candidate/candidate/PR/main order. Each process gets its own content root and fresh database. The normal reference server performs its migrations. The existing startup test seam registers one background client. EF command logging is set to Warning on both builds.

At each size (50 and 500) the client exercises two cases. The **existing** group overwrites every row with new values on each call. The **half-new** group gets fresh IDs for each iteration, with half inserted before the call. For each group in each process there are three warmups, eight measured calls, one separately traced call, and a fresh public-store readback. Entity construction, host startup, seeding, SQL diagnostics, and durable verification run outside the stopwatch. Concurrent work on the same physical machine can still affect individual observations.

After every warmup and measured call an independently opened PostgreSQL connection checks the exact row count, default owner, revision marker, compression codec, and the serialized ordinal matching each row's primary key. A fresh service scope also reads the final state through the public store and checks the deserialized activity state. The diagnostic subscriber follows the async context of one additional public save, counts EF commands and statements, and records transaction-start and commit events in that call. These figures exclude the client’s independent verification queries and do not measure protocol round trips. [Probe source](BulkProbe.cs) · [Isolated runner](run.py)

To reproduce, build the three pinned source copies with the same SDK options, then supply their output directories to the runner.

```sh
dotnet build src/apps/Flamme.Server.Web/Flamme.Server.Web.csproj -c Release -f net10.0 \
  -p:NuGetAudit=false -p:RunAnalyzers=false -m:4 --nologo -v:q --disable-build-servers
python3 run.py --main /path/to/main/src/apps/Flamme.Server.Web/bin/Release/net10.0 \
  --previous /path/to/previous/src/apps/Flamme.Server.Web/bin/Release/net10.0 \
  --candidate /path/to/candidate/src/apps/Flamme.Server.Web/bin/Release/net10.0 > trials.jsonl
```

## Results

Main was `7185fa3f2d87bbfa8be4024294801da5ee3b802d`, PR #112 was `d65ae53eaf81f203d2c4231702ab69c3287b5e92`, and the candidate was `3a39d8347ca8facfc4c09f2a59314cb8222e700b`. Each build used a clean disposable git clone, SDK 11.0.100, Release/net10.0, with zero build errors. Each of six completed processes reported .NET 10.0.11, PostgreSQL 16.15, 15 migrations and `Flamme.Persistence.EFCore.Modules.Runtime.EFCoreActivityExecutionStore`. [Measured trials, assembly hashes and command counts](../results/activity-batches.jsonl)

Median public save latency in milliseconds, pooled over 16 measured calls per revision per row. The parenthesized values are the two process medians in execution order.

| Records | Mix | Main | PR #112 | Candidate | Candidate / main |
|---:|---|---:|---:|---:|---:|
| 50 | existing | 4.55 (5.04, 4.55) | 5.49 (5.35, 5.54) | 7.26 (7.85, 6.78) | 1.60× |
| 50 | half-new | 4.55 (4.56, 4.55) | 5.65 (5.91, 5.21) | 7.25 (7.26, 7.22) | 1.59× |
| 500 | existing | 50.64 (52.44, 49.63) | 43.92 (43.61, 47.32) | 61.83 (66.57, 55.59) | 1.22× |
| 500 | half-new | 42.21 (36.38, 45.03) | 32.77 (29.76, 32.77) | 53.63 (54.52, 52.16) | 1.27× |

All 192 timed calls passed durable verification, along with all warmups and 24 separately traced calls. Each fresh-scope public readback returned the expected row count and deserialized revision. Every traced PR and candidate call reported one EF transaction start and commit. Main reported zero EF transaction events. The candidate uses the regular SaveChanges transaction for its multi-statement batch.

| Size and mix | Main EF commands | PR #112 EF commands | Candidate EF commands |
|---|---|---|---|
| 50, existing | 2 (1 ownership SELECT + 1 native upsert) | 4 (2 catalog SELECT + 1 table lock + 1 guarded upsert) | 2 (1 ownership SELECT + one batch of 50 UPDATE) |
| 50, half-new | 3 (2 ownership SELECT + 1 native upsert) | 4 (2 catalog SELECT + 1 table lock + 1 guarded upsert) | 2 (1 ownership SELECT + one batch of 25 UPDATE and 25 INSERT) |
| 500, existing | 20 (10 ownership SELECT + 10 native upsert) | 13 (2 catalog SELECT + 1 table lock + 10 guarded upsert) | 2 (1 ownership SELECT + one batch of 500 UPDATE) |
| 500, half-new | 25 (15 ownership SELECT + 10 native upsert) | 13 (2 catalog SELECT + 1 table lock + 10 guarded upsert) | 2 (1 ownership SELECT + one batch of 250 UPDATE and 250 INSERT) |

The number of statements inside the candidate DML batch grows with the row count. The elapsed timings include mapping detached entries, save handlers, client batching, network I/O, PostgreSQL execution, and commit acknowledgement. The timing aggregates these costs and applies to this local workload.
