# Synthetic-image advisory review

The image extension uses the existing graph after scalar validation and before
operator review: local image intake → numerical statistics → seeded PNG generation
→ deterministic image checks → advisory vision critique. It is separate from the
admitted `hylogger_scalar_teaching_v1` case. Selecting that scalar case together
with `--images` is refused; its contract, thresholds and benchmark are unchanged.

Only the first decodable PNG in sorted intake order supplies the fitted statistics.
Other image files are inventoried. JPEG cannot supply statistics in the current
stdlib pipeline. An empty directory or no decodable PNG produces an explicit error.
No real source images are present in the project data, so current engineering
coverage uses controlled fixtures; real-image suitability is not established.

## Engine boundary

`engine/artifacts/images.py` admits only an image matching both the immutable
registered payload and its artifact record. Its producer must be a known capability
explicitly declaring `reads_raw_source: false`. Raw-intake capabilities alone may
register source hashes. Original source paths, copied source bytes, unknown or
mutated artifacts, wrong media types and symlink paths are refused. The transport
receives a private staged copy of registered bytes.

`models/base.py` declares model capabilities explicitly. The shared reader in
`models/image_input.py` validates byte and pixel ceilings, PNG chunks/CRC, decoded
length, zlib completion and row filter bytes. Attachments support 8-bit,
noninterlaced grayscale, RGB, grayscale with alpha and RGBA PNGs, up to 4 MiB and
4,000,000 pixels on the configured vision adapters. Indexed, 16-bit and interlaced
attachments are refused. These are attachment limits, not a claim that local source
intake is an OS sandbox.

The normal model policy and token budget apply before transport. Usage is charged
once using the existing adapter accounting convention; Azure completion totals
already include reasoning. As in the scalar workflow, the ceiling admits or refuses
the next call; it is not an advance guarantee of a provider's final bill. A failed
image call with unknown usage blocks further model calls. Missing monetary cost is
recorded as unavailable, never as zero. Remote schema references are refused.

These checks protect the engine interface against accidental misuse. In-process
Python profiles and model adapters remain trusted code, without OS confinement.

## Explicit provider selection

Azure Glimmer is preferred when a deployment is available. The 5 November capacity
evidence (`runs/azure-vision-20261105/capacity-20261104T170936Z.json`, retained
locally) reports no SKU, empty capacity queries and no deployment. The Azure adapter
implements image content parts and strict structured-response validation for the
Glimmer configuration, but this is protocol support with no live Azure vision proof.
Azure Spark remains text-only and refuses images before authentication or networking.

Muse API access through the existing `meta_cli` adapter is explicitly authorized for
vision. Select it directly; there is no automatic provider switch:

```sh
ruloom run SOURCE.csv --profile hylogger --images EXPORTED_PNG_DIR \
  --max-attempts 1 --role-adapter critic=meta_cli \
  --role-model critic=muse-spark-1.3 --role-effort critic=low
```

The critic composition disables contributor mode for this seat. Vision uses an
explicit Meta endpoint and empty tool preset, with shell/write/web/context tools
disabled and one model step. Its working directory contains only the staged
synthetic PNG and response schema. Scalar invocation behavior stays unchanged. Authentication
continues through the existing provider configuration; do not put credentials in
commands, prompts or release evidence. See [model configuration](model-families.md).

## What the vision opinion means

`profiles/hylogger/imaging/critic.py` asks for four bounded observations: broken bands,
abrupt colour changes, visible cropping/clipping and rendering defects. Intentional
band transitions may be normal. The original image is not attached, so loss of its
original framing cannot be assessed. Only whitelisted numeric source statistics
enter the prompt, without source filenames, source hashes or raw sample records.

The model must echo the generated artifact digest and exact deterministic check
facts. Local validation rejects missing categories, wrong types, altered facts and
unknown citations. The observations help a teacher locate visible issues to inspect;
they do not establish mineral identity, geological validity, grade, privacy,
anonymity or licensing rights.

`vision-advisory.json` is an image-only frozen release member. Its versioned receipt
separates opinions, deterministic facts and reported model/provider/usage/cost.
Injected, unverified and provider-reported evidence are distinct. A failure or an
unavailable critic is explicit; known usage from a rejected opinion is preserved,
while unreported usage remains unknown. The optional critic can continue on code
checks; a required critic failure stops the run. Neither opinion can change a
failed deterministic check or accept material on behalf of a human.

Recipient verification checks the receipt digest, exact shipped image, check facts,
status and provenance fields. Recomputed internal hashes alone do not establish
external origin; retain the trusted adjacent manifest and operator evidence. Legacy
image bundles without this receipt are not retrospectively described as vision
validated. Scalar releases contain no image advisory member.

## Verification scope

Controlled tests cover attachment payloads, provenance refusals, malformed responses,
limits, budgets, error redaction, policy failures and recipient binding. They use
injected responses and establish engineering behavior only. Genuine live evidence,
when recorded, is a separately bounded image smoke, not the paired Muse-versus-Claude
benchmark. That comparison remains pending the owner's instruction.
