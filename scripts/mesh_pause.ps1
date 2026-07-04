# Mesh Pause Runbook (supervisor executes)
# 1. Stop all runners (graceful drain)
ssh root@Alexandria "pkill -TERM -f 'kiroshi runner'"
# On Chronos:
schtasks /end /tn "KiroshiReduce30Runner"
schtasks /end /tn "KiroshiPipelineMonitor"
# 2. Stop coordinators
# 8787 (admin service): nssm stop kiroshi-fixer  (requires admin)
# 8800 (reduce30): kiroshi stop --role fixer  (or kill pid)
# 8801 (slerp): kiroshi stop --role fixer
# 3. Back up DBs
Copy-Item reduce30.db reduce30.db.bak-$(Get-Date -Format yyyyMMdd)
Copy-Item slerp.db slerp.db.bak-$(Get-Date -Format yyyyMMdd)