# Mesh Resume Runbook (supervisor executes after deploying new code)
# 1. Start coordinator on 8800 (auto-migrates DB on open)
wscript //nologo //B _reduce30_fixer_hidden.vbs
# 2. Start coordinator on 8801
wscript //nologo //B _slerp_fixer_hidden.vbs
# 3. Verify /status + /storage (topology intact, counts survived)
# 4. Restart runners
schtasks /run /tn "KiroshiReduce30Runner"
wscript //nologo //B _slerp_runner_hidden.vbs
# 5. Restart cascade seeder + monitor
wscript //nologo //B _cascade_seeder_hidden.vbs
schtasks /run /tn "KiroshiPipelineMonitor"
# 6. Confirm done sub-jobs are skipped (counts don't regress)