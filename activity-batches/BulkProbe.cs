using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Net.Http;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using Flamme.Workflows;
using Flamme.Workflows.Runtime;
using Flamme.Workflows.Runtime.Entities;
using Flamme.Workflows.Runtime.Filters;
using Microsoft.EntityFrameworkCore.Diagnostics;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Npgsql;

// Adds a background client to the unchanged reference server via the existing test composition hook.
public class StartupHook
{
    public static void Initialize() => Program.ConfigureForTest = module => module.Services.AddHostedService<BulkProbe>();
}

public sealed class SqlObserver : IObserver<DiagnosticListener>, IObserver<KeyValuePair<string, object?>>, IDisposable
{
    private readonly List<IDisposable> subscriptions = new();
    public readonly List<string> Commands = new();
    public readonly AsyncLocal<bool> Active = new();
    public int Starts;
    public int Commits;
    public void OnNext(DiagnosticListener listener)
    {
        if (listener.Name == "Microsoft.EntityFrameworkCore")
            subscriptions.Add(listener.Subscribe(this, name => name == RelationalEventId.CommandExecuted.Name
                || name == RelationalEventId.TransactionStarted.Name || name == RelationalEventId.TransactionCommitted.Name));
    }
    public void OnNext(KeyValuePair<string, object?> ev)
    {
        if (!Active.Value) return;
        if (ev.Key == RelationalEventId.TransactionStarted.Name) Starts++;
        if (ev.Key == RelationalEventId.TransactionCommitted.Name) Commits++;
        if (ev.Value is CommandExecutedEventData completed)
            Commands.Add(completed.Command.CommandText);
    }
    public void OnError(Exception error) { }
    public void OnCompleted() { }
    public void Dispose() { foreach (var subscription in subscriptions) subscription.Dispose(); }
}

public sealed class BulkProbe(IServiceProvider services, IConfiguration configuration, IHostApplicationLifetime lifetime) : BackgroundService
{
    static void Output(object value) => Console.WriteLine("BULK " + JsonSerializer.Serialize(value));

    protected override async Task ExecuteAsync(CancellationToken token)
    {
        try
        {
            using var http = new HttpClient { BaseAddress = new Uri(Environment.GetEnvironmentVariable("ASPNETCORE_URLS")!) };
            var startup = Stopwatch.StartNew();
            while (startup.Elapsed < TimeSpan.FromSeconds(90))
            {
                try { using var response = await http.GetAsync("/health/ready", token); if (response.IsSuccessStatusCode) break; }
                catch (HttpRequestException) { }
                await Task.Delay(100, token);
            }
            if (startup.Elapsed >= TimeSpan.FromSeconds(90)) throw new Exception("Reference host did not reach readiness");

            using var scope = services.CreateScope();
            var store = scope.ServiceProvider.GetRequiredService<IActivityExecutionStore>();
            await using var observer = new NpgsqlConnection(configuration.GetConnectionString("PostgreSql"));
            await observer.OpenAsync(token);
            using var version = new NpgsqlCommand("SHOW server_version", observer);
            using var migrations = new NpgsqlCommand("""SELECT count(*) FROM "Elsa"."__EFMigrationsHistory" """, observer);
            Output(new { kind = "host", version = await version.ExecuteScalarAsync(token), migrations = await migrations.ExecuteScalarAsync(token),
                runtime = Environment.Version.ToString(), store = store.GetType().FullName });

            foreach (var size in new[] {50, 500})
            foreach (var shape in new[] {"existing", "half-new"})
            {
                var prefix = $"bulk-review-{size}-{shape}";
                var group = prefix;
                if (shape == "existing")
                {
                    await store.SaveManyAsync(Rows(group, size, 0), token);
                    await Verify(observer, group, size, 0, token);
                }

                async Task PrepareHalfAsync(int iteration)
                {
                    if (shape != "half-new") return;
                    group = $"{prefix}-{iteration}";
                    await store.SaveManyAsync(Rows(group, size / 2, 0), token);
                    await Verify(observer, group, size / 2, 0, token);
                }
                // Recreate detached entities before each call. Only SaveManyAsync lies between timestamps.
                for (var iteration = 1; iteration <= 11; iteration++)
                {
                    await PrepareHalfAsync(iteration);
                    var rows = Rows(group, size, iteration);
                    var start = Stopwatch.GetTimestamp();
                    await store.SaveManyAsync(rows, token);
                    var elapsed = Stopwatch.GetElapsedTime(start).TotalMilliseconds;
                    await Verify(observer, group, size, iteration, token);
                    Output(new { kind = iteration <= 3 ? "warmup" : "measure", shape, size, iteration, milliseconds = elapsed, verified = size });
                }

                // Observe commands on a separate untimed call. No subscription during the timed trials.
                await PrepareHalfAsync(12);
                var diagnostic = Rows(group, size, 12);
                var observation = new SqlObserver();
                using (observation)
                using (DiagnosticListener.AllListeners.Subscribe(observation))
                {
                    observation.Active.Value = true;
                    await store.SaveManyAsync(diagnostic, token);
                    observation.Active.Value = false;
                }
                await Verify(observer, group, size, 12, token);
                int Count(string prefix) => observation.Commands.Sum(sql => Regex.Matches(sql, prefix, RegexOptions.Multiline).Count);
                var firstDml = observation.Commands.First(sql => sql.Contains("UPDATE ") || sql.Contains("INSERT INTO")).Split('\n').First();
                Output(new {kind = "commands", shape, size, commands = observation.Commands.Count,
                    starts = observation.Starts, commits = observation.Commits,
                    catalogProbes = observation.Commands.Count(sql => sql.Contains("pg_catalog.pg_index")),
                    tableLocks = observation.Commands.Count(sql => sql.StartsWith("LOCK TABLE")),
                    advisoryLocks = observation.Commands.Count(sql => sql.Contains("pg_advisory_xact_lock")),
                    reads = Count("^SELECT"), upserts = Count("ON CONFLICT"), inserts = Count("^INSERT INTO"), updates = Count("^UPDATE "),
                    readSample = observation.Commands.FirstOrDefault(sql => sql.StartsWith("SELECT"))?.Split('\n').First(),
                    dmlSample = firstDml[..Math.Min(176, firstDml.Length)]});

                using var freshScope = services.CreateScope();
                var read = (await freshScope.ServiceProvider.GetRequiredService<IActivityExecutionStore>().FindManyAsync(
                    new ActivityExecutionRecordFilter {WorkflowInstanceId = group}, token)).ToArray();
                if (read.Length != size || read.Any(x => x.ActivityName != "revision-12" || x.ActivityState?["revision"]?.ToString() != "12"))
                    throw new Exception("Public readback failed");
                Output(new {kind = "readback", shape, size, verified = read.Length});
            }
            Output(new {kind = "pass"});
        }
        catch (Exception error)
        {
            Output(new {kind = "fail", error = error.ToString()});
            Environment.ExitCode = 1;
        }
        finally { lifetime.StopApplication(); }
    }

    static ActivityExecutionRecord[] Rows(string group, int size, int iteration) =>
        Enumerable.Range(0, size).Select(i => new ActivityExecutionRecord
        {
            Id = $"{group}-{i}", WorkflowInstanceId = group, ActivityId = $"activity-{i}", ActivityNodeId = $"node-{i}",
            ActivityType = "Console.WriteLine", ActivityTypeVersion = 1, ActivityName = $"revision-{iteration}",
            StartedAt = DateTimeOffset.UnixEpoch, Status = ActivityStatus.Completed,
            SerializedSnapshot = new ActivityExecutionRecordSnapshot
            {
                SerializedActivityStateCompressionAlgorithm = "None",
                SerializedActivityState = $"{{\"revision\":{iteration},\"ordinal\":{i}}}"
            }
        }).ToArray();

    static async Task Verify(NpgsqlConnection observer, string group, int size, int iteration, CancellationToken token)
    {
        const string query = """
            SELECT count(*), count(*) FILTER (WHERE
                "ActivityName" = $2 AND "TenantId" = '' AND "SerializedActivityStateCompressionAlgorithm" = 'None'
                AND ("SerializedActivityState"::jsonb->>'revision')::integer = $3
                AND "Id" = ($1 || '-' || ("SerializedActivityState"::jsonb->>'ordinal'))
                AND ("SerializedActivityState"::jsonb->>'ordinal')::integer BETWEEN 0 AND $4 - 1)
            FROM "Elsa"."ActivityExecutionRecords" WHERE "WorkflowInstanceId" = $1
            """;
        using var cmd = new NpgsqlCommand(query, observer);
        cmd.Parameters.Add(new NpgsqlParameter {Value = group});
        cmd.Parameters.Add(new NpgsqlParameter {Value = $"revision-{iteration}"});
        cmd.Parameters.Add(new NpgsqlParameter {Value = iteration});
        cmd.Parameters.Add(new NpgsqlParameter {Value = size});
        await using var reader = await cmd.ExecuteReaderAsync(token);
        if (!await reader.ReadAsync(token) || reader.GetInt64(0) != size || reader.GetInt64(1) != size)
            throw new Exception($"Durable verification failed for {group}, iteration {iteration}");
    }
}
