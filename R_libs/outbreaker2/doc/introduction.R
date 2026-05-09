## ----echo = FALSE-------------------------------------------------------------
knitr::opts_chunk$set(
  collapse = TRUE,
  comment = "#>", 
  fig.width=8, 
  fig.height=5, 
  fig.path="figs-introduction/"
)

## ----eval=FALSE---------------------------------------------------------------
# vignette("Overview", package = "outbreaker2")

## ----data---------------------------------------------------------------------
library(ape)
library(outbreaker2)

col <- "#6666cc"
fake_outbreak

## ----w------------------------------------------------------------------------

plot(fake_outbreak$w, type = "h", xlim = c(0, 5), 
     lwd = 30, col = col, lend = 2, 
     xlab = "Days after infection", 
     ylab = "p(new case)", 
     main = "Generation time distribution")


## -----------------------------------------------------------------------------
args(outbreaker)

## -----------------------------------------------------------------------------

class(res)
dim(res)
res


## -----------------------------------------------------------------------------

names(res)


## ----basic_trace--------------------------------------------------------------

plot(res)


## ----traces-------------------------------------------------------------------

plot(res, "prior")
plot(res, "mu")
plot(res, "t_inf_15")


## ----basic_trace_burn---------------------------------------------------------

## compare this to plot(res)
plot(res, burnin = 2000)


## ----many_plots---------------------------------------------------------------

plot(res, "mu", "hist", burnin = 2000)

plot(res, "mu", "density", burnin = 2000)

plot(res, type = "alpha", burnin = 2000)

plot(res, type = "t_inf", burnin = 2000)

plot(res, type = "kappa", burnin = 2000)

plot(res, type = "network", burnin = 2000, min_support = 0.01)


## ----summary------------------------------------------------------------------

summary(res)


## ----res2---------------------------------------------------------------------

summary(res2, burnin = 3000)
tree2 <- summary(res2, burnin = 3000)$tree

comparison <- data.frame(case = 1:30,
                       	 inferred = paste(tree2$from),
			 true = paste(fake_outbreak$ances),
			 stringsAsFactors = FALSE)
			 
comparison$correct <- comparison$inferred == comparison$true
comparison
mean(comparison$correct)


## ----net2---------------------------------------------------------------------

plot(res2, type = "network", burnin = 3000, min_support = 0.01)


