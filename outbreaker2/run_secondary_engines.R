#!/usr/bin/env Rscript
# Secondary engine validation scaffold (TransPhylo + BactDating)

suppressWarnings(suppressMessages({
  library(jsonlite)
}))

out_path <- file.path("exports", "secondary_engine_validation.json")
dir.create("exports", showWarnings = FALSE)

pkg_status <- function(pkg_name) {
  ok <- requireNamespace(pkg_name, quietly = TRUE)
  if (!ok) {
    return(list(
      status = "optional_unavailable",
      package = pkg_name,
      version = NA_character_,
      message = paste0(pkg_name, " optional engine not available in current runtime")
    ))
  }

  list(
    status = "installed",
    package = pkg_name,
    version = as.character(utils::packageVersion(pkg_name)),
    message = paste0(pkg_name, " available")
  )
}

has_tree <- file.exists(file.path("exports", "outbreaker_phylo.nwk"))
has_dates <- file.exists(file.path("exports", "cases.csv"))

transphylo <- pkg_status("TransPhylo")
bactdating <- pkg_status("BactDating")

if (transphylo$status == "installed") {
  if (!has_tree || !has_dates) {
    transphylo$status <- "ready_missing_inputs"
    transphylo$message <- "TransPhylo installed but needs exports/outbreaker_phylo.nwk and exports/cases.csv"
  } else {
    transphylo$status <- "ready"
    transphylo$message <- "TransPhylo installed and key inputs detected"
  }
}

if (bactdating$status == "installed") {
  if (!has_tree || !has_dates) {
    bactdating$status <- "ready_missing_inputs"
    bactdating$message <- "BactDating installed but needs exports/outbreaker_phylo.nwk and exports/cases.csv"
  } else {
    bactdating$status <- "ready"
    bactdating$message <- "BactDating installed and key inputs detected"
  }
}

consensus_status <- "primary_evidence_only"
if (transphylo$status == "ready" || bactdating$status == "ready") {
  consensus_status <- "ready_to_run"
}

payload <- list(
  generated_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"),
  status = "completed",
  scaffold_version = "1.0",
  engines = list(
    transphylo = transphylo,
    bactdating = bactdating
  ),
  prerequisites = list(
    has_tree_newick = has_tree,
    has_cases_csv = has_dates,
    tree_path = "exports/outbreaker_phylo.nwk",
    dates_path = "exports/cases.csv"
  ),
  consensus = list(
    status = consensus_status,
    agreement = NA_real_,
    confidence = "unknown"
  ),
  notes = c(
    "This scaffold validates optional secondary-engine availability and input readiness.",
    "It does not infer additional transmission links until tree/date prerequisites are satisfied."
  )
)

write_json(payload, out_path, pretty = TRUE, auto_unbox = TRUE, na = "null")
cat(jsonlite::toJSON(payload, pretty = TRUE, auto_unbox = TRUE, na = "null"))
cat("\n")
