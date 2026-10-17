# GOAL

This repo is a photo-driven inventory for small parts in bags and bins. A photo is the record. A name is optional.

A person captures one or more photos, sets a quantity and a location, and sees the item on the dashboard. Anyone can view. An admin can edit after login.

The public single-tenant server in this repo is the shipped product. Run it with python3 server.py. The check is the command set in CLAUDE.md and the tests in tests/.

The successor is a private Next.js rewrite. It is the same product. It is not a second product. Phase 1 has accounts, organizations, tenant-isolated rows, auth, items, a wishlist API, and a dashboard. Billing is not built. That tree has no GitHub remote, so a checkout of this repo cannot edit it.

Do not rebuild the production container until a backup restore of the data directory and the captures directory has been rehearsed. scripts/backup.sh and scripts/restore.sh are the tools. They read the host from the environment. Do not write a host, an address, or a password into this repo.

Read docs/PRD.md and docs/ARD.md before you change the product.
