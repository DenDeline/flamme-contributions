# Run the stock Elsa PostgreSQL control

This .NET 10 Web project consumes the official Elsa 3.10.2 packages from nuget.org. It runs one `Event("store-bench")` code workflow through Elsa's default local runtime with EF Core PostgreSQL on both workflow management and runtime. Every run starts with a dedicated empty PostgreSQL 16 container. Kestrel and the database bind to loopback. The launcher generates fresh database and API credentials and removes its container, volumes and temporary working directory on exit.

## Reproduce

Use the .NET 10.0.400 SDK (or a matching patch selected by `global.json`), the .NET 10 ASP.NET Core shared runtime, Python 3 and a running Docker engine with access to `postgres:16`. From the directory containing this README, use an empty package cache and fresh output paths.

```sh
run_dir="$(mktemp -d)"
dotnet restore UpstreamControl.csproj --locked-mode --configfile NuGet.Config --packages "$run_dir/packages"
dotnet build UpstreamControl.csproj -c Release --no-restore
python3 -B run.py --smoke --output "$run_dir/routes.jsonl"
python3 -B run.py --output "$run_dir/trials.jsonl"
python3 -B serial.py "$run_dir/serial.json"
```

The smoke run registers a forwarding store decorator for public method counts. The 48-peer and serial runs use the ordinary DI-selected store, with tracing disabled. Build, startup migrations, definition registration, workflow creation, export preparation and audit requests are outside the timed requests. A fresh SQL connection and request scope verify persisted state. The full run exits with an error on a failed HTTP request, a missing writer witness, a premature anchor completion or a failed final audit.

## Workload and compatibility

- **Seed.** The authenticated fixture endpoint `/seed/{id}` calls the stock runtime client's `CreateAndRunInstanceAsync` on a published code definition. The SQL observer verifies one suspended instance and one durable event bookmark for each ID.
- **Resume.** The timed request posts an instance ID and correlation ID to Elsa's stock authenticated `/elsa/api/events/store-bench/trigger` with synchronous execution. It reaches the final workflow commit. Both stored representations must say Finished and the bookmark must have been consumed.
- **Explicit save.** The fixture's `/prepare/{id}` exports a finished instance's serialized state before timing. Its timed `/save/{id}` request deserializes the state, changes its correlation marker, maps it with the DI-provided Elsa mapper, and awaits `IWorkflowInstanceStore.SaveAsync`. Updating an existing row through the fork's stock HTTP import requires this adaptation. Elsa 3.10.2 rejects an existing ID on archive restore. [Archive experiment](../evidence/upstream.archive.json)
- **Observer.** The standalone reference directory contains the I7 driver's timing functions, HTTP call implementation and SQL observer. The Elsa launcher supplies the authentication and request construction. For an independent comparison, `--driver /path/to/I7/run.py` accepts the original driver with its sibling `observer.py`. Each JSONL run records hashes for the driver, SQL observer, launcher, lock file and host assembly.
- **Controls.** One warmup and three measured rounds each include a no-hold wave and a held-row wave. Every wave resumes 49 existing suspended IDs and saves those same 49 finished instances. In a held wave, one writer must be visibly waiting on the holder's PostgreSQL backend before the 48 other IDs are released. The observer samples durable state before releasing the row after about one second. Peer latency uses nearest-rank percentiles. `serial.py` measures the separate one-request-at-a-time control.

The published packages, database image and 17 applied migration IDs are recorded with the measurements. The Flamme reference web host additionally enables distributed runtime, caching and multitenancy. This experiment uses the stock single-host registration and default tenant. [Package research](../results/upstream-provenance.json) · [Measured results](../results/upstream-trials.jsonl)
