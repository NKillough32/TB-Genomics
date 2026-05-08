# TB Genomics Project Explained in Simple Terms

## What this project is

This project is a working system that helps a TB programme turn lab sequencing data into practical public health insight.

In plain terms, it helps answer:

- Which TB cases look genetically linked?
- Are there active clusters that may need investigation?
- How well is sequencing coverage performing across regions?
- What should teams prioritize this week?

## What it does, step by step

1. It receives TB case and sequencing-related data.
2. It stores and organizes that data in a database.
3. It runs analysis jobs (including outbreak-style analysis workflows).
4. It calculates programme metrics (coverage, QC, turnaround, etc.).
5. It generates summaries, visuals, and a PDF investigation report.
6. It shows all of this in a web interface and API for operational use.

## The main parts

- Backend API: runs the logic, calculations, and report generation.
- Database schema: defines how case, cluster, and quality data are stored.
- GUI: gives teams a simple web view of cases, jobs, and outputs.
- Scripts: run clustering/outbreaker pipelines and data export utilities.
- Governance docs: explain setup and secure integration (for example Azure VM ingestion).

## What users get from it

- Case and cluster summaries
- Surveillance KPIs over a selected time window
- Weekly trends for sequencing and QC performance
- Priority lists (for clusters and likely transmission signals)
- A generated outbreak investigation PDF report
- Job status and logs for pipeline runs

## Why this is useful for a TB programme

- It turns technical sequencing outputs into decision-ready information.
- It supports faster and more consistent outbreak response.
- It helps monitor sequencing service quality, not just case counts.
- It creates a repeatable reporting process instead of manual collation.

## How Azure VM data fits in

If sequencing files are produced on an Azure VM, the project includes a connector that can push new or changed files into the platform.

This means you can automate data flow from sequencing infrastructure into reporting, instead of manually moving files.

## Important boundaries (what it does not do by itself)

- It does not replace clinical judgment or epidemiology investigation.
- It does not provide direct patient care decisions.
- It depends on data quality and completeness from upstream systems.
- It is a surveillance and operational intelligence tool, not a full LIMS.

## Short summary in one sentence

This project is a TB genomic surveillance platform that ingests sequencing-linked data, analyses cluster and transmission patterns, tracks programme performance, and produces actionable reports for public health teams.
