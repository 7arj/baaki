# Postmortem: database outage game day (blameless)

**Date:** 18 Oct 2026 · **Duration:** 3 min 10 s of user impact (06:12:48 → 06:15:58 UTC) ·
**Type:** planned fault injection · **Evidence:** [02-gameday-db-outage.txt](evidence/02-gameday-db-outage.txt)

## Summary

Postgres was stopped on purpose (StatefulSet scaled to 0, with Argo CD self-heal paused so it
wouldn't immediately undo it). Every user request failed with 503 for the duration. Both page alerts
fired. Service recovered within 5 s of Postgres returning, with no data loss.

## Timeline (UTC)

| Time | Event |
|------|-------|
| 06:12:32 | Steady state: postgres 1/1, web 2/2, `GET /login` 200, no alerts |
| 06:12:48 | Postgres stopped. Within one readiness period both web pods fail `/readyz` and leave the Service endpoints; Traefik returns 503. `BaakiPostgresDown` → pending |
| 06:12:58 | `BaakiNoReadyWebPods` → pending |
| 06:14:46 | `BaakiPostgresDown` → **firing** (after its `for: 2m`) |
| 06:15:01 | `BaakiNoReadyWebPods` → **firing** |
| 06:15:53 | Mitigation: Argo CD self-heal re-enabled; it restores `replicas: 1` from git |
| 06:15:58 | postgres 1/1, web 2/2, `GET /login` 200. Users served again |
| 06:16:0x | Smoke journey: sign-in works and all 14 invoices are present (PVC kept the data) |

## What went well

- **Readiness, not liveness, checks the database.** Web pods were taken out of rotation but not
  restarted, so no restart storm, and they came back by themselves the moment the DB returned.
  Had liveness checked the DB, Kubernetes would have killed and restarted every pod repeatedly.
- **Storage outlived the pod.** The PVC kept the data across the pod being removed and recreated.
- **Symptom alerts caught it.** Both page alerts fired about 2 minutes in, as designed.

## What we learned

- **The SLO burn-rate alerts did not fire, and couldn't.** With zero Ready pods, Traefik answered 503
  itself, so the app never saw the requests and its 5xx counter didn't move. Measuring errors only
  inside the app is blind to total outages. That's why the "no ready pods" and "postgres down"
  symptom alerts exist alongside the SLO alerts.
- **Single point of failure.** One Postgres replica means any DB outage is a full outage. Acceptable
  for a local project; in production: a managed HA database or an operator with a standby.
- **Time to detect = the `for:` window.** 2 minutes is a deliberate trade-off against paging on a
  pod reschedule that heals by itself in seconds.

## Five whys (for a real version of this incident)

1. Why did users get 503s? No web pod was Ready.
2. Why? Readiness requires the database, and it was unreachable.
3. Why did one DB failure take the whole site down? There is one DB instance with no standby.
4. Why no standby? The project targets one local cluster with local-path storage.
5. Why accept that? Cost and complexity versus a project scope where minutes of downtime are tolerable.
   The fix is architectural (managed or replicated DB), not a person.

## Action items

| Action | Type |
|--------|------|
| Measure availability at the ingress as well (Traefik metrics), so total outages burn the error budget | detect |
| Managed or replicated Postgres for any real deployment | prevent |
| Keep the game-day script in the repo and re-run it after infrastructure changes | verify |
