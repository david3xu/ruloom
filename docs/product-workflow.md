# Request-to-data workflow

Ruloom helps a person prepare synthetic teaching or software-test data from
a supported source. A model interprets the request and proposes settings;
seeded code generates the rows; executed checks show measured quality;
the operator reviews the evidence before accepting a release. The current
product is a local CLI, with scalar CSV profiles for HyLogger and common
tabular data. It does not accept arbitrary datasets or quality promises.

## One user journey

| Stage | Work and evidence | What stops progression |
| --- | --- | --- |
| Ask | `prepare` interprets a short request using the selected profile's declared strategies, output columns, checks and release members. It hashes the supplied source locally. | Invalid configuration, unsupported requirements, malformed model response or provider failure. |
| Review the draft | Read `request.json`, `spec.json` and `plan.json`. Check that the proposed row count, strategy and limitations fit the request. The receipt keeps the capability context, its digest, the prompt digest and model evidence. | The interpretation is wrong or incomplete. Model interpretation is fallible; the operator must check it. |
| Generate | `run --spec ... --plan ... --model-policy offline` reuses the reviewed plan. Source intake and profiling supply aggregates to the seeded generator. | Changed source bytes, unsupported settings or generation errors. |
| Measure | Required applicable checks measure the exact candidate. Failures remain failures; optional skips are shown separately. | A required check is absent, duplicated, failed, errored or skipped. A supported method does not guarantee a passing result. |
| Review the result | The operator reads measured outcomes and limitations, then accepts, modifies or rejects. Optional model critiques are advice. | Failed eligibility or rejection. A critic cannot grant acceptance. |
| Deliver and verify | Acceptance permits a package containing the data and declared evidence. Consumer verification checks the downloaded bytes and required members. | Missing members, mismatched hashes, damaged evidence or invalid acceptance binding. |

Example: “I need 10,000 fictional drill-sample records for a data-checking
lesson. Keep the depth spacing, blank result labels and inclusion flags
similar to this source, and show which checks passed.” The HyLogger scalar
profile offers aggregate-fitted strategies (legacy approximate
`fitted_markov_joint` and opt-in exact-count `fitted_markov_balanced`) and
fixed checks for those properties; balanced generation requires the actual
valid row count to match the fitted integer target exactly alongside the
calibrated band. It produces `depth_m`, `valid`, `swir_group`
and `tir_group`; blank group labels mean no classification label was reported. The route
does not produce measurement curves, sample IDs, coordinates or photographs.
An explicit privacy guarantee or custom tolerance remains unsupported. The balanced strategy fits an integer inclusion count from rounded source aggregates; it does not preserve the source row order or exact run-length distribution.

## Models, roles and development workers

Roles do not require different models. The smallest model-assisted route is
one planner call during `prepare`, followed by a reviewed offline run with
zero further model calls. A fresh `run` can call the planner instead; bounded
repair uses that role again. The HyLogger profile also declares an optional
image critic and an opt-in local text critic. Those seats are not invoked
as independent workers on every scalar run. Actual invocations are recorded;
the number of configured roles is not the number of calls made.

AGY family/model configuration is documented in [model families](model-families.md).
The selected adapter determines transport; a model name alone cannot switch
providers. Provider identity is what the CLI reports, not verified weights.

Implementation helpers and code reviewers are a separate development process.
Earlier AGY Opus work produced partial code before a quota error, and Codex
finished that stage. The owner-provided Gemini Flash high session implemented
the balanced generator and review corrections. A separate native Claude
Sonnet high session independently reviewed the frozen changes; Codex audited,
added boundary/identity checks and ran the real-source acceptance test. These
workers do not participate in every user's data run. Datacore records their handoffs and evidence; it
does not turn a worker's completion message into product acceptance.

## Scope and remaining limits

This release keeps the existing strategies, thresholds, repair bounds,
human gate, source rights and package verification. New work adds
request capability context, strict metadata validation, explicit model-family
configuration and an opt-in balanced inclusion strategy, with regression
checks and workflow documentation. Profile text is reviewed against
the code; validating its shape alone cannot establish its truth.

Free-form requirements are not fully compiled into independent executable
tests. Exact row count and existing profile checks are enforced, while a
human must review the request interpretation. Arbitrary new fields,
distributions, tolerances, privacy promises and licensing promises refuse
preparation. There is no clarification dialogue or hosted multi-user UI.
Passing engineering checks does not establish scientific validity, anonymity
or unrestricted reuse. Source terms remain recorded per release.

Requested no-tools prompts provide behavioral guidance at the role boundary,
while AGY tool-use detection remains a post-call audit rather than process
confinement. Untrusted third-party profiles and worker isolation remain outside this
readiness claim. Optional procedural image machinery remains fixture-tested;
it is not evidence of real core-photo fidelity.

## Later model comparison

The owner will select the comparison tasks after this workflow works. Freeze
the request, source, allowed source disclosure, output contract, check
versions, attempts and budgets before testing. Score both outputs with the
same independent measured checks, including failed and incomplete runs;
record time, tokens, actual model evidence and manual interventions.

First compare the same model directly and through Ruloom to study the
workflow's contribution. A separate comparison can use a smaller-model
workflow against a stronger model working directly. Report any differences
in tools, information and compute budgets. A saved-plan replay measures
reproducibility, not fresh model performance. No general superiority claim
is established by the current smoke tests.
