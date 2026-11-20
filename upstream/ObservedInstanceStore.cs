using System.Collections.Concurrent;
using System.Reflection;
using System.Runtime.ExceptionServices;
using Elsa.Workflows.Management;

// Transparent DI decorator. Counts indicate entry into a store method. Durable
// success is checked by the runner's independent database connection.
public class ObservedInstanceStore : DispatchProxy
{
    public static readonly AsyncLocal<string?> Phase = new();
    public static readonly ConcurrentDictionary<string, long> Counts = new();
    private IWorkflowInstanceStore _inner = null!;
    public string? ImplementationName => _inner.GetType().FullName;

    public static IWorkflowInstanceStore Wrap(IWorkflowInstanceStore inner)
    {
        var proxy = Create<IWorkflowInstanceStore, ObservedInstanceStore>();
        ((ObservedInstanceStore)proxy)._inner = inner;
        return proxy;
    }

    protected override object? Invoke(MethodInfo? method, object?[]? args)
    {
        ArgumentNullException.ThrowIfNull(method);
        if (Phase.Value is { } phase)
            Counts.AddOrUpdate($"{phase}.{method.Name}", 1, static (_, n) => n + 1);
        try { return method.Invoke(_inner, args); }
        catch (TargetInvocationException ex) when (ex.InnerException is not null)
        {
            ExceptionDispatchInfo.Capture(ex.InnerException).Throw();
            throw;
        }
    }
}
