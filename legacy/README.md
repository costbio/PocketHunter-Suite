# Legacy bootstrappers

`start_app.sh` and `setup.sh` are pre-v2 shell scripts that reference
a conda environment named `dockspot` and try to copy `PocketHunter/`
from sibling repositories (`../shiny_pockethunter_webapp/`,
`../Streamlit_Dockspot/`). They predate the Docker-first architecture
the suite uses today.

**Do not run these in a new deployment.** They are preserved here for
historical reference only — they document the v1 development workflow
that originally seeded the project.

Current path: see `docs/deployment.md` at the repo root. The
Docker Compose stack handles every step these scripts attempted, plus
the Phase C hardening they predate.
