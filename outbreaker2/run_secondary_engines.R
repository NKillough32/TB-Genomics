#!/usr/bin/env Rscript
# Secondary engine validation scaffold (TransPhylo + BactDating)

suppressWarnings(suppressMessages({
  library(jsonlite)
}))

exports_dir <- Sys.getenv("TB_EXPORTS_DIR", "exports")
export_file <- function(...) file.path(exports_dir, ...)
out_path <- export_file("secondary_engine_validation.json")
dir.create(exports_dir, showWarnings = FALSE, recursive = TRUE)

pkg_status <- function(pkg_name) {
  ok <- requireNamespace(pkg_name, quietly = TRUE)
  if (!ok) {
    return(list(
      status = "optional_unavailable",
      package = pkg_name,
      version = NA_character_,
      message = paste0(
        pkg_name,
        " optional engine not available in current runtime"
      )
    ))
  }

  list(
    status = "installed",
    package = pkg_name,
    version = as.character(utils::packageVersion(pkg_name)),
    message = paste0(pkg_name, " available")
  )
}

has_tree <- file.exists(export_file("outbreaker_phylo.nwk"))
has_dates <- file.exists(export_file("cases.csv"))

transphylo <- pkg_status("TransPhylo")
bactdating <- pkg_status("BactDating")

if (transphylo$status == "installed") {
  if (!has_tree || !has_dates) {
    transphylo$status <- "ready_missing_inputs"
    transphylo$message <- paste0(
      "TransPhylo installed but needs ",
      export_file("outbreaker_phylo.nwk"),
      " and ",
      export_file("cases.csv")
    )
  } else {
    transphylo$status <- "ready"
    transphylo$message <- "TransPhylo installed and key inputs detected"
  }
}

if (bactdating$status == "installed") {
  if (!has_tree || !has_dates) {
    bactdating$status <- "ready_missing_inputs"
    bactdating$message <- paste0(
      "BactDating installed but needs ",
      export_file("outbreaker_phylo.nwk"),
      " and ",
      export_file("cases.csv")
    )
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
    tree_path = export_file("outbreaker_phylo.nwk"),
    dates_path = export_file("cases.csv")
  ),
  consensus = list(
    status = consensus_status,
    agreement = NA_real_,
    confidence = "unknown"
  ),
  notes = c(
    paste0(
      "This scaffold validates optional secondary-engine ",
      "availability and input readiness."
    ),
    paste0(
      "It does not infer additional transmission links until ",
      "tree/date prerequisites are satisfied."
    )
  )
)

write_json(payload, out_path, pretty = TRUE, auto_unbox = TRUE, na = "null")
cat(jsonlite::toJSON(payload, pretty = TRUE, auto_unbox = TRUE, na = "null"))
cat("\n")
