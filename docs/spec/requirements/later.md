# Later requirements (draft, M8–M9)

These are placeholders, refined in each milestone's review.

**ANA-01 (M8)** Analyser plugins run on new or changed files and store a
summary and details in `file_analysis`; the built-in time-series analyser is
configured from YAML.

**ANA-02 (M8)** Analysis results are returned by the API and client and shown
in the UI.

**ING-01 (M9)** CSV and CDF files matching a rule are ingested into Postgres
tables; a retention job deletes rows older than a configured age.

**ING-02 (M9)** A flow can be triggered when another flow completes.

**STO-L1 (later)** An rclone storage location as a project's first storage
location. Needs: interactive requests not queued behind rule batches on the
config lock, and a plan for SharePoint changing uploaded Office files.
