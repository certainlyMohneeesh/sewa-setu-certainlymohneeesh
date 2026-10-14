Sewa Setu — production log archive
Server: dbprod01 / appprod01 (IST, +0530)
Period: 01 Jan 2026 – 30 Jun 2026

  nginx-access.log.gz   HTTP access log (combined + rt=<seconds> response time)
  sewasetu-app.log.gz   application log (gunicorn worker, app.logger)
  cron.log              nightly scheduled-job output (/var/log/sewasetu/cron.log)

Rotated logs were consolidated into these three files at handover.
Read with: gzip -dc nginx-access.log.gz | less      (or zcat / gzcat)
