# Ruloom: the idea and design

## Why I built Ruloom

I built Ruloom to help freelance workers get useful practice data faster. Preparing a lesson or demo takes time finding and fixing files. I wanted to make that easier.

## One small teaching task

Imagine teaching someone to follow one rock sample: find the drill-hole location on a map, read the depth, open the picture and look at the infrared chart. A small dataset is enough, but the files need to agree.

## From a request to useful files

We start with a real example and suitable source material. Then we decide what the task needs. A model suggests a plan, code creates the records, and checks look for problems. A failed required check stops the release.

## How the parts fit together

The engine keeps the steps in order. A domain profile adds rules for one kind of data. That lets us add another kind of data without rebuilding the whole process.

## What Muse contributes

We’ve used Muse Spark to suggest settings and Muse Glimmer to review results. Next, we plan to give Muse models separate jobs: researching examples, planning the work, and reviewing data and pictures. Code checks the results, and a person decides whether to accept them.

## What we want to deliver

The goal is a small ZIP with useful files, a guide and check results. That could reduce the need to share original records. We’re preparing a comparison with a separate Claude Code run on the same request and sources, to see whether someone can use each package.

## The example at a glance

The linked sample example is the target of the pending full-workflow test.
It is not presented here as a completed result.

```text
One rock sample
  |-- drill-hole location on a map
  |-- recorded depth
  |-- sample picture
  `-- infrared chart

The same sample identity connects all four.
```

## Project folders

```text
engine/             Keeps the workflow in order and controls release decisions.
profiles/hylogger/  Adds the rules and checks for drill-hole data.
profiles/tabular/   Adds the rules for the supported general CSV workflow.
models/             Connects model roles to the chosen providers.
```

## Evidence and current status

- [Final test prompt and evidence status](final-test.md): results pending.
- [Earlier Muse model calls](quality/critic-receipts-gp01.md): recorded Spark
  vision-review calls and a Glimmer text review. These are earlier runs,
  not evidence that the new full-workflow comparison has passed.
- [Model roles and settings](model-families.md).

The new comparison will check whether both delivered packages let someone
complete the same teaching task. It will record failures, preparation time,
model usage and any manual corrections. No winner is claimed here.

Synthetic data does not by itself guarantee anonymity. The choice of source
and the information retained in a shared package still need review.
