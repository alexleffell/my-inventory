# PRD

## User

The user keeps small parts in labelled bags or bins. The user wants a record of what is in the bag without typing a part number first.

## Job

Capture the part. Set how many and where it sits. Find it later from the dashboard or from /i/<id>. Ask for a missing part on the to-buy list.

## In scope

- Several photos per item. The first photo is the card thumbnail and shows the part.
- Public read of the dashboard, the item page, and the to-buy list.
- Admin edit after login. Auth stays off until the first admin exists.
- Search and filters in the URL, including the item id.
- A storage map of bags and bins.
- Duplicate handling in the browser. No silent second entry.
- A public create on the to-buy list so an agent can request a part with no credentials.
- Read access for agents through the summary, then the item list, then full records by id. There is no keyword search endpoint.
- Optional 3D models on an item.

## Out of scope for this repo

- Multi-tenant billing.
- Edits to the private Next.js successor. That tree is not on this remote.
- A second Paperclip project for the successor.
- A production container rebuild before a rehearsed restore of data and captures.

## Acceptance

A new item with a photo appears on the dashboard and at /i/<id>. A public viewer does not see edit controls. An admin does. A duplicate candidate opens a decision in the browser. tests/ covers the server rules you touch.
