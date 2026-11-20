using System.Reflection;
using System.Security.Cryptography;
using Elsa.Common.Models;
using Elsa.Extensions;
using Elsa.Persistence.EFCore.Extensions;
using Elsa.Persistence.EFCore.Modules.Management;
using Elsa.Persistence.EFCore.Modules.Runtime;
using Elsa.Workflows;
using Elsa.Workflows.Management;
using Elsa.Workflows.Management.Filters;
using Elsa.Workflows.Management.Mappers;
using Elsa.Workflows.Models;
using Elsa.Workflows.Runtime;
using Elsa.Workflows.Runtime.Activities;

var builder = WebApplication.CreateBuilder(args);
var connection = builder.Configuration.GetConnectionString("PostgreSql")
    ?? throw new InvalidOperationException("Set ConnectionStrings__PostgreSql to the disposable database.");
var apiKey = Environment.GetEnvironmentVariable("ELSA_BENCH_API_KEY")
    ?? throw new InvalidOperationException("Set a private ELSA_BENCH_API_KEY.");
var tokenSigningKey = Convert.ToHexString(RandomNumberGenerator.GetBytes(48));
builder.Services.AddElsa(elsa => elsa
    .UseIdentity(tokenSigningKey)
    .UseDefaultAuthentication(auth => auth.UseAdminApiKey(apiKey))
    .UseWorkflowManagement(m => m.UseEntityFrameworkCore(ef => ef.UsePostgreSql(connection)))
    .UseWorkflowRuntime(r => r.UseEntityFrameworkCore(ef => ef.UsePostgreSql(connection)))
    .AddWorkflow<CheckpointWorkflow>()
    .UseWorkflowsApi()
    .UseHttp());
var traceEnabled = string.Equals(Environment.GetEnvironmentVariable("ELSA_BENCH_TRACE"), "1", StringComparison.Ordinal);
if (traceEnabled)
    builder.Services.Decorate<IWorkflowInstanceStore>((inner, _) => ObservedInstanceStore.Wrap(inner));

var app = builder.Build();
app.UseRouting();
app.UseAuthentication();
app.UseAuthorization();
if (traceEnabled)
{
    app.Use(async (context, next) =>
    {
        var path = context.Request.Path;
        ObservedInstanceStore.Phase.Value =
            path.StartsWithSegments("/elsa/api/events") ? "resume" :
            path.StartsWithSegments("/seed") ? "seed" :
            path.StartsWithSegments("/save") ? "save" : null;
        try { await next(); }
        finally { ObservedInstanceStore.Phase.Value = null; }
    });
}
app.MapWorkflowsApi("elsa/api");
app.UseWorkflows();
app.MapGet("/ready", () => Results.Ok(new { ready = true }));
var fixture = app.MapGroup("").RequireAuthorization();
fixture.MapGet("/routes", (IWorkflowRuntime runtime, IWorkflowInstanceStore store) =>
{
    var assemblies = new[] { "Elsa", "Elsa.Workflows.Runtime", "Elsa.Persistence.EFCore.PostgreSql",
        "Elsa.Persistence.EFCore.Common", "Microsoft.EntityFrameworkCore", "Npgsql.EntityFrameworkCore.PostgreSQL", "Npgsql" };
    return new
    {
        traceEnabled, runtimeType = runtime.GetType().FullName,
        storeType = store is ObservedInstanceStore observed ? observed.ImplementationName : store.GetType().FullName,
        counts = ObservedInstanceStore.Counts.ToArray().ToDictionary(),
        versions = assemblies.ToDictionary(x => x, x => Assembly.Load(x)
            .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion)
    };
});

fixture.MapPost("/seed/{id}", async (string id, IWorkflowRuntime runtime, CancellationToken ct) =>
{
    var client = await runtime.CreateClientAsync(id, ct);
    var run = await client.CreateAndRunInstanceAsync(new()
    {
        WorkflowDefinitionHandle = WorkflowDefinitionHandle.ByDefinitionId(CheckpointWorkflow.DefinitionId, VersionOptions.Published),
        CorrelationId = id
    }, ct);
    if (run.CannotStart || run.WorkflowInstanceId != id || run.Status != WorkflowStatus.Running ||
        run.SubStatus != WorkflowSubStatus.Suspended || run.Incidents.Count != 0 || run.Bookmarks.Count != 1)
        throw new InvalidOperationException($"Expected one suspended bookmark for {id}, observed {run.Status}/{run.SubStatus}.");
    return new { id, correlationId = id, definitionId = CheckpointWorkflow.DefinitionId,
        status = run.Status.ToString(), subStatus = run.SubStatus.ToString(), bookmarkId = run.Bookmarks.Single().Id };
});

fixture.MapGet("/prepare/{id}", async (string id, IWorkflowInstanceStore store, IWorkflowStateSerializer serializer, CancellationToken ct) =>
{
    var instance = await store.FindAsync(new WorkflowInstanceFilter { Id = id }, ct);
    return instance == null ? Results.NotFound() : Results.Ok(new { state = serializer.Serialize(instance.WorkflowState) });
});

// The stock 3.10.2 archive import returns 409 for an existing ID. This benchmark adapter
// measures the same public SaveAsync that a consumer with an existing state can call.
fixture.MapPost("/save/{id}", async (string id, SaveRequest request, IWorkflowStateSerializer serializer,
    WorkflowStateMapper mapper, IWorkflowInstanceStore store, CancellationToken ct) =>
{
    var state = serializer.Deserialize(request.State);
    if (state.Id != id || state.DefinitionId != CheckpointWorkflow.DefinitionId ||
        state.Status != WorkflowStatus.Finished || state.SubStatus != WorkflowSubStatus.Finished ||
        state.Bookmarks.Count != 0 || string.IsNullOrEmpty(request.Marker))
        return Results.BadRequest();
    state.CorrelationId = request.Marker;
    await store.SaveAsync(mapper.Map(state)!, ct);
    return Results.Ok(new { id, marker = request.Marker, imported = 1 });
});

fixture.MapGet("/audit/{id}", async (string id, IWorkflowInstanceStore store, CancellationToken ct) =>
{
    var row = await store.FindAsync(new WorkflowInstanceFilter { Id = id }, ct);
    if (row == null) return Results.NotFound();
    var state = row.WorkflowState;
    return Results.Ok(new
    {
        row.Id, row.CorrelationId, row.DefinitionId, row.TenantId, row.FinishedAt, row.IncidentCount,
        status = row.Status.ToString(), subStatus = row.SubStatus.ToString(),
        stateId = state.Id, stateCorrelationId = state.CorrelationId,
        stateStatus = state.Status.ToString(), stateSubStatus = state.SubStatus.ToString(),
        bookmarkCount = state.Bookmarks.Count, stateIncidentCount = state.Incidents.Count
    });
});
await app.RunAsync();

public record SaveRequest(string Marker, string State);

public class CheckpointWorkflow : WorkflowBase
{
    public const string DefinitionId = "upstream-store-control";
    protected override void Build(IWorkflowBuilder builder)
    {
        builder.WithDefinitionId(DefinitionId);
        builder.Root = new Event("store-bench") { Id = "wait" };
    }
}
