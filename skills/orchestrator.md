# Skill: GenAI Ops Review Orchestrator

## Description
Orchestrate an end-to-end GenAI operational review by executing data collection, aggregation, and assessment generation in sequence.

## Instructions

When given customer account IDs and optionally regions, execute these three steps in order:

### Step 1: Data Collection
Run shell command:
```
cd <repo_path> && python3 dante_collect.py --accounts "<account_ids>" --regions "<regions>" --output <output_dir>
```
Default regions: `us-east-1,us-west-2`. If it fails with auth error, ask user to run `mwinit`.

### Step 2: Aggregation
Run shell command:
```
cd <repo_path> && python3 analyze.py --input <output_dir> --output <output_dir>/analysis-report.txt
```
Read the generated `analysis-report.txt`.

### Step 3: Assessment Generation
Read `skills/bedrock_ops_review.md` — this is your assessment skill with Bedrock-specific concepts and output format. Read `<output_dir>/analysis-report.txt` — this is your metrics input. Follow the skill instructions and generate the full operational assessment.

## Constraints
- Steps 1 and 2 MUST be executed as shell commands — do not replicate their logic
- Step 3 is YOUR task — apply the skill to the metrics and generate the narrative assessment
- If user specifies a different skill file (e.g., `skills/sagemaker_ops_review.md`), use that instead
- Confirm each step completed before moving to the next
