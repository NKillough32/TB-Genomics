
#!/usr/bin/env Rscript
# TB Genomic Surveillance - Outbreaker2 Analysis Script

tryCatch({
  # Set up local library path for packages
  local_lib <- file.path(getwd(), "R_libs")
  if (dir.exists(local_lib)) {
    .libPaths(c(local_lib, .libPaths()))
  }

  library(outbreaker2)
  library(ape)
  library(jsonlite)
  has_coda <- requireNamespace("coda", quietly = TRUE)
  has_yaml <- requireNamespace("yaml", quietly = TRUE)
  has_digest <- requireNamespace("digest", quietly = TRUE)
  has_visnetwork <- requireNamespace("visNetwork", quietly = TRUE)
  has_htmlwidgets <- requireNamespace("htmlwidgets", quietly = TRUE)

  # Create outputs directory. Defaults preserve the Windows/local layout.
  exports_dir <- Sys.getenv("TB_EXPORTS_DIR", "exports")
  export_file <- function(...) file.path(exports_dir, ...)
  dir.create(exports_dir, showWarnings = FALSE, recursive = TRUE)

  config_path <- Sys.getenv("TB_OUTBREAKER_CONFIG", export_file("outbreaker_config.yml"))
  file_sha256 <- function(path) {
    if (!file.exists(path) || !has_digest) {
      return(NA_character_)
    }
    as.character(digest::digest(file = path, algo = "sha256"))
  }
  git_commit_hash <- function() {
    hash <- tryCatch(
      system("git rev-parse --short=12 HEAD", intern = TRUE, ignore.stderr = TRUE),
      error = function(e) NA_character_
    )
    if (length(hash) == 0 || !nzchar(hash[1])) NA_character_ else hash[1]
  }
  cfg_doc <- list()
  if (file.exists(config_path) && has_yaml) {
    cfg_doc <- yaml::read_yaml(config_path)
    cat("Loaded outbreaker config from ", config_path, "\n", sep = "")
  } else if (file.exists(config_path) && !has_yaml) {
    cat("Config file found but yaml package unavailable; using environment/defaults.\n")
  }
  cfg_value <- function(path, env_name, default) {
    env_val <- Sys.getenv(env_name, unset = NA_character_)
    if (!is.na(env_val) && nzchar(env_val)) {
      return(env_val)
    }
    cur <- cfg_doc
    for (key in path) {
      if (!is.list(cur) || is.null(cur[[key]])) {
        return(default)
      }
      cur <- cur[[key]]
    }
    if (is.null(cur)) default else cur
  }
  cfg_int <- function(path, env_name, default) as.integer(cfg_value(path, env_name, default))
  cfg_num <- function(path, env_name, default) as.numeric(cfg_value(path, env_name, default))
  cfg_chr <- function(path, env_name, default) as.character(cfg_value(path, env_name, default))
  truthy <- function(x) tolower(trimws(as.character(x))) %in% c("1", "true", "t", "yes", "y")

  # Read input data
  cases <- read.csv(export_file("cases.csv"), stringsAsFactors = FALSE)
  dna <- read.dna(export_file("dna.fasta"), format = "fasta")

  set_seed <- cfg_int(c("mcmc", "seed"), "TB_OUTBREAKER_SEED", 20260522)
  set.seed(set_seed)
  cat("Using RNG seed ", set_seed, "\n", sep = "")

  qc_exclusions <- data.frame()
  qc_metrics_path <- export_file("sample_qc_metrics.csv")
  min_depth <- cfg_num(c("qc", "min_depth"), "TB_QC_MIN_DEPTH", 20)
  min_coverage_breadth <- cfg_num(c("qc", "min_coverage_breadth"), "TB_QC_MIN_COVERAGE_BREADTH", 90)
  max_ambiguous_base_percent <- cfg_num(c("qc", "max_ambiguous_base_percent"), "TB_QC_MAX_AMBIGUOUS_BASE_PERCENT", 5)
  exclude_failed_qc <- truthy(cfg_chr(c("qc", "exclude_failed_qc"), "TB_QC_EXCLUDE_FAILED", "1"))
  if (file.exists(qc_metrics_path)) {
    qc_metrics <- read.csv(qc_metrics_path, stringsAsFactors = FALSE)
    qc_metrics$sample_id <- as.character(qc_metrics$sample_id)
    qc_metrics$mean_depth_num <- suppressWarnings(as.numeric(qc_metrics$mean_depth))
    qc_metrics$coverage_breadth_num <- suppressWarnings(as.numeric(qc_metrics$coverage_breadth))
    qc_metrics$ambiguous_base_percent_num <- suppressWarnings(as.numeric(qc_metrics$ambiguous_base_percent))
    qc_metrics$contamination_bool <- truthy(qc_metrics$contamination_flag)
    qc_metrics$exclude_reason <- ""
    qc_metrics$exclude_reason[exclude_failed_qc & tolower(qc_metrics$qc_status) == "fail"] <- "qc_status_fail"
    qc_metrics$exclude_reason[is.finite(qc_metrics$mean_depth_num) & qc_metrics$mean_depth_num < min_depth] <- "low_depth"
    qc_metrics$exclude_reason[is.finite(qc_metrics$coverage_breadth_num) & qc_metrics$coverage_breadth_num < min_coverage_breadth] <- "low_coverage_breadth"
    qc_metrics$exclude_reason[is.finite(qc_metrics$ambiguous_base_percent_num) & qc_metrics$ambiguous_base_percent_num > max_ambiguous_base_percent] <- "high_ambiguous_base_fraction"
    qc_metrics$exclude_reason[qc_metrics$contamination_bool] <- "contamination_flag"
    qc_exclusions <- qc_metrics[nzchar(qc_metrics$exclude_reason), c("sample_id", "exclude_reason", "mean_depth", "coverage_breadth", "ambiguous_base_percent", "contamination_flag", "qc_status")]
    if (nrow(qc_exclusions) > 0) {
      write.csv(qc_exclusions, export_file("outbreaker_qc_exclusions.csv"), row.names = FALSE)
      keep_case <- !(as.character(cases$case_id) %in% qc_exclusions$sample_id)
      cases <- cases[keep_case, , drop = FALSE]
      keep_dna <- as.character(labels(dna)) %in% as.character(cases$case_id)
      dna <- dna[keep_dna, , drop = FALSE]
      cat("Excluded ", nrow(qc_exclusions), " sample(s) using genomic QC thresholds.\n", sep = "")
    }
  }

  # TB serial interval defaults (long-tailed compared with acute infections).
  # Defaults can be tuned per deployment using environment variables.
  si_window <- cfg_int(c("tb", "generation_interval_window_days"), "TB_OUTBREAKER_SI_WINDOW", 730)
  serial_w_shape <- cfg_num(c("tb", "generation_interval_shape"), "TB_OUTBREAKER_W_SHAPE", 2.0)
  serial_w_scale <- cfg_num(c("tb", "generation_interval_scale"), "TB_OUTBREAKER_W_SCALE", 90.0)
  serial_f_shape <- cfg_num(c("tb", "sampling_delay_shape"), "TB_OUTBREAKER_F_SHAPE", 1.5)
  serial_f_scale <- cfg_num(c("tb", "sampling_delay_scale"), "TB_OUTBREAKER_F_SCALE", 90.0)

  w_raw <- dgamma(1:si_window, shape = serial_w_shape, scale = serial_w_scale)
  f_raw <- dgamma(1:si_window, shape = serial_f_shape, scale = serial_f_scale)
  w_dens <- w_raw / sum(w_raw)
  f_dens <- f_raw / sum(f_raw)

  # Prepare data for outbreaker
  # Issue #3: Prefer symptom_onset_date for the serial interval prior; fall back to sample_date.
  # In TB, specimens are collected 1-6 weeks AFTER symptom onset, so sample_date alone
  # compresses inferred transmission intervals and biases ancestor assignments toward recent cases.
  has_onset_col <- "symptom_onset_date" %in% names(cases)
  date_col_used <- if (has_onset_col && any(nchar(trimws(cases$symptom_onset_date)) > 0, na.rm = TRUE)) {
    onset_parsed <- suppressWarnings(as.Date(ifelse(
      nchar(trimws(cases$symptom_onset_date)) > 0, cases$symptom_onset_date, cases$sample_date
    )))
    cat("Using symptom_onset_date (with sample_date fallback) for Outbreaker2 date prior.\n")
    onset_parsed
  } else {
    cat("symptom_onset_date not available; using sample_date for Outbreaker2 date prior.\n")
    as.Date(cases$sample_date)
  }
  case_dates <- date_col_used
  names(case_dates) <- as.character(cases$case_id)
  dna_ids <- as.character(labels(dna))
  cases <- cases[match(dna_ids, as.character(cases$case_id)), , drop = FALSE]
  aligned_dates <- case_dates[dna_ids]

  if (any(is.na(aligned_dates))) {
    stop("Some DNA labels do not have matching case dates")
  }

  names(aligned_dates) <- dna_ids

  build_contact_tracing <- function(case_table, ids) {
    fields <- c(
      "household_id", "shared_accommodation_id", "prison_exposure_id",
      "hospital_episode_id", "hospital_id", "ward_id", "postcode",
      "postcode_sector", "hsc_trust", "location_id"
    )
    present <- intersect(fields, names(case_table))
    links <- data.frame(i = integer(), j = integer(), reason = character())
    if (length(present) == 0) {
      return(list(ctd = NULL, evidence = links, fields = character()))
    }
    for (field in present) {
      values <- trimws(as.character(case_table[[field]]))
      values[is.na(values)] <- ""
      for (value in unique(values[nzchar(values)])) {
        idx <- which(values == value)
        if (length(idx) > 1) {
          pairs <- t(combn(idx, 2))
          links <- rbind(
            links,
            data.frame(i = pairs[, 1], j = pairs[, 2], reason = field),
            data.frame(i = pairs[, 2], j = pairs[, 1], reason = field)
          )
        }
      }
    }
    if (nrow(links) == 0) {
      return(list(ctd = NULL, evidence = links, fields = present))
    }
    links <- unique(links)
    links <- links[links$i != links$j, , drop = FALSE]
    write.csv(
      data.frame(
        source = ids[links$i],
        target = ids[links$j],
        reason = links$reason
      ),
      export_file("outbreaker_contact_tracing_links.csv"),
      row.names = FALSE
    )
    list(ctd = links[, c("i", "j"), drop = FALSE], evidence = links, fields = present)
  }

  ctd_payload <- build_contact_tracing(cases, dna_ids)
  out_data <- tryCatch(
    {
      if (!is.null(ctd_payload$ctd) && nrow(ctd_payload$ctd) > 0) {
        cat("Using ", nrow(ctd_payload$ctd), " undirected contact-tracing/co-location constraints.\n", sep = "")
        outbreaker_data(
          dates = aligned_dates,
          dna = dna,
          ctd = ctd_payload$ctd,
          w_dens = w_dens,
          f_dens = f_dens
        )
      } else {
        outbreaker_data(
          dates = aligned_dates,
          dna = dna,
          w_dens = w_dens,
          f_dens = f_dens
        )
      }
    },
    error = function(e) {
      cat("Contact-tracing data rejected by outbreaker2; retrying without ctd: ", as.character(e), "\n", sep = "")
      outbreaker_data(
        dates = aligned_dates,
        dna = dna,
        w_dens = w_dens,
        f_dens = f_dens
      )
    }
  )

  # Run outbreak investigation
  cat("Running outbreaker2 analysis...\n")
  n_iter_total <- cfg_int(c("mcmc", "iterations"), "TB_OUTBREAKER_ITER", 50000)
  burnin_iters <- cfg_int(c("mcmc", "burnin"), "TB_OUTBREAKER_BURNIN", 10000)
  thin_every <- cfg_int(c("mcmc", "thin"), "TB_OUTBREAKER_THIN", 10)
  n_chains <- max(1L, cfg_int(c("mcmc", "chains"), "TB_OUTBREAKER_CHAINS", 1))
  init_pi <- cfg_num(
    c("mcmc", "init_reporting_probability"),
    "TB_OUTBREAKER_INIT_REPORTING_PROBABILITY",
    0.05
  )
  prior_pi <- cfg_num(
    c("mcmc", "prior_reporting_probability"),
    "TB_OUTBREAKER_PRIOR_REPORTING_PROBABILITY",
    0.05
  )
  init_kappa <- cfg_num(c("mcmc", "init_unsampled_ancestors"), "TB_OUTBREAKER_INIT_KAPPA", 5)
  burnin_rows <- max(0L, as.integer(floor(burnin_iters / max(1L, thin_every))))
  make_config <- function(chain_id) {
    init_tree_options <- c("star", "random", "seq")
    init_tree_value <- cfg_chr(
      c("mcmc", "init_tree"),
      "TB_OUTBREAKER_INIT_TREE",
      init_tree_options[((chain_id - 1L) %% length(init_tree_options)) + 1L]
    )
    pi_multiplier <- c(0.75, 1, 1.25, 1.5)[((chain_id - 1L) %% 4L) + 1L]
    kappa_offset <- (chain_id - 1L) %% 4L
    args <- list(
      n_iter = n_iter_total,
      sample_every = thin_every,
      init_pi = min(0.99, max(0.001, init_pi * pi_multiplier)),
      prior_pi = prior_pi,
      init_kappa = max(1, init_kappa + kappa_offset),
      init_tree = init_tree_value,
      ctd_directed = FALSE
    )
    tryCatch(
      do.call(create_config, args),
      error = function(e) {
        args$ctd_directed <- NULL
        tryCatch(
          do.call(create_config, args),
          error = function(e2) {
            args$init_tree <- NULL
            do.call(create_config, args)
          }
        )
      }
    )
  }
  run_one_chain <- function(chain_id) {
    set.seed(set_seed + chain_id - 1L)
    chain_config <- make_config(chain_id)
    result <- outbreaker(data = out_data, config = chain_config)
    chain <- as.data.frame(result)
    chain$chain_id <- chain_id
    list(result = result, chain = chain, config = chain_config)
  }
  chain_runs <- lapply(seq_len(n_chains), run_one_chain)
  res <- chain_runs[[1]]$result
  chain_df <- do.call(rbind, lapply(chain_runs, function(x) x$chain))
  posterior_chain_df <- do.call(
    rbind,
    lapply(split(chain_df, chain_df$chain_id), function(chain_part) {
      start_row <- min(max(1, burnin_rows + 1), nrow(chain_part))
      chain_part[start_row:nrow(chain_part), , drop = FALSE]
    })
  )

  estimate_ess_lag1 <- function(series) {
    x <- as.numeric(series)
    x <- x[is.finite(x)]
    n <- length(x)
    if (n < 3) {
      return(NA_real_)
    }
    rho1 <- suppressWarnings(cor(x[1:(n - 1)], x[2:n], use = "complete.obs"))
    if (is.na(rho1)) {
      return(NA_real_)
    }
    rho1 <- max(min(rho1, 0.99), -0.99)
    ess <- n * (1 - rho1) / (1 + rho1)
    max(1, min(n, ess))
  }
  estimate_ess <- function(series) {
    x <- as.numeric(series)
    x <- x[is.finite(x)]
    if (length(x) < 3) {
      return(NA_real_)
    }
    if (has_coda) {
      return(as.numeric(coda::effectiveSize(coda::mcmc(x))))
    }
    estimate_ess_lag1(x)
  }

  posterior_entropy <- function(probabilities) {
    p <- probabilities[is.finite(probabilities) & probabilities > 0]
    if (length(p) == 0) return(NA_real_)
    round(-sum(p * log(p)), 4)
  }

  edge_chain_stability <- function(chain_local, target_idx, source_idx, n_cases) {
    if (!("chain_id" %in% names(chain_local))) {
      return(list(
        top_ancestor_agreement = NA_real_,
        probability_min = NA_real_,
        probability_max = NA_real_,
        probability_range = NA_real_
      ))
    }
    alpha_col <- paste0("alpha_", target_idx)
    if (!(alpha_col %in% names(chain_local))) {
      return(list(
        top_ancestor_agreement = NA_real_,
        probability_min = NA_real_,
        probability_max = NA_real_,
        probability_range = NA_real_
      ))
    }
    chain_ids <- sort(unique(chain_local$chain_id))
    top_hits <- 0
    probabilities <- numeric(0)
    for (chain_id in chain_ids) {
      part <- chain_local[chain_local$chain_id == chain_id, , drop = FALSE]
      values <- suppressWarnings(as.integer(part[[alpha_col]]))
      values <- values[!is.na(values) & values >= 1 & values <= n_cases & values != target_idx]
      if (length(values) == 0) {
        probabilities <- c(probabilities, 0)
        next
      }
      freq <- sort(table(values), decreasing = TRUE)
      if (as.integer(names(freq)[1]) == source_idx) {
        top_hits <- top_hits + 1
      }
      probabilities <- c(probabilities, sum(values == source_idx) / length(part[[alpha_col]]))
    }
    list(
      top_ancestor_agreement = round(top_hits / max(1, length(chain_ids)), 4),
      probability_min = round(min(probabilities, na.rm = TRUE), 4),
      probability_max = round(max(probabilities, na.rm = TRUE), 4),
      probability_range = round(diff(range(probabilities, na.rm = TRUE)), 4)
    )
  }

  operational_risk_score <- function(row) {
    score <- 0
    if ("smear_status" %in% names(row) && grepl("positive", tolower(row$smear_status))) score <- score + 3
    if ("smear_positive" %in% names(row) && truthy(row$smear_positive)) score <- score + 3
    if ("homelessness" %in% names(row) && truthy(row$homelessness)) score <- score + 2
    if ("prison_exposure" %in% names(row) && truthy(row$prison_exposure)) score <- score + 3
    if ("household_child_exposure" %in% names(row) && truthy(row$household_child_exposure)) score <- score + 4
    if ("healthcare_worker" %in% names(row) && truthy(row$healthcare_worker)) score <- score + 2
    score
  }

  build_transmission_network <- function(chain_local, ids, case_table, burnin_iter, posterior_reliable = TRUE, reliability_status = "reliable", reliability_reasons = c(), decycle_status = "not_run") {
    alpha_cols <- grep("^alpha_", names(chain_local), value = TRUE)

    n_cases <- length(ids)
    posterior_samples <- 0
    edge_list <- list()
    incoming <- setNames(rep(0, n_cases), ids)
    outgoing <- setNames(rep(0, n_cases), ids)
    # Issue #10: Track proportion of unlinked (NA) ancestry for each case to identify imports/index cases
    p_unlinked <- setNames(rep(NA_real_, n_cases), ids)

    if (length(alpha_cols) == n_cases) {
      alpha_matrix <- as.matrix(chain_local[, alpha_cols, drop = FALSE])
      start_row <- min(max(1, burnin_iter + 1), nrow(alpha_matrix))
      alpha_post <- alpha_matrix[start_row:nrow(alpha_matrix), , drop = FALSE]
      posterior_samples <- nrow(alpha_post)

      for (target_idx in seq_len(n_cases)) {
        alpha_samples <- suppressWarnings(as.integer(alpha_post[, target_idx]))
        # Issue #10: Calculate proportion unlinked before filtering
        n_unlinked <- sum(is.na(alpha_samples))
        p_unlinked[ids[target_idx]] <- round(n_unlinked / max(1, length(alpha_samples)), 4)
        
        ancestry <- alpha_samples[!is.na(alpha_samples)]
        ancestry <- ancestry[ancestry >= 1 & ancestry <= n_cases]

        if (length(ancestry) == 0) {
          next
        }

        ancestry <- ancestry[ancestry != target_idx]
        if (length(ancestry) == 0) {
          next
        }

        freq <- table(ancestry)
        sorted_freq <- sort(freq, decreasing = TRUE)
        all_probs <- as.numeric(sorted_freq) / max(1, nrow(alpha_post))
        # Issue #5: Export top alternative ancestors (up to 3 total candidates) with probabilities
        best_ancestor_idx <- as.integer(names(sorted_freq)[1])
        edge_prob <- as.numeric(sorted_freq[1]) / max(1, nrow(alpha_post))

        src <- ids[best_ancestor_idx]
        dst <- ids[target_idx]
        chain_stability <- edge_chain_stability(chain_local, target_idx, best_ancestor_idx, n_cases)
        outgoing[src] <- outgoing[src] + edge_prob
        incoming[dst] <- incoming[dst] + edge_prob

        # Issue #5: Build alternative ancestor candidates
        # Guard with seq_len to avoid R's 2:1 = c(2,1) pitfall when sorted_freq has only 1 entry
        alternative_ancestors <- list()
        n_alts <- min(3, length(sorted_freq))
        if (n_alts >= 2) {
          for (cand_rank in 2:n_alts) {
            alt_ancestor_idx <- as.integer(names(sorted_freq)[cand_rank])
            alt_prob <- as.numeric(sorted_freq[cand_rank]) / max(1, nrow(alpha_post))
          alternative_ancestors[[cand_rank - 1]] <- list(
              ancestor_id = ids[alt_ancestor_idx],
              probability = round(alt_prob, 4),
              rank = cand_rank
            )
          }
        }

        source_row <- case_table[case_table$case_id == src, , drop = FALSE]
        target_row <- case_table[case_table$case_id == dst, , drop = FALSE]
        shared_context <- c()
        for (ctx_col in c("hsc_trust", "postcode", "postcode_sector", "lineage")) {
          if (ctx_col %in% names(case_table) && nrow(source_row) == 1 && nrow(target_row) == 1) {
            if (nzchar(as.character(source_row[[ctx_col]])) && identical(as.character(source_row[[ctx_col]]), as.character(target_row[[ctx_col]]))) {
              shared_context <- c(shared_context, paste0("shared ", ctx_col, " ", as.character(source_row[[ctx_col]])))
            }
          }
        }
        interpretation <- paste0(
          "Case ", substr(src, 1, 8), " has posterior support ",
          sprintf("%.2f", edge_prob), " as the likely source for case ",
          substr(dst, 1, 8), ifelse(length(shared_context) > 0, paste0("; ", paste(shared_context, collapse = ", ")), ""), "."
        )

        edge_list[[length(edge_list) + 1]] <- list(
          source = src,
          target = dst,
          probability = round(edge_prob, 4),
          posterior_entropy = posterior_entropy(all_probs),
          chain_stability = chain_stability,
          credibility_class = ifelse(edge_prob >= 0.8, "Strong", ifelse(edge_prob >= 0.5, "Moderate", "Weak")),
          confidence = ifelse(
            posterior_reliable,
            ifelse(edge_prob >= 0.8, "high", ifelse(edge_prob >= 0.6, "medium", "low")),
            "not_assessable"
          ),
          posterior_reliability = reliability_status,
          inference = "posterior_marginal_mode",
          interpretation = interpretation,
          alternative_ancestors = alternative_ancestors
        )
      }
    }

    node_list <- lapply(ids, function(case_id) {
      case_row <- case_table[case_table$case_id == case_id, , drop = FALSE]
      op_score <- if (nrow(case_row) == 1) operational_risk_score(case_row) else 0
      score <- round(
        as.numeric(outgoing[case_id]) * 0.7 +
          as.numeric(incoming[case_id]) * 0.3,
        4
      )
      band <- ifelse(
        score >= 1.2, "high",
        ifelse(score >= 0.5, "medium", "low")
      )
      list(
        case_id = case_id,
        display_case_id = substr(case_id, 1, 8),
        full_case_id = case_id,
        risk_score = score,
        risk_band = band,
        public_health_risk_score = op_score,
        public_health_priority = ifelse(op_score >= 6, "high", ifelse(op_score >= 3, "medium", "low")),
        outgoing_links = as.integer(
          sum(vapply(
            edge_list, function(e) e$source == case_id, logical(1)
          ))
        ),
        incoming_links = as.integer(
          sum(vapply(
            edge_list, function(e) e$target == case_id, logical(1)
          ))
        ),
        # Issue #10: Proportion of posterior samples with NA (unlinked) ancestry
        p_unlinked = p_unlinked[case_id],
        likely_index_case = if (is.na(p_unlinked[case_id])) NA else (p_unlinked[case_id] > 0.5)
      )
    })

    key_nodes <- head(
      node_list[order(
        vapply(node_list, function(n) n$risk_score, numeric(1)),
        decreasing = TRUE
      )],
      10
    )
    high_conf_count <- sum(
      vapply(edge_list, function(e) e$probability >= 0.8, logical(1))
    )

    list(
      generated_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"),
      inference_source = "outbreaker2_posterior",
      provenance = "real",
      posterior_reliable = posterior_reliable,
      posterior_reliability_status = reliability_status,
      posterior_reliability_reasons = as.list(reliability_reasons),
      node_count = length(node_list),
      edge_count = length(edge_list),
      high_confidence_edges = as.integer(high_conf_count),
      consensus_methods = list(
        marginal_posterior_ancestry = "exported",
        decycle = decycle_status,
        instability_flag = any(vapply(edge_list, function(e) e$posterior_entropy > 0.7, logical(1)))
      ),
      posterior_samples = as.integer(posterior_samples),
      all_nodes = node_list,
      key_nodes = key_nodes,
      edges = edge_list
    )
  }

  build_decycled_consensus <- function(result) {
    payload <- tryCatch(
      {
        decycled <- summary(result, method = "decycle")
        list(
          status = "completed",
          method = "decycle",
          text = as.list(capture.output(print(decycled)))
        )
      },
      error = function(e) {
        list(
          status = "unavailable",
          method = "decycle",
          error = as.character(e)
        )
      }
    )
    write_json(payload, export_file("outbreaker_decycled_consensus.json"), pretty = TRUE, auto_unbox = TRUE)
    payload
  }

  generate_interactive_network <- function(network, case_table) {
    if (!has_visnetwork || !has_htmlwidgets) {
      return(list(status = "optional_unavailable", path = NA_character_))
    }
    node_rows <- lapply(network$all_nodes, function(node) {
      case_id <- node$full_case_id
      case_row <- case_table[case_table$case_id == case_id, , drop = FALSE]
      lookup <- function(col) {
        if (col %in% names(case_row) && nrow(case_row) == 1) as.character(case_row[[col]]) else ""
      }
      title <- paste0(
        "<b>", node$display_case_id, "</b><br/>",
        "Full ID: ", case_id, "<br/>",
        "Risk score: ", node$risk_score, "<br/>",
        "Public health risk: ", node$public_health_risk_score, " (", node$public_health_priority, ")<br/>",
        "p_unlinked: ", node$p_unlinked, "<br/>",
        "Onset: ", lookup("symptom_onset_date"), "<br/>",
        "Sample: ", lookup("sample_date"), "<br/>",
        "HSC Trust: ", lookup("hsc_trust"), "<br/>",
        "Lineage: ", lookup("lineage"), "<br/>",
        "Smear: ", lookup("smear_status")
      )
      data.frame(
        id = case_id,
        label = node$display_case_id,
        title = title,
        value = max(1, as.numeric(node$public_health_risk_score) + as.numeric(node$risk_score) * 5),
        group = node$public_health_priority,
        shape = ifelse(isTRUE(node$likely_index_case), "diamond", "dot"),
        stringsAsFactors = FALSE
      )
    })
    edge_rows <- lapply(network$edges, function(edge) {
      edge_color <- if (edge$credibility_class == "Strong") {
        "#15803d"
      } else if (edge$credibility_class == "Moderate") {
        "#d97706"
      } else {
        "#dc2626"
      }
      stability <- edge$chain_stability
      alt_text <- ""
      if (length(edge$alternative_ancestors) > 0) {
        alt_text <- paste(
          vapply(edge$alternative_ancestors, function(alt) {
            paste0(substr(alt$ancestor_id, 1, 8), " (", alt$probability, ")")
          }, character(1)),
          collapse = ", "
        )
      }
      title <- paste0(
        "<b>", substr(edge$source, 1, 8), " -> ", substr(edge$target, 1, 8), "</b><br/>",
        "Posterior probability: ", edge$probability, "<br/>",
        "Credibility: ", edge$credibility_class, "<br/>",
        "Posterior entropy: ", edge$posterior_entropy, "<br/>",
        "Top-ancestor chain agreement: ", stability$top_ancestor_agreement, "<br/>",
        "Probability range by chain: ", stability$probability_min, "-", stability$probability_max, "<br/>",
        "Alternative ancestors: ", alt_text, "<br/>",
        edge$interpretation
      )
      data.frame(
        from = edge$source,
        to = edge$target,
        label = paste0(round(100 * as.numeric(edge$probability)), "%"),
        title = title,
        width = max(1, 8 * as.numeric(edge$probability)),
        color = edge_color,
        dashes = as.numeric(edge$probability) < 0.5,
        arrows = "to",
        stringsAsFactors = FALSE
      )
    })
    nodes_df <- if (length(node_rows) > 0) {
      do.call(rbind, node_rows)
    } else {
      data.frame(id = character(), label = character(), title = character(), value = numeric(), group = character(), shape = character())
    }
    edges_df <- if (length(edge_rows) > 0) {
      do.call(rbind, edge_rows)
    } else {
      data.frame(from = character(), to = character(), label = character(), title = character(), width = numeric(), color = character(), dashes = logical(), arrows = character())
    }
    widget <- visNetwork::visNetwork(nodes_df, edges_df, height = "760px", width = "100%", main = "Interactive TB Transmission Network")
    widget <- visNetwork::visNodes(widget, scaling = list(min = 12, max = 38), font = list(size = 16))
    widget <- visNetwork::visEdges(widget, smooth = list(type = "dynamic"), shadow = TRUE)
    widget <- visNetwork::visGroups(widget, groupname = "high", color = list(background = "#fee2e2", border = "#dc2626"))
    widget <- visNetwork::visGroups(widget, groupname = "medium", color = list(background = "#fef3c7", border = "#d97706"))
    widget <- visNetwork::visGroups(widget, groupname = "low", color = list(background = "#dcfce7", border = "#15803d"))
    widget <- visNetwork::visOptions(widget, highlightNearest = list(enabled = TRUE, degree = 1, hover = TRUE), selectedBy = "group")
    widget <- visNetwork::visLegend(widget, useGroups = TRUE, addEdges = data.frame(
      label = c("Strong edge", "Moderate edge", "Weak edge"),
      color = c("#15803d", "#d97706", "#dc2626"),
      width = c(5, 3, 1),
      dashes = c(FALSE, FALSE, TRUE)
    ))
    widget <- visNetwork::visInteraction(widget, hover = TRUE, navigationButtons = TRUE, keyboard = TRUE)
    out_html <- export_file("outbreaker_interactive_network.html")
    htmlwidgets::saveWidget(widget, out_html, selfcontained = TRUE)
    list(status = "completed", path = out_html)
  }

  # Save R object
  saveRDS(
    list(
      primary = res,
      chains = lapply(chain_runs, function(x) x$result),
      chain_configs = lapply(chain_runs, function(x) x$config)
    ),
    export_file("outbreaker2_results.rds")
  )
  cat("Results saved to ", export_file("outbreaker2_results.rds"), "\n", sep = "")

  # Generate plots
  cat("Generating diagnostic plots...\n")

  # Build robust diagnostics directly from the posterior chain. This avoids
  # empty/placeholder images when plot(res, type=...) methods are unavailable
  # or fragile across outbreaker2 versions.
  metric_col <- if ("like" %in% names(chain_df)) {
    "like"
  } else if ("post" %in% names(chain_df)) {
    "post"
  } else {
    NA_character_
  }
  chain_df$plot_step <- ave(seq_len(nrow(chain_df)), chain_df$chain_id, FUN = seq_along)
  if (!is.na(metric_col)) {
    metric_vals <- as.numeric(posterior_chain_df[[metric_col]])
  } else {
    metric_vals <- as.numeric(seq_len(nrow(posterior_chain_df)))
  }
  post_metric_vals <- metric_vals
  metric_label <- ifelse(
    is.na(metric_col), "Iteration index", paste0(toupper(metric_col), " value")
  )
  recent_window <- min(250, length(post_metric_vals))
  early_mean <- if (length(post_metric_vals) > recent_window) {
    mean(post_metric_vals[1:recent_window], na.rm = TRUE)
  } else {
    NA_real_
  }
  late_mean <- if (length(post_metric_vals) > recent_window) {
    mean(tail(post_metric_vals, recent_window), na.rm = TRUE)
  } else {
    NA_real_
  }
  late_drift <- if (!is.na(early_mean) && abs(early_mean) > 0) {
    abs(late_mean - early_mean) / abs(early_mean)
  } else {
    NA_real_
  }
  diagnostic_status <- if (!is.na(late_drift) && late_drift > 0.10) {
    "Trace still drifting - repeat with longer chains"
  } else {
    "No strong late drift by simple mean check"
  }
  build_mcmc_diagnostics <- function(full_chain, post_chain) {
    diagnostic_cols <- intersect(
      c("post", "like", "prior", "mu", "pi", "eps", "lambda", "kappa"),
      names(full_chain)
    )
    ess_values <- list()
    for (col_name in diagnostic_cols) {
      ess_values[[col_name]] <- round(estimate_ess(post_chain[[col_name]]), 2)
    }
    diagnostics <- list(
      coda_available = has_coda,
      effective_size = ess_values,
      gelman_rubin = list(status = "not_run", psrf_max = NA_real_),
      geweke = list(status = "not_run", max_abs_z = NA_real_),
      heidelberg = list(status = "not_run", failed_count = NA_integer_)
    )
    if (!has_coda || length(diagnostic_cols) == 0 || nrow(post_chain) < 10) {
      return(diagnostics)
    }
    if (n_chains > 1) {
      mcmc_parts <- lapply(split(post_chain, post_chain$chain_id), function(chain_part) {
        coda::mcmc(chain_part[, diagnostic_cols, drop = FALSE])
      })
      if (length(mcmc_parts) > 1) {
        gelman_result <- tryCatch(coda::gelman.diag(coda::mcmc.list(mcmc_parts), autoburnin = FALSE), error = function(e) NULL)
        if (!is.null(gelman_result)) {
          psrf_values <- as.numeric(gelman_result$psrf[, 1])
          diagnostics$gelman_rubin <- list(
            status = "completed",
            psrf_max = round(max(psrf_values, na.rm = TRUE), 4)
          )
        }
      }
    }
    first_mcmc <- coda::mcmc(post_chain[, diagnostic_cols, drop = FALSE])
    geweke_result <- tryCatch(coda::geweke.diag(first_mcmc), error = function(e) NULL)
    if (!is.null(geweke_result)) {
      z_values <- as.numeric(geweke_result$z)
      diagnostics$geweke <- list(
        status = "completed",
        max_abs_z = round(max(abs(z_values), na.rm = TRUE), 4)
      )
    }
    heidel_result <- tryCatch(coda::heidel.diag(first_mcmc), error = function(e) NULL)
    if (!is.null(heidel_result)) {
      stationarity <- if (is.matrix(heidel_result)) heidel_result[, 1] else numeric(0)
      diagnostics$heidelberg <- list(
        status = "completed",
        failed_count = as.integer(sum(stationarity != 1, na.rm = TRUE))
      )
    }
    diagnostics
  }
  mcmc_diagnostics <- build_mcmc_diagnostics(chain_df, posterior_chain_df)

  png(export_file("outbreaker_trace.png"), width = 1400, height = 900, res = 140)
  par(mar = c(4.8, 5.2, 4.6, 1.5), family = "sans")
  trace_values <- if (!is.na(metric_col)) as.numeric(chain_df[[metric_col]]) else chain_df$plot_step
  plot(
    chain_df$plot_step,
    trace_values,
    type = "n",
    xlab = "Saved sample within chain",
    ylab = metric_label,
    main = "Outbreaker2 MCMC Trace by Chain",
    cex.main = 1.15,
    cex.lab = 0.95,
    cex.axis = 0.85
  )
  palette <- c("#2563eb", "#0f766e", "#b45309", "#7c3aed", "#dc2626", "#475569")
  for (chain_id in sort(unique(chain_df$chain_id))) {
    part <- chain_df[chain_df$chain_id == chain_id, , drop = FALSE]
    part_vals <- if (!is.na(metric_col)) as.numeric(part[[metric_col]]) else part$plot_step
    lines(
      part$plot_step,
      part_vals,
      col = palette[((as.integer(chain_id) - 1L) %% length(palette)) + 1L],
      lwd = 1.1
    )
  }
  grid(col = "grey88", lty = "dotted")
  if (burnin_rows > 0) {
    abline(v = burnin_rows, col = "#dc2626", lty = 2, lwd = 1.2)
    legend(
      "bottomright",
      legend = c(paste("Chain", sort(unique(chain_df$chain_id))), "Burn-in cutoff"),
      col = c(rep(palette, length.out = length(unique(chain_df$chain_id))), "#dc2626"),
      lty = c(rep(1, length(unique(chain_df$chain_id))), 2),
      lwd = c(rep(1.2, length(unique(chain_df$chain_id))), 1.2),
      bty = "n",
      cex = 0.82
    )
  }
  mtext(diagnostic_status, side = 3, line = 0.35, cex = 0.78, col = "#475569")
  dev.off()
  cat("✓ Trace plot saved\n")

  png(export_file("outbreaker_hist.png"), width = 1400, height = 900, res = 140)
  par(mar = c(4.8, 5.2, 4.6, 1.5), family = "sans")
  clean_post <- post_metric_vals[is.finite(post_metric_vals)]
  if (length(clean_post) < 2) {
    plot.new()
    title(main = "Outbreaker2 Posterior Distribution After Burn-in")
    text(
      0.5,
      0.55,
      labels = paste0("Insufficient posterior draws after burn-in (n=", length(clean_post), ")"),
      cex = 1.0,
      col = "#334155"
    )
    text(
      0.5,
      0.45,
      labels = "Increase MCMC iterations / reduce burn-in before operational interpretation",
      cex = 0.88,
      col = "#64748b"
    )
  } else {
    n_bins <- max(8, min(60, floor(sqrt(length(clean_post)))))
    h <- hist(clean_post, breaks = n_bins, plot = FALSE)
    plot(
      h,
      col = "#93c5fd",
      border = "white",
      main = "Outbreaker2 Posterior Distribution After Burn-in",
      xlab = metric_label,
      ylab = "Frequency",
      cex.main = 1.15,
      cex.lab = 0.95,
      cex.axis = 0.85
    )
    grid(col = "grey88", lty = "dotted")
    abline(v = mean(clean_post), col = "#1d4ed8", lwd = 1.4)
    abline(v = median(clean_post), col = "#0f766e", lwd = 1.2, lty = 2)
    if (length(unique(clean_post)) > 5) {
      lines(density(clean_post, na.rm = TRUE), col = "#334155", lwd = 1.2)
    }
    if (max(h$counts, na.rm = TRUE) <= 1) {
      mtext(
        "Sparse posterior draws: histogram bars are single-count; treat diagnostics as low-confidence",
        side = 3,
        line = 0.35,
        cex = 0.78,
        col = "#b45309"
      )
    }
    legend(
      "topleft",
      legend = c(
        paste0("Posterior draws (n=", length(clean_post), ", bins=", n_bins, ")"),
        "Mean",
        "Median",
        "Density"
      ),
      fill = c("#93c5fd", NA, NA, NA),
      border = c("white", NA, NA, NA),
      lty = c(NA, 1, 2, 1),
      col = c(NA, "#1d4ed8", "#0f766e", "#334155"),
      lwd = c(NA, 1.4, 1.2, 1.2),
      bty = "n",
      cex = 0.82
    )
  }
  dev.off()
  cat("✓ Histogram saved\n")

  # Generate transmission tree/network
  cat("Generating transmission tree...\n")
  tryCatch(
    {
      png(export_file("outbreaker_tree.png"), width = 1400, height = 1000)
      tryCatch(
        {
          plot(res, type = "tree")
        },
        error = function(tree_err) {
          # Newer/alternate outbreaker2 versions may not expose type='tree'.
          # Fallback to the supported network visualization.
          cat("  tree plot mode unavailable, falling back to type='network'\n")
          plot(res, type = "network")
        }
      )
      dev.off()
      cat("✓ Transmission tree saved\n")
    },
    error = function(e) {
      try(dev.off(), silent = TRUE)
      if (file.exists(export_file("outbreaker_tree.png"))) {
        file.remove(export_file("outbreaker_tree.png"))
      }
      cat("⚠ Could not generate transmission tree plot:\n")
      cat("  ", as.character(e), "\n")
    }
  )

  # Reliability gate for posterior interpretation depth/ESS.
  # Confidence bands are only exposed when depth + ESS + drift checks pass.
  like_col_for_gate <- if ("like" %in% names(chain_df)) {
    "like"
  } else if ("post" %in% names(chain_df)) {
    "post"
  } else {
    NA_character_
  }
  like_values_for_gate <- if (!is.na(like_col_for_gate) && nrow(posterior_chain_df) > 0) {
    as.numeric(posterior_chain_df[[like_col_for_gate]])
  } else {
    numeric(0)
  }
  alpha_cols_for_gate <- grep("^alpha_", names(chain_df), value = TRUE)
  alpha_ess_values_for_gate <- numeric(0)
  if (length(alpha_cols_for_gate) > 0 && nrow(posterior_chain_df) > 0) {
    alpha_post_for_gate <- posterior_chain_df[, alpha_cols_for_gate, drop = FALSE]
    if (nrow(alpha_post_for_gate) > 2) {
      for (col_name in names(alpha_post_for_gate)) {
        ess_local <- estimate_ess(alpha_post_for_gate[[col_name]])
        if (is.finite(ess_local)) {
          alpha_ess_values_for_gate <- c(alpha_ess_values_for_gate, ess_local)
        }
      }
    }
  }
  like_ess_for_gate <- if (length(like_values_for_gate) > 2) estimate_ess(like_values_for_gate) else NA_real_
  alpha_ess_min_for_gate <- if (length(alpha_ess_values_for_gate) > 0) min(alpha_ess_values_for_gate) else NA_real_
  posterior_samples_for_gate <- nrow(posterior_chain_df)
  reliability_reasons <- c()
  if (posterior_samples_for_gate < 1000) {
    reliability_reasons <- c(reliability_reasons, sprintf("posterior_samples_below_minimum(%s<1000)", posterior_samples_for_gate))
  }
  if (!is.finite(like_ess_for_gate) || like_ess_for_gate < 200) {
    reliability_reasons <- c(reliability_reasons, sprintf("mcmc_ess_inadequate(%s)", ifelse(is.finite(like_ess_for_gate), round(like_ess_for_gate, 2), "NA")))
  }
  if (!is.finite(alpha_ess_min_for_gate) || alpha_ess_min_for_gate < 100) {
    reliability_reasons <- c(reliability_reasons, sprintf("alpha_ess_min_inadequate(%s)", ifelse(is.finite(alpha_ess_min_for_gate), round(alpha_ess_min_for_gate, 2), "NA")))
  }
  if (!is.na(late_drift) && late_drift > 0.10) {
    reliability_reasons <- c(reliability_reasons, sprintf("late_drift_exceeds_threshold(%.4f)", late_drift))
  }
  rhat_max <- suppressWarnings(as.numeric(mcmc_diagnostics$gelman_rubin$psrf_max))
  if (n_chains > 1 && is.finite(rhat_max) && rhat_max > 1.1) {
    reliability_reasons <- c(reliability_reasons, sprintf("gelman_rubin_psrf_high(%.4f)", rhat_max))
  }
  posterior_reliable <- length(reliability_reasons) == 0
  reliability_status <- if (posterior_reliable) "reliable" else "not_assessable"

  decycled_consensus <- build_decycled_consensus(res)

  # Export posterior-derived transmission network in JSON format.
  network <- build_transmission_network(
    posterior_chain_df,
    as.character(cases$case_id),
    cases,
    0,
    posterior_reliable = posterior_reliable,
    reliability_status = reliability_status,
    reliability_reasons = reliability_reasons,
    decycle_status = decycled_consensus$status
  )
  write_json(
    network, export_file("transmission_network.json"),
    pretty = TRUE, auto_unbox = TRUE
  )
  cat("✓ Transmission network JSON saved\n")
  interactive_network <- generate_interactive_network(network, cases)
  if (interactive_network$status == "completed") {
    cat("✓ Interactive transmission network HTML saved\n")
  } else {
    cat("⚠ Interactive network skipped: visNetwork/htmlwidgets unavailable\n")
  }

  # Generate summary statistics
  cat("Generating summary report...\n")
  burnin_effective <- burnin_rows
  like_col <- if ("like" %in% names(chain_df)) {
    "like"
  } else if ("post" %in% names(chain_df)) {
    "post"
  } else {
    NA_character_
  }
  like_values <- if (!is.na(like_col) && nrow(posterior_chain_df) > 0) {
    as.numeric(posterior_chain_df[[like_col]])
  } else {
    numeric(0)
  }
  alpha_cols <- grep("^alpha_", names(chain_df), value = TRUE)
  alpha_post <- if (length(alpha_cols) > 0 && nrow(posterior_chain_df) > 0) {
    posterior_chain_df[, alpha_cols, drop = FALSE]
  } else {
    data.frame()
  }
  alpha_ess_by_case <- list()
  alpha_ess_values <- numeric(0)
  if (nrow(alpha_post) > 2 && ncol(alpha_post) > 0) {
    case_ids_chr <- as.character(cases$case_id)
    for (col_name in names(alpha_post)) {
      idx <- suppressWarnings(as.integer(sub("^alpha_", "", col_name)))
      case_label <- if (!is.na(idx) && idx >= 1 && idx <= length(case_ids_chr)) {
        case_ids_chr[idx]
      } else {
        col_name
      }
      ess <- estimate_ess(alpha_post[[col_name]])
      alpha_ess_by_case[[case_label]] <- ifelse(is.finite(ess), round(ess, 2), NA_real_)
      if (is.finite(ess)) {
        alpha_ess_values <- c(alpha_ess_values, ess)
      }
    }
  }
  summary_stats <- list(
    n_generations = nrow(chain_df),
    burnin = burnin_iters,
    burnin_rows = burnin_effective,
    n_samples = nrow(posterior_chain_df),
    thinning = thin_every,
    chains = n_chains,
    rng_seed = set_seed,
    case_count = length(cases$case_id),
    qc_excluded_case_count = nrow(qc_exclusions),
    likelihood_mean = ifelse(
      length(like_values) > 0, mean(like_values), NA_real_
    ),
    likelihood_sd = ifelse(
      length(like_values) > 1, sd(like_values), NA_real_
    ),
    transmission_edges = length(network$edges),
    posterior_samples = network$posterior_samples,
    mcmc_effective_sample_size = ifelse(
      length(like_values) > 2,
      round(estimate_ess(like_values), 2),
      NA_real_
    ),
    posterior_reliable = posterior_reliable,
    posterior_reliability_status = reliability_status,
    posterior_reliability_reasons = as.list(reliability_reasons),
    alpha_column_count = length(alpha_cols),
    alpha_mcmc_effective_sample_size_mean = ifelse(
      length(alpha_ess_values) > 0,
      round(mean(alpha_ess_values), 2),
      NA_real_
    ),
    alpha_mcmc_effective_sample_size_min = ifelse(
      length(alpha_ess_values) > 0,
      round(min(alpha_ess_values), 2),
      NA_real_
    ),
    alpha_mcmc_effective_sample_size_max = ifelse(
      length(alpha_ess_values) > 0,
      round(max(alpha_ess_values), 2),
      NA_real_
    ),
    alpha_mcmc_effective_sample_size_by_case = alpha_ess_by_case,
    coda_diagnostics = mcmc_diagnostics,
    mcmc_diagnostic_status = diagnostic_status,
    mcmc_late_drift_fraction = ifelse(is.na(late_drift), NA_real_, late_drift),
    mcmc_iteration_config = list(
      n_iter_total = n_iter_total,
      burnin_iters = burnin_iters,
      thin_every = thin_every,
      chains = n_chains,
      init_reporting_probability = init_pi,
      prior_reporting_probability = prior_pi,
      reporting_probability_note = "outbreaker2 pi is the case reporting/sampling probability, not an importation probability",
      init_kappa = init_kappa
    ),
    serial_interval_config = list(
      si_window = si_window,
      w_shape = serial_w_shape,
      w_scale = serial_w_scale,
      f_shape = serial_f_shape,
      f_scale = serial_f_scale
    ),
    contact_tracing = list(
      fields_used = as.list(ctd_payload$fields),
      link_count = ifelse(is.null(ctd_payload$ctd), 0, nrow(ctd_payload$ctd)),
      directed = FALSE,
      note = "co-location/contact fields are encoded as undirected evidence; paired rows are emitted only for outbreaker2 ctd compatibility"
    ),
    interactive_outputs = list(
      transmission_network_html = interactive_network
    ),
    qc_config = list(
      min_depth = min_depth,
      min_coverage_breadth = min_coverage_breadth,
      max_ambiguous_base_percent = max_ambiguous_base_percent,
      exclude_failed_qc = exclude_failed_qc
    ),
    data_provenance = "real",
    git_commit = git_commit_hash(),
    artifact_hashes = list(
      cases_csv_sha256 = file_sha256(export_file("cases.csv")),
      dna_fasta_sha256 = file_sha256(export_file("dna.fasta")),
      sample_qc_metrics_csv_sha256 = file_sha256(export_file("sample_qc_metrics.csv")),
      analysis_provenance_csv_sha256 = file_sha256(export_file("analysis_provenance.csv")),
      config_sha256 = file_sha256(config_path)
    ),
    package_versions = list(
      outbreaker2 = as.character(utils::packageVersion("outbreaker2")),
      ape = as.character(utils::packageVersion("ape")),
      jsonlite = as.character(utils::packageVersion("jsonlite")),
      coda = ifelse(has_coda, as.character(utils::packageVersion("coda")), NA_character_),
      visNetwork = ifelse(has_visnetwork, as.character(utils::packageVersion("visNetwork")), NA_character_)
    ),
    analysis_engine = "outbreaker2",
    analysis_engine_version = as.character(utils::packageVersion("outbreaker2")),
    generated_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC")
  )

  # Save summary as JSON
  write_json(
    summary_stats, export_file("outbreaker_summary.json"),
    pretty = TRUE, auto_unbox = TRUE
  )
  cat("✓ Summary saved\n")

  cat("\n✅ Outbreaker2 analysis complete\n")
}, error = function(e) {
  cat("ERROR:", as.character(e), "\n")
  quit(status = 1)
})
