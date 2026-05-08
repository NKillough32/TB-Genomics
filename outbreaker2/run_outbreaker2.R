
#!/usr/bin/env Rscript
# TB Genomic Surveillance - Outbreaker2 Analysis Script

tryCatch({
  library(outbreaker2)
  library(ape)
  
  # Create outputs directory
  dir.create("exports", showWarnings=FALSE)
  
  # Read input data
  cases <- read.csv('exports/cases.csv', stringsAsFactors=FALSE)
  dna <- read.dna('exports/dna.fasta', format='fasta')
  
  # Prepare data for outbreaker
  out_data <- outbreaker_data(
    dates=as.Date(cases$sample_date),
    dna=dna,
    id=cases$case_id
  )
  
  # Run outbreak investigation
  cat("Running outbreaker2 analysis...\n")
  res <- outbreaker(out_data, n_iter=2000, burnin=500)
  
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
  cat(\"Generating transmission tree...\n\")
  tryCatch({
    png('exports/outbreaker_tree.png', width=1400, height=1000)
    plot(res, type='tree')
    dev.off()
    cat(\"✓ Transmission tree saved\n\")
  }, error=function(e) {
    cat(\"⚠ Could not generate transmission tree plot\n\")
  })  
  # Generate summary statistics
  cat("Generating summary report...\n")
  summary_stats <- list(
    n_generations = nrow(res$chain),
    burnin = res$call$burnin,
    n_samples = nrow(res$chain) - res$call$burnin,
    case_count = length(cases$case_id),
    likelihood_mean = mean(res$chain$likelihood),
    likelihood_sd = sd(res$chain$likelihood)
  )
  
  # Save summary as JSON
  library(jsonlite)
  write_json(summary_stats, 'exports/outbreaker_summary.json', pretty=TRUE)
  cat("✓ Summary saved\n")
  
  cat("\n✅ Outbreaker2 analysis complete\n")
  
}, error = function(e) {
  cat("ERROR:", as.character(e), "\n")
  quit(status=1)
})

