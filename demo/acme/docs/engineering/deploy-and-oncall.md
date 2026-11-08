# Deploys and On-call

Engineering internal reference: CANARY-ENG-8WD1.

## Deploys

We deploy to production on Monday to Thursday, between 09:00 and 15:00 CET. No deploys on Fridays or during the
December change freeze (15 December to 6 January). Every deploy needs a green CI pipeline and one approval.

## Rollback

Use the blue-green switch in the deploy console. Rolling back must take less than five minutes.

## On-call

On-call rotations last one week. The on-call engineer acknowledges pages within 15 minutes and opens an incident
channel for any Sev-1 or Sev-2 issue.
