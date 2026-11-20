# Concurrent workflow saves on PostgreSQL

This branch contains the standalone stock Elsa control and measurements for [Flamme PR #112](https://github.com/Moubee/flamme/pull/112). The Flamme workload and its host launcher are in the [PR benchmark directory](https://github.com/Moubee/flamme/tree/9dfd666fe2d645d931e0be6f178565d09cfece17/scripts/benchmarks/workflow-store).

## Run the stock Elsa control

Clone this branch and follow the locked restore, build and run commands in the stock harness README. It uses the public Elsa 3.10.2 packages and a fresh PostgreSQL 16 container. The timed host uses the ordinary Elsa store and its default local runtime. A separate tracing run confirms which persistence method the application reaches. [Stock harness and commands](upstream/README.md) · [Package provenance](results/upstream-provenance.json)

## Reproduce the Flamme comparison

Use one clean checkout per revision. Build its Release .NET 10 reference web server. Use the same benchmark launcher and workload from the simplified PR revision against each checkout. The workload publishes an event workflow through HTTP, creates fresh suspended instances, and resumes them with authenticated synchronous event requests. A separate import control explicitly saves finished instances. The driver also runs unblocked bursts. [Host prerequisites and command](https://github.com/Moubee/flamme/blob/9dfd666fe2d645d931e0be6f178565d09cfece17/scripts/benchmarks/workflow-store/README.md)

| Arm | Source | Unrelated instances committed during a one-second hold, three resume trials | Median trial p95 for the 48 unrelated resumes |
| --- | --- | --- | ---: |
| Stock Elsa 3.10.2 | [Upstream source](https://github.com/elsa-workflows/elsa-core/tree/76700bad5435ab08db5815866f5640101f6d1e25) | 0, 0, 0 | 1,080.4 ms |
| Flamme main | [7185fa3f2](https://github.com/Moubee/flamme/tree/7185fa3f2d87bbfa8be4024294801da5ee3b802d) | 0, 0, 0 | 1,119.8 ms |
| Earlier PR | [d65ae53ea](https://github.com/Moubee/flamme/tree/d65ae53eaf81f203d2c4231702ab69c3287b5e92) | 48, 48, 48 | 69.0 ms |
| Published PR | [9dfd666fe](https://github.com/Moubee/flamme/tree/9dfd666fe2d645d931e0be6f178565d09cfece17) | 48, 48, 48 | 37.6 ms |

All four arms completed and durably audited 49 requests per trial. The final published Flamme reference server was built at `9dfd666fe` on .NET 10. With the same inputs it committed 48/48 unrelated imports in each held trial, with a median trial p95 of 16.7 ms. Earlier results at the original tracked-write revision are retained as `candidate-*`. [Final trial data](results/published-trials.jsonl) · [Final validation](results/published-summary.json) Fixed run order and changing workstation load limit fine-grained latency comparisons. Stock Elsa’s explicit-save endpoint is a benchmark adapter because archive import rejects an existing finished ID.

The raw JSONL includes warmup, unblocked controls, before-release observations and final state audits. The SQL traces cover independent untimed calls. The 50- and 500-record activity-store probe measures a separate bulk workload and has its own reproduction instructions. [Results](results) · [Activity batch probe](activity-batches/README.md)
