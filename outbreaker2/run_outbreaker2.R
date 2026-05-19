
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

  # Create outputs directory
  dir.create("exports", showWarnings = FALSE)

  # Read input data
  cases <- read.csv("exports/cases.csv", stringsAsFactors = FALSE)
  dna <- read.dna("exports/dna.fasta", format = "fasta")

  # TB serial interval defaults (long-tailed compared with acute infections).
  # Defaults can be tuned per deployment using environment variables.
  si_window <- as.integer(Sys.getenv("TB_OUTBREAKER_SI_WINDOW", "730"))
  serial_w_shape <- as.numeric(Sys.getenv("TB_OUTBREAKER_W_SHAPE", "2.0"))
  serial_w_scale <- as.numeric(Sys.getenv("TB_OUTBREAKER_W_SCALE", "90.0"))
  serial_f_shape <- as.numeric(Sys.getenv("TB_OUTBREAKER_F_SHAPE", "1.5"))
  serial_f_scale <- as.numeric(Sys.getenv("TB_OUTBREAKER_F_SCALE", "90.0"))

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
  aligned_dates <- case_dates[dna_ids]

  if (any(is.na(aligned_dates))) {
    stop("Some DNA labels do not have matching case dates")
  }

  names(aligned_dates) <- dna_ids

  out_data <- outbreaker_data(
    dates = aligned_dates,
    dna = dna,
    w_dens = w_dens,
    f_dens = f_dens
  )

  # Run outbreak investigation
  cat("Running outbreaker2 analysis...\n")
  n_iter_total <- as.integer(Sys.getenv("TB_OUTBREAKER_ITER", "50000"))
  burnin_iters <- as.integer(Sys.getenv("TB_OUTBREAKER_BURNIN", "10000"))
  thin_every <- as.integer(Sys.getenv("TB_OUTBREAKER_THIN", "10"))
  cfg <- create_config(n_iter = n_iter_total, sample_every = thin_every)
  res <- outbreaker(data = out_data, config = cfg)
  chain_df <- as.data.frame(res)

  estimate_ess <- function(series) {
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

  build_transmission_network <- function(result, ids, burnin_iter) {
    chain_local <- as.data.frame(result)
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
        # Issue #5: Export top alternative ancestors (up to 3 total candidates) with probabilities
        best_ancestor_idx <- as.integer(names(sorted_freq)[1])
        edge_prob <- as.numeric(sorted_freq[1]) / max(1, nrow(alpha_post))

        src <- ids[best_ancestor_idx]
        dst <- ids[target_idx]
        outgoing[src] <- outgoing[src] + edge_prob
        incoming[dst] <- incoming[dst] + edge_prob

        # Issue #5: Build alternative ancestor candidates
        alternative_ancestors <- list()
        for (cand_rank in 2:min(3, length(sorted_freq))) {
          alt_ancestor_idx <- as.integer(names(sorted_freq)[cand_rank])
          alt_prob <- as.numeric(sorted_freq[cand_rank]) / max(1, nrow(alpha_post))
          alternative_ancestors[[cand_rank - 1]] <- list(
            ancestor_id = ids[alt_ancestor_idx],
            probability = round(alt_prob, 4),
            rank = cand_rank
          )
        }

        edge_list[[length(edge_list) + 1]] <- list(
          source = src,
          target = dst,
          probability = round(edge_prob, 4),
          confidence = ifelse(
            edge_prob >= 0.8, "high",
            ifelse(edge_prob >= 0.6, "medium", "low")
          ),
          inference = "posterior_marginal_mode",
          alternative_ancestors = alternative_ancestors
        )
      }
    }

    node_list <- lapply(ids, function(case_id) {
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
        case_id = substr(case_id, 1, 8),
        full_case_id = case_id,
        risk_score = score,
        risk_band = band,
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
      node_count = length(node_list),
      edge_count = length(edge_list),
      high_confidence_edges = as.integer(high_conf_count),
      posterior_samples = as.integer(posterior_samples),
      all_nodes = node_list,
      key_nodes = key_nodes,
      edges = edge_list
    )
  }

  # Save R object
  saveRDS(res, "exports/outbreaker2_results.rds")
  cat("Results saved to exports/outbreaker2_results.rds\n")

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
  if (!is.na(metric_col)) {
    metric_vals <- as.numeric(chain_df[[metric_col]])
  } else {
    metric_vals <- as.numeric(seq_len(nrow(chain_df)))
  }

  burnin_effective_for_plot <- min(burnin_iters, max(0, length(metric_vals) - 1))
  post_metric_vals <- if (burnin_effective_for_plot < length(metric_vals)) {
    metric_vals[(burnin_effective_for_plot + 1):length(metric_vals)]
  } else {
    metric_vals
  }
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

  png("exports/outbreaker_trace.png", width = 1400, height = 900, res = 140)
  par(mar = c(4.8, 5.2, 4.6, 1.5), family = "sans")
  plot(
    metric_vals,
    type = "l",
    col = "#2563eb",
    lwd = 1.2,
    xlab = "Iteration",
    ylab = metric_label,
    main = "Outbreaker2 MCMC Trace",
    cex.main = 1.15,
    cex.lab = 0.95,
    cex.axis = 0.85
  )
  grid(col = "grey88", lty = "dotted")
  if (burnin_effective_for_plot > 0) {
    abline(v = burnin_effective_for_plot, col = "#dc2626", lty = 2, lwd = 1.2)
    legend(
      "bottomright",
      legend = c("Trace", "Burn-in cutoff"),
      col = c("#2563eb", "#dc2626"),
      lty = c(1, 2),
      lwd = c(1.2, 1.2),
      bty = "n",
      cex = 0.82
    )
  }
  mtext(diagnostic_status, side = 3, line = 0.35, cex = 0.78, col = "#475569")
  dev.off()
  cat("✓ Trace plot saved\n")

  png("exports/outbreaker_hist.png", width = 1400, height = 900, res = 140)
  par(mar = c(4.8, 5.2, 4.6, 1.5), family = "sans")
  hist(
    post_metric_vals,
    breaks = 40,
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
  abline(v = mean(post_metric_vals, na.rm = TRUE), col = "#1d4ed8", lwd = 1.4)
  legend(
    "topleft",
    legend = c("Posterior samples", "Mean"),
    fill = c("#93c5fd", NA),
    border = c("white", NA),
    lty = c(NA, 1),
    col = c(NA, "#1d4ed8"),
    lwd = c(NA, 1.4),
    bty = "n",
    cex = 0.82
  )
  dev.off()
  cat("✓ Histogram saved\n")

  # Generate transmission tree/network
  cat("Generating transmission tree...\n")
  tryCatch(
    {
      png("exports/outbreaker_tree.png", width = 1400, height = 1000)
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
      if (file.exists("exports/outbreaker_tree.png")) {
        file.remove("exports/outbreaker_tree.png")
      }
      cat("⚠ Could not generate transmission tree plot:\n")
      cat("  ", as.character(e), "\n")
    }
  )

  # Export posterior-derived transmission network in JSON format.
  network <- build_transmission_network(
    res, as.character(cases$case_id), burnin_iters
  )
  write_json(
    network, "exports/transmission_network.json",
    pretty = TRUE, auto_unbox = TRUE
  )
  cat("✓ Transmission network JSON saved\n")

  # Generate summary statistics
  cat("Generating summary report...\n")
  burnin_effective <- min(burnin_iters, max(0, nrow(chain_df) - 1))
  post_start <- burnin_effective + 1
  like_col <- if ("like" %in% names(chain_df)) {
    "like"
  } else if ("post" %in% names(chain_df)) {
    "post"
  } else {
    NA_character_
  }
  like_values <- if (!is.na(like_col)) {
    as.numeric(chain_df[[like_col]][post_start:nrow(chain_df)])
  } else {
    numeric(0)
  }
  alpha_cols <- grep("^alpha_", names(chain_df), value = TRUE)
  alpha_post <- if (length(alpha_cols) > 0 && post_start <= nrow(chain_df)) {
    chain_df[post_start:nrow(chain_df), alpha_cols, drop = FALSE]
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
    burnin = burnin_effective,
    n_samples = max(0, nrow(chain_df) - burnin_effective),
    thinning = thin_every,
    case_count = length(cases$case_id),
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
    mcmc_diagnostic_status = diagnostic_status,
    mcmc_late_drift_fraction = ifelse(is.na(late_drift), NA_real_, late_drift),
    mcmc_iteration_config = list(
      n_iter_total = n_iter_total,
      burnin_iters = burnin_iters,
      thin_every = thin_every
    ),
    serial_interval_config = list(
      si_window = si_window,
      w_shape = serial_w_shape,
      w_scale = serial_w_scale,
      f_shape = serial_f_shape,
      f_scale = serial_f_scale
    ),
    data_provenance = "real",
    analysis_engine = "outbreaker2",
    generated_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC")
  )

  # Save summary as JSON
  write_json(
    summary_stats, "exports/outbreaker_summary.json",
    pretty = TRUE, auto_unbox = TRUE
  )
  cat("✓ Summary saved\n")

  cat("\n✅ Outbreaker2 analysis complete\n")
}, error = function(e) {
  cat("ERROR:", as.character(e), "\n")
  quit(status = 1)
})
