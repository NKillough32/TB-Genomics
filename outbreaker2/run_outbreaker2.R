
library(outbreaker2);library(ape)
cases<-read.csv('exports/cases.csv',stringsAsFactors=FALSE)
dna<-read.dna('exports/dna.fasta',format='fasta')
out<-outbreaker_data(dates=as.Date(cases$sample_date),dna=dna,id=cases$case_id)
res<-outbreaker(out,n_iter=2000,burnin=500)
saveRDS(res,'outbreaker2_results.rds');plot(res)
