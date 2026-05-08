
#!/usr/bin/env Rscript
# TB Genomic Surveillance - Outbreaker2 Analysis Script

tryCatch({
  library(outbreaker2)
  library(ape)
  library(jsonlite)
  
  # Create outputs directory
  dir.create("exports", showWarnings=FALSE)
  
  # Read input data
  cases <- read.csv('exports/cases.csv', stringsAsFactors=FALSE)
  dna <- read.dna('exports/dna.fasta', format='fasta')

  # Provide explicit interval distributions to avoid outbreaker2 default SI
  # construction edge cases on sparse/synthetic timestamp data.
  si_window <- 120
  w_raw <- dgamma(1:si_window, shape=2.0, scale=14.0)
  f_raw <- dgamma(1:si_window, shape=2.0, scale=7.0)
  w_dens <- w_raw / sum(w_raw)
  f_dens <- f_raw / sum(f_raw)
  
  # Prepare data for outbreaker
  case_dates <- as.Date(cases$sample_date)
  names(case_dates) <- as.character(cases$case_id)
  dna_ids <- as.character(labels(dna))
  aligned_dates <- case_dates[dna_ids]

  if (any(is.na(aligned_dates))) {
    stop("Some DNA labels do not have matching case dates")
  }

  names(aligned_dates) <- dna_ids

  out_data <- outbreaker_data(
    dates=aligned_dates,
    dna=dna,
    w_dens=w_dens,
    f_dens=f_dens
  )
  
  # Run outbreak investigation
  cat("Running outbreaker2 analysis...\n")
  n_iter_total <- 2000
  burnin_iters <- 500
  cfg <- create_config(n_iter=n_iter_total, sample_every=1)
  res <- outbreaker(data=out_data, config=cfg)
  chain_df <- as.data.frame(res)

  build_transmission_network <- function(result, ids, burnin_iter) {
    chain_local <- as.data.frame(result)
    alpha_cols <- grep("^alpha_", names(chain_local), value=TRUE)

    n_cases <- length(ids)
    posterior_samples <- 0
    edge_list <- list()
    incoming <- setNames(rep(0, n_cases), ids)
    outgoing <- setNames(rep(0, n_cases), ids)

    if (length(alpha_cols) == n_cases) {
      alpha_matrix <- as.matrix(chain_local[, alpha_cols, drop=FALSE])
      start_row <- min(max(1, burnin_iter + 1), nrow(alpha_matrix))
      alpha_post <- alpha_matrix[start_row:nrow(alpha_matrix), , drop=FALSE]
      posterior_samples <- nrow(alpha_post)

      for (target_idx in seq_len(n_cases)) {
        ancestry <- suppressWarnings(as.integer(alpha_post[, target_idx]))
        ancestry <- ancestry[!is.na(ancestry)]
        ancestry <- ancestry[ancestry >= 1 & ancestry <= n_cases]

        if (length(ancestry) == 0) {
          next
        }

        ancestry <- ancestry[ancestry != target_idx]
        if (length(ancestry) == 0) {
          next
        }

        freq <- table(ancestry)
        best_ancestor_idx <- as.integer(names(freq)[which.max(freq)])
        edge_prob <- as.numeric(max(freq)) / max(1, nrow(alpha_post))

        src <- ids[best_ancestor_idx]
        dst <- ids[target_idx]
        outgoing[src] <- outgoing[src] + edge_prob
        incoming[dst] <- incoming[dst] + edge_prob

        edge_list[[length(edge_list) + 1]] <- list(
          source = src,
          target = dst,
          probability = round(edge_prob, 4),
          confidence = ifelse(edge_prob >= 0.8, "high", ifelse(edge_prob >= 0.6, "medium", "low")),
          inference = "posterior_marginal_mode"
        )
      }
    }

    node_list <- lapply(ids, function(case_id) {
      score <- round(as.numeric(outgoing[case_id]) * 0.7 + as.numeric(incoming[case_id]) * 0.3, 4)
      band <- ifelse(score >= 1.2, "high", ifelse(score >= 0.5, "medium", "low"))
      list(
        case_id = substr(case_id, 1, 8),
        full_case_id = case_id,
        risk_score = score,
        risk_band = band,
        outgoing_links = as.integer(sum(vapply(edge_list, function(e) e$source == case_id, logical(1)))),
        incoming_links = as.integer(sum(vapply(edge_list, function(e) e$target == case_id, logical(1))))
      )
    })

    key_nodes <- head(node_list[order(vapply(node_list, function(n) n$risk_score, numeric(1)), decreasing=TRUE)], 10)
    high_conf_count <- sum(vapply(edge_list, function(e) e$probability >= 0.8, logical(1)))

    list(
      generated_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz="UTC"),
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
  saveRDS(res, 'outbreaker2_results.rds')
  cat("Results saved to outbreaker2_results.rds\n")
  
  # Generate plots
  cat("Generating diagnostic plots...\n")
  png('exports/outbreaker_trace.png', width=1200, height=800)
  plot(res, type='trace')
  dev.off()
  cat("✓ Trace plot saved\n")
  
  png('exports/outbreaker_hist.png', width=1200, height=800)
  plot(res, type='hist')
  dev.off()
  cat("✓ Histogram saved\n")  
  # Generate transmission tree
  cat("Generating transmission tree...\n")
  tryCatch({
    png('exports/outbreaker_tree.png', width=1400, height=1000)
    plot(res, type='tree')
    dev.off()
    cat("✓ Transmission tree saved\n")
  }, error=function(e) {
    cat("⚠ Could not generate transmission tree plot\n")
  })  

  # Export posterior-derived transmission network in JSON format.
  network <- build_transmission_network(res, as.character(cases$case_id), burnin_iters)
  write_json(network, 'exports/transmission_network.json', pretty=TRUE, auto_unbox=TRUE)
  cat("✓ Transmission network JSON saved\n")

  # Generate summary statistics
  cat("Generating summary report...\n")
  burnin_effective <- min(burnin_iters, max(0, nrow(chain_df) - 1))
  post_start <- burnin_effective + 1
  like_col <- if ("like" %in% names(chain_df)) "like" else if ("post" %in% names(chain_df)) "post" else NA_character_
  like_values <- if (!is.na(like_col)) as.numeric(chain_df[[like_col]][post_start:nrow(chain_df)]) else numeric(0)
  summary_stats <- list(
    n_generations = nrow(chain_df),
    burnin = burnin_effective,
    n_samples = max(0, nrow(chain_df) - burnin_effective),
    case_count = length(cases$case_id),
    likelihood_mean = ifelse(length(like_values) > 0, mean(like_values), NA_real_),
    likelihood_sd = ifelse(length(like_values) > 1, sd(like_values), NA_real_),
    transmission_edges = length(network$edges),
    posterior_samples = network$posterior_samples,
    data_provenance = "real",
    analysis_engine = "outbreaker2",
    generated_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz="UTC")
  )
  
  # Save summary as JSON
  write_json(summary_stats, 'exports/outbreaker_summary.json', pretty=TRUE, auto_unbox=TRUE)
  cat("✓ Summary saved\n")
  
  cat("\n✅ Outbreaker2 analysis complete\n")
  
}, error = function(e) {
  cat("ERROR:", as.character(e), "\n")
  quit(status=1)
})

